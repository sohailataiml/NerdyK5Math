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

/* Rendered as sent and received, not prettified into something friendlier.
   The point of this panel is to show what actually crossed the boundary. */
function payload(obj) {
  const keys = Object.keys(obj || {});
  if (!keys.length) return '<pre class="empty">nothing recorded</pre>';
  return '<pre>' + esc(JSON.stringify(obj, null, 2)) + '</pre>';
}

function stageCard(s) {
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
    <article class="stage ${cls}">
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
    ${t.stages.map(stageCard).join('')}
    <div class="timeline">
      <h4>Session events (no stage)</h4>
      <ul>${t.timeline.map(e =>
        `<li><code>${e.sequence}</code> ${esc(e.event_type)}
         ${Object.keys(e.detail).length ? esc(JSON.stringify(e.detail)) : ''}</li>`).join('')}</ul>
    </div>`;
}

loadRuns();
</script>
"""
