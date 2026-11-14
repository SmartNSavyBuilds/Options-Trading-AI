const byId = (id) => document.getElementById(id);
const loginView = byId('login-view');
const appView = byId('app-view');
const apiError = byId('api-error');
const loginError = byId('login-error');

function showLogin() {
  loginView.hidden = false;
  appView.hidden = true;
}

function showApp() {
  loginView.hidden = true;
  appView.hidden = false;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  if (response.status === 401) throw Object.assign(new Error('Please sign in again.'), { status: 401 });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw Object.assign(new Error(body.detail || `Request failed (${response.status}).`), { status: response.status });
  return body;
}

function number(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function money(value) {
  const parsed = number(value);
  return parsed === null ? '--' : new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(parsed);
}

function setText(id, value) {
  byId(id).textContent = value === null || value === undefined || value === '' ? '--' : String(value);
}

function setStatus(id, value, good = false, bad = false) {
  const node = byId(id);
  node.textContent = value || 'Unknown';
  node.className = good ? 'status-good' : bad ? 'status-bad' : 'status-neutral';
}

function cell(row, value, className = '') {
  const node = document.createElement('td');
  node.textContent = value === null || value === undefined || value === '' ? '--' : String(value);
  if (className) node.className = className;
  row.append(node);
}

function emptyRow(body, columns, message) {
  const row = document.createElement('tr');
  const item = document.createElement('td');
  item.colSpan = columns;
  item.className = 'table-empty';
  item.textContent = message;
  row.append(item);
  body.replaceChildren(row);
}

function renderPositions(payload) {
  const body = byId('positions-body');
  const rows = payload.rows || [];
  setText('positions-count', `${rows.length} positions`);
  if (!rows.length) return emptyRow(body, 5, 'No position records available.');
  body.replaceChildren();
  for (const item of rows) {
    const row = document.createElement('tr');
    cell(row, item.symbol);
    cell(row, item.side);
    cell(row, item.qty);
    cell(row, money(item.market_value));
    const pnl = number(item.unrealized_pl);
    cell(row, money(pnl), pnl > 0 ? 'positive' : pnl < 0 ? 'negative' : '');
    body.append(row);
  }
}

function renderPerformance(payload) {
  const summary = payload.summary?.rows?.[0] || {};
  const execution = payload.execution_quality?.rows || [];
  const entry = execution.find((row) => String(row.execution_style).toLowerCase().includes('single leg'));
  const options = number(summary.option_positions);
  const stocks = number(summary.stock_positions);
  setText('metric-positions', summary.open_positions ?? '--');
  setText('metric-position-detail', `${stocks ?? 0} stock / ${options ?? 0} option`);
  setText('metric-market-value', money(summary.total_market_value));
  const pnl = number(summary.total_unrealized_pl);
  setText('metric-pnl', money(pnl));
  byId('metric-pnl').className = pnl > 0 ? 'positive' : pnl < 0 ? 'negative' : '';
  setText('metric-pnl-detail', `${summary.winning_positions ?? 0} winners / ${summary.losing_positions ?? 0} losers`);
  setText('metric-fill-rate', entry ? `${number(entry.fill_rate_pct)?.toFixed(1) ?? '--'}%` : '--');
  setText('metric-fill-detail', entry ? `${entry.filled_orders ?? 0} of ${entry.submitted_orders ?? 0} entry fills` : 'No entry report');
}

function renderIv(payload) {
  const body = byId('iv-body');
  const rows = payload.rows || [];
  if (!rows.length) return emptyRow(body, 4, 'IV history is unavailable or has fewer than 20 prior observations.');
  body.replaceChildren();
  for (const item of rows) {
    const row = document.createElement('tr');
    cell(row, item.symbol);
    cell(row, number(item.atm_iv_30d) === null ? '--' : `${(Number(item.atm_iv_30d) * 100).toFixed(1)}%`);
    cell(row, number(item.iv_rank) === null ? 'Insufficient history' : `${Number(item.iv_rank).toFixed(1)}%`);
    cell(row, item.observations);
    body.append(row);
  }
}

function renderActivity(payload) {
  const root = byId('guardrails-list');
  const rows = [...(payload.blocks || []), ...(payload.alerts || [])].slice(-30).reverse();
  setText('guardrail-count', `${rows.length} events`);
  if (!rows.length) {
    const empty = document.createElement('p');
    empty.className = 'table-empty';
    empty.textContent = 'No recent blocks or alerts.';
    root.replaceChildren(empty);
    return;
  }
  root.replaceChildren();
  for (const item of rows) {
    const article = document.createElement('article');
    article.className = 'activity-row';
    const kind = document.createElement('span');
    kind.className = 'activity-kind';
    kind.textContent = item.status || item.alert_type || 'event';
    const content = document.createElement('div');
    const title = document.createElement('p');
    title.className = 'activity-title';
    title.textContent = item.ticker || item.underlying_ticker || item.symbol || item.rejection_code || item.action || 'Guardrail event';
    const detail = document.createElement('p');
    detail.className = 'activity-detail';
    detail.textContent = item.detail || item.reason || item.rejection_detail || item.rejection_code || 'No detail recorded.';
    content.append(title, detail);
    article.append(kind, content);
    root.append(article);
  }
}

function renderSystem(payload) {
  const monitor = payload.monitor || {};
  const account = payload.account || {};
  const gate = payload.pre_live_gate || {};
  const rawMonitorStatus = monitor.monitor_status || 'Unavailable';
  const ageMinutes = number(payload.monitor_age_seconds) === null ? null : Math.floor(Number(payload.monitor_age_seconds) / 60);
  const monitorStatus = payload.monitor_stale ? `Stale${ageMinutes === null ? '' : ` (${ageMinutes}m)`}` : rawMonitorStatus;
  const brokerStatus = account.connection_status || 'Unavailable';
  const gateStatus = gate.verdict || 'Unknown';
  setStatus('worker-status', monitorStatus, !payload.monitor_stale && rawMonitorStatus.toLowerCase() === 'running', rawMonitorStatus.toLowerCase() === 'error');
  setStatus('broker-status', brokerStatus, brokerStatus.toLowerCase() === 'connected', brokerStatus.toLowerCase() === 'unauthorized');
  setStatus('gate-status', gateStatus, gateStatus.toLowerCase() === 'pass', gateStatus.toLowerCase() === 'fail');
  const health = payload.health || 'unknown';
  const overall = byId('overall-status');
  overall.className = `status-badge ${health === 'nominal' ? 'status-good' : health === 'review_required' ? 'status-neutral' : 'status-bad'}`;
  overall.lastElementChild.textContent = health === 'nominal' ? 'Nominal' : health === 'review_required' ? 'Review needed' : 'Unknown';
  setText('updated-at', payload.monitor_updated_at ? `Monitor snapshot ${new Date(payload.monitor_updated_at).toLocaleString()}` : 'No monitor snapshot');
}

async function refreshDashboard() {
  const button = byId('refresh-button');
  button.disabled = true;
  apiError.hidden = true;
  try {
    const [system, positions, performance, iv, guardrails] = await Promise.all([
      api('/api/v1/system/status'),
      api('/api/v1/positions'),
      api('/api/v1/performance'),
      api('/api/v1/iv-ranks'),
      api('/api/v1/guardrails'),
    ]);
    renderSystem(system);
    renderPositions(positions);
    renderPerformance(performance);
    renderIv(iv);
    renderActivity(guardrails);
    setText('footer-time', `Refreshed ${new Date().toLocaleTimeString()}`);
  } catch (error) {
    if (error.status === 401) return showLogin();
    apiError.textContent = error.message || 'Unable to load monitoring data.';
    apiError.hidden = false;
  } finally {
    button.disabled = false;
  }
}

byId('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  loginError.hidden = true;
  const form = new FormData(byId('login-form'));
  try {
    await api('/api/v1/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email: form.get('email'), password: form.get('password') }),
    });
    byId('email').value = '';
    byId('password').value = '';
    showApp();
    await refreshDashboard();
  } catch (error) {
    loginError.textContent = error.message || 'Sign in failed.';
    loginError.hidden = false;
  }
});

byId('refresh-button').addEventListener('click', refreshDashboard);
byId('logout-button').addEventListener('click', async () => {
  try { await api('/api/v1/auth/logout', { method: 'POST', body: '{}' }); } catch { /* End local view even if the session expired. */ }
  showLogin();
});

async function start() {
  try {
    await api('/api/v1/auth/session');
    showApp();
    await refreshDashboard();
  } catch (error) {
    if (error.status === 503) {
      showLogin();
      loginError.textContent = error.message;
      loginError.hidden = false;
      return;
    }
    showLogin();
  }
}

if ('serviceWorker' in navigator) navigator.serviceWorker.register('/service-worker.js').catch(() => {});
start();