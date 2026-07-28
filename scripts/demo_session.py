"""Walk one session through the foundation and print its audit trail.

There is no pipeline yet (M0.3 onward builds it), so the stage outputs here are
hand-written rather than produced by a diagnoser or generator. What this
demonstrates is the part that *is* built: §5's entities persist, every model
call lands in the ledger, and the append-only guarantee holds against tampering.

Run from the repo root (``-m`` so the repo root lands on ``sys.path``)::

    .venv/Scripts/python -m scripts.demo_session
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import Session as DbSession

from packages.domain import models as m
from packages.domain import tables as t
from packages.domain.append_only import AppendOnlyError
from packages.domain.enums import (
    AnswerType,
    DiagnosisSource,
    GradeBand,
    GradeMethod,
    HintSource,
    Operation,
    PipelineStage,
    SessionState,
)
from packages.domain.mapping import from_row, to_row

NOW = dt.datetime(2026, 7, 27, 12, 0, 0, tzinfo=dt.UTC)


def _rule(label: str) -> str:
    return f"  {label}"


def main() -> None:
    engine = create_engine("sqlite://")
    t.Base.metadata.create_all(engine)

    with DbSession(engine) as db:
        # --- Reference data: a student, a curriculum node, a problem -----------
        student = m.Student(grade_level=1, created_at=NOW)
        node = m.CurriculumNode(
            standard_code="1.OA.C.6",
            grade_band=GradeBand.K_1,
            definition="Add and subtract within 20.",
            remediation_strategies=[
                "Ten-frame: build the first addend, then count on.",
                "Number line: start at the larger addend and jump forward.",
            ],
        )
        problem = m.Problem(
            curriculum_node_id=node.id,
            prompt="What is 7 + 5?",
            correct_answer="12",
            answer_type=AnswerType.NUMERIC,
            grade_band=GradeBand.K_1,
        )
        tag = m.MisconceptionTag(
            label="subtracted_instead_of_added",
            operation_type=Operation.ADDITION,
            description="Student applies subtraction to an addition problem.",
            example_pattern="wrong_answer == abs(a - b)",
        )
        for entity, row_cls in [
            (student, t.StudentRow),
            (node, t.CurriculumNodeRow),
            (problem, t.ProblemRow),
            (tag, t.MisconceptionTagRow),
        ]:
            db.add(to_row(entity, row_cls))

        # --- The session: student answers 2 to "7 + 5" ------------------------
        session = m.Session(
            student_id=student.id,
            problem_id=problem.id,
            started_at=NOW,
            state=SessionState.DIAGNOSING,
            attempt_count=1,
        )
        attempt = m.Attempt(
            session_id=session.id, student_answer="2", timestamp=NOW, hint_level_shown=0
        )
        db.add(to_row(session, t.SessionRow))
        db.add(to_row(attempt, t.AttemptRow))

        # --- Diagnosis. The rule pre-check fires, so no model call is made.
        # This is §3.1's cost lever: the common cases never reach an LLM.
        diagnosis = m.DiagnosisLog(
            attempt_id=attempt.id,
            misconception_tag_id=tag.id,
            confidence=1.0,
            evidence="student answer (2) equals a-b; correct op is addition",
            source=DiagnosisSource.RULE,
        )
        db.add(to_row(diagnosis, t.DiagnosisLogRow))

        # --- Hint generation. This one *is* a model call, so it is ledgered.
        hint_call = m.LLMCall(
            session_id=session.id,
            stage=PipelineStage.GENERATE_HINT,
            model_id="claude-sonnet-5",
            prompt_version="hint/k-1/v2",
            input_payload={
                "misconception_tag": tag.label,
                "strategy": node.remediation_strategies[0],
                "hint_level": 1,
            },
            output_payload={"hint": "You have 7 counters. What happens when you add 5 more?"},
            tokens_in=380,
            tokens_out=24,
            latency_ms=910,
            cost_usd=0.00114,
            created_at=NOW,
        )
        db.add(to_row(hint_call, t.LLMCallRow))

        hint = m.HintLog(
            session_id=session.id,
            attempt_number=1,
            misconception_tag_id=tag.id,
            curriculum_node_id=node.id,
            hint_text="You have 7 counters. What happens when you add 5 more?",
            hint_level=1,
            source=HintSource.GENERATED,
            leak_check_passed=True,
            leak_checker_version="leak/v1.0",
            llm_call_id=hint_call.id,
        )
        db.add(to_row(hint, t.HintLogRow))

        # --- Student retries and gets it right. Symbolic check, no model call.
        retry = m.Attempt(
            session_id=session.id,
            student_answer="12",
            timestamp=NOW + dt.timedelta(minutes=1),
            hint_level_shown=1,
        )
        db.add(to_row(retry, t.AttemptRow))
        grade = m.GradeResult(
            attempt_id=retry.id,
            score=1.0,
            confidence=1.0,
            method=GradeMethod.SYMBOLIC,
            symbolic_agreed=True,
        )
        db.add(to_row(grade, t.GradeResultRow))
        db.commit()

        # --- §4 replay: reconstruct the session from the record ---------------
        print("\n=== Session replay ===")
        print(_rule(f"Problem:        {problem.prompt}  (correct: {problem.correct_answer})"))
        for row in db.query(t.AttemptRow).order_by(t.AttemptRow.timestamp):
            a = from_row(row, m.Attempt)
            print(_rule(f"Attempt:        {a.student_answer!r} (hint level {a.hint_level_shown})"))
        d = from_row(db.query(t.DiagnosisLogRow).one(), m.DiagnosisLog)
        print(_rule(f"Diagnosis:      {tag.label} via {d.source.value} (conf {d.confidence})"))
        h = from_row(db.query(t.HintLogRow).one(), m.HintLog)
        print(_rule(f"Hint shown:     {h.hint_text}"))
        print(_rule(f"                cleared by leak-checker {h.leak_checker_version}"))
        g = from_row(db.query(t.GradeResultRow).one(), m.GradeResult)
        print(_rule(f"Grade:          {g.score} via {g.method.value}"))

        # --- The cost/audit view §8 wants, per stage --------------------------
        print("\n=== Model-call ledger (§5 LLMCall) ===")
        calls = db.query(t.LLMCallRow).all()
        for c in calls:
            print(
                _rule(
                    f"{c.stage.value:<14} {c.model_id:<22} {c.prompt_version:<14} "
                    f"{c.latency_ms:>5}ms  ${c.cost_usd:.5f}"
                )
            )
        print(
            _rule(
                f"{'':<14} {'':<22} {'session total:':<14} "
                f"{sum(c.latency_ms for c in calls):>5}ms  "
                f"${sum(c.cost_usd for c in calls):.5f}"
            )
        )
        print(_rule("diagnosis made no model call — the rule pre-check fired (§3.1)"))

        # --- The guarantee: the record cannot be quietly rewritten ------------
        print("\n=== Append-only guard (§5) ===")
        stored_hint = db.query(t.HintLogRow).one()
        stored_hint.hint_text = "The answer is 12."
        try:
            db.commit()
            print(_rule("FAILED: the hint was rewritten"))
        except AppendOnlyError as exc:
            db.rollback()
            print(_rule(f"tamper refused: {exc}"))

    engine.dispose()
    print()


if __name__ == "__main__":
    main()
