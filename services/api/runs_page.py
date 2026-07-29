"""The pipeline inspector (M0.8, §8).

Per-stage inputs and outputs for one run. Server-rendered, like the other
operator surfaces here, for the same reason: it is a debugging instrument for a
handful of people and a build toolchain would cost more than it renders.

Two things it is built to make obvious at a glance, because both are invisible
in a raw log:

**Which stages called a model and which did not.** A deterministic stage is not
a missing stage — the rule pre-check firing is the cheap path working, and a
template served during an outage is §4's degradation path working. Colouring
them differently is the difference between reading a run and guessing at it.

**Where a run degraded.** Fallbacks are tinted, and the header counts them, so
"every hint was a template today" is answerable without scrolling.

There is deliberately no auto-refresh. This reads an append-only record, so
there is nothing to race, and a panel that reloads while someone is reading a
prompt payload is a panel people stop using.
"""

RUNS_PAGE = """
<title>Pipeline inspector</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 14px/1.5 system-ui, sans-serif; margin: 0;
         display: grid; grid-template-columns: 320px 1fr; height: 100vh; }
  aside { border-right: 1px solid rgba(128,128,128,.3); overflow-y: auto; padding: 1rem; }
  main { overflow-y: auto; padding: 1.25rem 1.5rem; }
  h1 { font-size: 1.1rem; margin: 0 0 .25rem; }
  .sub { opacity: .7; font-size: .85rem; margin: 0 0 1rem; }
  .run { display: block; width: 100%; text-align: left; font: inherit; cursor: pointer;
         border: 1px solid rgba(128,128,128,.3); border-radius: 8px; background: transparent;
         color: inherit; padding: .55rem .7rem; margin-bottom: .4rem; }
  .run:hover { border-color: currentColor; }
  .run.active { border-color: #5b8def; background: rgba(91,141,239,.12); }
  .run code { font-size: .78rem; opacity: .8; }
  .run .meta { font-size: .75rem; opacity: .65; margin-top: .15rem; }
  .summary { display: flex; flex-wrap: wrap; gap: 1.25rem; padding: .75rem 1rem;
             border: 1px solid rgba(128,128,128,.3); border-radius: 10px; margin-bottom: 1rem; }
  .summary div { display: flex; flex-direction: column; }
  .summary .k { font-size: .72rem; text-transform: uppercase; letter-spacing: .05em; opacity: .6; }
  .summary .v { font-size: 1.05rem; font-weight: 600; }
  .stage { border: 1px solid rgba(128,128,128,.3); border-left-width: 4px;
           border-radius: 10px; margin-bottom: .85rem; overflow: hidden; }
  .stage.model { border-left-color: #5b8def; }
  .stage.deterministic { border-left-color: #9ca3af; }
  .stage.fallback { border-left-color: #d98b3a; background: rgba(217,139,58,.06); }
  .stage.failed { border-left-color: #b3452f; background: rgba(179,69,47,.07); }
  .stage > header { display: flex; align-items: baseline; gap: .6rem;
                    flex-wrap: wrap; padding: .6rem .9rem; cursor: pointer; }
  .stage h3 { font-size: .95rem; margin: 0; }
  .tag { font-size: .7rem; text-transform: uppercase; letter-spacing: .05em; opacity: .8;
         border: 1px solid currentColor; border-radius: 999px; padding: .05rem .45rem; }
  .stage .nums { margin-left: auto; font-size: .78rem; opacity: .7; }
  .io { display: grid; grid-template-columns: 1fr 1fr; gap: .75rem; padding: 0 .9rem .9rem; }
  .io section { min-width: 0; }
  .io h4 { font-size: .72rem; text-transform: uppercase; letter-spacing: .06em;
           opacity: .6; margin: 0 0 .3rem; }
  pre { margin: 0; padding: .6rem .7rem; border-radius: 8px; overflow-x: auto;
        background: rgba(128,128,128,.12); font-size: .76rem; line-height: 1.45;
        max-height: 22rem; }
  /* --- the guided tour ------------------------------------------------- */
  .tour { border: 1px solid rgba(128,128,128,.3); border-radius: 10px;
          padding: .9rem 1rem 1rem; margin-bottom: 1rem; }
  .tourbar { display: flex; align-items: center; gap: .6rem; flex-wrap: wrap;
             margin-bottom: .75rem; }
  .tourbar button { font: inherit; cursor: pointer; border-radius: 7px;
                    border: 1px solid rgba(128,128,128,.5); background: transparent;
                    color: inherit; padding: .3rem .8rem; }
  .tourbar button:hover:not(:disabled) { border-color: currentColor; }
  .tourbar button:disabled { opacity: .4; cursor: default; }
  .step { font-size: .82rem; opacity: .75; margin-left: auto; }
  .narration { font-size: .9rem; padding: .5rem .75rem; border-left: 3px solid #5b8def;
               background: rgba(91,141,239,.07); border-radius: 0 6px 6px 0;
               margin-bottom: .8rem; min-height: 2.6rem; }
  svg.canvas { width: 100%; height: auto; display: block; }
  .n-box { fill: rgba(128,128,128,.10); stroke: rgba(128,128,128,.45); stroke-width: 1.5; }
  .n-box.visited { stroke: #5b8def; fill: rgba(91,141,239,.12); }
  .n-box.current { stroke: #d98b3a; stroke-width: 3; fill: rgba(217,139,58,.18); }
  .n-box.skipped { stroke-dasharray: 4 3; opacity: .55; }
  .n-label { font-size: 11px; font-weight: 600; fill: currentColor; }
  .n-tier  { font-size: 9px; fill: currentColor; opacity: .65; }
  .edge { stroke: rgba(128,128,128,.45); stroke-width: 1.5; fill: none; }
  .edge.taken { stroke: #5b8def; stroke-width: 2.5; }
  .purpose { font-size: .85rem; line-height: 1.5; padding: .5rem .75rem;
             border-left: 3px solid rgba(128,128,128,.45); margin-bottom: .8rem;
             opacity: .85; }
  svg.canvas g { cursor: help; }
  .active-stage { margin-top: .9rem; border: 1px solid rgba(217,139,58,.5);
                  border-left: 4px solid #d98b3a; border-radius: 10px;
                  background: rgba(217,139,58,.05); }
  .active-stage > header { display: flex; align-items: baseline; gap: .6rem;
                           flex-wrap: wrap; padding: .55rem .9rem; }
  .active-stage h3 { font-size: .95rem; margin: 0; }
  .active-stage .io { padding: 0 .9rem .9rem; }
  .active-stage pre { max-height: 15rem; }
  /* --- cost and latency (§8, P1.10) ------------------------------------ */
  .econ { border: 1px solid rgba(128,128,128,.3); border-radius: 10px;
          padding: .8rem 1rem; margin-bottom: 1rem; font-size: .84rem; }
  .econ h2 { font-size: .82rem; margin: 0 0 .1rem; text-transform: uppercase;
             letter-spacing: .05em; opacity: .7; }
  .econ .headline { font-size: 1.15rem; font-weight: 600; margin-bottom: .1rem; }
  .econ .note { opacity: .6; font-size: .72rem; margin-bottom: .55rem; }
  .econ table { width: 100%; border-collapse: collapse; }
  .econ td { padding: .2rem .5rem .2rem 0; vertical-align: baseline; }
  .econ td:first-child { width: 40%; }
  .econ td.n { text-align: right; font-variant-numeric: tabular-nums;
               white-space: nowrap; width: 15%; }
  .econ thead td { font-size: .72rem; text-transform: uppercase;
                   letter-spacing: .05em; opacity: .6; }
  .econ .bar { height: 4px; border-radius: 2px; background: #5b8def; margin-top: .1rem; }
  .econ .bar.deep { background: #d98b3a; }
  .econ .unknown { opacity: .5; font-style: italic; }
  .econ details { margin-top: .5rem; }
  .econ summary { cursor: pointer; opacity: .75; }
  .timeline { margin-top: 1.5rem; }
  .timeline li { font-size: .8rem; opacity: .85; margin-bottom: .15rem; }
  .warn { color: #b3452f; font-weight: 600; }
  .empty { opacity: .65; }
  @media (max-width: 1100px) { .io { grid-template-columns: 1fr; } }
</style>

<aside>
  <h1>Runs</h1>
  <p class="sub">Newest first</p>
  <div id="runs"><p class="empty">Loading…</p></div>
</aside>

<main>
  <div id="econ"></div>
  <div id="detail"><p class="empty">Pick a run on the left.</p></div>
</main>

<script>
const principal = new URLSearchParams(location.search).get('as') || '';
const headers = {'x-principal-id': principal};
let active = null;

function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

/* Cost and latency, from the ledger (§8, P1.10).

   The bar is share of spend, so the expensive stage is obvious without reading
   a number — on this pipeline that is generation, and the leak check behind it
   is the price of the guardrail rather than an inefficiency.

   A p95 below the sample threshold prints "—" rather than a figure. A p95 over
   nine calls is the second-slowest call wearing a statistic's name, and this
   panel would otherwise be the place someone quotes it from. */
function econRow(seg, deep) {
  const p95 = seg.latency_p95_ms === null
    ? `<span class="unknown">—</span>`
    : `${seg.latency_p95_ms}ms`;
  return `<tr>
    <td>${esc(seg.label)}<div class="bar${deep ? ' deep' : ''}"
        style="width:${Math.max(seg.share_of_cost * 100, 1)}%"></div></td>
    <td class="n">${seg.calls}</td>
    <td class="n">$${seg.cost_usd.toFixed(4)}</td>
    <td class="n">${(seg.share_of_cost * 100).toFixed(0)}%</td>
    <td class="n">${seg.latency_p50_ms}ms</td>
    <td class="n">${p95}</td>
  </tr>`;
}

async function loadEconomics() {
  const box = document.getElementById('econ');
  const res = await fetch('/admin/economics', {headers});
  if (!res.ok) { box.innerHTML = ''; return; }
  const e = await res.json();
  if (!e.calls) {
    box.innerHTML = '<div class="econ"><h2>Cost &amp; latency</h2>' +
      '<div class="note">No model calls recorded yet.</div></div>';
    return;
  }
  const ratio = (e.tokens_in / Math.max(e.tokens_out, 1)).toFixed(0);
  const versions = e.by_prompt_version.map(s => econRow(s, false)).join('');
  box.innerHTML = `
    <div class="econ">
      <h2>Cost &amp; latency</h2>
      <div class="headline">$${e.total_cost_usd.toFixed(4)}</div>
      <div class="note">${e.calls} model call(s) across ${e.sessions} session(s) ·
        median $${e.cost_per_session_p50.toFixed(4)}/session,
        max $${e.cost_per_session_max.toFixed(4)} ·
        ${ratio}:1 tokens in:out</div>
      <table>
        <thead><tr><td>stage</td><td class="n">calls</td><td class="n">cost</td>
            <td class="n">share</td><td class="n">p50</td><td class="n">p95</td></tr></thead>
        ${e.by_stage.map(s => econRow(s, s.stage === 'generate_hint')).join('')}
      </table>
      <details>
        <summary>By prompt version (${e.by_prompt_version.length})</summary>
        <table>${versions}</table>
        <div class="note">§8 asks for this split because a prompt edit can
          double spend quietly. Two rows for one stage means two versions are
          live at once.</div>
      </details>
      <div class="note">p95 shown once a stage has ${e.min_calls_for_p95}+ calls.
        Deterministic stages make no model call, so they are absent here rather
        than free.</div>
    </div>`;
}

async function loadRuns() {
  const box = document.getElementById('runs');
  const res = await fetch('/admin/runs', {headers});
  if (!res.ok) {
    box.innerHTML = '<p class="empty">Could not load runs (' + res.status +
      '). Add <code>?as=&lt;admin-principal-id&gt;</code> to the URL — ' +
      'this view is admin-only.</p>';
    return;
  }
  const runs = await res.json();
  if (!runs.length) { box.innerHTML = '<p class="empty">No sessions yet.</p>'; return; }
  box.innerHTML = runs.map(r => `
    <button class="run" id="r-${r.session_id}" onclick="showRun('${r.session_id}')">
      <code>${esc(r.session_id.slice(0, 8))}</code>
      <div class="meta">${esc(r.state)} · ${r.attempt_count} attempt(s)<br>
        ${esc(new Date(r.started_at).toLocaleString())}</div>
    </button>`).join('');
}

/* ---------------------------------------------------------------------------
   The guided tour.

   The canvas is the swarm's real topology: /admin/topology derives nodes and
   edges from each agent's Command[Literal[...]] return annotation, the same one
   LangGraph validates against, so this drawing cannot disagree with the graph
   that ran. A hand-drawn diagram is accurate the day it is drawn.

   The walkthrough is driven by a real session rather than a script. That is the
   point: a scripted tour shows the happy path, and this shows what actually
   happened — including the stage that fell back, the diagnosis that came back
   `unknown`, and the template served because shadow mode is on.
   --------------------------------------------------------------------------- */
let TOPOLOGY = null;
let TOUR = null;   // { stages, cursor }

async function loadTopology() {
  const res = await fetch('/admin/topology', {headers});
  if (res.ok) TOPOLOGY = await res.json();
}

/* Left-to-right by handoff distance from the entry node, so the picture follows
   the code's order rather than a hand-placed guess at it. */
function layout(nodes) {
  const byId = Object.fromEntries(nodes.map(n => [n.id, n]));
  const col = {};
  const entry = (nodes.find(n => n.entry) || nodes[0]).id;
  // Breadth-first, and each node keeps the FIRST depth it is reached at.
  // This graph has a cycle by design -- leakcheck hands back to generate for
  // the §3.3 retry -- so a longest-path assignment never terminates: every trip
  // round the loop is a larger depth, so a "keep the deeper one" rule re-queues
  // forever. Shortest distance is well defined on a cyclic graph, and it puts
  // the retry edge where it belongs: an arrow pointing back upstream.
  const queue = [[entry, 0]];
  while (queue.length) {
    const [id, depth] = queue.shift();
    if (col[id] !== undefined) continue;
    col[id] = depth;
    for (const next of (byId[id]?.handoffs || [])) {
      if (byId[next] && col[next] === undefined) queue.push([next, depth + 1]);
    }
  }
  const columns = {};
  for (const n of nodes) {
    const c = col[n.id] ?? 0;
    (columns[c] = columns[c] || []).push(n);
  }
  const W = 132, H = 46, GAPX = 40, GAPY = 18;
  const pos = {};
  for (const [c, group] of Object.entries(columns)) {
    group.forEach((n, i) => {
      pos[n.id] = {x: Number(c) * (W + GAPX) + 10, y: i * (H + GAPY) + 14, w: W, h: H};
    });
  }
  const width = (Math.max(...Object.values(col)) + 1) * (W + GAPX) + 10;
  const height = Math.max(...Object.values(columns).map(g => g.length)) * (H + GAPY) + 20;
  return {pos, width, height};
}

/* Which NODE produced this stage run.
   Stage is not enough: shadow_agent and generate_agent both report
   `generate_hint`, so keying the canvas on stage lit the shadow node whenever
   any generation ran — including with shadow mode off, when shadow_agent is a
   pass-through that does nothing. The shadow pass marks its own completion with
   `shadow: true`, which is the only thing that tells the two apart. */
function nodeIdFor(run, topology) {
  if (run.stage === 'generate_hint') {
    return run.outputs && run.outputs.shadow === true ? 'shadow_agent' : 'generate_agent';
  }
  const match = topology.find(n => n.stage === run.stage);
  return match ? match.id : null;
}

function canvas(topology, visitedNodes, currentNode) {
  const {pos, width, height} = layout(topology);
  const parts = [];
  for (const n of topology) {
    for (const target of n.handoffs) {
      const a = pos[n.id], b = pos[target];
      if (!a || !b) continue;   // __end__ has no box; the run simply stops
      const taken = visitedNodes.has(n.id) && visitedNodes.has(target);
      const x1 = a.x + a.w, y1 = a.y + a.h / 2, x2 = b.x, y2 = b.y + b.h / 2;
      const mid = (x1 + x2) / 2;
      parts.push(`<path class="edge${taken ? ' taken' : ''}"
        d="M${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}"/>`);
    }
  }
  for (const n of topology) {
    const p = pos[n.id];
    if (!p) continue;
    const isCurrent = n.id === currentNode;
    const isVisited = visitedNodes.has(n.id);
    const cls = isCurrent ? 'current' : isVisited ? 'visited' : 'skipped';
    parts.push(`
      <g><title>${esc(n.purpose)}</title>
      <rect class="n-box ${cls}" x="${p.x}" y="${p.y}" width="${p.w}" height="${p.h}" rx="8"/>
      <text class="n-label" x="${p.x + 10}" y="${p.y + 20}">${esc(n.id.replace('_agent',''))}</text>
      <text class="n-tier" x="${p.x + 10}" y="${p.y + 35}">
        ${esc(n.tier || 'deterministic')}</text></g>`);
  }
  return `<svg class="canvas" viewBox="0 0 ${width} ${height}"
            preserveAspectRatio="xMinYMin meet">${parts.join('')}</svg>`;
}

function narrate(run) {
  if (!run) return 'Press Play, or Step, to walk the pipeline as it actually ran.';
  const how = run.used_model
    ? `called ${esc(run.model_id)} (${esc(run.prompt_version)}) — `
      + `${run.tokens_in}→${run.tokens_out} tok, ${run.latency_ms}ms, `
      + `$${run.cost_usd.toFixed(6)}`
    : 'took its deterministic path — no model call, no cost';
  const outcome = run.outcome === 'fallback'
    ? ' It <strong>fell back</strong>: ' + esc(JSON.stringify(run.outputs.reason ?? run.outputs))
    : run.outcome === 'failed' ? ' It <strong>failed</strong>.'
    : run.outcome === 'unterminated' ? ' It never recorded an ending.' : '';
  const pass = run.ordinal > 1 ? ' (pass ' + run.ordinal + ')' : '';
  return `<strong>${esc(run.stage)}</strong>${pass} ${how}.${outcome}`;
}

function renderTour() {
  if (!TOPOLOGY || !TOUR) return '';
  const {stages, cursor} = TOUR;
  if (!stages.length) {
    // Common, and not an error: a child opened a problem and never answered.
    // The phase-0 report counts these separately for the same reason — they are
    // not sessions that went wrong, they are sessions that never started.
    return `<div class="tour">
      <div class="narration">This session recorded no stage runs — the problem
      was opened and never answered, so the pipeline never ran. Pick a run with
      at least one attempt to walk through.</div>
      ${canvas(TOPOLOGY, new Set(), null)}
    </div>`;
  }
  // Node ids, not stages — see nodeIdFor. `cursor < 0` means nothing has been
  // stepped yet, so nothing is lit.
  const seen = new Set(
    (cursor < 0 ? [] : stages.slice(0, cursor + 1))
      .map(s => nodeIdFor(s, TOPOLOGY)).filter(Boolean));
  const current = cursor >= 0 ? stages[cursor] : null;
  const currentNode = current ? nodeIdFor(current, TOPOLOGY) : null;
  // The terminal nodes never produce a stage run of their own, so light them
  // from the outcome the run actually reached rather than leaving both dashed.
  if (cursor === stages.length - 1) {
    seen.add(TOUR.escalated ? 'escalate_agent' : 'record_hint_agent');
  }
  return `
    <div class="tour">
      <div class="tourbar">
        <button id="t-prev" ${cursor <= 0 ? 'disabled' : ''}>&larr; Back</button>
        <button id="t-next" ${cursor >= stages.length - 1 ? 'disabled' : ''}>Step &rarr;</button>
        <button id="t-play">Play</button>
        <button id="t-reset">Reset</button>
        <span class="step">${cursor + 1} / ${stages.length}</span>
      </div>
      <div class="narration">${narrate(current)}</div>
      ${purposeOf(currentNode)}
      ${canvas(TOPOLOGY, seen, currentNode)}
      ${current ? activeStage(current) : ''}
    </div>`;
}

/* The lit node's own in/out, inside the tour.

   It used to scroll the page to the matching card instead, which meant the
   canvas — the thing the tour is about — left the viewport on the first step,
   and Play turned into a page that scrolled itself. Bringing one stage's detail
   to the reader keeps the graph and the payload on screen together, which is
   the comparison the walkthrough exists to make. The full list stays below,
   unscrolled, for anyone who wants to browse rather than be walked. */
/* What the lit node is *for*, as opposed to what it just did. The narration
   above says "leak_check called haiku, 1200ms, $0.0006"; that is the run. This
   is the reason the node exists at all, which is the part a reader meeting the
   pipeline for the first time actually needs. Hover any box for the same text. */
function purposeOf(nodeId) {
  if (!nodeId || !TOPOLOGY) return '';
  const node = TOPOLOGY.find(n => n.id === nodeId);
  if (!node) return '';
  return `<div class="purpose"><strong>${esc(node.id.replace('_agent',''))}</strong> — `
       + `${esc(node.purpose)}</div>`;
}

function activeStage(run) {
  const badge = run.used_model
    ? `<span class="tag">${esc(run.model_id)}</span>`
      + `<span class="tag">${esc(run.prompt_version)}</span>`
    : '<span class="tag">deterministic — no model call</span>';
  return `
    <div class="active-stage">
      <header>
        <h3>${esc(run.stage)}${run.ordinal > 1 ? ' #' + run.ordinal : ''}</h3>
        <span class="tag">${esc(run.outcome)}</span>
        ${badge}
      </header>
      <div class="io">
        <section><h4>in</h4>${payload(run.inputs)}</section>
        <section><h4>out</h4>${payload(run.outputs)}</section>
      </div>
    </div>`;
}

function wireTour() {
  const redraw = () => {
    const host = document.getElementById('tour-host');
    if (host) host.innerHTML = renderTour();
    wireTour();
    // Deliberately no scrolling. The tour renders the lit node's detail in
    // place, so moving the page would only take the canvas away from the reader.
    // The stage list below is still marked, for anyone who scrolls there by
    // choice rather than by being dragged.
    document.querySelectorAll('.stage').forEach((el, i) =>
      el.style.outline = (TOUR && i === TOUR.cursor) ? '2px solid #d98b3a' : 'none');
  };
  const prev = document.getElementById('t-prev');
  const next = document.getElementById('t-next');
  const play = document.getElementById('t-play');
  const reset = document.getElementById('t-reset');
  if (prev) prev.onclick = () => { TOUR.cursor = Math.max(0, TOUR.cursor - 1); redraw(); };
  if (next) next.onclick = () => {
    TOUR.cursor = Math.min(TOUR.stages.length - 1, TOUR.cursor + 1); redraw();
  };
  if (reset) reset.onclick = () => { TOUR.cursor = -1; redraw(); };
  if (play) play.onclick = () => {
    TOUR.cursor = -1; redraw();
    const tick = () => {
      if (!TOUR || TOUR.cursor >= TOUR.stages.length - 1) return;
      TOUR.cursor += 1; redraw();
      setTimeout(tick, 900);
    };
    setTimeout(tick, 400);
  };
}

/* Rendered as sent and received, not prettified into something friendlier.
   The point of this panel is to show what actually crossed the boundary. */
function payload(obj) {
  const keys = Object.keys(obj || {});
  if (!keys.length) return '<pre class="empty">nothing recorded</pre>';
  return '<pre>' + esc(JSON.stringify(obj, null, 2)) + '</pre>';
}

function stageCard(s, index) {
  const cls = s.outcome === 'failed' ? 'failed'
            : s.outcome === 'fallback' ? 'fallback'
            : s.used_model ? 'model' : 'deterministic';
  const badge = s.used_model
    ? `<span class="tag">${esc(s.model_id)}</span><span class="tag">${esc(s.prompt_version)}</span>`
    : '<span class="tag">deterministic — no model call</span>';
  const nums = s.used_model
    ? `${s.tokens_in}→${s.tokens_out} tok · ${s.latency_ms}ms · $${s.cost_usd.toFixed(6)}`
    : (s.duration_ms !== null ? `${s.duration_ms}ms` : '');
  return `
    <article class="stage ${cls}" id="stage-${index}">
      <header onclick="this.parentElement.querySelector('.io').classList.toggle('hidden')">
        <h3>${esc(s.stage)}${s.ordinal > 1 ? ' #' + s.ordinal : ''}</h3>
        <span class="tag">${esc(s.outcome)}</span>
        ${badge}
        <span class="nums">${esc(nums)}</span>
      </header>
      <div class="io">
        <section><h4>in</h4>${payload(s.inputs)}</section>
        <section><h4>out</h4>${payload(s.outputs)}</section>
      </div>
    </article>`;
}

async function showRun(id) {
  if (active) document.getElementById('r-' + active)?.classList.remove('active');
  active = id;
  document.getElementById('r-' + id)?.classList.add('active');

  const box = document.getElementById('detail');
  const res = await fetch('/admin/runs/' + id, {headers});
  if (!res.ok) {
    box.innerHTML = '<p class="empty">Could not load that run (' + res.status + ').</p>';
    return;
  }
  const t = await res.json();

  const gaps = t.gaps.length
    ? `<p class="warn">INCOMPLETE — missing sequence number(s): ${t.gaps.join(', ')}.
       This trace is a plausible story, not a verified account.</p>`
    : '';
  const degraded = t.degraded_stages.length
    ? t.degraded_stages.join(', ') : 'none';
  // A fresh walkthrough per run; the cursor starts before the first step so the
  // canvas opens showing the topology rather than a stage already selected.
  TOUR = {stages: t.stages, cursor: -1,
          escalated: t.timeline.some(e => e.event_type === 'escalated')};

  box.innerHTML = `
    <h1>Run <code>${esc(id.slice(0, 8))}</code></h1>
    ${gaps}
    <div class="summary">
      <div><span class="k">stage runs</span><span class="v">${t.stages.length}</span></div>
      <div><span class="k">model calls</span><span class="v">${t.model_calls}</span></div>
      <div><span class="k">deterministic</span>
           <span class="v">${t.deterministic_stages}</span></div>
      <div><span class="k">degraded</span><span class="v">${esc(degraded)}</span></div>
      <div><span class="k">cost</span><span class="v">$${t.total_cost_usd.toFixed(6)}</span></div>
    </div>
    <div id="tour-host"></div>
    ${t.stages.map(stageCard).join('')}
    <div class="timeline">
      <h4>Session events (no stage)</h4>
      <ul>${t.timeline.map(e =>
        `<li><code>${e.sequence}</code> ${esc(e.event_type)}
         ${Object.keys(e.detail).length ? esc(JSON.stringify(e.detail)) : ''}</li>`).join('')}</ul>
    </div>`;

  document.getElementById('tour-host').innerHTML = renderTour();
  wireTour();
}

loadTopology().then(loadRuns).then(loadEconomics);
</script>
"""
