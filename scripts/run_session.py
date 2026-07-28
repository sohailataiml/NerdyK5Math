"""M0's exit criterion, executed (§2, §4).

The plan's M0 exit: "the pipeline runs end-to-end on a 3-node fixture KB with
real model calls at every stage, every call ledgered, every stage falling back
cleanly when the provider is stubbed to fail, and the eval harness scoring a
fixture suite in CI."

This script is the first clause. It runs one whole session — a wrong answer,
diagnosis, retrieval, hint generation, the blocking leak check, a retry, and
grading — against Postgres and the real provider, then reconstructs the session
from the append-only record.

Run::

    docker compose -f ops/docker-compose.yml up -d
    .venv/Scripts/alembic upgrade head
    .venv/Scripts/python -m scripts.run_session

Add `--offline` to run the same session with no provider at all, which is what
Phase 0 shadow mode and a provider outage both look like.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from packages.curriculum.seed import seed
from packages.domain import models as m
from packages.domain import tables as t
from packages.domain.enums import GradeBand, SessionState
from packages.domain.mapping import to_row
from packages.llm import DatabaseLedger, LLMClient
from packages.prompts import PromptRegistry
from packages.telemetry import DatabaseEventSink, EventRecorder, replay
from services.orchestrator import graph
from services.orchestrator.shadow import DatabaseShadowSink
from services.orchestrator.state import PipelineDeps, Problem
from services.orchestrator.symbolic_client import InProcessSymbolicChecker
from services.safety.alerts import ConsoleResponder, DatabaseAlertSink

DEFAULT_URL = "postgresql+psycopg://tutor:tutor@localhost:5433/tutor"
WRONG_ANSWER = "2"
RETRY_ANSWER = "12"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.run_session")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="run with no provider at all — the provider-down path",
    )
    parser.add_argument(
        "--shadow",
        action="store_true",
        help="Phase 0: run the model, record it for rating, serve the template",
    )
    parser.add_argument(
        "--distress",
        metavar="TEXT",
        help="submit this instead of the usual wrong answer, to exercise the "
        "§7 welfare screen (P1.8)",
    )
    args = parser.parse_args(argv)

    load_dotenv(".env", override=True)
    engine = create_engine(os.environ.get("DATABASE_URL", DEFAULT_URL))

    with Session(engine) as db:
        seed(db)
        problem_row = db.execute(select(t.ProblemRow).limit(1)).scalar_one()

        student = m.Student(grade_level=1, created_at=dt.datetime.now(dt.UTC))
        session = m.Session(
            student_id=student.id,
            problem_id=problem_row.id,
            started_at=dt.datetime.now(dt.UTC),
            state=SessionState.AWAITING_ANSWER,
            attempt_count=1,
        )
        db.add(to_row(student, t.StudentRow))
        db.flush()
        db.add(to_row(session, t.SessionRow))
        db.commit()

        llm: LLMClient | None = None
        if not args.offline:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                print("ANTHROPIC_API_KEY is not set; use --offline or fill in .env")
                return 1
            from packages.llm.transport import AnthropicTransport

            llm = LLMClient(AnthropicTransport(), DatabaseLedger(db))

        # §5's Attempt is the record of what the child submitted, and the teacher
        # console reads it — without this row a rater sees a hint with no idea
        # what the child actually wrote. The orchestrator emits an event for the
        # submission but does not own persistence, so the composition root does.
        db.add(
            to_row(
                m.Attempt(
                    session_id=session.id,
                    student_answer=args.distress or WRONG_ANSWER,
                    timestamp=dt.datetime.now(dt.UTC),
                    hint_level_shown=0,
                ),
                t.AttemptRow,
            )
        )
        db.commit()

        recorder = EventRecorder(DatabaseEventSink(db), session.id)
        recorder.session_started(problem=problem_row.prompt)

        deps = PipelineDeps(
            recorder=recorder,
            prompts=PromptRegistry(),
            llm=llm,
            # In-process for this demo. Production uses HttpSymbolicChecker so
            # the evaluation stays inside the isolated container (M0.7); the
            # container has no published port precisely because it has no egress.
            symbolic=InProcessSymbolicChecker(),
            db=db,
            shadow_mode=args.shadow,
            shadow_sink=DatabaseShadowSink(db) if args.shadow else None,
            # A ConsoleResponder is a development stand-in, not an on-call path.
            # It is wired here so the screen can be exercised end to end without
            # making "screening on, destination absent" a valid configuration --
            # PipelineDeps refuses that outright.
            screen_for_distress=True,
            safety_sink=DatabaseAlertSink(db, ConsoleResponder()),
        )

        problem = Problem(
            prompt=problem_row.prompt,
            correct_answer=problem_row.correct_answer,
            grade_band=GradeBand(problem_row.grade_band.value),
            operands={"a": "7", "b": "5"},
        )

        mode = (
            "OFFLINE (no provider)"
            if args.offline
            else ("SHADOW (Phase 0 — model runs, template shown)" if args.shadow else "LIVE")
        )
        print(f"\n=== Session {session.id} — {mode} ===")
        print(f"  Problem: {problem.prompt}   correct: {problem.correct_answer}")
        answer = args.distress or WRONG_ANSWER
        print(f"  Student answers: {answer}\n")

        result = graph.run_attempt(
            deps,
            session_id=session.id,
            problem=problem,
            student_answer=answer,
            hint_level=1,
            student_id=student.id,
        )

        print(
            f"  diagnosis:   {result.diagnosis.tag} "
            f"({result.diagnosis.source.value}, confidence {result.diagnosis.confidence})"
        )
        print(f"  strategy:    {result.retrieval.primary[:70]}")
        if result.hint_text:
            print(f"  hint shown:  {result.hint_text}")
            print(f"  hint source: {result.hint_source.value if result.hint_source else '-'}")
        else:
            print(f"  NO HINT — escalated: {result.escalation_reason}")
        if result.leak_check:
            print(
                f"  leak check:  passed={result.leak_check.passed} "
                f"[{result.leak_check.checker_version}]"
            )
        if result.ran_degraded:
            print(f"  DEGRADED:    {', '.join(result.degraded_stages)}")

        if result.shadow_ran:
            shadow_rows = (
                db.execute(
                    select(t.ShadowCandidateRow)
                    .where(t.ShadowCandidateRow.session_id == session.id)
                    .order_by(t.ShadowCandidateRow.created_at.desc())
                )
                .scalars()
                .all()
            )
            print("\n  --- shadow candidate (recorded, NOT shown to the child) ---")
            for row in shadow_rows:
                print(f"  generated:   {row.generated_text}")
                print(f"  leak check:  passed={row.leak_check_passed} [{row.leak_checker_version}]")
                print("  awaiting a teacher rating (P0.8)")

        print(f"\n  Student retries: {RETRY_ANSWER}")
        grade = graph.grade_answer(
            deps,
            session_id=session.id,
            problem=problem,
            student_answer=RETRY_ANSWER,
            prior_diagnosis=result.diagnosis,
        )
        print(f"  grade:       {grade.score} via {grade.method.value}")
        if grade.needs_review:
            print(f"  review:      {grade.reason}")

        print("\n=== Replay from the append-only record (§4) ===\n")
        print(replay(db, session.id).render())
        print()

    engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
