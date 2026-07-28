"""One real model call, ledgered to Postgres (M0.4 smoke test).

Everything in `tests/` runs against a fake transport. This is the only path that
touches the provider, so it is a script rather than a test: it costs money, needs
a key, and must not run in CI.

Prerequisites::

    docker compose -f ops/docker-compose.yml up -d
    .venv/Scripts/alembic upgrade head

Run::

    .venv/Scripts/python -m scripts.smoke_llm
"""

from __future__ import annotations

import datetime as dt
import os
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from packages.curriculum.seed import seed
from packages.domain import models as m
from packages.domain import tables as t
from packages.domain.enums import GradeBand, PipelineStage, SessionState
from packages.domain.mapping import from_row, to_row
from packages.llm import DatabaseLedger, LLMClient, PromptContext
from packages.llm.transport import AnthropicTransport
from packages.prompts import PromptRegistry
from packages.telemetry import DatabaseEventSink, EventRecorder, replay, stage_span

# The version the eval gate currently endorses (M0.6 / M0.5).
DEFAULT_PROMPT_VERSION = "v2"


def main() -> int:
    load_dotenv(".env", override=True)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set (copy .env.example to .env).")
        return 1

    url = os.environ.get("DATABASE_URL", "postgresql+psycopg://tutor:tutor@localhost:5433/tutor")
    engine = create_engine(url)

    with Session(engine) as db:
        seed(db)

        # A session row must exist before a ledger row can reference it.
        problem = db.execute(select(t.ProblemRow).limit(1)).scalar_one()
        student = m.Student(grade_level=1, created_at=dt.datetime.now(dt.UTC))
        session = m.Session(
            student_id=student.id,
            problem_id=problem.id,
            started_at=dt.datetime.now(dt.UTC),
            state=SessionState.DIAGNOSING,
            attempt_count=1,
        )
        db.add(to_row(student, t.StudentRow))
        db.flush()  # student must exist before session's FK references it
        db.add(to_row(session, t.SessionRow))
        db.commit()

        client = LLMClient(AnthropicTransport(), DatabaseLedger(db))
        context = PromptContext(
            session_id=session.id,
            grade_band=GradeBand.K_1,
            problem_prompt=problem.prompt,
            correct_answer=problem.correct_answer,
            student_answer="2",
            attempt_number=1,
        )

        prompt = PromptRegistry().render(
            stage=PipelineStage.DIAGNOSE,
            band="K-1",
            version=DEFAULT_PROMPT_VERSION,
            values={
                "problem": problem.prompt,
                "correct_answer": problem.correct_answer,
                "student_answer": "2",
            },
        )

        print(f"\nProblem: {problem.prompt}   correct: {problem.correct_answer}   student said: 2")
        print(f"Prompt:  {prompt.version} [{prompt.content_hash}]")
        print("Calling the diagnoser...\n")

        recorder = EventRecorder(DatabaseEventSink(db), session.id)
        recorder.session_started(problem=problem.prompt)
        recorder.answer_submitted(attempt_number=1, answer="2")
        recorder.state_changed(frm=SessionState.AWAITING_ANSWER, to=SessionState.DIAGNOSING)
        recorder.stage_started(PipelineStage.DIAGNOSE)

        with stage_span(PipelineStage.DIAGNOSE, session.id):
            result = client.complete(stage=PipelineStage.DIAGNOSE, context=context, prompt=prompt)

        recorder.stage_completed(
            PipelineStage.DIAGNOSE, llm_call_id=result.llm_call_id, chars=len(result.text)
        )

        print(f"  diagnosis:  {result.text.strip()}")
        print(f"  model:      {result.model_id}")
        print(f"  tokens:     {result.usage.input_tokens} in / {result.usage.output_tokens} out")
        print(f"  latency:    {result.latency_ms}ms")
        print(f"  cost:       ${result.cost_usd:.6f}")

        # The point of the exercise: read the audit trail back out of Postgres.
        row = db.get(t.LLMCallRow, result.llm_call_id)
        assert row is not None, "ledger row missing — M0.4's guarantee is broken"
        call = from_row(row, m.LLMCall)

        print("\n=== Ledger row (§5 LLMCall, read back from Postgres) ===")
        print(f"  id:             {call.id}")
        print(f"  session:        {call.session_id}")
        print(f"  stage:          {call.stage.value}")
        print(f"  model_id:       {call.model_id}")
        print(f"  prompt_version: {call.prompt_version}")
        print(f"  cost_usd:       ${call.cost_usd:.6f}")
        print(f"  input keys:     {sorted(call.input_payload)}")
        print("\n  No student name or ID appears above — PromptContext cannot carry them.")

        # M0.8: the same session, rebuilt from the append-only record alone.
        print("\n=== Session replay (§4, reconstructed from the event log) ===\n")
        print(replay(db, session.id).render())
        print()

    engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
