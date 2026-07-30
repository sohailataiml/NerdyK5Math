"""The class dashboard (§3.6, §8) — the overview before the queue.

Visually a continuation of `review_page.py` rather than a new design: same border,
radius, and type idiom, because a teacher moving between the two is doing one job
and a second visual language would read as a second product.

Three things about the presentation are decisions, not styling.

**A withheld rate is shown as a withheld rate.** `2 of 3 · too few to rate` takes
more room than `67%` and is the entire point of the surface — see
`teacher_dashboard.MIN_FOR_A_RATE`. Rendering a blank cell instead would read as a
bug and get "fixed" back into a percentage.

**Recurring is the loudest element on the page.** It is the one fact here that a
teacher cannot get from the review queue, so it gets the accent, the badge, and the
top of the sort order. Everything else on the row is context for it.

**Nothing is conveyed by colour alone.** The recurrence badge carries text, the
accuracy bars carry their numbers, and the abstention note is a sentence. A pilot
runs on whatever screen a classroom has.
"""

from __future__ import annotations

DASHBOARD_PAGE = """
<title>Class overview</title>
<style>
  :root {
    color-scheme: light dark;
    --line: rgba(128,128,128,.35);
    --soft: rgba(128,128,128,.12);
    --accent: #8a4fbf;
    --warn: #b07d2b;
    --good: #2f855a;
    --bad: #b3452f;
  }
  body { font: 16px/1.55 system-ui, sans-serif; max-width: 64rem; margin: 2rem auto;
         padding: 0 1rem; }
  a { color: inherit; }
  h1 { font-size: 1.35rem; margin-bottom: .25rem; }
  h2 { font-size: 1rem; letter-spacing: .04em; text-transform: uppercase;
       opacity: .65; margin: 2.2rem 0 .75rem; font-weight: 700; }
  .sub { opacity: .7; font-size: .9rem; margin-top: 0; }
  .nav { margin: .5rem 0 1.5rem; font-size: .9rem; }
  .nav a { text-decoration: underline; opacity: .8; }
  .nav a:hover, .nav a:focus-visible { opacity: 1; }

  /* Scale contrast carries the hierarchy: the numbers are the headline, their
     labels are annotation. */
  .stats { display: flex; flex-wrap: wrap; gap: .75rem; }
  .stat { flex: 1 1 8rem; border: 1px solid var(--line); border-radius: 10px;
          padding: .8rem .9rem; }
  .stat .n { font-size: 1.9rem; font-weight: 700; line-height: 1.1;
             font-variant-numeric: tabular-nums; }
  .stat .l { font-size: .78rem; opacity: .7; margin-top: .15rem; }
  .stat.flag { border-color: var(--accent); }
  .stat.flag .n { color: var(--accent); }

  table { width: 100%; border-collapse: collapse; margin-top: .25rem; }
  th, td { text-align: left; padding: .55rem .5rem; border-bottom: 1px solid var(--line);
           font-size: .9rem; vertical-align: top; }
  th { font-size: .74rem; letter-spacing: .05em; text-transform: uppercase;
       opacity: .65; border-bottom-width: 2px; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  tbody tr:hover { background: var(--soft); }
  .who { font-weight: 600; }
  .grade { font-size: .78rem; opacity: .6; font-weight: 400; }

  .badge { display: inline-block; font-size: .7rem; letter-spacing: .05em;
           text-transform: uppercase; font-weight: 700; padding: .18rem .45rem;
           border-radius: 5px; border: 1px solid currentColor; margin: .1rem .2rem .1rem 0; }
  .badge.recur { color: var(--accent); background: rgba(138,79,191,.1); }
  .badge.queue { color: var(--warn); }
  .tag { font-size: .82rem; opacity: .8; }
  .tag .c { opacity: .6; }

  /* The bar is decoration on a number that is always printed. */
  .bar { height: .35rem; border-radius: 3px; background: var(--soft); margin-top: .25rem;
         overflow: hidden; max-width: 7rem; }
  .bar > i { display: block; height: 100%; background: var(--good); }
  .withheld { font-size: .8rem; opacity: .6; font-style: italic; }

  .mis { display: grid; grid-template-columns: minmax(10rem, 16rem) 1fr auto;
         gap: .5rem .8rem; align-items: center; }
  .mis .label { font-size: .88rem; }
  .mis .track { height: .5rem; background: var(--soft); border-radius: 3px; overflow: hidden; }
  .mis .track > i { display: block; height: 100%; background: var(--accent); opacity: .75; }
  .mis .n { font-size: .82rem; opacity: .75; font-variant-numeric: tabular-nums; }

  .note { border-left: 3px solid var(--line); padding: .5rem .8rem; margin: 1rem 0;
          font-size: .88rem; opacity: .85; }
  .empty { opacity: .7; }
</style>

<h1>Class overview</h1>
<p class="sub">Your students, across every session — not one session at a time.
Sorted so the children worth looking at first are first.</p>
<nav class="nav" aria-label="Teacher surfaces">
  <a href="#" id="to-review">Review queue</a> ·
  <a href="#" id="to-rate">Rate generated hints</a>
</nav>

<div id="body"><p class="empty">Loading…</p></div>

<script>
const principal = new URLSearchParams(location.search).get('as') || '';
const headers = {'content-type': 'application/json', 'x-principal-id': principal};

function esc(s) {
  return (s ?? '').replace(/[&<>"]/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

/* Carry the principal across, so moving between surfaces does not mean
   re-pasting an id into the address bar.

   Omitted entirely when there is no principal rather than sent empty: `?as=`
   propagates the missing id to the next page, so a teacher who arrived without
   one gets a second failure that looks like a broken link instead of an
   incomplete address. */
for (const [id, href] of [['to-review','/teacher/review'], ['to-rate','/teacher/rate']]) {
  document.getElementById(id).href =
    principal ? `${href}?as=${encodeURIComponent(principal)}` : href;
}

function pct(m) {
  if (!m || m.of === 0) return '<span class="withheld">no attempts graded</span>';
  if (m.rate === null) {
    return `<span class="withheld">${m.count} of ${m.of} · too few to rate</span>`;
  }
  const p = Math.round(m.rate * 100);
  return `${p}% <span class="grade">(${m.count}/${m.of})</span>
    <div class="bar"><i style="width:${p}%"></i></div>`;
}

function when(iso) {
  if (!iso) return '<span class="withheld">never answered</span>';
  const d = new Date(iso);
  return `<span title="${esc(iso)}">${d.toLocaleDateString()}</span>`;
}

function studentRow(s) {
  const flags = [
    ...s.recurring.map(r =>
      `<span class="badge recur">recurring: ${esc(r.replace(/_/g, ' '))}</span>`),
    s.awaiting_review
      ? `<span class="badge queue">${s.awaiting_review} awaiting review</span>` : '',
  ].join('');
  const tags = s.diagnosed.length
    ? s.diagnosed.map(t =>
        `<div class="tag">${esc(t.label.replace(/_/g, ' '))}
          <span class="c">&times; ${t.count}</span></div>`).join('')
    : '<div class="withheld">nothing diagnosed yet</div>';
  return `<tr>
    <td>
      <div class="who">${esc(s.name)} <span class="grade">grade ${s.grade_level}</span></div>
      ${flags}
    </td>
    <td>${tags}</td>
    <td>${pct(s.correct)}</td>
    <td class="num">${s.attempts}</td>
    <td class="num">${s.sessions_answered}${s.sessions_abandoned
      ? ` <span class="grade">+${s.sessions_abandoned} left</span>` : ''}</td>
    <td>${when(s.last_seen)}</td>
  </tr>`;
}

function misconceptions(list) {
  if (!list.length) {
    return `<p class="empty">No misconception has been diagnosed for your class yet.
      Every attempt is diagnosed, so this stays empty only until a child answers.</p>`;
  }
  const top = Math.max(...list.map(t => t.count));
  return '<div class="mis">' + list.map(t => `
    <div class="label">${esc(t.label.replace(/_/g, ' '))}</div>
    <div class="track"><i style="width:${Math.round(100 * t.count / top)}%"></i></div>
    <div class="n">${t.count}&times; &middot; ${t.students_affected}
      ${t.students_affected === 1 ? 'child' : 'children'}</div>`).join('') + '</div>';
}

/* Said plainly, because misconception counts read as a complete account of the
   class's errors when they are a partial one. */
function abstention(m) {
  if (!m || m.of === 0) return '';
  /* The whole phrase differs per branch rather than just the number: a withheld
     rate is a count of a total ("1 of 2 diagnosed attempts"), and splicing it into
     a sentence built for a percentage produces "1 of 2 of diagnosed attempts". */
  const phrase = m.rate === null
    ? `${m.count} of ${m.of} diagnosed attempts`
    : `${Math.round(m.rate * 100)}% of diagnosed attempts (${m.count} of ${m.of})`;
  return `<div class="note"><strong>${phrase}</strong> produced no misconception at
    all — the diagnoser abstained rather than guess. The counts above describe the
    rest, not the whole class.</div>`;
}

async function load() {
  const body = document.getElementById('body');
  const res = await fetch('/teacher/class-summary', {headers});
  if (!res.ok) {
    /* Name the cause rather than the status code. A bare 401 sends a teacher
       looking for a login screen that does not exist; the actual fix is a
       missing parameter in the address they used. */
    body.innerHTML = res.status === 401
      ? `<p class="empty">No principal id in the address. Open this page as
         <code>/teacher/dashboard?as=&lt;your principal id&gt;</code> —
         <code>python -m scripts.seed_pilot</code> prints it.</p>`
      : `<p class="empty">Could not load the overview (${res.status}).</p>`;
    return;
  }
  const d = await res.json();
  const withRecurring = d.students.filter(s => s.recurring.length).length;

  body.innerHTML = `
    <div class="stats">
      <div class="stat"><div class="n">${d.students.length}</div>
        <div class="l">students in your class</div></div>
      <div class="stat"><div class="n">${d.sessions_answered}</div>
        <div class="l">sessions answered</div></div>
      <div class="stat"><div class="n">${d.attempts}</div>
        <div class="l">attempts</div></div>
      <div class="stat ${withRecurring ? 'flag' : ''}"><div class="n">${withRecurring}</div>
        <div class="l">with a recurring misconception</div></div>
      <div class="stat ${d.awaiting_review ? 'flag' : ''}">
        <div class="n">${d.awaiting_review}</div>
        <div class="l">sessions awaiting review</div></div>
    </div>
    ${d.sessions_abandoned ? `<div class="note">${d.sessions_abandoned} session(s) were
      opened and never answered. They are excluded from every rate on this page —
      a child who walked away is not a child who got it wrong.</div>` : ''}

    <h2>Across the class</h2>
    ${misconceptions(d.class_misconceptions)}
    ${abstention(d.abstained)}

    <h2>By student</h2>
    ${d.students.length ? `<table>
      <thead><tr>
        <th scope="col">Student</th>
        <th scope="col">Diagnosed</th>
        <th scope="col">Correct</th>
        <th scope="col" class="num">Attempts</th>
        <th scope="col" class="num">Sessions</th>
        <th scope="col">Last answered</th>
      </tr></thead>
      <tbody>${d.students.map(studentRow).join('')}</tbody>
    </table>` : '<p class="empty">No students are enrolled in your class yet.</p>'}`;
}

load();
</script>
"""
