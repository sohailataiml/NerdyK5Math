"""Versioned evaluation datasets (M0.5).

A metric is only meaningful alongside the data it was measured on. Two numbers
computed against different example sets are not comparable, and comparing them
anyway is how a regression gate ends up reporting an improvement that is really
a dataset edit. So every dataset carries a content hash, every result records
it, and the regression gate refuses to compare across a mismatch.

Splits are derived from a hash of the example ID rather than shuffled with a
seed. That gives a property a seeded shuffle does not: **appending examples
never moves an existing one between train and holdout.** A dataset that grows
as teachers label more sessions (Implementation-Plan.md P0.9) would otherwise
silently leak holdout examples into few-shot prompts.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class Split(StrEnum):
    TRAIN = "train"
    HOLDOUT = "holdout"


class Example(BaseModel):
    """One labeled case.

    `expected` is a teacher-confirmed label (§3.6). `inputs` is deliberately a
    flat string map rather than a typed structure so one harness serves the
    diagnosis, retrieval, grading, and leak-check suites.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    inputs: dict[str, str]
    expected: str
    metadata: dict[str, str] = Field(default_factory=dict)


class Dataset(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    examples: tuple[Example, ...]

    @property
    def content_hash(self) -> str:
        """Stable digest of the examples themselves.

        Covers content, not file bytes: reformatting the JSONL or reordering
        lines leaves the hash unchanged, while editing a label changes it. That
        is the correct sensitivity — the gate should complain when the *data*
        moved, not when someone ran a formatter.
        """
        payload = sorted(
            json.dumps(ex.model_dump(mode="json"), sort_keys=True) for ex in self.examples
        )
        digest = hashlib.sha256("\n".join(payload).encode("utf-8"))
        return digest.hexdigest()[:16]

    def split(self, example: Example, holdout_pct: int = 30) -> Split:
        """Assign an example to a split, deterministically and stably."""
        key = f"{self.name}:{example.id}".encode()
        bucket = int.from_bytes(hashlib.sha256(key).digest()[:4], "big") % 100
        return Split.HOLDOUT if bucket < holdout_pct else Split.TRAIN

    def subset(self, split: Split, holdout_pct: int = 30) -> tuple[Example, ...]:
        return tuple(e for e in self.examples if self.split(e, holdout_pct) is split)

    def labels(self) -> tuple[str, ...]:
        return tuple(sorted({e.expected for e in self.examples}))


class DatasetError(ValueError):
    """Raised when a dataset file is malformed or internally inconsistent."""


def load_jsonl(path: Path) -> Dataset:
    """Load a dataset from `<name>-<version>.jsonl` with a leading header line.

    JSONL because these files grow by appending teacher-labeled examples, and a
    line-oriented format keeps that a clean diff rather than a whole-file rewrite.
    """
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        raise DatasetError(f"{path} is empty")

    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise DatasetError(f"{path} line 1 is not valid JSON: {exc}") from exc
    if "name" not in header or "version" not in header:
        raise DatasetError(f"{path} line 1 must be a header with 'name' and 'version'")

    examples: list[Example] = []
    seen: set[str] = set()
    for number, line in enumerate(lines[1:], start=2):
        try:
            example = Example.model_validate_json(line)
        except ValueError as exc:
            raise DatasetError(f"{path} line {number}: {exc}") from exc
        if example.id in seen:
            # Duplicates would double-count in every metric and quietly weight
            # one case above the rest.
            raise DatasetError(f"{path} line {number}: duplicate example id {example.id!r}")
        seen.add(example.id)
        examples.append(example)

    if not examples:
        raise DatasetError(f"{path} has a header but no examples")

    return Dataset(name=header["name"], version=header["version"], examples=tuple(examples))
