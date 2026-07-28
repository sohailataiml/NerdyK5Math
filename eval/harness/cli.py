"""Eval harness entry point (M0.5).

Deliberately a CLI rather than pytest. Suites that call a model are slow, cost
money, and are nondeterministic — asserting them in the unit suite produces
flaky tests that get deleted, which is exactly how a quality gate stops
existing (Implementation-Plan.md §3 keeps the two regimes apart).

Usage::

    python -m eval.harness.cli run diagnosis            # rule baseline, free
    python -m eval.harness.cli run diagnosis --split train
    python -m eval.harness.cli run diagnosis --record   # write/update baseline
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from eval.harness.baseline import Baseline, DatasetMismatchError, check
from eval.harness.dataset import Split
from eval.harness.runner import Predictor, SuiteRun, run
from eval.suites import diagnosis

BASELINE_DIR = Path("eval/baselines")

SUITES = {
    "diagnosis": (diagnosis.build, diagnosis.rule_predictor, "rules/pre-check/v1", "deterministic"),
}


def _report(suite_run: SuiteRun) -> None:
    s = suite_run.scores
    print(f"\n=== {suite_run.suite} ({suite_run.split.value}) ===")
    print(
        f"  dataset:   {suite_run.dataset_name} {suite_run.dataset_version} "
        f"[{suite_run.dataset_hash}]"
    )
    print(f"  predictor: {suite_run.model_id} @ {suite_run.prompt_version}")
    print(f"  examples:  {s.total}  answered {s.answered}  correct {s.correct}")
    print()
    print(f"  accuracy                    {s.accuracy:.4f}")
    print(f"  accuracy_when_answered      {s.accuracy_when_answered:.4f}")
    print(f"  unknown_rate                {s.unknown_rate:.4f}")
    print(f"  macro_f1                    {s.macro_f1:.4f}")
    print(
        f"  high_confidence_precision   {s.high_confidence_precision:.4f} "
        f"(n={s.high_confidence_count})"
    )
    print(f"  expected_calibration_error  {s.expected_calibration_error:.4f}")

    if s.per_label:
        print("\n  per-tag:")
        for label in s.per_label:
            print(
                f"    {label.label:<32} P {label.precision:.2f}  R {label.recall:.2f}  "
                f"F1 {label.f1:.2f}  n={label.support}"
            )

    if s.calibration:
        print("\n  calibration (stated confidence vs observed accuracy):")
        for bucket in s.calibration:
            print(
                f"    [{bucket.lower:.1f}-{bucket.upper:.1f})  n={bucket.count:<4} "
                f"stated {bucket.mean_confidence:.2f}  actual {bucket.accuracy:.2f}  "
                f"gap {bucket.gap:.2f}"
            )


def _phase0() -> int:
    """Report the Phase 0 exit gates.

    Exit code is the decision: 0 means every gate is met and generated hints may
    be shown to students. Anything else means shadow mode stays on.
    """
    import os

    from dotenv import load_dotenv
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from eval.phase0 import build_report

    load_dotenv(".env", override=True)
    url = os.environ.get("DATABASE_URL", "postgresql+psycopg://tutor:tutor@localhost:5433/tutor")
    engine = create_engine(url)
    with Session(engine) as db:
        report = build_report(db)
    engine.dispose()

    print(report.render())
    print()
    return 0 if report.ready else 1


def _distress(with_classifier: bool) -> int:
    """Measure the §7 welfare screen's false-negative rate (P1.8).

    Exits 0 either way. This is a measurement, not a gate: layer 1's release
    gates already run in CI, and there is no defensible pass mark to set against
    an engineer-written corpus. Inventing one would turn "we have not measured
    this properly" into a green check.
    """
    from eval.distress import build_report

    client = None
    if with_classifier:
        from dotenv import load_dotenv

        from packages.llm import InMemoryLedger, LLMClient
        from packages.llm.transport import AnthropicTransport

        load_dotenv(".env", override=True)
        client = LLMClient(AnthropicTransport(), InMemoryLedger())

    print(build_report(client).render())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eval.harness.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser("run", help="run a suite and check it against its baseline")
    run_cmd.add_argument("suite", choices=sorted(SUITES))
    run_cmd.add_argument("--split", choices=[s.value for s in Split], default=Split.HOLDOUT.value)
    run_cmd.add_argument(
        "--record", action="store_true", help="write this run's metrics as the new baseline"
    )
    run_cmd.add_argument(
        "--predictor",
        choices=["rules", "llm"],
        default="rules",
        help="'llm' calls the real prompt — needs a key and costs money",
    )

    sub.add_parser("phase0", help="check the Phase 0 exit gates against the record")

    distress_cmd = sub.add_parser(
        "distress", help="measure the welfare screen's false-negative rate (P1.8)"
    )
    distress_cmd.add_argument(
        "--classifier",
        action="store_true",
        help="run the second layer too — needs a key and costs money",
    )

    args = parser.parse_args(argv)

    if args.command == "phase0":
        return _phase0()
    if args.command == "distress":
        return _distress(args.classifier)

    build, default_predictor, prompt_version, model_id = SUITES[args.suite]
    predictor: Predictor = default_predictor
    suite = build(split=Split(args.split))

    if args.predictor == "llm":
        from dotenv import load_dotenv

        from packages.llm import InMemoryLedger, LLMClient
        from packages.llm.transport import AnthropicTransport

        load_dotenv(".env", override=True)
        # An in-memory ledger: an eval run is not a student session, so its calls
        # do not belong in the audit trail alongside real ones.
        client = LLMClient(AnthropicTransport(), InMemoryLedger())
        predictor = diagnosis.llm_predictor(client)
        prompt_version = f"diagnose/K-1/{diagnosis.DEFAULT_PROMPT_VERSION}"
        model_id = "claude-haiku-4-5"

    suite_run = run(suite, predictor, prompt_version=prompt_version, model_id=model_id)
    _report(suite_run)

    baseline_path = BASELINE_DIR / f"{args.suite}.json"
    baseline = Baseline.load(baseline_path)

    try:
        report = check(
            suite=args.suite,
            current=suite_run.scores.as_metrics(),
            specs=suite.specs,
            dataset_hash=suite_run.dataset_hash,
            baseline=baseline,
        )
    except DatasetMismatchError as exc:
        print(f"\n=== gate: ERROR ===\n  {exc}\n")
        print("  Re-run with --record once you have reviewed the dataset change.\n")
        return 2

    print(f"\n=== gate: {'PASS' if report.passed else 'FAIL'} ===")
    print(report.render())

    if args.record:
        suite_run.to_baseline().save(baseline_path)
        print(f"\n  baseline written to {baseline_path}")
    print()

    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
