# Findings

What running the system revealed, as distinct from what reading it suggests.

Every entry here was found by executing something — a deploy, a browser, a query
against the record — not by review. That is the common thread and the reason the
file exists: each one passed every test in the suite, broke nothing a user could
see, and was invisible until the system was actually driven.

Dated 2026-07-28, except the welfare-screen entry below (2026-07-29). Evidence is
quoted as measured; where a number is small the sample size is stated rather than
rounded away.

---

## 1. Fixed

### The welfare screen only reached children who got the answer wrong

`services/api/student.py` grades first — it has to, to know whether to hint —
and called `run_attempt` only when the answer was wrong. The §7 distress screen
was the first node *inside* `run_attempt`. So the screen was unconditional
within a pipeline that was itself conditional, and a child who answered
correctly was never screened at all.

Measured on the deployed record: **0 of 2** sessions answered correctly on the
first try were screened; **17 of 17** with a wrong answer were. Small sample,
unambiguous code path.

`graph.py` had already written down the rule it was breaking — a screen running
after the pipeline decides the answer is correct *"would miss exactly the
children who are keeping up."* The code described the bug it had.

The screen is now hoisted into `submit_answer`, ahead of `check_answer`, and its
outcome is handed to `run_attempt` rather than re-derived there — otherwise one
disclosure raises two alerts and bills two classifier calls, and a responder
paged twice for the same child trusts the count less.

**What this did not fix, stated plainly.** Checked rather than assumed: the
symbolic grader rejects any answer carrying words, so `12 nobody would miss me`
grades *wrong* today and reached the screen the long way round. No disclosure
was being dropped. What was really missing was screening on correct submissions
at all — and P2.1 is specifically an LLM normalization pass meant to accept
messy answers, which turns the latent gap into a live one. It was cheaper to
close while still theoretical.

Worth recording separately: the test that should have caught this was
`test_screening_runs_before_the_answer_is_judged`, which tested screening before
*diagnosis* inside `run_attempt` — never before judgment, and never on the path
that skipped `run_attempt` entirely. It passed throughout. A test named for a
property it does not check is worse than no test, because it retires the
question. Renamed to what it actually asserts, with the real ordering now pinned
at the API level where grading happens.

### `requirements.txt` could not run the server it ships

The file listed the data layer only — pydantic, sqlalchemy, alembic, psycopg,
pgvector — while the application imports fastapi, uvicorn, anthropic, sympy,
httpx, dotenv, and opentelemetry. A clean install ran all six migrations and
then died on `import uvicorn`.

Invisible locally because the development venv had the rest installed by hand.
Found by the first real deploy. **A reviewer cloning the repo would have hit the
same wall**, which makes it the most expensive kind of packaging bug: it fails
for everyone except the author.

### The `grade` stage never recorded an ending

`recorder.graded` carries the verdict but no `stage`, so grading emitted
`stage_started` and no terminal event. Consequences: the per-stage trace read
`grade — unterminated, nothing recorded` on every run ever inspected, §8 could
not measure grade latency at all, and a grade that completed was
indistinguishable from one that died mid-call.

Now emits `stage_completed` with score, method, and whether the checker agreed;
a missing checker emits `fallback_used` so grading degrades visibly like every
other stage. First measurement after the fix: 3712 ms on the first grade of a
process, 10–22 ms after — a cold SymPy import that had never been visible.

### `grade_result` was a table nothing wrote to

The entity had validation, the table was in the schema and in a migration, and
the only code that ever inserted a row was `scripts/demo_session.py`. Every real
session recorded a `graded` event and nothing else.

This is the same shape as the `ReviewItem` gap the README already describes —
a table, an endpoint, and a console, with nothing ever enqueuing a row — which
suggests the pattern is worth checking for deliberately rather than waiting to
notice it twice.

### `diagnosis_log` was the third one, found by checking deliberately

Same shape again, and this is the instance that argues for the check: `ReviewItem`
and `GradeResult` were both noticed by accident, and this one was found by looking
for the pattern on purpose. Every attempt ran the diagnoser; `diagnosis_log` was
empty in every real session; the only code that ever inserted a row was
`scripts/demo_session.py`.

It is also the most expensive of the three to have missed. §8 names diagnoser
accuracy and calibration against teacher-confirmed tags as the core quality metric
for the whole system, and that metric is a join — diagnoses on one side,
`ReviewVerdict` on the other. The left side was recoverable only by scanning
`pipeline_event` detail JSON, which is unindexed, untyped, and not a contract:
workable for one session, not for a phase whose exit gate *is* the measurement.

Two decisions in the fix are worth recording because neither is obvious:

- **Abstentions are rows.** `unknown` is the majority of real diagnoses on
  fixture content. A table holding only confident diagnoses would report a
  diagnoser that is always right about everything it has an opinion on, while
  hiding how rarely it has one — so the `unknown` rate has to be queryable too.
- **An unrecognised label resolves to no tag, not to a new tag.** §3.1 confines
  the diagnoser's vocabulary to the taxonomy, and P0.1 authors that taxonomy with
  a teacher. A sink that inserted a row for every unfamiliar label would let the
  model extend it at runtime. The label is preserved in `evidence` instead, so an
  out-of-vocabulary reply stays measurable rather than being discarded.

### The teacher surfaces were both queues, so nothing showed a child over time

`/teacher/rate` and `/teacher/review` each ask "judge this row" and neither says
anything across rows. The consequence is not a missing feature so much as a missing
question: a teacher could reach a verdict on twelve sessions without ever learning
that three of them were the same child making the same mistake.

`/teacher/dashboard` is that view, and it is the first thing in the repo that
`diagnosis_log` having a producer actually buys — per-student misconception history
is a query over that table, so before this week the honest version of the page was
empty.

Building it surfaced one thing worth recording on its own. **The dashboard
independently reproduced the 57-abandoned-session correction** that
`eval.harness.cli phase0` reports. That figure was derived once, by hand, for the
gate readout; the dashboard computes it from the same append-only rows by a
different path and agrees. Two independent derivations agreeing is the cheapest
evidence available that neither is an artefact of how it was counted — and the
reason both are worth keeping rather than consolidating.

The rate-withholding rule is a deliberate refusal, not a limitation: below five
observations the page prints the count and no percentage. A dashboard is the one
surface where a number gets acted on without being interrogated, so `1 of 1 = 100%`
beside a child's name is worse than no number at all.

### A teacher could not see the grade they were being asked to confirm

The review console showed the child's answers and the correct answer, but not
what the system had decided about them. A teacher reaching a verdict on a grade
had to redo the arithmetic to discover what they were confirming. Verdicts are
now joined per attempt, and a checker disagreement is called out rather than
folded into the number.

### `seed()` was idempotent by being inert

It returned early whenever any curriculum node existed. That prevented
duplicates and also meant adding a problem to the file could never reach an
already-seeded database, including a deployed one. Problem ids are now derived
from the prompt with `uuid5`, so re-seeding inserts what is missing.

Symptom that surfaced it: the demo served the same two questions repeatedly.
`start_session` picks uniformly at random with no memory, so two problems means
half of all sessions repeat the last one. Now twelve.

### Three things that only fail on a host

- **`DATABASE_URL` scheme.** Managed providers hand out `postgres://` or
  `postgresql://`. SQLAlchemy 2 rejects the first outright and resolves the
  second to psycopg2, which this project does not install. Both surface as a
  missing *module*, not a wrong scheme, in the deploy's first command.
- **`InProcessSymbolicChecker` in production.** The composition root wired the
  in-process checker, whose own docstring says it is not for production: the
  symbolic service exists as a separate process because it evaluates
  attacker-influenced input, and calling it in-process discards the no-egress
  network, read-only filesystem, and memory cap in one line. `SYMBOLIC_URL` now
  selects the isolated path.
- **The obvious start command is a trap.** `uvicorn services.api.app:app` boots,
  passes its health check, and silently serves every stage from its
  deterministic fallback with no model behind it — because import contract 2
  keeps the SDK out of `services`, and only `scripts/serve.py` injects a
  provider. The failure looks like success.

### Smaller

- `scripts/seed_pilot.py` created no admin principal, so the rollout control and
  the pipeline inspector were unreachable by anyone running the pilot.
- The rollout `reason` field accepted `"   "` — `min_length` counts whitespace,
  so a spacebar satisfied it, and the audit value of the table is entirely in
  that field.
- `swarm.py` shipped one 102-character line, failing two of the five CI gates.

---

## 2. Open — code

### No rate limiting anywhere

No endpoint has a limit, and each wrong answer costs roughly $0.003 in model
calls. The deployed instance is public with a live API key. An endpoint that is
effectively unauthenticated *and* billed is a way to spend someone else's money.

### Authentication is an unsigned header in a query string

A principal id is an unguessable UUID, so it behaves like a bearer token — but
it travels in the demo URL, so browser history, referrers, and screenshots all
leak it. Whoever holds the admin link can read every session, every prompt
payload, and the audit trail. Documented as pilot-grade; still true.

### The inspector over-counts `generate_hint` runs in shadow mode

Found while verifying the LangGraph swarm. A shadow run shows three
`generate_hint` runs and two `leak_check` runs, which reads like a hint escaping
the guardrail. It is not: the extra run is the shadow-candidate *recording*
event, which carries `stage=GENERATE_HINT` with no matching `stage_started`, and
the trace turns any orphaned terminal event into its own single-event run.

Every generated candidate was leak-checked — verified from the raw sequence.
But a tool whose entire value is faithfulness should not need that check.

### Quality gaps, measured

| | |
|---|---|
| `diagnose` returns `unknown` | **52%** (24 of 46 real diagnoses) |
| `rerank` falls back | **56%** (27 of 48) |
| hint latency | 3–5 s against a stated p95 target of **< 2 s** |

None of these are bugs. The diagnoser abstaining is correct behaviour against a
three-tag vocabulary, and `rerank` falling back is honest about a keyed lookup
with no embeddings. They are the fixture content and the unbuilt P1.3 showing up
as numbers rather than as impressions.

---

## 3. Open — needs a human

### An open question that was never written down

Three modules — `packages/domain/tables.py`, `packages/curriculum/seed.py`, and
`services/orchestrator/stages/retrieve.py` — say the embedding model "has not
been chosen yet (Implementation-Plan.md §7 open question)". **§7 does not ask
it.** Its six questions are the Phase 0 strand, the shadow cohort, provider
terms, the cost ceiling, the Numeria codebase, and leak-checker ownership.

So a decision that blocks P1.3 is believed by three modules to be tracked, and
is tracked nowhere. `EMBEDDING_DIM = 1024` is currently a guess whose own
comment says changing it is "a migration plus a full re-index — a decision to
make before Phase 1 retrieval work, not after."

The codebase is otherwise careful in the opposite direction: a gate with no data
reads `INSUFFICIENT DATA`, never `PASS`. This failed the other way — a question
nobody is answering looks answered because the code cites a section number.

### A leak-checker near-miss for the corpus

On `13 - 8`, hint level 3 read: *"Start at 8 on the number line and hop up to 9,
10, 11, 12, 13 — how many hops did you make in all?"* The checker passed it; the
literal answer `5` never appears. A child can count the enumerated list and get
5 without subtracting.

P0.5 says the corpus grows from exactly this. It has not been added yet.

### `langsmith` is now a transitive dependency

The LangGraph swarm brings `langsmith`, which ships payloads to a third-party
SaaS when tracing is enabled — and enabling it is one stray
`LANGCHAIN_TRACING_V2=true`. Those payloads contain children's answers. M0.11,
the DPA and zero-retention work, is already the outstanding compliance blocker;
this adds a second egress path that a config typo turns on. It wants a
structural block, not a note.

### Standing blockers, unchanged

- **P0.1 / P0.2** — the taxonomy and curriculum KB must be authored *with* a
  teacher. Everything currently in `seed.py` is engineer-written and labelled as
  such, and the plan says it must be replaced rather than extended.
- **M0.11** — DPA, zero-retention configuration, district data-flow review.
- **Render free Postgres expires 2026-08-27.**

---

## 4. Clarifications worth recording

### Shadow mode is narrower than "the model is off"

In shadow mode **four of six stages still call a model**: safety screen,
diagnose, hint generation (recorded, never shown), and leak-check — which runs
twice, once on the shadow candidate and once on the template actually served.
Deterministic throughout: grading (SymPy), retrieval (keyed lookup), and the
hint text itself.

**The README overstates the guarantee.** It says "the student experience stays
deterministic". The hint *text* comes from a fixed library, but *which* template
is chosen is keyed on the diagnosed tag — and in shadow mode that tag is a model
output whenever the rule pre-check does not fire, which is most of the time. So
the same child with the same answer can receive a different template across
runs.

What shadow mode actually guarantees is narrower and still sufficient for the
Phase 0 gate: **no model-authored sentence reaches a child.** Every word read
was written by a human and pre-approved. Full determinism is the `--offline`
path, where the tag is a pure function of the arithmetic.

### The plan chose LangGraph; the code did not, then did

Implementation-Plan.md §0 locked LangGraph as the orchestration decision. Until
2026-07-28 no LangGraph was installed or imported — `graph.py` was a
hand-written state machine — and the plan and the code disagreed silently for
the whole of M0. `services/orchestrator/swarm.py` has since made the plan true.

Worth noting what the migration did *not* have to fix: the concern that the
event-sequence allocator (`max(sequence) + 1`) is single-writer by assumption
and would race under parallel nodes. Sequential `Command(goto=…)` handoffs
preserve it — verified as `gaps=[]` on live runs. That constraint still binds
any future move to parallel fan-out.

### The README's status section describes a project that does not exist

It opens: *"There is no application to run yet — no API, no CLI, no pipeline
stages"*, and later: *"The pipeline stages are docstring stubs."*

Both are false. There is a deployed application, a React client, 498 tests, and
fully implemented stages. This is the first substantive thing a reader meets and
it undersells the work by roughly everything.
