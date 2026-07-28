"""Create a pilot teacher and enrol the students from recent sessions (P0.8).

Sessions created by `scripts/run_session.py` have no classroom, so nothing shows
up in the teacher console — the queue is scoped per student and an unenrolled
student belongs to no teacher. This wires the demo data together so the console
has something real to show.

Run::

    .venv/Scripts/python -m scripts.seed_pilot
"""

from __future__ import annotations

import datetime as dt
import os
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from packages.domain import models as m
from packages.domain import tables as t
from packages.domain.enums import Role
from packages.domain.mapping import to_row

DEFAULT_URL = "postgresql+psycopg://tutor:tutor@localhost:5433/tutor"
TEACHER_NAME = "Pilot teacher"
OPERATOR_NAME = "Pilot operator"


def main() -> int:
    load_dotenv(".env", override=True)
    engine = create_engine(os.environ.get("DATABASE_URL", DEFAULT_URL))
    now = dt.datetime.now(dt.UTC)

    with Session(engine) as db:
        teacher = db.execute(
            select(t.PrincipalRow).where(t.PrincipalRow.display_name == TEACHER_NAME)
        ).scalar_one_or_none()
        if teacher is None:
            created = m.Principal(role=Role.TEACHER, display_name=TEACHER_NAME, created_at=now)
            db.add(to_row(created, t.PrincipalRow))
            db.flush()
            teacher_id = created.id
        else:
            teacher_id = teacher.id

        # Someone has to be able to reach P1.1's rollout control, and the policy
        # scopes it to an admin. Without this principal the kill switch exists,
        # is tested, and is unreachable by anyone running the pilot — which is
        # the same shape of gap as a review queue nothing ever writes to.
        operator = db.execute(
            select(t.PrincipalRow).where(t.PrincipalRow.display_name == OPERATOR_NAME)
        ).scalar_one_or_none()
        if operator is None:
            made = m.Principal(role=Role.ADMIN, display_name=OPERATOR_NAME, created_at=now)
            db.add(to_row(made, t.PrincipalRow))
            db.flush()
            operator_id = made.id
        else:
            operator_id = operator.id

        classroom = db.execute(
            select(t.ClassroomRow).where(t.ClassroomRow.teacher_id == teacher_id)
        ).scalar_one_or_none()
        if classroom is None:
            room = m.Classroom(teacher_id=teacher_id, name="Pilot classroom", created_at=now)
            db.add(to_row(room, t.ClassroomRow))
            db.flush()
            classroom_id = room.id
        else:
            classroom_id = classroom.id

        enrolled = set(
            db.execute(
                select(t.EnrollmentRow.student_id).where(
                    t.EnrollmentRow.classroom_id == classroom_id
                )
            )
            .scalars()
            .all()
        )
        students = db.execute(select(t.StudentRow.id)).scalars().all()
        added = 0
        for student_id in students:
            if student_id in enrolled:
                continue
            db.add(
                to_row(
                    m.Enrollment(classroom_id=classroom_id, student_id=student_id, created_at=now),
                    t.EnrollmentRow,
                )
            )
            added += 1
        db.commit()

        # Each child needs a principal of their own to reach the student page
        # (P0.11). Without one there is a `Student` row nobody can sign in as,
        # and the pilot has a tutor no student can open.
        with_principals = set(
            db.execute(select(t.PrincipalRow.student_id).where(t.PrincipalRow.role == Role.STUDENT))
            .scalars()
            .all()
        )
        student_logins: list[tuple[str, str]] = []
        for index, student_id in enumerate(students, start=1):
            if student_id in with_principals:
                continue
            child = m.Principal(
                role=Role.STUDENT,
                display_name=f"Pilot student {index}",
                student_id=student_id,
                created_at=now,
            )
            db.add(to_row(child, t.PrincipalRow))
            student_logins.append((child.display_name, str(child.id)))
        db.commit()

        if not student_logins:
            existing = db.execute(
                select(t.PrincipalRow).where(t.PrincipalRow.role == Role.STUDENT).limit(3)
            ).scalars()
            student_logins = [(row.display_name, str(row.id)) for row in existing]

        unrated = (
            db.execute(
                select(t.ShadowCandidateRow).where(
                    t.ShadowCandidateRow.id.not_in(select(t.ShadowRatingRow.shadow_candidate_id))
                )
            )
            .scalars()
            .all()
        )

        print(f"\n  teacher:            {teacher_id}")
        print(f"  operator (admin):   {operator_id}")
        print(f"  students enrolled:  {len(enrolled) + added} ({added} new)")
        print(f"  awaiting rating:    {len(unrated)} shadow candidate(s)")
        print("\n  Start the server:")
        print("    .venv/Scripts/python -m scripts.serve")
        print(f"\n  Teacher:  http://localhost:8080/teacher/rate?as={teacher_id}")
        print("  Students:")
        for name, principal_id in student_logins[:3]:
            print(f"    {name:<18} http://localhost:8080/?as={principal_id}")
        print("\n  Rollout control (P1.1), admin only:")
        print(f'    curl -H "x-principal-id: {operator_id}" localhost:8080/admin/rollout')
        print()

    engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
