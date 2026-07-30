"""The teacher's review queue (§3.6).

The other half of the routing work: sessions now reach a queue, and this is where
a teacher works it. Without it the queue is readable only with `curl`, and a row
nobody can see is barely better than a row that was never written.

Three things §3.6 asks for that shape this page.

**Navigation, not recommendation.** Each card states what happened — the problem,
what the child typed each time, what they were shown, why it routed — and offers
three verdicts with no default and no suggestion. P1.7 will add model-written
triage text here, and the plan is explicit that it must remain navigation: a
suggested verdict is a verdict, because a queue of forty is cleared by agreeing
with whatever is pre-selected.

**Evidence one click away.** "Show what happened" fetches the session replay
reconstructed from append-only rows. A teacher defending a grade needs to be able
to check rather than trust, and evidence they have to go hunting for is evidence
they stop consulting.

**The three verdicts are not a scale.** Confirmed and overridden are judgments
about this session; *needs follow-up* is about the child, and it is the one that
should be easy to reach when a teacher is unsure. Making the unsure case cheap is
what stops it being resolved as "confirmed" to clear the row.
"""

from __future__ import annotations

REVIEW_PAGE = """
<title>Review queue</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 16px/1.55 system-ui, sans-serif; max-width: 52rem; margin: 2rem auto;
         padding: 0 1rem; }
  h1 { font-size: 1.35rem; margin-bottom: .25rem; }
  .sub { opacity: .7; font-size: .9rem; margin-top: 0; }
  .card { border: 1px solid rgba(128,128,128,.35); border-radius: 10px;
          padding: 1rem 1.15rem; margin: 1.25rem 0; }
  .why { display: inline-block; font-size: .72rem; letter-spacing: .06em;
         text-transform: uppercase; font-weight: 700; padding: .2rem .5rem;
         border-radius: 5px; border: 1px solid currentColor; opacity: .85; }
  .why.leak_fallback { color: #b3452f; }
  .why.max_hints { color: #b07d2b; }
  .why.symbolic_disagreement { color: #8a4fbf; }
  .why.audit_sample { opacity: .55; }
  .meta { font-size: .82rem; opacity: .7; margin-top: .4rem; }
  .problem { font-weight: 600; font-size: 1.1rem; margin: .6rem 0 .5rem; }
  .answers { margin: .3rem 0 .8rem; }
  .grades { margin: .4rem 0 .8rem; }
  .grade { font-size: .88rem; padding: .3rem .6rem; border-left: 3px solid;
           margin-bottom: .25rem; border-radius: 0 5px 5px 0; }
  .grade.ok { border-left-color: #2f855a; background: rgba(47,133,90,.08); }
  .grade.no { border-left-color: #b3452f; background: rgba(179,69,47,.07); }
  .answers code { font-size: 1rem; padding: .1rem .4rem; border-radius: 4px;
                  background: rgba(128,128,128,.16); margin-right: .35rem; }
  .hint { border-left: 3px solid rgba(128,128,128,.4); padding: .4rem .8rem;
          margin: .35rem 0; font-size: .95rem; }
  .degraded { font-size: .82rem; color: #b07d2b; }
  button { font: inherit; padding: .5rem .9rem; margin: .25rem .35rem .25rem 0;
           border-radius: 7px; border: 1px solid rgba(128,128,128,.5);
           background: transparent; cursor: pointer; }
  button:hover { border-color: currentColor; }
  .primary { font-weight: 600; }
  .followup { border-color: #8a4fbf; color: #8a4fbf; }
  .link { border: none; text-decoration: underline; padding-left: 0; opacity: .75; }
  label { display: block; font-size: .85rem; margin-top: .6rem; }
  input[type=text] { width: 100%; padding: .45rem; font: inherit; border-radius: 6px;
                     border: 1px solid rgba(128,128,128,.5); background: transparent;
                     color: inherit; }
  pre { font-size: .78rem; line-height: 1.5; overflow-x: auto; padding: .8rem;
        border-radius: 8px; background: rgba(128,128,128,.12); margin: .6rem 0 0; }
  .empty { opacity: .7; }
  .nav { margin: .5rem 0 1.25rem; font-size: .9rem; }
  .nav a { color: inherit; text-decoration: underline; opacity: .8; }
  .nav a:hover, .nav a:focus-visible { opacity: 1; }
</style>

<h1>Review queue</h1>
<p class="sub">Sessions from your class that need a look. Phase 0 reviews every
one, so most will say <em>audit sample</em> — that means nothing went wrong, not
that nothing is worth reading.</p>
<nav class="nav" aria-label="Teacher surfaces">
  <a href="#" id="to-dashboard">Class overview</a> ·
  <a href="#" id="to-rate">Rate generated hints</a>
</nav>
<div id="queue"><p class="empty">Loading…</p></div>

<script>
const principal = new URLSearchParams(location.search).get('as') || '';
const headers = {'content-type': 'application/json', 'x-principal-id': principal};

/* Carry the principal across rather than making a teacher re-paste it — but omit
   it entirely when absent, so a missing id is not propagated into a link that then
   looks broken rather than incomplete. */
for (const [id, href] of [['to-dashboard','/teacher/dashboard'], ['to-rate','/teacher/rate']]) {
  document.getElementById(id).href =
    principal ? `${href}?as=${encodeURIComponent(principal)}` : href;
}

function esc(s) {
  return (s ?? '').replace(/[&<>"]/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

async function load() {
  const queue = document.getElementById('queue');
  const res = await fetch('/teacher/review-queue', {headers});
  if (!res.ok) {
    /* A bare 401 sends a teacher looking for a login screen that does not exist.
       The cause is always a missing `as=` parameter, so say that. */
    queue.innerHTML = res.status === 401
      ? '<p class="empty">No principal id in the address. Open this page as ' +
        '<code>/teacher/review?as=&lt;your principal id&gt;</code> — ' +
        '<code>python -m scripts.seed_pilot</code> prints it.</p>'
      : '<p class="empty">Could not load the queue (' + res.status + ').</p>';
    return;
  }
  const items = await res.json();
  if (!items.length) {
    queue.innerHTML = '<p class="empty">Nothing waiting. Every session in your ' +
      'class has been reviewed.</p>';
    return;
  }
  queue.innerHTML = items.map(card).join('');
  for (const item of items) wire(item.id);
}

/* The verdict the teacher is being asked to confirm.
   Shown per attempt rather than per session, because a grade belongs to the
   answer that earned it — a list of scores beside a list of answers leaves the
   teacher matching them up by position. */
function grades(item) {
  if (!item.grades || !item.grades.length) {
    return '<div class="meta"><em>no grade was recorded for this session</em></div>';
  }
  const rows = item.grades.map((g, i) => {
    const verdict = g.score >= 1 ? 'marked correct' : 'marked wrong';
    const checker = g.symbolic_agreed === null ? ''
      : g.symbolic_agreed ? ' · checker agreed' : ' · CHECKER DISAGREED';
    return `<div class="grade ${g.score >= 1 ? 'ok' : 'no'}">`
      + `Attempt ${i + 1}: <code>${esc(g.answer)}</code> — ${verdict}`
      + `<span class="meta"> by ${esc(g.method)}${checker}</span></div>`;
  }).join('');
  return `<div class="grades">${rows}</div>`;
}

function card(item) {
  const answers = item.answers.map(a => `<code>${esc(a)}</code>`).join('');
  const hints = item.hints_shown.length
    ? item.hints_shown.map((h, i) =>
        `<div class="hint"><strong>Hint ${i + 1}:</strong> ${esc(h)}</div>`).join('')
    : '<div class="hint"><em>No hint was shown.</em></div>';
  return `
  <div class="card" id="c-${item.id}">
    <span class="why ${esc(item.reason)}">${esc(item.reason.replace(/_/g, ' '))}</span>
    ${item.ran_degraded
      ? '<span class="degraded"> · ran degraded (a stage fell back)</span>' : ''}
    <div class="problem">${esc(item.problem)}
      <span class="meta">answer: ${esc(item.correct_answer)}</span></div>
    <div class="answers">The child answered: ${answers || '<em>nothing</em>'}</div>
    ${grades(item)}
    ${hints}
    <div class="meta">${item.misconception_tag
      ? 'diagnosed: ' + esc(item.misconception_tag) : 'no misconception identified'}</div>
    <button class="link" id="ev-${item.id}">Show what happened</button>
    <div id="evidence-${item.id}"></div>
    <label>Notes (optional)
      <input type="text" id="notes-${item.id}" placeholder="what you did, or what to watch">
    </label>
    <div style="margin-top:.7rem">
      <button class="primary" id="ok-${item.id}">Looks right</button>
      <button id="no-${item.id}">I'd grade this differently</button>
      <button class="followup" id="fu-${item.id}">Follow up with this child</button>
    </div>
  </div>`;
}

/* addEventListener rather than inline handlers: an inline handler resolves names
   against the element and its form first, which is how the student page ended up
   calling a DOM method instead of its own function. */
function wire(id) {
  document.getElementById(`ev-${id}`).addEventListener('click', () => evidence(id));
  document.getElementById(`ok-${id}`).addEventListener('click', () => resolve(id, 'confirmed'));
  document.getElementById(`no-${id}`).addEventListener('click', () => resolve(id, 'overridden'));
  document.getElementById(`fu-${id}`).addEventListener('click',
    () => resolve(id, 'needs_followup'));
}

async function evidence(id) {
  const target = document.getElementById(`evidence-${id}`);
  if (target.innerHTML) { target.innerHTML = ''; return; }
  const res = await fetch(`/teacher/review/${id}/evidence`, {headers});
  if (!res.ok) { target.innerHTML = '<p class="meta">Could not load it.</p>'; return; }
  const data = await res.json();
  target.innerHTML = `<pre>${esc(data.steps.join('\\n'))}</pre>
    <div class="meta">${data.model_calls} model call(s), $${data.cost_usd.toFixed(6)}</div>`;
}

async function resolve(id, verdict) {
  const notes = document.getElementById(`notes-${id}`).value.trim();
  const res = await fetch(`/teacher/review/${id}`, {
    method: 'POST', headers,
    body: JSON.stringify({verdict, notes: notes || null}),
  });
  if (!res.ok) { alert('Could not save that (' + res.status + ')'); return; }
  document.getElementById(`c-${id}`).remove();
  if (!document.querySelector('.card')) load();
}

load();
</script>
"""
