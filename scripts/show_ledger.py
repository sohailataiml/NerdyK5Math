"""Print the most recent `LLMCall` rows (§5, M0.4).

The operator-facing view of the audit trail — the thing that makes a grade
defensible after the fact. Run::

    .venv/Scripts/python -m scripts.show_ledger
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from packages.domain import tables as t

DEFAULT_URL = "postgresql+psycopg://tutor:tutor@localhost:5433/tutor"


def main(limit: int = 5) -> int:
    load_dotenv(".env", override=True)
    engine = create_engine(os.environ.get("DATABASE_URL", DEFAULT_URL))

    with Session(engine) as db:
        rows = (
            db.execute(select(t.LLMCallRow).order_by(t.LLMCallRow.created_at.desc()).limit(limit))
            .scalars()
            .all()
        )
        print(f"\n=== {len(rows)} most recent ledger row(s) ===")
        for row in rows:
            print(f"\n  stage:          {row.stage.value}")
            print(f"  model_id:       {row.model_id}")
            print(f"  prompt_version: {row.prompt_version}")
            print(f"  tokens:         {row.tokens_in} in / {row.tokens_out} out")
            print(f"  latency:        {row.latency_ms}ms")
            print(f"  cost_usd:       ${row.cost_usd:.6f}")
            print(f"  input keys:     {sorted(row.input_payload)}")
            print(f"  output:         {row.output_payload}")
        print()

    engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
