# Adaptive Socratic Tutor + Auto-Grader
A K-5 math tutoring swarm. The student's wrong answer is parsed for its underlying misconception, the curriculum KB is consulted for the canonical remediation, a Socratic hint (not the answer) is generated, the student's follow-up is graded against the rubric, and a per-student summary is queued for the teacher. 


K–5 math tutoring pipeline: diagnose misconception → curriculum retrieval →
Socratic hint → grade → teacher review.

- [Architecture.md](Architecture.md) — system design (current revision: a model call at every stage)
- [Architecture-deterministic.md](Architecture-deterministic.md) — prior no-LLM revision, kept for comparison
- [Implementation-Plan.md](Implementation-Plan.md) — build plan, milestones, exit criteria

## Status

**M0 foundations, partially complete.** There is no application to run yet — no
API, no CLI, no pipeline stages. What exists is the ground floor everything else
sits on:

| Task | State |
|---|---|
| M0.1 scaffold, tooling, CI, import contracts | done |
| M0.2 §5 domain entities + append-only enforcement | done |
| M0.3 Alembic baseline + pgvector + Postgres + fixture KB | done, verified against live Postgres |
| M0.4 LLM client + `LLMCall` ledger + tiering + cost + PII boundary | done, verified against a live model call |
| M0.5 eval harness + regression gate | done, running in CI on the deterministic suite |
| M0.6 prompt versioning + immutability lock | done, verified in CI |
| M0.7 symbolic service + container isolation | done, isolation verified against a running container |
| M0.8 event log, session replay, OTel spans | done, replay verified on a real session |
| M0.9 auth + tenancy (roles, teacher scoped to class) | done |
| M0.12 deterministic fallbacks (rules, lookup, templates, leak check) | done, provider-down path tested end to end |
| M0.11 compliance workstream | not started — **external dependency, see below** |

**M0 is code-complete apart from M0.11, and its exit criterion is met.** The
pipeline runs end to end on the fixture KB with real model calls at every stage,
every call ledgered, and every stage falling back cleanly when the provider is
unavailable:

```bash
.venv/Scripts/python -m scripts.run_session             # live
.venv/Scripts/python -m scripts.run_session --shadow    # Phase 0 shadow mode
.venv/Scripts/python -m scripts.run_session --offline   # no provider at all
```

`--shadow` is Phase 0 (P0.4): the full model pipeline runs and its output is
recorded for teacher rating, but the child is served a template hint. Generated
text never reaches a student until the Phase 0 exit gates are met.

Both produce a full session and a replay reconstructed from the append-only
record. The offline run is what Phase 0 shadow mode and a provider outage both
look like — a child still gets a hint, from the template library, and the record
says plainly that it ran degraded.

M0.4's PII boundary also delivers M0.10 ahead of schedule: `PromptContext` has no
field that can carry a student name, ID, or IEP flag, so a prompt cannot leak one.
M0.5 pulled in the §3.1 rule pre-check (part of M0.12) because the eval harness
needed a predictor — it doubles as the free, deterministic baseline any model
has to beat.

**M0.11 is not an engineering task and cannot be closed from here.** It is the
compliance work in Architecture.md §7 — a DPA with the model provider,
zero-retention API configuration, and documented data flows for district review.
Student work leaves the system boundary on every request in this architecture
revision, so this gates real student data reaching the pipeline, not the code
being finished. Implementation-Plan.md M0.11 says to start it at M0 rather than
at pilot; that remains outstanding and needs a human owner.

The pipeline stages under `services/orchestrator/stages/` are docstring stubs.
They exist so the import contracts have something to constrain.

## Phase 0 — what is built, and what is blocked

The engineering side of Phase 0 (shadow mode, the adversarial gates) is done.
The phase itself cannot start, and neither blocker is a code problem:

| Phase 0 task | State |
|---|---|
| P0.4 shadow mode | **done** — model runs, output recorded, template served |
| P0.5 leak corpus + release gate | **done** — 19 cases, 100% must-fail, in CI |
| P0.10 prompt-injection corpus | **done** — 12 attacks, zero success, in CI |
| P0.1 misconception taxonomy | **blocked** — must be authored *with a teacher* |
| P0.2 curriculum KB (~10 nodes) | **blocked** — teacher/curriculum-designer authored |
| P0.8 teacher review console + rating flow | **done** — API, scoped queues, rating page |
| P0.9 teacher-labeled eval datasets | **blocked** — needs the pilot |
| P0.11 minimal student UI | **done** — a child can run a session unaided |
| §3.6 review routing | **done** — finished sessions actually reach a teacher |
| Phase 0 exit-gate report | **done** — computed from the record, not asserted |
| P1.8 welfare screening | **done, pulled forward from Phase 1** — see below |
| P1.1 staged rollout + kill switch | **done, built before launch** — see below |
| M0.11 compliance sign-off | **blocked** — DPA, zero-retention, district review |

**P1.8 was moved earlier on purpose.** Implementation-Plan.md puts §7 distress
screening in Phase 1, after the pilot. Shadow mode is not a dry run — real
children type real free text into it from day one — so screening protects the
pilot rather than depending on it, and belongs on before the first student
session, not after.

```bash
.venv/Scripts/python -m scripts.run_session --offline --distress "2 nobody would miss me"
```

Two layers, like leak-check: patterns first (`packages/fallbacks/distress.py`,
free, always runs), then a classifier for what a regex cannot see. Three
properties are load-bearing:

- **A screen that alerts nobody is unbuildable, not discouraged.** `PipelineDeps`
  refuses to compose with `screen_for_distress` on and no destination. No
  screening is a gap someone owns; screening wired to nothing produces green
  dashboards and the belief children are watched over while every alert lands in
  a table nobody reads.
- **A flag never changes what the child sees.** Same hint, same grading, same
  pacing. A machine that visibly stops after a child types something frightening
  teaches them not to type it again. The alert goes sideways to an adult.
- **A classifier outage does not flag everything.** That is the opposite of
  leak-check's fail-closed, deliberately: flagging every attempt during an outage
  buries the real alert under thousands of false ones, which disables the
  responder rather than protecting anyone. The gap is recorded as degradation
  instead, and `safety_alert` carries the child's identity while the prompt that
  produced it cannot — the model judges text, the adult gets the name.

P1.8 requires a *measured* false-negative rate, so there is one:

```bash
.venv/Scripts/python -m eval.harness.cli distress               # layer 1 only, free
.venv/Scripts/python -m eval.harness.cli distress --classifier  # both layers, ~1¢
```

On `eval/adversarial/distress_corpus.jsonl` (38 cases): patterns alone miss
**45.8%** of signals — every misspelling and every indirect phrasing — and both
layers together currently miss none. The first number is the argument for the
second layer; the second number is mostly evidence the corpus is too easy. It is
a measurement, not a gate, and it exits `0` either way: there is no defensible
pass mark against a corpus like this, and inventing one would turn "not properly
measured" into a green check. Layer 1 has real CI gates in
`tests/test_adversarial.py`, including that a case labelled classifier-only
genuinely defeats the patterns — otherwise recall could be improved by moving
cases rather than catching them.

> **The patterns are engineer-written and are not adequate.** They must be
> reviewed and replaced by a school counsellor, like the misconception taxonomy
> must be authored with a teacher — but the failure mode differs in kind. A wrong
> tag produces a bad hint; a missed distress signal produces nothing at all, and
> the system looks like it is working right up until it matters. P1.8 also
> requires a *defined on-call response*, which no code supplies: `ConsoleResponder`
> is a development stand-in and is documented as unfit for production.

## The rollout and the kill switch (P1.1)

Implementation-Plan.md P1.1 puts generated hints in front of 5% of sessions, then
25%, then 100%, each step gated on leak rate and teacher rating holding, with a
"kill switch [that] reverts to templates instantly, **tested before launch, not
after**". That last clause is why this is built now rather than at the rollout:
the first time a kill switch is exercised should not be the incident it exists
for.

```bash
curl -H "x-principal-id: $ADMIN" localhost:8080/admin/rollout
curl -X POST -H "x-principal-id: $ADMIN" -H 'content-type: application/json' \
  -d '{"generation_enabled":true,"percentage":5,"reason":"Phase 0 gates met."}' \
  localhost:8080/admin/rollout
curl -X POST -H "x-principal-id: $ADMIN" -H 'content-type: application/json' \
  -d '{"reason":"Teacher flagged a leaked answer in 4B."}' \
  localhost:8080/admin/rollout/kill
```

The setting is the latest row of an append-only `rollout_change` table, read on
every attempt. Five properties are load-bearing, each covered by a test in
[tests/test_rollout.py](tests/test_rollout.py):

- **It is live.** No restart, no redeploy. The gap between deciding to stop and
  stopping would otherwise be measured in children.
- **A session's cohort never changes.** The bucket is a fixed digest of the
  session id — deliberately not `hash()`, which Python salts per process, so the
  same child would flip between generated and template hints across workers and
  the "5% cohort" would be a different 5% after every deploy.
- **Advancing evicts nobody.** `bucket < percentage` is monotonic, so 5% → 25%
  only adds. A fresh draw per step would mean the teacher ratings that justified
  advancing describe a cohort that no longer exists.
- **Withholding is not degradation.** During a 5% rollout, 95% of sessions serve
  templates *by design*. Logged as fallbacks they would put a working rollout in
  the same bucket as a provider outage — and §8's dashboards would read a healthy
  system as broken, hiding the real outage inside the noise. A template hint now
  has four distinguishable causes, and the record names which one.
- **The unconfigured state is off.** A deployment where nobody has recorded a
  setting serves templates. So leaving shadow mode is not by itself enough to put
  a model's words in front of a child: someone has to say so, in a row carrying
  their name and their reason. Two switches rather than one, because the phase and
  the percentage are different decisions made on different evidence — and killing
  a bad rollout must not re-enter shadow mode and start refilling a rating queue
  nobody asked for.

`kill` takes only a reason and preserves the configured percentage. Whoever
reaches for it is reacting to something that just went wrong, and requiring them
to also restate the cohort invites a fat-fingered `100` in the field next to the
one they meant. Admin-only: there is one setting for the whole deployment, so
scoping it to a role granted per classroom would hand a system-wide switch to
whoever has the most students.

## Pipeline inspector — what each stage received and returned (M0.8, §8)

```
http://localhost:8080/admin/pipeline?as=<admin-principal-id>
```

Pick a run on the left; every stage of it appears with its input and output side
by side. `GET /admin/runs` lists sessions, `GET /admin/runs/{id}` is the same
trace as JSON.

**A note on the name:** Implementation-Plan.md §0 chose LangGraph, but no
LangGraph is installed and none is imported. `services/orchestrator/graph.py` is
a hand-written state machine, so these are *pipeline stages*, not LangGraph
nodes. The plan and the code disagree, and this is the code.

Built from the append-only record — `pipeline_event` joined to `llm_call` — so it
reads the same months later as it did on the day, and shows nothing that was not
logged at the time. Three properties it is built for:

- **Deterministic stages are shown, not omitted.** A stage with no model call is
  the rule pre-check firing, the keyed lookup hitting, or a template rendering
  during an outage. A dashboard that showed only model calls would render a full
  provider outage as a blank screen. They are the grey cards; model-backed ones
  are blue and carry the model id, prompt version, tokens, latency, and cost.
- **Repeats are numbered.** Three hint levels means three passes through generate
  and leak-check, shown as `#1 #2 #3`. A view showing only the last hides the two
  that explain it.
- **Gaps and unterminated stages are reported.** A stage that started and logged
  no ending means the process died mid-run, and a missing sequence number means
  the record is incomplete. Both are stated rather than smoothed over — a trace
  from an incomplete record is a plausible story, not what happened.

**This surface carries children's answers and full prompt payloads, including
`correct_answer` on the diagnose and leak-check inputs.** It is governed by
M0.9's `can_read_audit_trail` — admin, or a teacher for their own student's
session — and is never reachable from the student client. The list endpoint is
admin-only because it spans every child in the deployment.

To ask where the phase actually stands, read the record rather than the table:

```bash
.venv/Scripts/python -m eval.harness.cli phase0
```

It computes each exit gate from the append-only tables and exits `0` only when
every one is met — the same condition under which generated hints may be shown to
a child. **A gate with no data reads `INSUFFICIENT DATA`, never `PASS`.** That is
the load-bearing property: on the current pilot data no teacher has flagged a
leak, but only one hint has been reviewed, so "no leaks reported" means nobody
looked and the gate says so. Compliance sign-off reports `NEEDS A HUMAN` rather
than being quietly omitted from the list.

The two hard blockers are **compliance sign-off** and **teacher-authored
content**. Everything currently in `packages/curriculum/seed.py` and
`eval/datasets/` is engineer-written fixture data, labelled as such in the files
themselves. It exists to exercise the machinery and must be *replaced* by
teacher-authored content, not extended — otherwise the eval inherits one
engineer's assumptions about what a K-1 error looks like.

## Setup

Requires Python 3.12+ (developed against 3.14).

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt   # Windows
# .venv/bin/python -m pip install -r requirements-dev.txt     # macOS/Linux
```

## What you can run

Commands below use the Windows venv path; swap `Scripts` for `bin` elsewhere.

**Tests** — 470 of them, against in-memory SQLite, no container needed:

```bash
.venv/Scripts/python -m pytest
.venv/Scripts/python -m pytest -v                    # per-test names
.venv/Scripts/python -m pytest tests/test_append_only.py
```

**Quality gates** — the same five CI runs, in order:

```bash
.venv/Scripts/python -m ruff check .        # lint
.venv/Scripts/python -m ruff format --check .
.venv/Scripts/python -m mypy                # strict, zero ignores
.venv/Scripts/lint-imports                  # architecture contracts
.venv/Scripts/python -m pytest
```

**Database** — everything above runs on in-memory SQLite. For the real thing:

```bash
docker compose -f ops/docker-compose.yml up -d      # Postgres 16 + pgvector on :5433
.venv/Scripts/alembic upgrade head                  # create schema
.venv/Scripts/python -m pytest -m integration       # pgvector-specific tests
```

`alembic` reads `$DATABASE_URL`, defaulting to the compose instance
(`postgresql+psycopg://tutor:tutor@localhost:5433/tutor`). The migration runs on
both engines — SQLite gets a JSON column where Postgres gets `vector`, so the
unit suite needs no container. Only the `integration` tests prove pgvector
itself works.

**Eval suites** — quality measurement, deliberately separate from `pytest`.
Model-backed suites are nondeterministic and billed; asserting them in the unit
suite produces flaky tests that get deleted, which is how a quality gate stops
existing. The deterministic suite needs no key and runs in CI:

```bash
.venv/Scripts/python -m eval.harness.cli run diagnosis            # score + gate
.venv/Scripts/python -m eval.harness.cli run diagnosis --split train
.venv/Scripts/python -m eval.harness.cli run diagnosis --record   # set the baseline
```

Exit codes: `0` pass, `1` regression or floor breach, `2` the dataset changed and
the comparison was refused. That last one is not a failure — it means the
baseline must be re-recorded rather than compared across versions.

**Symbolic service** — mathematical equivalence for closed-form answers (§3.5).
It evaluates attacker-influenced input, so it is the most isolated component:

```bash
docker compose -f ops/docker-compose.yml up -d symbolic
# It sits on an internal network with no egress, so it has no published port.
# To reach it, join that network:
docker run --rm --network ops_symbolic-net curlimages/curl -s \
  -X POST http://symbolic:8000/equivalent -H 'content-type: application/json' \
  -d '{"expected":"1/2","actual":"4/8"}'

# Or run it directly for local poking:
.venv/Scripts/python -m uvicorn services.symbolic.app:app --port 8000
```

The service reports facts — equivalent, canonical form, whether the answer is in
lowest terms — and does **not** decide whether an unsimplified answer counts.
`4/8` is right in grade 3 and wrong in grade 6; that is curriculum policy and
belongs to the grading stage.

**The server** — both surfaces, student and teacher:

```bash
.venv/Scripts/python -m scripts.seed_pilot   # creates logins, prints both URLs
.venv/Scripts/python -m scripts.serve        # http://localhost:8080
```

`scripts/serve.py` rather than `uvicorn services.api.app:app` for a structural
reason: import contract 2 forbids anything under `services` from reaching the
model SDK, even transitively, because that rule is what guarantees no call
escapes the `LLMCall` ledger. Something still has to pick a provider, so the
composition root lives outside `services` and injects it. Started without
`ANTHROPIC_API_KEY` the server runs fully deterministic — rule diagnosis, keyed
lookup, template hints, pattern-only welfare screening — which is a real mode,
not a broken one.

**Jungle Math client** (P0.11) — the React surface, built to the Jungle Math
Tutor design spec:

```bash
cd clients/student && npm install && npm run dev   # :5173, proxies to :8080
```

Open `http://localhost:5173/?as=<student-principal-id>`. React + Vite +
TypeScript + Tailwind v4 + Framer Motion; see
[clients/student/README.md](clients/student/README.md), which records where it
departs from the design spec and why — notably that the spec's score and streak
are not built, because §11.5 rules them out and the API returns neither.

The server-rendered page below remains at `/` on :8080 and is the fallback: it
needs no build step, which matters for a pilot classroom where a node toolchain
may not be welcome.

**Student page** (P0.11) at `/?as=<student-principal-id>`. A problem, a §11.2
visual, an answer box, and up to three hint levels. Design constraints worth
knowing:

- **No response ever contains the correct answer** — not on a hint, not on a
  wrong answer, not on completion. Every other leak guard inspects *hint text*;
  a JSON field named `correct_answer` would walk straight past all of them. A
  child with the network tab open is still a child in the pilot.
- **The visual never draws the total.** A ten-frame when both parts fit in ten,
  a number line otherwise. `13 - 8` in a ten-frame is a picture that contradicts
  the question, which is worse than no picture.
- **No timer, no streak, no score** (§11.5), and running out of hints reads as
  *your teacher is going to look at this with you* — not as failure. A child who
  reads the end of a session as punishment stops asking for hints, which is the
  one behaviour the system exists to reward.

**Review routing** (§3.6). Every finished session reaches a teacher — in Phase 0
that is 100% of them (P0.8), tagged with the most significant reason it met:
`leak_fallback` > `symbolic_disagreement` > `max_hints` > `unknown_tag` >
`low_confidence` > `audit_sample`. One row per session, because a teacher
clearing the same session three times learns to clear things without reading.

This closed a gap worth naming, because every component passed its own tests
while the whole did nothing: `ReviewItem` had a table, a queue endpoint, and a
console that rendered it, and **nothing in the system ever wrote one**. Sessions
escalated, the child was told *"your teacher is going to look at this one with
you"*, and the queue stayed empty. The message now follows the record — if no row
was written, the child is not told one was.

Welfare alerts deliberately do **not** come through here; §7's distress path has
its own table and its own urgency. `ReviewReason.SAFETY_FLAG` exists in the enum
and is never emitted, which a test enforces.

**Teacher console** — Phase 0's rating instrument (P0.8) — at
`/teacher/rate?as=<teacher-principal-id>`, review queue at
`/teacher/review-queue`:

```bash
.venv/Scripts/python -m scripts.run_session --shadow   # produce a candidate to rate
```

The page shows the hint the child saw beside the one the model generated but
never displayed, and asks which would have helped more. Every queue is scoped per
student — a teacher sees their own class and nothing else, enforced by M0.9's
policy on each row.

> **Authentication is a pilot-grade stand-in.** `services/api/auth.py` trusts an
> `x-principal-id` header; there is no password, session, or token signature.
> That is bounded to the Phase 0 classroom pilot and must be replaced before
> anything wider. *Authorization* is fully enforced, so replacing identity does
> not change the access rules.

**Prompts** — published versions are immutable, enforced by a hash lock in CI:

```bash
.venv/Scripts/python -m packages.prompts.cli list
.venv/Scripts/python -m packages.prompts.cli verify          # runs in CI
.venv/Scripts/python -m packages.prompts.cli publish diagnose/K-1/v2
```

To change a prompt, add a new version file — editing a published one changes its
hash and fails `verify`. That is what keeps the `prompt_version` on an `LLMCall`
a true identifier of the text that was sent.

To score a prompt against the real model (costs ~3¢ for the holdout split):

```bash
.venv/Scripts/python -m eval.harness.cli run diagnosis --predictor llm
```

**Live model call** — needs `ANTHROPIC_API_KEY` in `.env` (see `.env.example`)
and Postgres up. Costs a fraction of a cent; deliberately not part of `pytest`:

```bash
.venv/Scripts/python -m scripts.check_api_access   # auth vs billing, spends nothing
.venv/Scripts/python -m scripts.smoke_llm          # one real diagnosis, ledgered
.venv/Scripts/python -m scripts.show_ledger        # read the audit trail back
.venv/Scripts/python -m scripts.show_replay        # rebuild a session from the record
```

`show_replay` is the §4 guarantee in operator form — given a session ID, it
reconstructs the ordered account of what happened and which model call produced
each step, reading only append-only tables. It flags gaps in the sequence rather
than rendering an incomplete record as a confident narrative.

`.env` takes precedence over an ambient `ANTHROPIC_API_KEY` (`override=True`);
without that, a key already exported in your shell silently shadows the file.

**Demo** — walks one session through the entities and prints its audit trail,
the per-stage cost ledger, and the append-only guard refusing a tamper:

```bash
.venv/Scripts/python -m scripts.demo_session
```

The stage outputs in the demo are hand-written, not produced by a diagnoser or
generator — those don't exist yet. It demonstrates persistence, ledgering, and
immutability, not tutoring.

## Architecture constraints enforced by the build

`lint-imports` fails the build on three rules from
[Implementation-Plan.md](Implementation-Plan.md) §1, so they can't erode through
review drift:

1. **Pipeline stages are independent** — stages never import each other;
   composition happens only in the orchestrator, so retries, timeouts, and
   logging live in one place (Architecture.md §6).
2. **The model SDK is reachable only through `packages.llm`** — this is what
   guarantees no model call escapes the `LLMCall` ledger or the tiering config
   (M0.4).
3. **The domain layer is pure** — no I/O, no services, no SDK, so §5 entities
   stay testable as plain values.

## Layout

```
packages/domain/      §5 entities, tables, mapping, append-only enforcement
packages/llm/         model client, tiering, LLMCall ledger, PII boundary
packages/prompts/     versioned prompts + immutability lock
packages/fallbacks/   rule pre-check, keyed lookup, templates, leak + distress patterns
packages/auth/        roles, tenancy, authorization policy
packages/telemetry/   event log, session replay, OTel spans
packages/curriculum/  fixture KB seed (engineer-written, to be replaced)
services/api/         student page, teacher console, admin rollout control
services/orchestrator/ §4 state machine, stages, review routing, rollout policy
services/symbolic/    isolated SymPy equivalence service
services/safety/      §7 welfare alert sinks and responders
eval/                 datasets, suites, adversarial corpora, harness, Phase 0 gate report
tests/                the five CI gates' unit suite
scripts/              demo, seeded pilot, server, ledger and replay readers
```
