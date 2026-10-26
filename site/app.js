/* ts-version-control console.
 *
 * A static page that drives the pipeline through the GitHub REST API from the browser:
 * dispatch ts-migration.yml / ts-redeploy.yml, follow the run's jobs and steps, review and
 * approve the promote PR, read the report Stage 11 commits to ts-dev, and build an org config.
 * No build step and no backend. The token stays in this browser (sessionStorage by default).
 */
'use strict';

const BRANCH = 'ts-prod';
const WF_MIGRATE = 'ts-migration.yml';
const WF_REDEPLOY = 'ts-redeploy.yml';
const FALLBACK_REPO = 'mbatovskyts/ThoughtSpot-Version-Control';

// The pipeline, as the UI explains it. `num` matches the "Stage <num> — ..." step names in
// org-migration.yml; `dry` says what a dry run does with the stage.
const PHASES = [
  { key: 'extract', job: 'Extract', label: 'Extract', sub: 'dev → ts-dev', stages: [
    ['1', 'Validate config'], ['2', 'Authenticate'],
    ['3', 'Discover tagged objects and dependencies'], ['4', 'Export TML'],
    ['4b', 'Export model memory'], ['4c', 'Export NL instructions'],
    ['5', 'Sanitize (strip GUIDs, find variables)'], ['6', 'Commit to ts-dev', { dry: 'writes repo' }],
  ] },
  { key: 'promote', job: 'Promote', label: 'Review', sub: 'PR → ts-prod', stages: [
    ['7', 'Open the promote PR'], ['7b', 'Wait for approval and merge', { dry: 'skipped' }],
  ] },
  { key: 'import', job: 'Import', label: 'Import', sub: 'ts-prod → prod', stages: [
    ['8', 'Variables, connections, validate-only import', { dry: 'writes nothing' }],
    ['8b', 'Snapshot what the target holds now', { dry: 'skipped' }],
    ['9', 'Import TML in dependency order', { dry: 'skipped' }],
    ['9b', 'Import model memory', { dry: 'writes nothing' }],
    ['9c', 'Set model NL instructions', { dry: 'writes nothing' }],
  ] },
  { key: 'untag', job: 'Untag source', label: 'Untag', sub: 'clean dev', stages: [
    ['10', 'Post-check, remove tag from successes', { dry: 'skipped' }],
  ] },
  { key: 'report', job: 'Report', label: 'Report', sub: 'summary', stages: [
    ['11', 'Generate reports'],
  ] },
  { key: 'release', job: 'Tag restore point', label: 'Restore point', sub: 'rollback tag', stages: [
    ['', 'Tag the imported ts-prod commit', { dry: 'skipped', step: 'Tag the imported ts-prod commit' }],
  ] },
];

// ------------------------------------------------------------------------------------------
// Small helpers
// ------------------------------------------------------------------------------------------
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
class Raw { constructor(v) { this.v = v; } }
const raw = v => new Raw(v);
const show = v => v == null || v === false ? '' : v instanceof Raw ? v.v : Array.isArray(v) ? v.map(show).join('') : esc(v);
// Tagged template: every interpolated value is escaped unless it is already Raw.
const h = (strings, ...vals) => raw(strings.reduce((out, s, i) => out + show(vals[i - 1]) + s));

const store = {
  get(k) { try { return sessionStorage.getItem(k) ?? localStorage.getItem(k); } catch { return null; } },
  set(k, v, persist = true) { try { (persist ? localStorage : sessionStorage).setItem(k, v); } catch { /* storage blocked */ } },
  del(k) { try { sessionStorage.removeItem(k); localStorage.removeItem(k); } catch { /* storage blocked */ } },
};

function timeAgo(iso) {
  if (!iso) return '';
  const s = Math.max(0, (Date.now() - new Date(iso)) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}
function dur(a, b) {
  if (!a) return '';
  const s = Math.max(0, Math.round(((b ? new Date(b) : Date.now()) - new Date(a)) / 1000));
  return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${Math.floor(s / 3600)}h ${Math.floor(s % 3600 / 60)}m`;
}
const host = u => { try { return new URL(u).host; } catch { return u || ''; } };

const ICON = {
  done: '<svg viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 5.2 4.2 7.4 8 3"/></svg>',
  failed: '<svg viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 3l4 4M7 3 3 7"/></svg>',
  cancelled: '<svg viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 5h4"/></svg>',
};
const dot = st => h`<span class="st ${st}" aria-label="${st}">${raw(ICON[st] || '')}</span>`;
const STATE_LABEL = { done: 'done', failed: 'failed', running: 'running', waiting: 'waiting', queued: 'queued', pending: 'not started', skipped: 'skipped', cancelled: 'cancelled' };
const pillFor = st => h`<span class="pill ${st === 'done' ? 'ok' : st === 'failed' ? 'bad' : st === 'running' || st === 'waiting' || st === 'queued' ? 'run' : ''}">${STATE_LABEL[st] || st}</span>`;

let toastTimer;
function toast(msg) {
  let el = $('.toast');
  if (!el) { el = document.createElement('div'); el.className = 'toast'; el.setAttribute('role', 'status'); document.body.append(el); }
  el.textContent = msg;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.remove(), 3200);
}
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast('Copied'); } catch { toast('Copy failed: select and copy it by hand'); }
}

// ------------------------------------------------------------------------------------------
// State and GitHub API
// ------------------------------------------------------------------------------------------
function defaultRepo() {
  // Served from GitHub Pages as <owner>.github.io/<repo>/: that repo runs the pipeline.
  const m = location.hostname.match(/^([^.]+)\.github\.io$/);
  const seg = location.pathname.split('/').filter(Boolean)[0];
  return m && seg ? `${m[1]}/${seg}` : FALLBACK_REPO;
}

const state = {
  repo: store.get('tsvc.repo') || defaultRepo(),
  token: store.get('tsvc.token') || '',
  user: null,
  orgs: null,
};

class ApiError extends Error {
  constructor(msg, status) { super(msg); this.status = status; }
}

async function api(url, { method = 'GET', body, accept } = {}) {
  const headers = { Accept: accept || 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28' };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (body) headers['Content-Type'] = 'application/json';
  const res = await fetch(`https://api.github.com${url}`, { method, headers, body: body ? JSON.stringify(body) : undefined, cache: 'no-store' });
  if (!res.ok) {
    let msg = '';
    try { msg = (await res.json()).message || ''; } catch { /* not JSON */ }
    if ((res.status === 403 || res.status === 429) && /rate limit/i.test(msg)) {
      msg = state.token ? 'GitHub rate limit reached. Try again in a few minutes.' : 'GitHub allows 60 requests an hour without a token. Connect a token to keep going.';
    }
    throw new ApiError(msg || `HTTP ${res.status}`, res.status);
  }
  if (res.status === 204) return null;
  // Raw file content arrives as "application/vnd.github.raw+json" whatever the file is.
  if (accept?.includes('.raw')) return res.text();
  return (res.headers.get('content-type') || '').includes('json') ? res.json() : res.text();
}
const gh = (path, opts) => api(`/repos/${state.repo}${path}`, opts);
const ghRaw = (path, ref) => gh(`/contents/${path}?ref=${encodeURIComponent(ref)}`, { accept: 'application/vnd.github.raw+json' });

// Remembers what this browser launched, because the runs API does not return dispatch inputs.
const meta = {
  all() { try { return JSON.parse(store.get('tsvc.runs') || '{}'); } catch { return {}; } },
  get(id) { return this.all()[id]; },
  set(id, v) { const all = this.all(); all[id] = { ...all[id], ...v }; store.set('tsvc.runs', JSON.stringify(all)); },
};

async function loadOrgs(force = false) {
  if (state.orgs && !force) return state.orgs;
  const list = await gh(`/contents/config/orgs?ref=${BRANCH}`);
  const files = list.filter(f => f.type === 'file' && f.name.endsWith('.yml'));
  const orgs = await Promise.all(files.map(async f => {
    const cfg = window.jsyaml.load(await ghRaw(f.path, BRANCH)) || {};
    return { ...cfg, _file: f.path };
  }));
  // Same rule as the workflow's matrix: the filename stem must equal org_key.
  state.orgs = orgs.filter(o => o.org_key && o._file.endsWith(`/${o.org_key}.yml`) && o.org_key !== 'example');
  return state.orgs;
}

// Job names look like "Migrate org <org> / Extract (<org>)"; a skipped job may keep the
// unexpanded "${{ inputs.org_key }}", so the org comes from the prefix.
function parseJobs(jobs) {
  const orgs = {};
  let setup = null;
  for (const j of jobs) {
    const m = j.name.match(/^Migrate org (.+?) \/ (.+?) \(/);
    if (!m) { if (/org matrix/i.test(j.name)) setup = j; continue; }
    const phase = PHASES.find(p => p.job === m[2]);
    if (phase) (orgs[m[1]] ||= {})[phase.key] = j;
  }
  return { orgs, setup };
}

const CONCLUSION = { success: 'done', failure: 'failed', cancelled: 'cancelled', skipped: 'skipped', timed_out: 'failed', neutral: 'done', action_required: 'waiting', startup_failure: 'failed' };
function jobState(job, runDone) {
  if (!job) return runDone ? 'skipped' : 'pending';
  if (job.status === 'completed') return CONCLUSION[job.conclusion] || 'done';
  if (job.status === 'in_progress') return 'running';
  if (job.status === 'waiting') return 'waiting';
  return 'queued';
}
function stepState(step, job, runDone) {
  if (!step) {
    if (job && job.status === 'completed') return 'skipped';
    return runDone ? 'skipped' : 'pending';
  }
  if (step.status === 'completed') return CONCLUSION[step.conclusion] || 'done';
  if (step.status === 'in_progress') return 'running';
  return 'pending';
}
function findStep(job, num, opts = {}) {
  if (!job?.steps) return null;
  const matches = opts.step
    ? job.steps.filter(s => s.name === opts.step)
    : job.steps.filter(s => new RegExp(`^Stage ${num} [—-]`).test(s.name));
  return matches.find(s => s.conclusion !== 'skipped') || matches[0] || null;
}
function isDryFromJobs(org) {
  const imp = org?.import;
  if (!imp?.steps) return null;
  const notice = imp.steps.find(s => /Dry-run skip notice/.test(s.name));
  if (notice?.conclusion === 'success') return true;
  const live = imp.steps.find(s => /^Stage 9 [—-] Import to production/.test(s.name));
  if (live?.conclusion === 'success') return false;
  return null;
}
const runKind = run => (run.path || '').includes(WF_REDEPLOY) ? 'redeploy' : 'migration';
function runState(run) {
  if (run.status === 'completed') return CONCLUSION[run.conclusion] || 'done';
  if (run.status === 'waiting') return 'waiting';
  if (run.status === 'in_progress') return 'running';
  return 'queued';
}

// ------------------------------------------------------------------------------------------
// Header: connection, theme, menu
// ------------------------------------------------------------------------------------------
function refreshHeader() {
  const btn = $('#conn-btn');
  btn.classList.toggle('on', !!state.token);
  $('#conn-label').textContent = state.token ? (state.user ? state.user.login : 'connected') : 'connect';
  $('#repo-link').href = `https://github.com/${state.repo}`;
  $('#repo-link').textContent = state.repo;
  $('#runbook-link').href = `https://github.com/${state.repo}/blob/${BRANCH}/docs/runbook.md`;
  const route = location.hash.split('?')[0] || '#/';
  $$('.nav a').forEach(a => {
    const href = a.getAttribute('href');
    if (route === href || route.startsWith(`${href}/`)) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
}

function openConnect() {
  const dlg = $('#connect');
  $('#repo-input').value = state.repo;
  $('#token-input').value = '';
  $('#token-input').placeholder = state.token ? 'saved (leave blank to keep)' : 'github_pat_…';
  $('#remember-input').checked = !!(() => { try { return localStorage.getItem('tsvc.token'); } catch { return null; } })();
  $('#connect-error').hidden = true;
  dlg.showModal();
}

$('#connect-form').addEventListener('submit', async e => {
  if (e.submitter?.value !== 'save') return;
  e.preventDefault();
  const repo = $('#repo-input').value.trim().replace(/^https:\/\/github\.com\//, '').replace(/\/$/, '');
  const token = $('#token-input').value.trim() || state.token;
  const err = $('#connect-error');
  const saveBtn = $('#connect-save');
  if (!/^[\w.-]+\/[\w.-]+$/.test(repo)) { err.textContent = 'Use the form owner/name.'; err.hidden = false; return; }
  saveBtn.disabled = true;
  const prev = { repo: state.repo, token: state.token };
  Object.assign(state, { repo, token });
  try {
    await gh('');
    state.user = token ? await api('/user') : null;
    const persist = $('#remember-input').checked;
    store.del('tsvc.token');
    if (token) store.set('tsvc.token', token, persist);
    store.set('tsvc.repo', repo);
    if (repo !== prev.repo) state.orgs = null;
    $('#connect').close();
    toast(token ? `Connected as ${state.user.login}` : `Using ${repo} without a token`);
    refreshHeader();
    route();
  } catch (ex) {
    Object.assign(state, prev);
    err.textContent = ex.status === 401 ? 'GitHub rejected that token.' : ex.status === 404 ? `Can't see ${repo}. Check the name, and that the token can read it.` : ex.message;
    err.hidden = false;
  } finally {
    saveBtn.disabled = false;
  }
});
$('#disconnect').addEventListener('click', () => {
  store.del('tsvc.token');
  state.token = ''; state.user = null;
  $('#connect').close();
  refreshHeader(); route();
  toast('Token forgotten');
});
$('#conn-btn').addEventListener('click', openConnect);
$('#theme-btn').addEventListener('click', () => {
  const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  document.documentElement.dataset.theme = next;
  store.set('tsvc.theme', next);
});
$('#menu-btn').addEventListener('click', () => {
  const open = $('#nav').classList.toggle('open');
  $('#menu-btn').setAttribute('aria-expanded', open);
});

// ------------------------------------------------------------------------------------------
// Router
// ------------------------------------------------------------------------------------------
const app = $('#app');
let cleanup = null;
function mount(view) {
  app.innerHTML = view.v;
}
function bind(actions, root = app) {
  $$('[data-act]', root).forEach(el => {
    const fn = actions[el.dataset.act];
    if (fn) el.addEventListener(el.matches('input, select, textarea') ? 'input' : 'click', ev => fn(el, ev));
  });
}
function errorBox(e) {
  const needsToken = !state.token && (e.status === 403 || e.status === 429 || e.status === 401);
  return h`<div class="notice bad"><div><b>Couldn't reach GitHub.</b> ${e.message}
    ${needsToken ? h` <button class="btn sm" data-act="connect">Connect a token</button>` : ''}</div></div>`;
}

function route() {
  if (cleanup) { cleanup(); cleanup = null; }
  $('#nav').classList.remove('open');
  const [path, qs] = (location.hash.slice(1) || '/').split('?');
  const q = new URLSearchParams(qs || '');
  refreshHeader();
  window.scrollTo(0, 0);
  let m;
  if (path === '/' || path === '') return viewHome();
  if (path === '/run') return viewWizard(q);
  if (path === '/runs') return viewRuns();
  if ((m = path.match(/^\/runs\/(\d+)$/))) return viewRun(new RealSource(Number(m[1])));
  if (path === '/demo') return viewRun(new DemoSource());
  if (path === '/rollback') return viewRollback(q);
  if (path === '/orgs/new') return viewBuilder();
  mount(h`<div class="empty">Nothing here. <a href="#/">Go home</a>.</div>`);
}
window.addEventListener('hashchange', route);
document.addEventListener('click', e => { if (e.target.closest('[data-act="connect"]')) openConnect(); });

// ------------------------------------------------------------------------------------------
// Home
// ------------------------------------------------------------------------------------------
function runRow(run, org) {
  const st = runState(run);
  const m = meta.get(run.id) || {};
  const kind = runKind(run);
  const orgName = m.org || org || '';
  const dry = m.dry;
  return h`<a class="item" href="#/runs/${run.id}">
    ${dot(st)}
    <div class="main">
      <div class="t">${kind === 'redeploy' ? 'Redeploy' : 'Migration'}${orgName ? h` · <span class="mono">${orgName}</span>` : ''}</div>
      <div class="s">#${run.run_number} · ${run.actor?.login || ''} · ${timeAgo(run.created_at)}${run.status === 'completed' ? ` · ${dur(run.run_started_at || run.created_at, run.updated_at)}` : ''}</div>
    </div>
    ${dry === true ? h`<span class="pill dry">dry run</span>` : dry === false ? h`<span class="pill">live</span>` : ''}
    ${pillFor(st)}
  </a>`;
}

async function fetchRuns(limit = 15) {
  const [mig, red] = await Promise.all([
    gh(`/actions/workflows/${WF_MIGRATE}/runs?per_page=${limit}`),
    gh(`/actions/workflows/${WF_REDEPLOY}/runs?per_page=${Math.ceil(limit / 2)}`).catch(() => ({ workflow_runs: [] })),
  ]);
  return [...mig.workflow_runs, ...red.workflow_runs].sort((a, b) => new Date(b.created_at) - new Date(a.created_at)).slice(0, limit);
}
// Org names for runs this browser didn't launch, read from job names. Only with a token:
// it costs one request per run.
async function orgsForRuns(runs) {
  const out = {};
  if (!state.token) return out;
  await Promise.all(runs.slice(0, 10).map(async r => {
    if (meta.get(r.id)?.org) return;
    try {
      const { jobs } = await gh(`/actions/runs/${r.id}/jobs?per_page=50`);
      if (runKind(r) === 'redeploy') {
        const m = jobs[0]?.name.match(/^Redeploy (\S+) @/);
        if (m) out[r.id] = m[1];
      } else {
        const names = Object.keys(parseJobs(jobs).orgs);
        if (names.length) out[r.id] = names.join(', ');
      }
    } catch { /* leave blank */ }
  }));
  return out;
}

function viewHome() {
  mount(h`
    <section class="hero">
      <div class="eyebrow">ThoughtSpot · dev → prod</div>
      <h1 style="margin-top:14px">Ship analytics like code.<br><em>Reviewed, then imported.</em></h1>
      <p class="lede">Tag content in your dev org. The pipeline exports it as TML, opens a pull request into prod, and imports only what you approve, with a restore point for every run.</p>
      <div class="row">
        <a class="btn primary" href="#/run">Start a migration</a>
        <a class="btn" href="#/demo">Watch a demo run</a>
      </div>
    </section>

    <section class="section">
      <header><h2>Four steps</h2><span class="muted small">You do the bold parts. The pipeline does the rest.</span></header>
      <div class="grid g4">
        <div class="card step-card"><div class="n">01</div><h3>Tag in dev</h3><p><b>Add the org's migration tag</b> to liveboards, answers or models. Dependencies come along.</p></div>
        <div class="card step-card"><div class="n">02</div><h3>Start a run</h3><p><b>Pick an org and a mode.</b> A dry run validates against prod without importing.</p></div>
        <div class="card step-card"><div class="n">03</div><h3>Review the PR</h3><p><b>Approve the promote PR.</b> Nothing reaches prod until it merges, and your edits are what ship.</p></div>
        <div class="card step-card"><div class="n">04</div><h3>It lands in prod</h3><p>Validated, imported in dependency order, untagged in dev, reported and tagged for rollback.</p></div>
      </div>
    </section>

    <section class="section grid g2">
      <div>
        <header class="row" style="margin-bottom:12px"><h2>Recent runs</h2><span class="spacer"></span><a class="small muted" href="#/runs">All runs →</a></header>
        <div class="list" id="home-runs"><div class="empty"><span class="spin"></span></div></div>
      </div>
      <div>
        <header class="row" style="margin-bottom:12px"><h2>Orgs</h2><span class="spacer"></span><a class="small muted" href="#/orgs/new">Add org →</a></header>
        <div class="list" id="home-orgs"><div class="empty"><span class="spin"></span></div></div>
      </div>
    </section>

    <section class="section">
      <header><h2>Guardrails</h2><span class="muted small">Enforced in code, not by convention.</span></header>
      <div class="grid g2">
        <div class="rule"><span class="mark">1</span><div><h3>Nothing is deleted in prod</h3><p>Re-runs update objects in place, matched by <code>obj_id</code>. No duplicates.</p></div></div>
        <div class="rule"><span class="mark">2</span><div><h3>Dev is read-only</h3><p>The only change in the source is removing the tag from objects that migrated.</p></div></div>
        <div class="rule"><span class="mark">3</span><div><h3>It fails closed</h3><p>An object that fails validation is held back with what depends on it, and keeps its tag for the next run.</p></div></div>
        <div class="rule"><span class="mark">4</span><div><h3>Every live run can be undone</h3><p>Prod is snapshotted before import, and the merged commit is tagged as a restore point.</p></div></div>
      </div>
    </section>`);

  fetchRuns(5).then(async runs => {
    const el = $('#home-runs'); if (!el) return;
    el.innerHTML = runs.length ? show(runs.map(r => runRow(r))) : '<div class="empty">No runs yet.</div>';
    const orgs = await orgsForRuns(runs);
    if ($('#home-runs') && Object.keys(orgs).length) el.innerHTML = show(runs.map(r => runRow(r, orgs[r.id])));
  }).catch(e => { const el = $('#home-runs'); if (el) { el.innerHTML = show(errorBox(e)); el.classList.remove('list'); } });

  loadOrgs().then(orgs => {
    const el = $('#home-orgs'); if (!el) return;
    el.innerHTML = orgs.length ? show(orgs.map(o => h`<a class="item" href="#/run?org=${o.org_key}">
      <div class="main"><div class="t">${o.display_name || o.org_key}</div>
      <div class="s mono">${host(o.source?.base_url)} · ${o.source?.org_id} → ${host(o.target?.base_url)} · ${o.target?.org_id}</div></div>
      <span class="pill">${o.org_key}</span></a>`)) : '<div class="empty">No orgs configured. <a href="#/orgs/new">Add one</a>.</div>';
  }).catch(e => { const el = $('#home-orgs'); if (el) { el.innerHTML = show(errorBox(e)); el.classList.remove('list'); } });
}

// ------------------------------------------------------------------------------------------
// New run wizard
// ------------------------------------------------------------------------------------------
function viewWizard(q) {
  const w = { step: 1, org: q.get('org') || null, tagged: false, dry: true, busy: false, error: null };
  let orgs = null, loadErr = null;
  const STEPS = ['Choose org', 'Tag content', 'Choose mode', 'Launch'];

  const orgObj = () => orgs?.find(o => o.org_key === w.org);
  const canGo = n => n === 1 || (!!orgs && !!w.org && (n < 3 || w.org === 'all' || w.tagged));

  function stepOrg() {
    if (loadErr) return errorBox(loadErr);
    if (!orgs) return h`<div class="empty"><span class="spin"></span></div>`;
    if (!orgs.length) return h`<div class="card"><p>No orgs are configured in <code>config/orgs/</code> on <code>${BRANCH}</code>.</p><div class="row" style="margin-top:12px"><a class="btn primary" href="#/orgs/new">Add an org</a></div></div>`;
    const cards = orgs.map(o => {
      const same = o.source?.base_url === o.target?.base_url;
      return h`<button class="card choice" data-act="pick" data-org="${o.org_key}" aria-pressed="${w.org === o.org_key}">
        <div class="row"><h3>${o.display_name || o.org_key}</h3><span class="spacer"></span><span class="pill">${o.org_key}</span></div>
        <div class="flow" style="margin-top:10px"><b>dev</b> ${host(o.source?.base_url)} · org ${o.source?.org_id} <span>→</span> <b>prod</b> ${same ? 'same cluster' : host(o.target?.base_url)} · org ${o.target?.org_id}</div>
        <div class="small muted" style="margin-top:8px">Tag <code>${o.migration_tag}</code> · dependencies: ${o.dependency_mode || 'include'}</div>
      </button>`;
    });
    if (orgs.length > 1) cards.push(h`<button class="card choice" data-act="pick" data-org="all" aria-pressed="${w.org === 'all'}"><h3>All orgs</h3><p class="small muted" style="margin-top:6px">One pipeline per org, side by side. Each opens its own PR.</p></button>`);
    return h`<div class="stack"><div><h2>Which org are you shipping?</h2><p class="muted" style="margin-top:6px">Each org moves content from one dev org to one prod org.</p></div><div class="grid g2">${cards}</div></div>`;
  }

  function stepTag() {
    const o = orgObj();
    if (w.org === 'all') {
      return h`<div class="stack"><h2>Tag content in each org</h2><p class="muted">For every org, add its migration tag in the source org:</p>
        <div class="list">${orgs.map(o => h`<div class="item"><div class="main"><div class="t">${o.display_name || o.org_key}</div><div class="s">${host(o.source?.base_url)} · org ${o.source?.org_id}</div></div><button class="btn sm" data-act="copy" data-text="${o.migration_tag}">${o.migration_tag}</button></div>`)}</div></div>`;
    }
    const deps = {
      include: 'Anything a tagged object is built on (its model, the model\'s tables) is pulled in automatically.',
      skip_if_exists: 'Dependencies are pulled in, except those already in prod.',
      require_tagged: 'Nothing is pulled in. Tag every dependency too, or the object is excluded.',
    }[o.dependency_mode || 'include'];
    return h`<div class="stack">
      <div><h2>Tag what you want to ship</h2><p class="muted" style="margin-top:6px">In ThoughtSpot, open the <b>dev</b> org and add this tag to each object. Untagged objects are ignored.</p></div>
      <div class="card"><div class="row"><span class="copy"><code>${o.migration_tag}</code></span><button class="btn sm" data-act="copy" data-text="${o.migration_tag}">Copy</button><span class="spacer"></span>
        <a class="btn sm ghost" href="${o.source?.base_url}" target="_blank" rel="noopener">Open dev ThoughtSpot ↗</a></div>
        <p class="small muted" style="margin-top:12px">${deps}${o.dependency_exclude?.length ? ` Never pulled in: ${o.dependency_exclude.join(', ')}.` : ''}</p></div>
      <div class="card tight">
        <label class="check"><input type="checkbox" data-act="tagged" ${w.tagged ? 'checked' : ''}><div><b>I've tagged the objects</b><p>Liveboards, answers, models or tables in org ${o.source?.org_id}.</p></div></label>
        <div class="check" style="cursor:default"><span class="pill">note</span><div><p>Every object needs an <code>obj_id</code>. Objects without one are excluded and listed in the report. A dry run will show you exactly what gets picked up.</p></div></div>
      </div></div>`;
  }

  function stepMode() {
    const rows = [
      ['Find, export and commit to ts-dev', true, true],
      ['Open a promote PR', 'titled [DRY RUN], no auto-merge', 'auto-merges once approved'],
      ['Check variables and connections, validate against prod', 'writes nothing', true],
      ['Import TML, memory, NL instructions', false, true],
      ['Remove the tag in dev', false, true],
      ['Tag a restore point', false, true],
    ];
    const cell = v => v === true ? h`<span class="yes">✓</span>` : v === false ? h`<span class="no">–</span>` : h`<span class="muted">${v}</span>`;
    return h`<div class="stack">
      <div><h2>Dry run or live?</h2><p class="muted" style="margin-top:6px">Start with a dry run when the content is new. It surfaces validation errors without touching prod.</p></div>
      <div class="grid g2">
        <button class="card choice" data-act="mode" data-dry="1" aria-pressed="${w.dry}"><div class="row"><h3>Dry run</h3><span class="pill ok">recommended first</span></div><p class="small muted" style="margin-top:8px">Validates against prod and reports what it would do. Imports nothing.</p></button>
        <button class="card choice" data-act="mode" data-dry="0" aria-pressed="${!w.dry}"><h3>Live run</h3><p class="small muted" style="margin-top:8px">Imports into prod after you approve the PR. Snapshots prod first.</p></button>
      </div>
      <div class="card tight tbl-wrap"><table class="cmp"><thead><tr><th></th><th>Dry run</th><th>Live</th></tr></thead>
        <tbody>${rows.map(([a, d, l]) => h`<tr><td>${a}</td><td>${cell(d)}</td><td>${cell(l)}</td></tr>`)}</tbody></table></div>
      ${w.dry ? h`<p class="small muted">A dry run still commits to <code>ts-dev</code> and opens a PR. Close that PR when you're done; its branch is deleted automatically.</p>` : ''}
    </div>`;
  }

  function stepLaunch() {
    const o = orgObj();
    return h`<div class="stack">
      <div><h2>Ready</h2><p class="muted" style="margin-top:6px">This starts <code>${WF_MIGRATE}</code> on <code>${BRANCH}</code> in ${state.repo}.</p></div>
      <div class="card"><dl class="kv">
        <dt>Org</dt><dd>${w.org === 'all' ? 'All orgs' : h`${o.display_name || o.org_key} <span class="pill">${o.org_key}</span>`}</dd>
        ${o ? h`<dt>From</dt><dd class="mono small">${host(o.source?.base_url)} · org ${o.source?.org_id}</dd><dt>To</dt><dd class="mono small">${host(o.target?.base_url)} · org ${o.target?.org_id}</dd><dt>Tag</dt><dd><code>${o.migration_tag}</code></dd>` : ''}
        <dt>Mode</dt><dd>${w.dry ? h`<span class="pill dry">dry run</span>` : h`<span class="pill">live</span>`}</dd>
      </dl></div>
      ${!w.dry ? h`<div class="notice warn"><div>You'll be asked to <b>approve the promote PR</b> before anything is imported. The run waits up to 5.5 hours for it.</div></div>` : ''}
      ${w.error ? h`<div class="notice bad"><div>${w.error}</div></div>` : ''}
      <div class="row">
        ${state.token
          ? h`<button class="btn primary" data-act="launch" ${w.busy ? 'disabled' : ''}>${w.busy ? h`<span class="spin"></span> ${w.busy}` : w.dry ? 'Start dry run' : 'Start live run'}</button>`
          : h`<button class="btn primary" data-act="connect">Connect GitHub to launch</button><a class="btn ghost" href="#/demo">Try the demo instead</a>`}
      </div></div>`;
  }

  function render() {
    const body = [stepOrg, stepTag, stepMode, stepLaunch][w.step - 1]();
    mount(h`<div class="wizard">
      <ol class="steps">${STEPS.map((s, i) => {
        const n = i + 1;
        const cls = n === w.step ? 'cur' : n < w.step ? 'done' : '';
        return h`<li class="${cls}"><button data-act="goto" data-n="${n}" ${canGo(n) ? '' : 'disabled'}><span class="row" style="gap:12px"><span class="n">${n < w.step ? '✓' : n}</span>${s}</span></button></li>`;
      })}</ol>
      <div class="stack" style="--gap:24px">
        ${body}
        <div class="row">
          ${w.step > 1 ? h`<button class="btn ghost" data-act="back">← Back</button>` : ''}
          <span class="spacer"></span>
          ${w.step < 4 ? h`<button class="btn primary" data-act="next" ${canGo(w.step + 1) ? '' : 'disabled'}>Continue →</button>` : ''}
        </div>
      </div></div>`);
    bind(actions);
  }

  const actions = {
    pick: el => { w.org = el.dataset.org; w.tagged = false; render(); },
    tagged: el => { w.tagged = el.checked; render(); },
    mode: el => { w.dry = el.dataset.dry === '1'; render(); },
    copy: el => copyText(el.dataset.text),
    goto: el => { const n = Number(el.dataset.n); if (canGo(n)) { w.step = n; render(); } },
    next: () => { if (canGo(w.step + 1)) { w.step++; render(); } },
    back: () => { w.step--; render(); },
    launch: async () => {
      w.busy = 'Starting…'; w.error = null; render();
      try {
        const id = await dispatch(WF_MIGRATE, { org: w.org, dry_run: String(w.dry) }, s => { w.busy = s; render(); });
        meta.set(id, { org: w.org, dry: w.dry });
        location.hash = `#/runs/${id}`;
      } catch (e) {
        w.busy = false;
        w.error = e.status === 403 || e.status === 404 ? `GitHub refused (${e.message}). The token needs Actions: read and write on ${state.repo}.` : e.message;
        render();
      }
    },
  };

  render();
  loadOrgs().then(o => {
    orgs = o;
    if (w.org && w.org !== 'all' && !orgs.some(x => x.org_key === w.org)) w.org = null;
    if (w.org) w.step = 2;
    render();
  }).catch(e => { loadErr = e; render(); });
}

// Dispatch a workflow and return the new run's id. Newer GitHub returns it directly
// (return_run_details); otherwise find the run this user just created.
async function dispatch(workflow, inputs, onStatus = () => {}) {
  const since = Date.now() - 10_000;
  let res;
  try {
    res = await gh(`/actions/workflows/${workflow}/dispatches`, { method: 'POST', body: { ref: BRANCH, inputs, return_run_details: true } });
  } catch (e) {
    if (e.status !== 422 || !/return_run_details/.test(e.message)) throw e;
    res = await gh(`/actions/workflows/${workflow}/dispatches`, { method: 'POST', body: { ref: BRANCH, inputs } });
  }
  if (res?.workflow_run_id) return res.workflow_run_id;
  onStatus('Finding the run…');
  if (!state.user) state.user = await api('/user');
  for (let i = 0; i < 20; i++) {
    await new Promise(r => setTimeout(r, 2000));
    const { workflow_runs } = await gh(`/actions/workflows/${workflow}/runs?event=workflow_dispatch&per_page=5`);
    const run = workflow_runs.find(r => new Date(r.created_at) >= since && r.actor?.login === state.user.login);
    if (run) return run.id;
  }
  throw new Error('The run started, but it did not show up in time. Open Runs to find it.');
}

// ------------------------------------------------------------------------------------------
// Runs list
// ------------------------------------------------------------------------------------------
function viewRuns() {
  mount(h`<div class="stack"><div class="row"><h2>Runs</h2><span class="muted small">${state.repo}</span><span class="spacer"></span><a class="btn primary sm" href="#/run">New run</a></div>
    <div class="list" id="runs"><div class="empty"><span class="spin"></span></div></div></div>`);
  fetchRuns(20).then(async runs => {
    const el = $('#runs'); if (!el) return;
    el.innerHTML = runs.length ? show(runs.map(r => runRow(r))) : '<div class="empty">No runs yet. <a href="#/run">Start one</a>.</div>';
    const orgs = await orgsForRuns(runs);
    if ($('#runs') && Object.keys(orgs).length) el.innerHTML = show(runs.map(r => runRow(r, orgs[r.id])));
  }).catch(e => { const el = $('#runs'); if (el) { el.classList.remove('list'); el.innerHTML = show(errorBox(e)); } });
}

// ------------------------------------------------------------------------------------------
// Run tracker
// ------------------------------------------------------------------------------------------
class RealSource {
  constructor(id) { this.id = id; this.demo = false; this.cache = { pr: {}, files: {}, report: {} }; }
  get meta() { return meta.get(this.id) || {}; }
  get interval() { return state.token ? 6000 : 20000; }
  async load() {
    const [run, { jobs }] = await Promise.all([gh(`/actions/runs/${this.id}`), gh(`/actions/runs/${this.id}/jobs?per_page=100`)]);
    return { run, jobs };
  }
  async pending(run) {
    if (run.status !== 'waiting' || !state.token) return [];
    return gh(`/actions/runs/${this.id}/pending_deployments`).catch(() => []);
  }
  async pr(org, run) {
    const c = this.cache.pr[org];
    if (c && c.state === 'closed') return { pr: c, files: this.cache.files[org] };
    const owner = state.repo.split('/')[0];
    const branch = `promote/${org}/${run.id}-${run.run_attempt}`;
    const list = await gh(`/pulls?state=all&base=${BRANCH}&head=${encodeURIComponent(`${owner}:${branch}`)}`);
    const pr = list[0] || null;
    if (!pr) return null;
    this.cache.pr[org] = pr;
    if (!this.cache.files[org]) this.cache.files[org] = await gh(`/pulls/${pr.number}/files?per_page=100`).catch(() => []);
    return { pr, files: this.cache.files[org] };
  }
  async report(org, run) {
    if (this.cache.report[org]) return this.cache.report[org];
    const suffix = `_${run.id}-${run.run_attempt}`;
    const dirs = await gh(`/contents/reports/${org}?ref=ts-dev`).catch(() => []);
    const dir = dirs.find(d => d.name.endsWith(suffix));
    if (!dir) return null;
    const [summary, failures, manifest] = await Promise.all([
      ghRaw(`${dir.path}/migration_summary.txt`, 'ts-dev').catch(() => ''),
      ghRaw(`${dir.path}/failures.json`, 'ts-dev').then(JSON.parse).catch(() => []),
      ghRaw(`${dir.path}/manifest.json`, 'ts-dev').then(JSON.parse).catch(() => []),
    ]);
    return (this.cache.report[org] = { summary, failures, manifest, url: `https://github.com/${state.repo}/tree/ts-dev/${dir.path}` });
  }
  async approvePR(pr) {
    await gh(`/pulls/${pr.number}/reviews`, { method: 'POST', body: { event: 'APPROVE', body: 'Approved from the ts/version-control console.' } });
    delete this.cache.pr[pr.head.ref.split('/')[1]];
  }
  async closePR(pr) {
    await gh(`/pulls/${pr.number}`, { method: 'PATCH', body: { state: 'closed' } });
    delete this.cache.pr[pr.head.ref.split('/')[1]];
  }
  async decideDeploy(ids, approve) {
    await gh(`/actions/runs/${this.id}/pending_deployments`, { method: 'POST', body: { environment_ids: ids, state: approve ? 'approved' : 'rejected', comment: approve ? 'Approved from the console.' : 'Rejected from the console.' } });
  }
  async cancel() { await gh(`/actions/runs/${this.id}/cancel`, { method: 'POST' }); }
}

function prettyFile(path) {
  const base = path.split('/').pop();
  const [stem, rest] = base.split(/__(.+)/);
  const objId = (rest || '').replace(/__\d{8}T\d{6}Z/, '').replace(/\.(\w+)\.tml$|\.(yaml|json)$/, '');
  return { name: stem.replace(/_+/g, ' ').trim(), objId, group: path.split('/').slice(-2, -1)[0] || '' };
}

function viewRun(src) {
  const ui = { open: new Set(), userToggled: new Set(), busy: null, msg: null, orgTab: null };
  let data = null, extras = {}, err = null, timer = null, alive = true, reportTries = 0;
  cleanup = () => { alive = false; clearTimeout(timer); src.stop?.(); };

  async function refresh() {
    try {
      data = await src.load();
      err = null;
      const { run, jobs } = data;
      const runDone = run.status === 'completed';
      const parsed = parseJobs(jobs);
      extras.pending = await src.pending(run);
      extras.pr = {}; extras.report = {};
      if (runKind(run) === 'migration') {
        await Promise.all(Object.entries(parsed.orgs).map(async ([org, js]) => {
          if (js.promote && jobState(js.promote, runDone) !== 'pending') extras.pr[org] = await src.pr(org, run).catch(() => null);
          if (js.report?.status === 'completed') extras.report[org] = await src.report(org, run).catch(() => null);
        }));
      }
    } catch (e) { err = e; }
    if (!alive) return;
    render();
    // Keep polling until the run is over, then a few more times until each org's report has
    // been committed to ts-dev.
    const done = data?.run.status === 'completed';
    const missingReport = done && runKind(data.run) === 'migration'
      && Object.keys(parseJobs(data.jobs).orgs).some(o => !extras.report[o]);
    if (err?.status === 404) return;
    if (!done || (missingReport && ++reportTries <= 3)) timer = setTimeout(refresh, src.interval);
  }

  function stageRows(phase, job, runDone, dry) {
    return phase.stages.map(([num, label, opts = {}]) => {
      const step = findStep(job, num, opts);
      let st = stepState(step, job, runDone);
      const note = dry && opts.dry ? `dry run: ${opts.dry}` : '';
      return h`<div class="stage ${st}">${dot(st)}<div class="name">${num ? h`<span class="num">${num}</span>` : ''}${label}</div>
        ${note ? h`<span class="note">${note}</span>` : ''}<span class="dur">${step?.started_at ? dur(step.started_at, step.completed_at) : ''}</span></div>`;
    });
  }

  function reviewCard(org, prInfo, promoteJob, dry) {
    if (!prInfo) {
      const waiting = promoteJob && findStep(promoteJob, '7')?.status !== 'completed';
      return waiting ? h`<div class="card tight"><span class="spin"></span> <span class="muted">Opening the promote PR…</span></div>` : '';
    }
    const { pr, files } = prInfo;
    const merged = !!pr.merged_at;
    const prDry = pr.title.startsWith('[DRY RUN]');
    const groups = {};
    (files || []).forEach(f => { const p = prettyFile(f.filename); (groups[p.group] ||= []).push({ ...p, status: f.status }); });
    const waitingOnYou = pr.state === 'open' && !prDry;
    const cls = merged ? 'accent' : waitingOnYou ? 'warn' : '';
    return h`<div class="card ${cls} stack" style="--gap:14px">
      <div class="row">
        <h3>${merged ? 'Approved and merged' : pr.state === 'closed' ? 'PR closed' : prDry ? 'Dry-run PR (for review only)' : 'Waiting for your review'}</h3>
        <span class="spacer"></span>
        <a class="small mono" href="${pr.html_url}" target="_blank" rel="noopener">#${pr.number} ↗</a>
      </div>
      <p class="small muted">${pr.title}</p>
      ${waitingOnYou ? h`<p class="small">Check the TML below. You can edit files in the PR before approving; the import uses the merged commit, so your edits are what ship.
        ${pr.auto_merge ? 'It merges itself once approved.' : h` <b>Auto-merge is off</b>, so merge it after approving.`}</p>` : ''}
      ${prDry && pr.state === 'open' ? h`<p class="small muted">Nothing will be imported from this PR. Look through it, then close it.</p>` : ''}
      ${pr.state === 'closed' && !merged ? h`<p class="small muted">${dry ? 'Closed. Its branch is deleted automatically.' : 'Closed without merging: nothing was imported, and the tags stay in dev.'}</p>` : ''}
      ${files?.length ? h`<div class="files">${Object.entries(groups).map(([g, fs]) => h`<div class="grp">${g} · ${fs.length}</div>${fs.map(f => h`<div class="f"><span class="${f.status === 'added' ? 'a' : 'm'}">${f.status === 'added' ? '+' : '~'}</span><span>${f.name}</span><span class="muted">${f.objId}</span></div>`)}`)}</div>` : ''}
      <div class="row">
        <a class="btn sm" href="${pr.html_url}/files" target="_blank" rel="noopener">Review files on GitHub ↗</a>
        ${waitingOnYou ? h`<button class="btn primary sm" data-act="approve" data-org="${org}" ${ui.busy || !(state.token || src.demo) ? 'disabled' : ''}>Approve</button>` : ''}
        ${prDry && pr.state === 'open' ? h`<button class="btn sm" data-act="closepr" data-org="${org}" ${ui.busy || !(state.token || src.demo) ? 'disabled' : ''}>Close dry-run PR</button>` : ''}
        ${!state.token && !src.demo && pr.state === 'open' ? h`<button class="btn ghost sm" data-act="connect">Connect to act here</button>` : ''}
      </div></div>`;
  }

  function reportCard(org, rep, run, js, dry) {
    if (!rep) return '';
    const byType = {};
    rep.manifest.forEach(o => { byType[o.obj_type] = (byType[o.obj_type] || 0) + 1; });
    const fails = rep.failures || [];
    const tag = `migrate/${org}/${run.id}-${run.run_attempt}`;
    const tagged = js.release?.conclusion === 'success' && !src.demo;
    return h`<div class="card stack ${fails.length ? 'bad' : ''}" style="--gap:16px">
      <div class="row"><h3>Report</h3><span class="spacer"></span><a class="small" href="${rep.url}" target="_blank" rel="noopener">Files on ts-dev ↗</a></div>
      <div class="grid g3">
        <div><div class="big-num">${rep.manifest.length}</div><div class="small muted">objects picked up</div></div>
        <div><div class="big-num" style="color:${fails.length ? 'var(--bad)' : 'inherit'}">${fails.length}</div><div class="small muted">failed (they keep their tag)</div></div>
        <div><div class="small muted">by type</div><div class="small">${Object.entries(byType).map(([t, n]) => `${n} ${t.toLowerCase()}`).join(' · ') || '—'}</div></div>
      </div>
      ${rep.manifest.length ? h`<div class="tbl-wrap"><table class="cmp"><thead><tr><th>Object</th><th>Type</th><th>Why</th></tr></thead><tbody>
        ${rep.manifest.slice(0, 50).map(o => h`<tr><td>${o.name}<div class="small muted mono">${o.obj_id}</div></td><td class="small">${(o.obj_type || '').toLowerCase()}</td><td class="small muted">${o.reason || ''}</td></tr>`)}
        </tbody></table></div>` : h`<p class="small muted">No tagged objects were found. Check the tag name in dev.</p>`}
      ${fails.map(f => h`<div class="notice bad"><div><b>${f.name || f.obj_id}</b> <span class="muted small">· ${(f.obj_type || '').toLowerCase()} · stage ${f.stage}</span><div class="small" style="margin-top:4px">${f.error}</div>${f.suggested_action ? h`<div class="small muted" style="margin-top:4px">→ ${f.suggested_action}</div>` : ''}</div></div>`)}
      ${tagged ? h`<div class="notice ok"><div>Restore point <code>${tag}</code> created. <a href="#/rollback?org=${org}&ref=${encodeURIComponent(tag)}">Roll back to it</a> any time.</div></div>` : ''}
      ${fails.length ? h`<p class="small muted">Fix what's listed, then start another run: only objects that still carry the tag are retried.</p>` : ''}
      ${rep.summary ? h`<details><summary class="small muted" style="cursor:pointer">Full summary</summary><pre style="margin-top:8px">${rep.summary}</pre></details>` : ''}
    </div>`;
  }

  function orgPipeline(org, js, run, dry) {
    const runDone = run.status === 'completed';
    const states = PHASES.map(p => jobState(js[p.key], runDone));
    // Stage 7b runs inside the Promote job: show the review as "waiting" while it polls.
    const prInfo = extras.pr?.[org];
    if (states[1] === 'running' && findStep(js.promote, '7b')?.status === 'in_progress') states[1] = 'waiting';

    const rail = h`<div class="rail">${PHASES.map((p, i) => h`<button class="ph ${states[i]}" data-act="jump" data-id="${org}-${p.key}">${dot(states[i])}<span class="lbl">${p.label}</span><span class="sub">${p.sub}</span></button>`)}</div>`;

    const blocks = PHASES.map((p, i) => {
      const id = `${org}-${p.key}`;
      if (!ui.userToggled.has(id)) {
        if (['running', 'waiting', 'failed'].includes(states[i])) ui.open.add(id);
        else if (states[i] === 'done') ui.open.delete(id);
      }
      const job = js[p.key];
      const block = h`<details class="phase" id="${id}" ${ui.open.has(id) ? 'open' : ''} data-toggle="${id}">
        <summary>${dot(states[i])}<b>${p.label}</b><span class="small muted">${STATE_LABEL[states[i]]}${job?.started_at ? ` · ${dur(job.started_at, job.completed_at)}` : ''}</span>
          ${job?.html_url && !src.demo ? h`<a class="small muted" href="${job.html_url}" target="_blank" rel="noopener" onclick="event.stopPropagation()">logs ↗</a>` : ''}<span class="chev">▶</span></summary>
        <div class="stages">${stageRows(p, job, runDone, dry)}</div></details>`;
      if (p.key === 'promote') return [block, reviewCard(org, prInfo, js.promote, dry)];
      if (p.key === 'report') return [block, reportCard(org, extras.report?.[org], run, js, dry)];
      return block;
    });
    return h`<div class="stack" style="--gap:12px">${rail}${blocks}</div>`;
  }

  function redeployView(run, jobs) {
    const job = jobs[0];
    const runDone = run.status === 'completed';
    const steps = (job?.steps || []).filter(s => !/^(Set up job|Complete job|Post |Checkout|Set up Python|Install dependencies)/.test(s.name));
    return h`<details class="phase" open><summary>${dot(jobState(job, runDone))}<b>${job?.name || 'Redeploy'}</b>${job?.html_url ? h`<a class="small muted" href="${job.html_url}" target="_blank" rel="noopener">logs ↗</a>` : ''}</summary>
      <div class="stages">${steps.length ? steps.map(s => { const st = stepState(s, job, runDone); return h`<div class="stage ${st}">${dot(st)}<div class="name">${s.name}</div><span class="dur">${s.started_at ? dur(s.started_at, s.completed_at) : ''}</span></div>`; }) : h`<div class="stage"><span class="spin"></span><div class="name muted">Waiting for the job to start…</div></div>`}</div></details>`;
  }

  function render() {
    if (err && !data) { mount(h`<div class="stack"><a class="small muted" href="#/runs">← Runs</a>${errorBox(err)}</div>`); bind(actions); return; }
    if (!data) { mount(h`<div class="empty"><span class="spin"></span></div>`); return; }
    const { run, jobs } = data;
    const kind = runKind(run);
    const parsed = parseJobs(jobs);
    const m = src.meta;
    let orgNames = Object.keys(parsed.orgs);
    const st = runState(run);
    const dryGuess = m.dry ?? orgNames.map(o => isDryFromJobs(parsed.orgs[o])).find(v => v !== null)
      ?? Object.values(extras.pr || {}).map(p => p?.pr?.title.startsWith('[DRY RUN]')).find(v => v !== undefined) ?? null;
    if (!ui.orgTab || !orgNames.includes(ui.orgTab)) ui.orgTab = orgNames[0];

    const pending = (extras.pending || []).filter(p => p.current_user_can_approve !== undefined);
    const head = h`<div class="stack" style="--gap:10px">
      <a class="small muted" href="${src.demo ? '#/' : '#/runs'}">← ${src.demo ? 'Home' : 'Runs'}</a>
      <div class="row">
        <h2>${kind === 'redeploy' ? 'Redeploy' : 'Migration'}${orgNames.length === 1 ? h` · <span class="mono">${orgNames[0]}</span>` : m.org && m.org !== 'all' ? h` · <span class="mono">${m.org}</span>` : ''}</h2>
        ${dryGuess === true ? h`<span class="pill dry">dry run</span>` : dryGuess === false ? h`<span class="pill">live</span>` : ''}
        ${pillFor(st)}
        ${src.demo ? h`<span class="pill">demo</span>` : ''}
        <span class="spacer"></span>
        ${run.status !== 'completed' && (state.token || src.demo) ? h`<button class="btn ghost sm danger" data-act="cancel" ${ui.busy ? 'disabled' : ''}>Cancel run</button>` : ''}
        ${!src.demo ? h`<a class="btn sm" href="${run.html_url}" target="_blank" rel="noopener">Open in GitHub ↗</a>` : ''}
      </div>
      <p class="small muted">#${run.run_number} · started by ${run.actor?.login || '—'} · ${timeAgo(run.run_started_at || run.created_at)} · ${dur(run.run_started_at || run.created_at, run.status === 'completed' ? run.updated_at : null)}${err ? ' · reconnecting…' : ''}</p>
    </div>`;

    const notices = [];
    if (src.demo) notices.push(h`<div class="notice"><div>This is a simulated run so you can see the flow. Nothing is sent to GitHub or ThoughtSpot. When the run reaches <b>Review</b>, approve the PR to let it continue.</div></div>`);
    if (ui.msg) notices.push(h`<div class="notice ${ui.msg.kind}"><div>${ui.msg.text}</div></div>`);
    if (pending.length) notices.push(h`<div class="card warn stack" style="--gap:10px"><h3>Deployment to ${pending.map(p => p.environment.name).join(', ')} needs approval</h3>
      <p class="small muted">This environment has required reviewers. The job waits until one of them approves.</p>
      <div class="row">${pending.some(p => p.current_user_can_approve)
        ? h`<button class="btn primary sm" data-act="deploy" data-ok="1" ${ui.busy ? 'disabled' : ''}>Approve deployment</button><button class="btn sm danger" data-act="deploy" data-ok="0" ${ui.busy ? 'disabled' : ''}>Reject</button>`
        : h`<span class="small muted">You are not a reviewer for this environment.</span>`}</div></div>`);
    if (run.status === 'completed') {
      const ok = run.conclusion === 'success';
      notices.push(h`<div class="notice ${ok ? 'ok' : run.conclusion === 'cancelled' ? '' : 'bad'}"><div>${ok
        ? (dryGuess ? 'Dry run finished cleanly. If the report looks right, start a live run.' : kind === 'redeploy' ? 'Redeploy finished.' : 'Migration finished. Content is in prod.')
        : run.conclusion === 'cancelled' ? 'Run cancelled.' : 'The run failed. The failed step is open below; its logs say why.'}
        ${ok && dryGuess && m.org ? h` <a href="#/run?org=${m.org}">Set up the live run →</a>` : ''}</div></div>`);
    }

    let body;
    if (kind === 'redeploy') body = redeployView(run, jobs);
    else if (!orgNames.length) body = h`<div class="card tight"><span class="spin"></span> <span class="muted">${parsed.setup?.status === 'completed' && parsed.setup.conclusion !== 'success' ? 'Building the org matrix failed. Open the run in GitHub for details.' : 'Building the org matrix…'}</span></div>`;
    else {
      const tabs = orgNames.length > 1 ? h`<div class="seg">${orgNames.map(o => h`<button data-act="tab" data-org="${o}" aria-pressed="${o === ui.orgTab}">${dot(jobState(Object.values(parsed.orgs[o]).find(j => j.status !== 'completed') || parsed.orgs[o].report, run.status === 'completed'))} ${o}</button>`)}</div>` : '';
      body = h`${tabs}${orgPipeline(ui.orgTab, parsed.orgs[ui.orgTab], run, dryGuess)}`;
    }

    // Keep scroll position and only re-render the tracker area.
    mount(h`<div class="stack" style="--gap:20px">${head}${notices}${body}</div>`);
    bind(actions);
    // A <details> rendered open fires "toggle" too; only a change from what we rendered is the user's.
    $$('details[data-toggle]').forEach(d => d.addEventListener('toggle', () => {
      const id = d.dataset.toggle;
      if (d.open === ui.open.has(id)) return;
      ui.userToggled.add(id);
      d.open ? ui.open.add(id) : ui.open.delete(id);
    }));
  }

  async function act(label, fn, okMsg) {
    ui.busy = label; ui.msg = null; render();
    try { await fn(); if (okMsg) ui.msg = { kind: 'ok', text: okMsg }; }
    catch (e) {
      let text = e.message;
      if (/approve your own pull request/i.test(text)) text = 'GitHub doesn\'t let you approve a PR opened with your own token. Have someone else approve it, or set up the GitHub App (GH_APP_CLIENT_ID / GH_APP_PRIVATE_KEY) so the app opens the PRs. See the runbook.';
      else if (e.status === 403 || e.status === 404) text = `GitHub refused: ${e.message}. Check the token's permissions.`;
      ui.msg = { kind: 'bad', text };
    }
    ui.busy = null;
    clearTimeout(timer);
    refresh();
  }

  const actions = {
    tab: el => { ui.orgTab = el.dataset.org; render(); },
    jump: el => { const d = document.getElementById(el.dataset.id); if (d) { d.open = true; d.scrollIntoView({ behavior: 'smooth', block: 'start' }); } },
    approve: el => act('approve', () => src.approvePR(extras.pr[el.dataset.org].pr), 'Approved. The PR merges itself if auto-merge is on, and the import starts after that.'),
    closepr: el => act('close', () => src.closePR(extras.pr[el.dataset.org].pr), 'Dry-run PR closed.'),
    deploy: el => act('deploy', () => src.decideDeploy(extras.pending.map(p => p.environment.id), el.dataset.ok === '1')),
    cancel: () => { if (confirm('Cancel this run? Stages already finished are not undone.')) act('cancel', () => src.cancel(), 'Cancel requested.'); },
  };

  render();
  refresh();
}

// A simulated run with the same shape as the GitHub API, so the tracker can be tried
// without a token or a ThoughtSpot cluster.
class DemoSource {
  constructor() {
    this.demo = true;
    this.interval = 700;
    this.meta = { org: 'sales', dry: false };
    this.t0 = new Date();
    this.cursor = 0;
    this.approved = false;
    this.merged = false;
    this.cancelled = false;
    this.flat = [];
    PHASES.forEach(p => p.stages.forEach(([num, label, o = {}]) => this.flat.push({ phase: p.key, name: o.step || `Stage ${num} — ${label}`, num })));
    this.times = [];
    this.files = [
      'orgs/sales/liveboards/pipeline_health__PipelineHealth-41e28ff7.liveboard.tml',
      'orgs/sales/answers/top_reps_by_region__TopRepsByRegion-d2677317.answer.tml',
      'orgs/sales/models/sales_model__SalesModel-29b76499.table.tml',
      'orgs/sales/tables/fact_opportunities__FACT_OPPORTUNITIES-1277592f.table.tml',
      'orgs/sales/tables/dim_accounts__DIM_ACCOUNTS-8cd69308.table.tml',
      'orgs/sales/memory/memory.yaml',
    ].map((filename, i) => ({ filename, status: i === 2 ? 'modified' : 'added' }));
  }
  now(i) { return this.times[i] ? new Date(this.times[i]).toISOString() : null; }
  advance() {
    if (this.cancelled || this.cursor >= this.flat.length) return;
    const cur = this.flat[this.cursor];
    if (cur.num === '7b' && !this.merged) return;
    this.times[this.cursor] ??= Date.now() - 700;
    this.cursor++;
    this.times[this.cursor] ??= Date.now();
  }
  async load() {
    this.advance();
    const done = this.cancelled || this.cursor >= this.flat.length;
    const jobs = [{ name: 'Build org matrix', status: 'completed', conclusion: 'success', steps: [] }];
    for (const p of PHASES) {
      const idx = this.flat.map((s, i) => s.phase === p.key ? i : -1).filter(i => i >= 0);
      if (this.cursor < idx[0]) continue;
      const finished = this.cursor > idx[idx.length - 1];
      jobs.push({
        name: `Migrate org sales / ${p.job} (sales)`,
        status: finished || this.cancelled ? 'completed' : 'in_progress',
        conclusion: finished ? 'success' : this.cancelled ? 'cancelled' : null,
        started_at: this.now(idx[0]), completed_at: finished ? this.now(idx[idx.length - 1] + 1) : null,
        steps: idx.map(i => ({
          name: this.flat[i].name,
          status: i < this.cursor ? 'completed' : i === this.cursor && !this.cancelled ? 'in_progress' : 'queued',
          conclusion: i < this.cursor ? 'success' : null,
          started_at: this.now(i), completed_at: i < this.cursor ? this.now(i + 1) : null,
        })),
      });
    }
    const run = {
      id: 1234, run_number: 42, run_attempt: 1, path: `.github/workflows/${WF_MIGRATE}`,
      status: done ? 'completed' : 'in_progress', conclusion: done ? (this.cancelled ? 'cancelled' : 'success') : null,
      actor: { login: 'you' }, created_at: this.t0.toISOString(), run_started_at: this.t0.toISOString(), updated_at: new Date().toISOString(), html_url: '#',
    };
    return { run, jobs };
  }
  async pending() { return []; }
  async pr() {
    if (this.cursor < this.flat.findIndex(s => s.num === '7b')) return null;
    return { pr: {
      number: 101, title: '[sales] Promote TML run 1234-1', state: this.merged ? 'closed' : 'open', merged_at: this.merged ? new Date().toISOString() : null,
      html_url: '#/demo', auto_merge: { merge_method: 'merge' }, head: { ref: 'promote/sales/1234-1' },
    }, files: this.files };
  }
  async report() {
    return { url: '#/demo', summary: 'ThoughtSpot Migration Report\nOrg: sales   Run: 1234-1   Mode: LIVE\n\n(demo)', failures: [], manifest: [
      { obj_type: 'LIVEBOARD', name: 'Pipeline Health', obj_id: 'PipelineHealth-41e28ff7', reason: 'tagged' },
      { obj_type: 'ANSWER', name: 'Top reps by region', obj_id: 'TopRepsByRegion-d2677317', reason: 'tagged' },
      { obj_type: 'LOGICAL_TABLE', name: 'Sales Model', obj_id: 'SalesModel-29b76499', reason: 'dependency' },
      { obj_type: 'LOGICAL_TABLE', name: 'FACT_OPPORTUNITIES', obj_id: 'FACT_OPPORTUNITIES-1277592f', reason: 'dependency' },
      { obj_type: 'LOGICAL_TABLE', name: 'DIM_ACCOUNTS', obj_id: 'DIM_ACCOUNTS-8cd69308', reason: 'dependency' },
    ] };
  }
  async approvePR() { this.approved = true; await new Promise(r => setTimeout(r, 900)); this.merged = true; }
  async closePR() {}
  async decideDeploy() {}
  async cancel() { this.cancelled = true; }
}

// ------------------------------------------------------------------------------------------
// Rollback (ts-redeploy.yml)
// ------------------------------------------------------------------------------------------
function viewRollback(q) {
  const r = { org: q.get('org') || null, ref: q.get('ref') || '', custom: '', dry: true, tags: null, err: null, busy: false, error: null };
  let orgs = null;

  async function loadTags() {
    r.tags = null; render();
    try {
      const refs = await gh(`/git/matching-refs/tags/migrate/${r.org}/`);
      r.tags = refs.map(x => x.ref.replace('refs/tags/', '')).sort((a, b) => Number(b.split('/').pop().split('-')[0]) - Number(a.split('/').pop().split('-')[0]));
    } catch (e) { r.tags = []; r.err = e; }
    render();
  }

  function render() {
    const ref = r.custom.trim() || r.ref;
    mount(h`<div class="stack" style="--gap:24px">
      <div><h2>Roll back or redeploy</h2><p class="muted" style="margin-top:6px;max-width:640px">Restores <code>orgs/&lt;org&gt;/</code> exactly as it was at an approved commit on <code>${BRANCH}</code> and imports it again. It doesn't touch dev. Nothing is deleted in prod, so objects added since stay as they are.</p></div>
      <div class="card stack">
        <fieldset><legend>1 · Org</legend>
          ${orgs ? h`<div class="seg">${orgs.map(o => h`<button data-act="org" data-org="${o.org_key}" aria-pressed="${r.org === o.org_key}">${o.org_key}</button>`)}</div>` : h`<span class="spin"></span>`}
        </fieldset>
      </div>
      ${r.org ? h`<div class="card stack">
        <fieldset><legend>2 · Restore point</legend>
          ${r.tags === null ? h`<span class="spin"></span>` : r.tags.length ? h`<div class="list">${r.tags.map(t => {
            const runId = t.split('/').pop().split('-')[0];
            return h`<label class="item" style="cursor:pointer"><input type="radio" name="tag" data-act="ref" value="${t}" ${r.ref === t && !r.custom ? 'checked' : ''} style="accent-color:var(--accent)">
              <div class="main"><div class="t mono small">${t}</div><div class="s">created by run <a href="#/runs/${runId}">${runId}</a></div></div></label>`;
          })}</div>` : h`<p class="small muted">${r.err ? r.err.message : 'No restore points yet for this org. One is created after every live run with no failures.'}</p>`}
          <label class="f" style="margin-top:14px"><span>Or a commit SHA from ${BRANCH}</span><input type="text" data-act="custom" value="${r.custom}" placeholder="e.g. 3dbe872" spellcheck="false"></label>
        </fieldset>
      </div>
      <div class="card stack">
        <fieldset><legend>3 · Mode</legend>
          <div class="seg"><button data-act="dry" data-v="1" aria-pressed="${r.dry}">Dry run</button><button data-act="dry" data-v="0" aria-pressed="${!r.dry}">Apply to prod</button></div>
          <p class="small muted" style="margin-top:8px">${r.dry ? 'Validates the restored content against prod. Writes nothing.' : 'Imports the restored content into prod. Uses the same prod environment approval as a migration.'}</p>
        </fieldset>
      </div>
      ${r.error ? h`<div class="notice bad"><div>${r.error}</div></div>` : ''}
      <div class="row">${state.token
        ? h`<button class="btn primary" data-act="go" ${!ref || r.busy ? 'disabled' : ''}>${r.busy ? h`<span class="spin"></span> ${r.busy}` : r.dry ? 'Start redeploy (dry run)' : 'Redeploy to prod'}</button>`
        : h`<button class="btn primary" data-act="connect">Connect GitHub to redeploy</button>`}
        ${ref ? h`<span class="small muted mono">${ref}</span>` : ''}</div>
    </div>` : ''}
    </div>`);
    bind(actions);
  }

  const actions = {
    org: el => { r.org = el.dataset.org; r.ref = ''; r.err = null; loadTags(); },
    ref: el => { r.ref = el.value; r.custom = ''; render(); },
    custom: el => { r.custom = el.value; const btn = $('[data-act="go"]'); if (btn) btn.disabled = !(r.custom.trim() || r.ref); },
    dry: el => { r.dry = el.dataset.v === '1'; render(); },
    go: async () => {
      const ref = r.custom.trim() || r.ref;
      if (!r.dry && !confirm(`Redeploy ${r.org} to prod from ${ref}?`)) return;
      r.busy = 'Starting…'; r.error = null; render();
      try {
        const id = await dispatch(WF_REDEPLOY, { org: r.org, ref, dry_run: String(r.dry) }, s => { r.busy = s; render(); });
        meta.set(id, { org: r.org, dry: r.dry });
        location.hash = `#/runs/${id}`;
      } catch (e) { r.busy = false; r.error = e.message; render(); }
    },
  };

  render();
  loadOrgs().then(o => { orgs = o; if (r.org) loadTags(); else render(); }).catch(e => { r.err = e; mount(errorBox(e)); bind({}); });
}

// ------------------------------------------------------------------------------------------
// Add org (config builder)
// ------------------------------------------------------------------------------------------
function viewBuilder() {
  const c = {
    org_key: '', display_name: '', migration_tag: '', migration_tag_guid: '',
    src_url: '', src_org: '', tgt_url: '', tgt_org: '', same: true,
    dependency_mode: 'include', validation_gate: 'block', exclude: 'TSBIServer-eaab6de7',
    db_var: 'ts_db_name', db_src: '', db_tgt: '', sc_var: 'ts_schema_name', sc_src: '', sc_tgt: '',
  };
  let tagTouched = false;

  const q = v => {
    const s = String(v ?? '');
    return s === '' ? '""' : /^[\w./:-]+$/.test(s) && !/^(true|false|null|yes|no|on|off|[\d.]+)$/i.test(s) ? s : JSON.stringify(s);
  };
  function problems() {
    const p = {};
    if (!/^[a-z0-9_]+$/.test(c.org_key)) p.org_key = 'Lowercase letters, digits and underscores only (a hyphen would break per-org secret names).';
    if (!c.migration_tag.trim()) p.migration_tag = 'Required.';
    if (!/^https:\/\/[^\s]+$/.test(c.src_url)) p.src_url = 'An https:// URL.';
    if (!/^\d+$/.test(c.src_org)) p.src_org = 'A number.';
    if (!c.same && !/^https:\/\/[^\s]+$/.test(c.tgt_url)) p.tgt_url = 'An https:// URL.';
    if (!/^\d+$/.test(c.tgt_org)) p.tgt_org = 'A number.';
    if (c.same && c.src_org && c.src_org === c.tgt_org) p.tgt_org = 'Same cluster needs a different org id.';
    if (!c.db_var.trim()) p.db_var = 'Required.';
    if (!c.sc_var.trim()) p.sc_var = 'Required.';
    return p;
  }
  function yaml() {
    const tgtUrl = c.same ? c.src_url : c.tgt_url;
    const exclude = c.exclude.split(/[\s,]+/).filter(Boolean);
    return [
      `org_key: ${q(c.org_key)}`,
      `display_name: ${q(c.display_name || c.org_key)}`,
      '',
      '# Tag applied to objects in the source org to queue them for migration.',
      `migration_tag: ${q(c.migration_tag)}`,
      c.migration_tag_guid ? `migration_tag_guid: ${q(c.migration_tag_guid)}` : '# migration_tag_guid: ""   # needed if the service account cannot search tags',
      '',
      'source:',
      `  base_url: ${q(c.src_url)}`,
      `  org_id: ${c.src_org || 0}`,
      '',
      'target:',
      `  base_url: ${q(tgtUrl)}${c.same ? '  # same cluster, different org' : ''}`,
      `  org_id: ${c.tgt_org || 0}`,
      '',
      `dependency_mode: ${c.dependency_mode}   # include | skip_if_exists | require_tagged`,
      `validation_gate: ${c.validation_gate}   # block | warn`,
      exclude.length ? `dependency_exclude:\n${exclude.map(x => `  - ${q(x)}`).join('\n')}` : '# dependency_exclude: []',
      '',
      'variables:',
      '  database:',
      `    ts_variable_name: ${q(c.db_var)}`,
      `    source_value: ${q(c.db_src)}`,
      `    target_value: ${q(c.db_tgt)}`,
      '  schema:',
      `    ts_variable_name: ${q(c.sc_var)}`,
      `    source_value: ${q(c.sc_src)}`,
      `    target_value: ${q(c.sc_tgt)}`,
      '  extra: []',
      '',
    ].join('\n');
  }

  const field = (key, label, opts = {}) => h`<label class="f"><span>${label}</span>
    <input type="${opts.type || 'text'}" data-act="set" data-k="${key}" value="${c[key]}" placeholder="${opts.ph || ''}" spellcheck="false" autocomplete="off">
    <span class="hint" data-hint="${key}">${opts.hint || ''}</span></label>`;
  const seg = (key, vals) => h`<div class="seg">${vals.map(([v, l]) => h`<button type="button" data-act="seg" data-k="${key}" data-v="${v}" aria-pressed="${c[key] === v}">${l}</button>`)}</div>`;

  function render() {
    mount(h`<div class="stack" style="--gap:24px">
      <div><h2>Add an org</h2><p class="muted" style="margin-top:6px;max-width:640px">Fill this in and the config file builds itself on the right. Committing it to <code>config/orgs/</code> on <code>${BRANCH}</code> is all it takes for the org to show up here.</p></div>
      <div class="builder">
        <form class="stack" style="--gap:20px" onsubmit="return false">
          <div class="card stack"><fieldset class="stack" style="--gap:14px"><legend>Org</legend>
            ${field('org_key', 'Org key', { ph: 'sales', hint: 'Also the file name.' })}
            ${field('display_name', 'Display name', { ph: 'Sales' })}
            ${field('migration_tag', 'Migration tag', { ph: 'sales_readyformigration', hint: 'The tag people add in dev.' })}
            ${field('migration_tag_guid', 'Tag GUID (optional)', { hint: 'Only if the service account cannot search tags. Admin → Tags → copy the id from the URL.' })}
          </fieldset></div>
          <div class="card stack"><fieldset class="stack" style="--gap:14px"><legend>Where content moves</legend>
            <div class="grid g2">${field('src_url', 'Dev cluster URL', { type: 'url', ph: 'https://dev.thoughtspot.cloud' })}${field('src_org', 'Dev org id', { ph: '12' })}</div>
            <label class="row small muted" style="gap:8px"><input type="checkbox" data-act="same" ${c.same ? 'checked' : ''} style="accent-color:var(--accent)"> Prod is a different org on the same cluster</label>
            <div class="grid g2">${c.same ? h`<div class="f small muted" style="align-self:end;padding-bottom:20px">Prod URL: same as dev</div>` : field('tgt_url', 'Prod cluster URL', { type: 'url', ph: 'https://prod.thoughtspot.cloud' })}${field('tgt_org', 'Prod org id', { ph: '3' })}</div>
          </fieldset></div>
          <div class="card stack"><fieldset class="stack" style="--gap:14px"><legend>Dependencies</legend>
            ${seg('dependency_mode', [['include', 'Include'], ['skip_if_exists', 'Skip if in prod'], ['require_tagged', 'Require tagged']])}
            <p class="hint">${{ include: 'Pull in everything a tagged object is built on.', skip_if_exists: 'Pull in dependencies, except those already in prod.', require_tagged: 'Pull in nothing; an object with an untagged dependency is excluded.' }[c.dependency_mode]}</p>
            <label class="f"><span>Never pull in (obj_ids, one per line)</span><textarea data-act="set" data-k="exclude" spellcheck="false">${c.exclude}</textarea><span class="hint">Built-ins that exist everywhere, like the TS: BI Server worksheet.</span></label>
            <div><div class="small muted" style="margin-bottom:6px">When an object fails validation</div>${seg('validation_gate', [['block', 'Hold it back'], ['warn', 'Import anyway']])}</div>
          </fieldset></div>
          <div class="card stack"><fieldset class="stack" style="--gap:14px"><legend>Database and schema variables</legend>
            <p class="hint" style="margin-top:-6px">Tables are bound to these TABLE_MAPPING variables so they point at the prod database. Leave values blank if the variable is already set in prod.</p>
            <div class="grid g3">${field('db_var', 'DB variable')}${field('db_src', 'Dev value', { ph: 'DEV_DB' })}${field('db_tgt', 'Prod value', { ph: 'PROD_DB' })}</div>
            <div class="grid g3">${field('sc_var', 'Schema variable')}${field('sc_src', 'Dev value', { ph: 'DEV_SCHEMA' })}${field('sc_tgt', 'Prod value', { ph: 'PROD_SCHEMA' })}</div>
          </fieldset></div>
        </form>
        <div class="preview stack" style="--gap:12px">
          <div class="row"><span class="mono small">config/orgs/<b id="fname">${c.org_key || '<org_key>'}</b>.yml</span><span class="spacer"></span>
            <button class="btn sm" data-act="copy">Copy</button><button class="btn sm" data-act="download">Download</button></div>
          <pre id="yaml"></pre>
          <div id="issues"></div>
          <a class="btn primary" id="create" data-act="create" target="_blank" rel="noopener">Create file on GitHub ↗</a>
          <p class="small muted">Opens GitHub's new-file page with this content filled in, where you can commit it or open a PR. Then run a dry run for the new org.</p>
        </div>
      </div></div>`);
    bind(actions);
    update();
  }

  function update() {
    const p = problems();
    const text = yaml();
    $('#yaml').textContent = text;
    $('#fname').textContent = c.org_key || '<org_key>';
    $$('[data-hint]').forEach(el => {
      const k = el.dataset.hint;
      const input = $(`[data-k="${k}"]`);
      const touched = c[k] !== '' && input;
      const bad = touched && p[k];
      el.classList.toggle('bad', !!bad);
      if (bad) el.textContent = p[k];
      else el.textContent = el.dataset.orig ??= el.textContent;
      input?.classList.toggle('invalid', !!bad);
    });
    const missing = Object.keys(p).length;
    $('#issues').innerHTML = missing ? show(h`<p class="hint">${missing} field${missing > 1 ? 's' : ''} still need${missing > 1 ? '' : 's'} attention.</p>`) : '';
    const a = $('#create');
    a.href = `https://github.com/${state.repo}/new/${BRANCH}/config/orgs?filename=${encodeURIComponent(`${c.org_key || 'org'}.yml`)}&value=${encodeURIComponent(text)}`;
    a.classList.toggle('primary', !missing);
    a.toggleAttribute('aria-disabled', !!missing);
  }

  const actions = {
    set: el => {
      c[el.dataset.k] = el.value;
      if (el.dataset.k === 'migration_tag') tagTouched = true;
      if (el.dataset.k === 'org_key' && !tagTouched) {
        c.migration_tag = c.org_key ? `${c.org_key}_readyformigration` : '';
        const t = $('[data-k="migration_tag"]'); if (t) t.value = c.migration_tag;
      }
      update();
    },
    seg: el => { c[el.dataset.k] = el.dataset.v; render(); },
    same: el => { c.same = el.checked; render(); },
    create: (el, ev) => { if (el.hasAttribute('aria-disabled')) { ev.preventDefault(); toast('Fill in the highlighted fields first'); } },
    copy: () => copyText(yaml()),
    download: () => {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(new Blob([yaml()], { type: 'text/yaml' }));
      a.download = `${c.org_key || 'org'}.yml`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    },
  };
  render();
}

// ------------------------------------------------------------------------------------------
// Boot
// ------------------------------------------------------------------------------------------
if (state.token) api('/user').then(u => { state.user = u; refreshHeader(); }).catch(() => {});
route();
