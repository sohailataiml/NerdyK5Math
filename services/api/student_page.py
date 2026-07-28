"""The student page (P0.11).

"Playable but unpolished" is the plan's bar, and the important half is *playable*:
Phase 0 measures hints on real student work, so if the interface is frustrating
in ways the tutoring is not, the phase measures the interface.

Server-rendered vanilla, like the teacher console, for the same reason — a build
toolchain to serve one input box and a ten-frame is a week spent away from the
thing being measured.

§11.2 and §11.5 shape what is here more than aesthetics do:

- The **ten-frame** shows what the child is starting from. It never shows the
  total, because a filled answer square is an answer leak wearing a picture.
- **No timer, no streak, no score.** §11.5 asks for pacing floors and no loss
  states; a countdown on a child who is stuck teaches them to guess.
- **Escalation reads as help arriving**, never as failure.
- **Reduced motion is honoured**, and every visual has a text equivalent for a
  screen reader — the ten-frame carries an aria-label describing it in words.
"""

from __future__ import annotations

STUDENT_PAGE = """
<title>Maths practice</title>
<style>
  :root {
    color-scheme: light dark;
    --ink: #21252b;
    --paper: #fbfaf7;
    --line: #cfc9bd;
    --filled: #4a7fd4;
    --adding: #e0913c;
    --ok: #3f8f5f;
  }
  @media (prefers-color-scheme: dark) {
    :root { --ink: #e9e6df; --paper: #1b1e22; --line: #4a4f57; }
  }
  * { box-sizing: border-box; }
  body {
    font: 18px/1.6 system-ui, sans-serif; color: var(--ink); background: var(--paper);
    max-width: 34rem; margin: 0 auto; padding: 2rem 1.25rem 4rem;
  }
  h1 { font-size: 1rem; font-weight: 600; letter-spacing: .04em;
       text-transform: uppercase; opacity: .55; margin: 0 0 2rem; }
  .problem { font-size: clamp(2rem, 9vw, 3.25rem); font-weight: 650;
             letter-spacing: -.02em; margin: 0 0 1.5rem; }
  .frame { display: grid; grid-template-columns: repeat(5, 2.5rem);
           gap: .3rem; margin: 0 0 .6rem; }
  .cell { width: 2.5rem; height: 2.5rem; border: 2px solid var(--line);
          border-radius: 6px; }
  .cell.filled { background: var(--filled); border-color: var(--filled); }
  .cell.adding { background: var(--adding); border-color: var(--adding); }
  .frame-note { font-size: .9rem; opacity: .7; margin: 0 0 1.75rem; }
  .numberline { display: flex; align-items: flex-end; gap: 0;
                overflow-x: auto; padding: 1.4rem 0 .2rem; margin: 0 0 .4rem;
                border-bottom: 2px solid var(--line); }
  .tick { flex: 0 0 2rem; height: .6rem; border-left: 2px solid var(--line);
          position: relative; }
  .tick.major { height: 1rem; }
  .tick span { position: absolute; top: -1.5rem; left: -.6rem;
               font-size: .8rem; opacity: .75; }
  .tick.start { border-left-color: var(--filled); height: 1.6rem; }
  .tick.start span { color: var(--filled); font-weight: 700; opacity: 1; }
  form { display: flex; gap: .6rem; align-items: center; margin: 0 0 1.5rem; }
  input[type=text] {
    font: inherit; font-size: 1.5rem; width: 7rem; padding: .5rem .7rem;
    border: 2px solid var(--line); border-radius: 10px;
    background: transparent; color: inherit;
  }
  input[type=text]:focus { outline: 3px solid var(--filled); outline-offset: 2px; }
  button {
    font: inherit; font-weight: 600; padding: .65rem 1.4rem; border-radius: 10px;
    border: 2px solid var(--ink); background: var(--ink); color: var(--paper);
    cursor: pointer;
  }
  button:hover { opacity: .88; }
  button:disabled { opacity: .4; cursor: default; }
  .hint {
    border-left: 4px solid var(--adding); padding: .85rem 1.1rem;
    background: color-mix(in oklab, var(--adding) 9%, transparent);
    border-radius: 0 10px 10px 0; margin: 0 0 1.25rem;
  }
  .hint h2 { font-size: .78rem; text-transform: uppercase; letter-spacing: .06em;
             opacity: .6; margin: 0 0 .35rem; font-weight: 600; }
  .done { border-left-color: var(--ok);
          background: color-mix(in oklab, var(--ok) 10%, transparent); }
  .thinking { opacity: .6; font-style: italic; }
  .quiet { font-size: .9rem; opacity: .65; }
  @media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>

<h1>Maths practice</h1>
<div id="app"><p class="thinking">Getting your first question…</p></div>

<script>
const student = new URLSearchParams(location.search).get('as') || '';
const headers = {'content-type': 'application/json', 'x-principal-id': student};
let session = null;

function esc(s) {
  return (s ?? '').replace(/[&<>"]/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

/* §11.2 representations. Neither one draws the total: a picture that answers
   the question is an answer leak wearing a diagram. Both carry the same words
   in an aria-label and in visible text, which is §11.5's text-only equivalent —
   written once so the two cannot drift apart. */
function visual(v) {
  if (!v) return '';
  const body = v.kind === 'ten_frame' ? tenFrame(v) : numberLine(v);
  return `
    <div role="img" aria-label="${esc(body.words)}">${body.svg}</div>
    <p class="frame-note">${esc(body.words)}</p>`;
}

/* A ten-frame holds ten, so it is only offered when both parts fit. */
function tenFrame(v) {
  const have = v.left, adding = v.right;
  const cells = [];
  for (let i = 0; i < 10; i++) {
    let cls = 'cell';
    if (i < have) cls += ' filled';
    else if (i < have + adding) cls += ' adding';
    cells.push(`<div class="${cls}"></div>`);
  }
  const spill = have + adding > 10
    ? ` ${have + adding - 10} of them won't fit in the frame — where do they go?`
    : '';
  return {
    svg: `<div class="frame">${cells.join('')}</div>`,
    words: `A ten-frame with ${have} squares filled in, and ${adding} more to add.${spill}`,
  };
}

/* Taking away, or numbers past ten. The start is marked; the finish is not. */
function numberLine(v) {
  const start = v.left;
  const span = Math.max(start + (v.operation === 'addition' ? v.right : 0), 10);
  const ticks = [];
  for (let n = 0; n <= span; n++) {
    const major = n % 5 === 0;
    ticks.push(`<div class="tick${major ? ' major' : ''}${n === start ? ' start' : ''}">
      ${major || n === start ? `<span>${n}</span>` : ''}</div>`);
  }
  const direction = v.operation === 'addition' ? 'forward' : 'back';
  return {
    svg: `<div class="numberline">${ticks.join('')}</div>`,
    words: `A number line from 0 to ${span}, with ${start} marked. `
         + `Count ${direction} ${v.right} from ${start}.`,
  };
}

function render(state) {
  const p = session.problem;
  document.getElementById('app').innerHTML = `
    <p class="problem">${esc(p.prompt)}</p>
    ${visual(p.visual)}
    ${state.hint
      ? `<div class="hint"><h2>Hint ${state.hint_level}</h2>${esc(state.hint)}</div>`
      : ''}
    ${state.done ? `
      <div class="hint done">${esc(state.message || '')}</div>
      <button id="next">Next question</button>
    ` : `
      <form id="answer-form">
        <input type="text" id="answer" inputmode="numeric" autocomplete="off"
               aria-label="Your answer" autofocus>
        <button type="submit" id="go">Check</button>
      </form>
      <p class="quiet">Take as long as you like. You can ask for another hint.</p>
    `}`;

  /* Wired here rather than with inline handler attributes, and that is not a
     style preference. An inline handler resolves names against the element and
     its form before the global scope, so a handler calling `submit` binds to
     the form's own native submit() method — the form posts for real, the
     browser navigates to a URL without ?as=, and the next request 401s from a
     page that looks like it lost the child's login. addEventListener has no
     such scope chain. A test asserts this file stays free of them. */
  const form = document.getElementById('answer-form');
  if (form) form.addEventListener('submit', checkAnswer);
  const next = document.getElementById('next');
  if (next) next.addEventListener('click', () => start());

  const box = document.getElementById('answer');
  if (box) box.focus();
}

async function start() {
  document.getElementById('app').innerHTML =
    '<p class="thinking">Getting your next question…</p>';
  const res = await fetch('/student/session', {method: 'POST', headers});
  if (!res.ok) {
    document.getElementById('app').innerHTML =
      `<p class="quiet">Could not start (${res.status}). ` +
      `Add <code>?as=&lt;your id&gt;</code> to the address.</p>`;
    return;
  }
  session = await res.json();
  render({});
}

async function checkAnswer(event) {
  event.preventDefault();
  const answer = document.getElementById('answer').value.trim();
  if (!answer) return;
  document.getElementById('go').disabled = true;

  const res = await fetch(`/student/session/${session.session_id}/answer`, {
    method: 'POST', headers, body: JSON.stringify({answer}),
  });
  if (!res.ok) {
    /* Say something. A dead button teaches a child that trying again is what
       failed, and they stop trying. */
    document.getElementById('go').disabled = false;
    const note = document.createElement('p');
    note.className = 'quiet';
    note.textContent = "That didn't send. Have another go, or tell your teacher.";
    document.getElementById('app').appendChild(note);
    return;
  }
  const state = await res.json();
  if (state.correct) state.message = state.message || 'You got it.';
  render(state);
}

start();
</script>
"""
