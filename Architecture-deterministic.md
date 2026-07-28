# Adaptive Socratic Tutor + Auto-Grader — System Architecture

**System:** K–12 math tutoring swarm
**Pipeline:** Diagnose misconception (rules) → Curriculum lookup (keyed retrieval) → Socratic hint (templates) → Student response → Grade (rules/symbolic) → Teacher review

> **Note on this revision:** this version replaces every LLM call in the pipeline with a deterministic equivalent — pattern-matching rules for diagnosis, a keyed lookup table (not embeddings) for curriculum retrieval, and pre-authored, curriculum-approved templates for hints. Nothing a student sees is generated at request time; it's assembled from vetted content. This is a real trade-off, not a free upgrade — see the callouts in Sections 3.1, 3.3, and 3.5 for exactly where determinism runs out of coverage and has to escalate to a teacher instead of guessing.

---

## 1. Design principles

1. **Never hand over the answer.** Every hint is a pre-authored question or a nudge toward a representation (number line, model, worked analogy) — because hints are template text filled with problem values rather than freshly generated, answer-leakage is prevented by review at authoring time, with an automated slot-fill check as a safety net rather than the primary defense.
2. **Ground everything in curriculum, nothing improvised.** The tutor's pedagogical moves come from a curriculum KB that a human authored and approved — there is no generation step that could drift from it, because there is no generation step at all.
3. **Grade defensibly, escalate honestly.** Auto-grading is hybrid: symbolic/rule-based for answers with one correct form, keyword/structure rule-matching for constrained short-response formats. Genuinely open-ended free text is the one place determinism has real limits — those go to a teacher by default rather than being forced through a rule engine that would guess.
4. **Human-in-the-loop is a first-class component, not an afterthought.** Teacher overrides feed back into the misconception taxonomy and the rule/template library — the system gets better because teachers and curriculum authors extend its coverage, not because a model retrains on its own outputs.
5. **Age and grade-band awareness everywhere.** A misconception tag, hint tone, and grading rule all vary between a 1st grader and a 10th grader; the architecture threads grade-band context through every stage's rule tables and template sets rather than bolting it on.

---

## 2. Pipeline overview

```mermaid
flowchart LR
    A[Student submits\nwrong answer] --> B[Misconception\nDiagnoser\n(rule engine)]
    B --> C[Curriculum KB\n(keyed lookup)]
    C --> D[Hint Template\nSelector]
    D --> E{Slot-fill\nsanity check}
    E -- fail --> D
    E -- pass --> F[Student sees hint,\nsubmits new response]
    F --> G[Grader]
    G -- confident --> H[Score + feedback\nto student]
    G -- low confidence /\nrepeated miss --> I[Teacher Review\nQueue]
    I --> J[Teacher verdict]
    J --> H
    J --> K[(Taxonomy + Hint\nLibrary update)]
    H --> L{Correct?}
    L -- no, attempts < max --> B
    L -- yes / max attempts --> M[Session log →\nAnalytics]
```

Each arrow is a logged, versioned event — the whole loop is designed to be replayed for debugging and for teacher audit.

---

## 3. Core components

### 3.1 Misconception Diagnoser (rule engine)
- **Input:** problem statement, correct answer, student's wrong answer, prior attempts in this session, grade band.
- **Method:** an ordered set of deterministic pattern checks per operation type — no model call at all. For a given operation, a small decision table tests the wrong answer against known error signatures (e.g. `wrong_answer == |a − b|` on an addition problem → `subtracted_instead_of_added`; `wrong_answer == a + b` on a subtraction problem → `added_instead_of_subtracted`; `wrong_answer == a × b` on a division problem → `multiplied_instead_of_divided`). Rules are tried in priority order; the first match wins. This is exactly the rule-based pass described in the original design — it's now the *only* diagnostic path, not a fast-path in front of an LLM.
- **Output contract:**
```json
{
  "misconception_tag": "subtracted_instead_of_added",
  "confidence": "high | low",
  "matched_rule": "addition.rule_04_reversed_operation",
  "grade_band": "K-1"
}
```
Confidence here is a property of the rule that fired (rules known to be unambiguous are `high`; rules that could plausibly match more than one real misconception are marked `low` at authoring time), not a probability score — there's no model output to calibrate.
- **Failure mode handling:** if no rule matches, tag as `unknown` and route to a generic, curriculum-level hint template rather than guessing. This is the primary coverage cost of going fully deterministic: **a misconception the rule table doesn't know about is invisible to the system**, not inferred. Rule coverage becomes an explicit, ongoing content-authoring task (see Section 12), not something a model absorbs for free from examples.

### 3.2 Curriculum Knowledge Base (keyed lookup, not RAG)
- **Content:** standards-aligned curriculum nodes (one per skill, e.g. `3.OA.A.1`), each with: definition, common misconceptions it's associated with, 2–3 teacher-approved remediation strategies, worked visual model types, and prerequisite/next-node links.
- **Storage:** a plain relational lookup table keyed on `(misconception_tag, grade_band, operation)` → `curriculum_node_id` + ordered list of remediation strategy IDs. No embeddings, no similarity search, no vector store — the diagnoser's output is already a discrete tag, so retrieval is an exact-match join, which is simpler to test, audit, and reason about than nearest-neighbor retrieval.
- **Retrieval:** deterministic — the tag either has a mapped curriculum node and remediation list, or it doesn't. If the tag is `unknown` (no rule fired) or has no mapping yet, fall back to a general-purpose "let's look at this together" strategy rather than a similarity-based guess.
- **Update path:** curriculum content and the tag→node mapping are authored/edited by teachers/curriculum designers through an admin tool. This is the entire "knowledge" of the system — there's no model weights or embedding space to keep in sync with it.

### 3.3 Hint Template Selector (was: Socratic Hint Generator)
- **Input:** misconception tag, retrieved remediation strategy ID, problem values (a, b, operation), current hint level for this attempt.
- **Method:** no generation. Each remediation strategy maps to a small set of **pre-authored hint templates**, one per escalation level (level 1: a guiding question with slots for the problem's actual numbers, e.g. "You have {a} and are taking away some — how many are left if you take away just 1 at a time?"; level 2: a partially worked example using the same representation type stored in the curriculum node; level 3: a fully worked example with the final step left blank). Templates are filled with the specific problem's values and rendered — a straightforward string/slot-fill operation, not free text generation.
- **Guardrail:** because hint *content* is fixed at authoring time and reviewed by a curriculum designer before it ever ships, answer-leakage is structurally prevented rather than caught after the fact. An automated **slot-fill sanity check** still runs (confirming the filled template doesn't accidentally render the literal correct answer as a side effect of variable substitution, e.g. when a slot value happens to equal the answer) as a cheap defense-in-depth step, not the primary safeguard.
- **Escalation:** after a configurable max (e.g. 3 hint levels) without a correct response, the system reveals the worked solution using the same template system, plainly, and logs the case for teacher review — persistent struggle is itself a signal, not a failure to hide.
- **Coverage cost:** this only works as well as the template library. A curriculum node with only one shallow remediation strategy authored will give the same hint every time it's hit — there's no model paraphrasing it differently per student. Template variety (2–3 phrasings per level, rotated or randomized) is worth authoring investment specifically to avoid this getting stale.

### 3.4 Student interaction loop
- Presents the hint, captures the next response with full context (time spent, whether the hint's visual model was expanded/viewed, attempt number).
- State is session-scoped and stateless between turns at the API layer — all context needed for the next stage is passed explicitly rather than relying on server memory, so any component can be scaled or restarted independently.

### 3.5 Auto-Grader
- **Grading by answer format, most to least deterministic:**
  - *Closed-form answers* (numeric, fraction, simple algebraic expressions): a symbolic checker (e.g. a SymPy-based microservice) checks mathematical equivalence, not string match — `4/8` and `1/2` both grade correct. Fully deterministic, no change from the original design.
  - *Constrained short responses* (multiple choice reasoning, sentence starters, fill-in-the-blank explanations, "which step is wrong" pickers): graded by rule/keyword/structure matching against a teacher-authored answer key retrieved from the curriculum KB. This works because the response format is constrained at the question-design stage specifically so a deterministic check is possible.
  - *Genuinely open-ended free text* (unconstrained "explain your reasoning" in the student's own words): this is where determinism has a real, honest limit. Keyword/regex matching on free text is brittle and will misgrade phrasing a teacher would clearly accept or reject correctly. **Default behavior: route these to the Teacher Review Console rather than auto-grading them.** The recommended mitigation is upstream, not downstream — favor constrained response formats (Section 3.5's second bullet) wherever the learning objective allows it, so less ends up in this bucket. Where fully free-text response is pedagogically necessary, that's a deliberate scope decision to make with stakeholders, not something to paper over with a rule engine pretending to understand prose.
- **Confidence output:** for the deterministic paths, "confidence" is really "which rule/check fired" rather than a probability. A grade that contradicts the diagnosed misconception (e.g. graded "correct" right after a diagnosed fundamental gap) still routes to teacher review as a consistency check, regardless of format.

### 3.6 Teacher Review Console
- **Queue composition:** any open-ended free-text response (by default, per 3.5), a student hitting max hints without success, any content-safety flag, `unknown`-tagged sessions where no diagnostic rule fired, and a configurable random sample (e.g. 2%) of high-confidence auto-grades for ongoing quality audit.
- **Teacher actions:** confirm/override grade, edit or approve the misconception tag, add a new diagnostic rule or hint template to close a coverage gap, flag a student for follow-up outside the system.
- **Feedback loop:** every override and every new rule/template a teacher authors is stored and versioned. This is now the *only* way the system's coverage grows — since there's no model absorbing patterns from examples, the rule table and template library only get better through deliberate authoring, informed by what teachers are seeing in the review queue. Track "rules added per month" and "`unknown`-tag rate over time" as the core improvement metrics.

---

## 4. Orchestration

The pipeline is a **stateful workflow**, not a single prompt chain — implemented as an explicit state machine (e.g. LangGraph, Temporal, or a custom FSM) so every transition is observable and resumable:

```
AwaitingAnswer → Diagnosing → RetrievingCurriculum → GeneratingHint
→ AwaitingLeakCheck → AwaitingStudentRetry → Grading
→ (Complete | Escalated | Diagnosing[retry])
```

Reasons to keep this explicit even with no model calls in the loop:
- Each stage is a **plain, independently testable function** — rule engine, lookup table, template renderer, symbolic checker are each unit-testable in isolation with fixed inputs/outputs, and none of them need mocking an LLM to test.
- **Timeouts and retries** are still attachable per stage (e.g. a database timeout on curriculum lookup shouldn't force re-running the diagnoser).
- Full **session replay** for teacher audit and debugging: given a session ID, you can reconstruct exactly which rule fired, which curriculum node was looked up, and which hint template was rendered — and because every step is deterministic, replaying the same inputs always reproduces the same output, which is a genuine advantage over the LLM version for debugging and for defending a grade to a parent or administrator.

---

## 5. Data model (core entities)

```
Student        { id, grade_level, iep_flags?, created_at }
Session        { id, student_id, problem_id, started_at, state, attempt_count }
Attempt        { id, session_id, student_answer, timestamp, hint_level_shown }
Problem        { id, curriculum_node_id, prompt, correct_answer, answer_type, grade_band }
CurriculumNode { id, standard_code, grade_band, definition, remediation_strategies[], prerequisite_ids[] }
MisconceptionTag { id, label, operation_type, description, example_pattern }
HintLog        { id, session_id, attempt_number, misconception_tag_id, curriculum_node_id, hint_text, leak_check_passed }
GradeResult    { id, attempt_id, score, method (symbolic|rule_match|teacher), matched_rule_id?, rubric_breakdown? }
ReviewItem     { id, session_id, reason (low_confidence|max_hints|safety_flag|audit_sample), teacher_verdict?, resolved_at? }
```

Every log-bearing table (`Attempt`, `HintLog`, `GradeResult`, `ReviewItem`) is append-only and timestamped — this is what makes both teacher audit and later model improvement possible.

---

## 6. API surface (indicative)

| Endpoint | Purpose |
|---|---|
| `POST /sessions` | Start a session for a student + problem |
| `POST /sessions/{id}/attempts` | Submit a student answer, triggers diagnose→retrieve→hint pipeline |
| `GET /sessions/{id}/hint` | Poll/stream the generated hint once leak-check passes |
| `POST /sessions/{id}/grade` | Trigger grading once a follow-up response is submitted |
| `GET /teacher/review-queue` | List pending `ReviewItem`s for a teacher's class |
| `POST /teacher/review/{id}` | Submit teacher verdict, optionally patch `CurriculumNode` or `MisconceptionTag` |
| `GET /curriculum/nodes/{id}` | Admin CRUD for curriculum content (teacher/curriculum-designer facing) |

Internal service-to-service calls (diagnoser → retriever → hint generator → leak checker) should go through the orchestration layer rather than calling each other directly, so retries/timeouts/logging are handled in one place.

---

## 7. Guardrails & safety

- **Answer-leak prevention** on every hint (Section 3.3) — enforced primarily by pre-launch curriculum review of every template, with the automated slot-fill check as a secondary safety net.
- **Grade-band language control:** template sets are authored separately per grade band (K–2 vs 9–12 templates are genuinely different files/entries, not one template with a tone parameter) — a kindergartner and a 10th grader structurally cannot receive the same phrasing, because there's no shared generation path that could blur the two.
- **Content-safety pass on open-ended student responses:** free-text answers (word-problem explanations) are screened for self-harm or distress signals — since these responses are already being routed to the Teacher Review Console by default (Section 3.5), this can be a keyword/pattern flag layered on that same routing rather than a separate model-based classifier, at the cost of being less sensitive than an LLM-based screen would be; this trade-off is worth flagging explicitly to whoever owns student safety sign-off.
- **Coverage, not hallucination, is the risk now:** with no generation step, the system can't state something false — but it *can* silently under-serve a student by matching an `unknown` tag or a shallow generic hint when a better rule/template exists and just hasn't been authored yet. This needs the same seriousness as a hallucination risk: track `unknown`-tag rate and generic-hint rate per curriculum node as first-class metrics (Section 8), because a quiet coverage gap is easy to miss without them.
- **Privacy/compliance (COPPA/FERPA):** no student PII needs to leave the system boundary at all now (no third-party model API calls in the core loop), which meaningfully simplifies this guardrail versus the LLM version — data retention and access controls still follow standard ed-tech compliance requirements and need sign-off before handling real student data.
- **Bias review of the misconception taxonomy and templates:** periodic audit that tags describe *error patterns*, not student ability labels, and that hint tone doesn't vary by demographic proxies — this is now a review of a fixed, readable set of rules and templates rather than an audit of a model's behavior, which is arguably easier to do thoroughly.

---

## 8. Observability & continuous improvement

- **Per-misconception heatmaps** for teachers: which errors are most common in their class/grade, trending over time.
- **`unknown`-tag rate and generic-hint rate**, overall and per curriculum node — this is now the core quality metric for the whole system (replacing "diagnoser accuracy" from the LLM version, since there's no model prediction to score against ground truth — the question is simply how often the rule table has no answer).
- **Hint effectiveness:** does a given hint (by curriculum node + hint level) correlate with a correct next attempt? Underperforming templates get flagged for a teacher/curriculum author to rewrite or add variants to.
- **Latency dashboards per pipeline stage** — with no model calls in the core loop, latency should be trivially fast (rule evaluation, a keyed DB lookup, and string templating are all sub-millisecond to low-millisecond operations); if the diagnose→hint round trip is *not* fast, that's a sign something's implemented wrong (e.g. an unindexed lookup table), not a cost/latency trade-off to manage.
- **Open-ended-response volume routed to teacher review** — since these bypass auto-grading by design (Section 3.5), track this as a load metric on the Teacher Review Console, and as a signal for where to invest in better constrained response formats.

---

## 9. Suggested tech stack

| Layer | Option |
|---|---|
| Diagnostic rule engine | Plain code (a per-operation decision table/ordered rule list) is often enough; a lightweight rules library (e.g. JSON-logic-style config) if you want non-engineers authoring/editing rules without a deploy |
| Curriculum lookup | A single Postgres table keyed on `(misconception_tag, grade_band, operation)` — no vector store, no embeddings; this is the main infrastructure simplification versus the LLM version |
| Hint templates | A template engine (Jinja2/Handlebars-style) for slot-filling problem values into pre-authored hint strings, versioned alongside the curriculum content it belongs to |
| Symbolic grading | SymPy (or similar CAS) as an isolated microservice, called by the Grader for closed-form answers — unchanged from the LLM version, this was always deterministic |
| Orchestration | A simple state machine library or even a well-structured job/queue system (LangGraph/Temporal still work fine here, but a lighter-weight FSM library is proportionate now that no stage needs long-running model calls) |
| Backend/API | FastAPI or Node/Express, stateless services behind the orchestration layer |
| Data store | Postgres for all relational entities in Section 5 |
| Teacher dashboard | React app consuming the `/teacher/*` endpoints |
| Observability | OpenTelemetry tracing across pipeline stages; a metrics store (Prometheus/Grafana or a hosted equivalent) for the dashboards in Section 8 |

No LLM API dependency is required anywhere in the core loop. If a future phase decides free-text grading needs more than teacher review can keep up with, that would be a deliberate, separately-scoped addition (see Section 10) — not part of this default architecture.

---

## 10. Phased roadmap

**Phase 0 — MVP (single strand, single grade band):**
Rule-based diagnoser, a small hand-authored curriculum KB (~10 nodes), template hints, symbolic grading for closed-form answers, and 100% teacher review of anything not closed-form. Goal: validate the pipeline shape and start measuring `unknown`-tag rate before expanding coverage.

**Phase 1 — Expand rule and template coverage:**
Grow the rule table and curriculum KB using real `unknown`-tag data and teacher-authored corrections from Phase 0 as the backlog; introduce constrained short-response formats (Section 3.5) to bring more question types into deterministic grading; teacher review narrows from 100% to just low-coverage cases (`unknown` tags, open-ended free text, max-hint escalations).

**Phase 2 — Full deterministic grading + multi-turn dialogue:**
Round out the hybrid grader (symbolic + rule/keyword-matched constrained responses) across all supported strands, support multi-level hint escalation dialogues, launch the teacher analytics dashboard (`unknown`-rate and hint-effectiveness views from Section 8).

**Phase 3 — Scale and integrate:**
Extend across the full K–12 range and more operation types, tie session data back into an adaptive curriculum sequencer (e.g. feeding difficulty/mastery signals into a broader practice product). If genuinely open-ended free-text grading at scale becomes a real product need at this point, evaluate it as its own separately-scoped decision — with its own review process and stakeholder sign-off — rather than folding it back into the default pipeline.

---

## 11. Student experience layer — making the pipeline fun

Everything in Sections 3–8 is backend. None of it needs to feel like a form. The student-facing client is a thin, game-shaped skin over the same `/sessions`, `/attempts`, `/hint` API surface from Section 6 — the pipeline doesn't change per grade band, but its presentation should, since a 1st grader and a 10th grader need very different framing to stay motivated.

```mermaid
flowchart TD
    subgraph Student Client (game UI)
        UI1[Problem screen] --> UI2[Answer buttons/input]
        UI2 --> UI3[Hint moment:\nmascot + visual model]
        UI3 --> UI4[Retry]
        UI4 --> UI5[Reward moment:\nstars, gems, streak]
    end
    UI2 -- POST /attempts --> API[Orchestration API]
    API -- streamed hint --> UI3
    API -- grade result --> UI5
    UI5 -- session summary --> Teacher[Teacher dashboard]
```

### 11.1 Turning each pipeline moment into a game moment

| Pipeline moment | Student-facing framing |
|---|---|
| Wrong answer submitted | No red "X" or buzzer — combo meter pauses, a mascot character reacts with curiosity, not disappointment |
| Misconception diagnosed | Invisible to the student — this is what decides *which* hint/visual shows up, never surfaced as a label like "you have a subtraction misconception" |
| Curriculum-grounded hint generated | Rendered as a visual manipulative (see 11.2), not a wall of text — the hint *is* the ten-frame, number line, array, or fraction bar, with a short spoken-style question layered on top |
| Hint escalation (level 1→3) | Framed as the mascot "zooming in" — level 1 is a gentle question, level 3 is a fully worked example with one blank — matching the seriousness of a struggling moment without ever feeling punitive |
| Correct grade, high confidence | Immediate reward: gems, stars scaled down for how many hints were needed, combo streak continues |
| Low-confidence grade / escalation to teacher | Framed to the student as "sending this one to your teacher for a closer look," not as a failure state — preserves trust and removes any stigma from being reviewed |
| Repeated resolution of the same misconception tag | Unlocks a named, specific achievement (e.g. "Regrouping Master") tied directly to the taxonomy in Section 5 — this is the single highest-leverage motivational hook, because it's evidence-based mastery, not a participation badge |
| Session end | Progress map / avatar evolution, matching the strand-mastery model — not a raw percentage score |

### 11.2 Shared visual-model library

The curriculum KB (3.2) already stores *which representation* a remediation strategy calls for (ten-frame, number line, array, fraction bar, base-10 blocks, etc.). The frontend should implement one small, reusable rendering library keyed to those same representation types, so a curriculum author picking "number line, jump of 3" in the admin tool gets the exact same visual a student sees in the hint — no separate design step, no drift between what a teacher approves and what gets shown.

*(The K-5 arithmetic game built earlier — Numeria — is a working reference implementation of exactly this: its SVG generators for ten-frames, number lines, arrays, fraction bars, and base-10 blocks are a natural starting point for this shared library, and its combo/gem/buddy-evolution mechanics are a concrete example of 11.1's reward mapping for the K-5 band.)*

### 11.3 Tone shifts across the K–12 span

One skin does not fit K–12. Recommend at least two presentation tracks sharing the same backend contract:
- **K–5:** adventure/collection framing (islands, a companion creature that evolves, bright saturated visuals) — this is what Numeria demonstrates.
- **6–12:** a cooler, less overtly "cute" register — think skill trees, streak stats, and a progress map styled more like a strategy or stats-tracking game than a storybook; mascot-style characters and cutesy sound effects should fade out, replaced by cleaner UI, leaderboards framed around personal-best streaks (not class rank, to avoid public shaming), and achievement language that reads as competence ("Fluent in two-step equations") rather than collectible-toy language.

### 11.4 Motivational mechanics wired to backend signals

- **Streak/combo meter** → directly reflects consecutive correct `GradeResult`s; resets (gently, no penalty) on a miss.
- **Gems/currency** → scaled by hint count already used per attempt (Section 3.5's grading confidence and Section 3.3's hint level are natural inputs), spendable on cosmetic-only rewards so difficulty is never pay-gated.
- **Mastery badges** → generated directly from the `MisconceptionTag` history in Section 5: a badge fires when a student who previously triggered a tag 2–3 times then clears that skill's problems without needing a hint, which is a much stronger signal of real mastery than "got 10 in a row right."
- **Progress map** → one node per `CurriculumNode`, visually locked/unlocked by prerequisite links already defined in the data model — the map *is* the curriculum graph, just skinned.
- **Teacher visibility** → the same badge/streak data students see doubles as the teacher heatmap in Section 8, so "fun for the student" and "useful for the teacher" are the same underlying event stream, not two separate systems to maintain.

### 11.5 UX guardrails specific to this layer

- Hints and rewards should **never be faster to click through than to actually read** — pacing (brief animation delays) matters as much as content for a Socratic approach to land.
- Sound and animation should be **muteable/reducible** by default settings — some students (and classrooms) need a low-stimulation mode.
- Escalation to a teacher must **never be styled as a loss state** (no "game over," no red screens) — struggling and needing a human is a normal, supported outcome of the pipeline, and the UI should say so plainly.
- Accessibility: hints should have a text-only equivalent alongside the visual model for screen readers, and reading level of any hint copy should be checked against grade band — the same constraint already applied to hint *generation* in Section 7 applies to hint *display*.

## 12. Key risks

- **Coverage gaps are invisible by default.** A rule table only knows what it's been taught; a genuinely novel wrong-answer pattern silently falls to `unknown` and a generic hint rather than a plausible-but-wrong guess (which is safer) — but that's only a good trade-off if someone is actually watching the `unknown`-tag rate and authoring new rules in response. Without that discipline, the system quietly stops helping on anything outside its original scope while looking healthy on paper.
- **Hint staleness.** The same student hitting the same misconception twice gets the exact same template both times, since nothing is paraphrasing it fresh — worth authoring 2–3 phrasing variants per hint level specifically to avoid this becoming a noticeably repetitive experience.
- **Open-ended free-text grading has a real ceiling.** Keyword/structure matching will misgrade phrasing a human teacher would clearly get right or wrong — this isn't a bug to fix, it's an inherent limit of the deterministic approach that should be designed around (favor constrained formats) rather than patched with brittle regex that creates false confidence.
- **Authoring becomes the bottleneck, not model quality.** Every phase's progress now depends on curriculum designers and teachers writing rules and templates, not on retraining or prompting a model — this shifts the roadmap's critical path to content-authoring capacity/tooling, which is worth staffing and tooling for deliberately rather than assuming it'll keep pace on its own.
- **Teacher trust/adoption** — if overrides feel ignored or the review queue is noisy (e.g. too much open-ended text landing there because constrained formats weren't adopted widely enough), teachers stop using it; the queue's composition needs real tuning against teacher feedback, not just the rules picked in advance.