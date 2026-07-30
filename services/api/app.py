"""The teacher console app (P0.8).

Server-rendered rather than the React client the plan's layout sketches. That is
a deliberate scope call for Phase 0 and worth stating: this instrument is used by
a handful of pilot teachers to answer one question per card, and a build
toolchain plus an API client plus a component library would be a week of work
serving the same three buttons. If the console grows past the pilot — the
analytics dashboards in §8, the curriculum authoring tool in P1.1 — that is the
point to reach for a real frontend, and the API below is unchanged by it.

What is *not* scoped down is the rating flow itself. Implementation-Plan.md P0.8
calls it "the phase's primary data-collection instrument, so it deserves real UX
attention rather than a debug screen": a teacher is comparing two hints for a
specific child, and the page has to make that comparison the obvious thing to do.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from services.api.admin import router as admin_router
from services.api.dashboard_page import DASHBOARD_PAGE
from services.api.review_page import REVIEW_PAGE
from services.api.runs_page import RUNS_PAGE
from services.api.student import router as student_router
from services.api.student_page import STUDENT_PAGE
from services.api.teacher import router as teacher_router
from services.api.teacher_dashboard import router as dashboard_router

app = FastAPI(
    title="Tutor",
    description="Phase 0: student sessions, shadow rating, and the review queue",
    version="0.1.0",
)
app.include_router(teacher_router)
app.include_router(dashboard_router)
app.include_router(student_router)
app.include_router(admin_router)


@app.get("/", response_class=HTMLResponse)
def student_page() -> str:
    """The child's surface (P0.11).

    Mounted at the root because it is the one page a pilot student is given a
    link to, and a URL a seven-year-old has to type accurately is a URL that
    produces support requests instead of sessions.
    """
    return STUDENT_PAGE


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


RATING_PAGE = """
<title>Shadow ratings</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 16px/1.55 system-ui, sans-serif; max-width: 46rem; margin: 2rem auto;
         padding: 0 1rem; }
  h1 { font-size: 1.35rem; margin-bottom: .25rem; }
  .sub { opacity: .7; font-size: .9rem; margin-top: 0; }
  .card { border: 1px solid rgba(128,128,128,.35); border-radius: 10px;
          padding: 1rem 1.15rem; margin: 1.25rem 0; }
  .meta { font-size: .82rem; opacity: .7; }
  .problem { font-weight: 600; margin: .35rem 0 .75rem; }
  .hint { border-left: 3px solid rgba(128,128,128,.4); padding: .5rem .85rem;
          margin: .5rem 0; }
  .hint h3 { font-size: .78rem; text-transform: uppercase; letter-spacing: .06em;
             margin: 0 0 .3rem; opacity: .65; }
  .shown { border-left-color: #5b8def; }
  .generated { border-left-color: #d98b3a; }
  .flag { color: #b3452f; font-size: .85rem; }
  button { font: inherit; padding: .5rem .9rem; margin: .25rem .35rem .25rem 0;
           border-radius: 7px; border: 1px solid rgba(128,128,128,.5);
           background: transparent; cursor: pointer; }
  button:hover { border-color: currentColor; }
  .primary { font-weight: 600; }
  label { display: block; font-size: .85rem; margin-top: .6rem; }
  input[type=text] { width: 100%; padding: .45rem; font: inherit;
                     border-radius: 6px; border: 1px solid rgba(128,128,128,.5);
                     background: transparent; color: inherit; }
  .empty { opacity: .7; }
</style>
<h1>Shadow ratings</h1>
<p class="sub">The child was shown the blue hint. The orange one is what the model
produced but never displayed. Which would have helped this child more?</p>
<div id="queue"><p class="empty">Loading…</p></div>
<script>
const principal = new URLSearchParams(location.search).get('as') || '';
const headers = {'content-type': 'application/json', 'x-principal-id': principal};

async function load() {
  const res = await fetch('/teacher/shadow-queue', {headers});
  const queue = document.getElementById('queue');
  if (!res.ok) {
    queue.innerHTML = '<p class="empty">Could not load the queue (' + res.status +
      '). Add <code>?as=&lt;your-principal-id&gt;</code> to the URL.</p>';
    return;
  }
  const items = await res.json();
  if (!items.length) {
    queue.innerHTML = '<p class="empty">Nothing waiting to be rated.</p>';
    return;
  }
  queue.innerHTML = items.map(card).join('');
}

function esc(s) {
  return (s ?? '').replace(/[&<>"]/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

function card(item) {
  return `
  <div class="card" id="c-${item.id}">
    <div class="meta">${esc(item.misconception_tag)} · hint level ${item.hint_level}
      · ${esc(item.prompt_version)}</div>
    <div class="problem">${esc(item.problem)} — the child answered
      "${esc(item.student_answer)}"</div>
    <div class="hint shown"><h3>Shown to the child</h3>${esc(item.shown_hint) ||
      '<em>none recorded</em>'}</div>
    <div class="hint generated"><h3>Generated, not shown</h3>${esc(item.generated_hint)}
      ${item.leak_check_passed ? '' :
        '<div class="flag">flagged by the leak checker: ' + esc(item.leak_reason) + '</div>'}
    </div>
    <label>If the misconception above is wrong, what should it be?
      <input type="text" id="tag-${item.id}" placeholder="leave blank if correct">
    </label>
    <div style="margin-top:.7rem">
      <button class="primary" onclick="rate('${item.id}', true, false)">Generated is better</button>
      <button onclick="rate('${item.id}', false, false)">Shown was better / equal</button>
      <button onclick="rate('${item.id}', false, true)">It gives the answer away</button>
    </div>
  </div>`;
}

async function rate(id, better, leak) {
  const tag = document.getElementById('tag-' + id).value.trim();
  const res = await fetch(`/teacher/shadow/${id}/rating`, {
    method: 'POST', headers,
    body: JSON.stringify({
      better_than_shown: better, would_leak: leak,
      corrected_tag: tag || null,
    }),
  });
  if (res.ok) document.getElementById('c-' + id).remove();
  else alert('Could not save that rating (' + res.status + ')');
  if (!document.querySelector('.card')) load();
}

load();
</script>
"""


@app.get("/teacher/rate", response_class=HTMLResponse)
def rating_page() -> str:
    """The rating instrument.

    One question per card, both hints side by side, and the tag correction inline
    — because a teacher who has to navigate elsewhere to fix a wrong tag mostly
    will not, and that correction is the label P0.9's calibration measurement
    depends on.
    """
    return RATING_PAGE


@app.get("/admin/pipeline", response_class=HTMLResponse)
def pipeline_page() -> str:
    """The per-stage inspector (M0.8, §8).

    Separate from the teacher surfaces because the audience and the content
    differ: this renders raw prompt payloads and a child's answers, and it exists
    to answer "what did each stage actually receive and return" — an engineering
    question, not a teaching one. The data behind it is admin- or
    teacher-of-that-student-scoped by `can_read_audit_trail`; the page itself is
    just markup and fetches nothing until it has a principal.
    """
    return RUNS_PAGE


@app.get("/teacher/review", response_class=HTMLResponse)
def review_page() -> str:
    """The §3.6 review queue — where a routed session actually gets worked.

    Separate page from `/teacher/rate` because they answer different questions.
    Rating asks "is the generated hint better than the template", which is Phase
    0 measuring the model. Review asks "is this child's session all right", which
    is the teacher doing their job. Merging them would make each a distraction
    from the other.
    """
    return REVIEW_PAGE


@app.get("/teacher/dashboard", response_class=HTMLResponse)
def dashboard_page() -> str:
    """The class overview (§3.6, §8).

    The third teacher surface, and the only one that is about the child rather
    than about a session. Review and rating are both queues — they ask "judge this
    row" and say nothing across rows, so a teacher working either of them cannot
    see that the same misconception has come up four times for the same child.

    Deliberately a peer of the queue rather than a landing page in front of it. A
    dashboard that intercepts a teacher on the way to work they were already going
    to do is a tax on that work; this one is somewhere they go when the question is
    "who needs me", not "what is next".
    """
    return DASHBOARD_PAGE
