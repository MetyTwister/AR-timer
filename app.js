'use strict';

// All authoritative timing happens on the Raspberry Pi. The browser only
// interpolates the displayed clock between server snapshots (re-synced on
// every update and on the 1 s heartbeat) and never computes lap times.

const STALE_AFTER_MS = 4000;      // no SSE message for this long => reconnect
const SYSTEM_POLL_MS = 10000;
const RESET_CONFIRM_MS = 3000;

const $ = (selector) => document.querySelector(selector);
const ui = {
  conn: $('#conn'), fault: $('#fault'),
  badge: $('#state-badge'), detail: $('#state-detail'), clock: $('#clock'),
  lapCount: $('#lap-count'), lastLap: $('#last-lap'), bestLap: $('#best-lap'),
  remaining: $('#remaining'), message: $('#message'), lapRows: $('#lap-rows'),
  start: $('#btn-start'), pause: $('#btn-pause'), stop: $('#btn-stop'), reset: $('#btn-reset'),
  form: $('#settings-form'), fieldset: $('#settings-fieldset'), hint: $('#settings-hint'),
};

const STATE_LABELS = {
  idle: 'READY', countdown: 'COUNTDOWN', armed: 'ARMED', running: 'RUNNING',
  paused: 'PAUSED', finished: 'FINISHED',
};
const IGNORE_LABELS = {
  lockout: 'lockout', countdown: 'countdown', paused: 'paused', idle: 'idle', finished: 'finished',
};

let race = null;          // last full snapshot from the server
let clockBase = null;     // { state, elapsed, countdown, remaining, maxMs, at }
let renderedLapsKey = '';
let lastMessageAt = 0;
let eventSource = null;
let settingsDirty = false;
let resetConfirmUntil = 0;
let messageTimer = null;

// ------------------------------------------------------------------ format

function pad(n, width = 2) { return String(n).padStart(width, '0'); }

function formatTime(ms, decimals = 3) {
  if (ms == null) return '—';
  const negative = ms < 0;
  ms = Math.abs(ms);
  const factor = 10 ** (3 - decimals);
  const frac = Math.floor((ms % 1000) / factor);
  const totalSeconds = Math.floor(ms / 1000);
  const s = totalSeconds % 60;
  const m = Math.floor(totalSeconds / 60) % 60;
  const h = Math.floor(totalSeconds / 3600);
  const tail = decimals ? `.${pad(frac, decimals)}` : '';
  const body = h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
  return (negative ? '-' : '') + body + tail;
}

function formatLap(ms) {
  if (ms == null) return '—';
  return ms < 60000 ? (ms / 1000).toFixed(3) : formatTime(ms);
}

function formatDuration(seconds) {
  if (seconds == null) return '—';
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return d ? `${d}d ${h}h ${m}m` : h ? `${h}h ${m}m` : `${m}m ${seconds % 60}s`;
}

function setText(el, text) {
  if (el.textContent !== text) el.textContent = text;
}

// ------------------------------------------------------------------ messages

function showMessage(text, kind = 'error') {
  ui.message.textContent = text;
  ui.message.className = `message message-${kind}`;
  ui.message.hidden = false;
  clearTimeout(messageTimer);
  messageTimer = setTimeout(() => { ui.message.hidden = true; }, 5000);
}

// --------------------------------------------------------------------- clock

function applyClock(r) {
  clockBase = {
    state: r.state,
    elapsed: r.elapsed_ms,
    countdown: r.countdown_remaining_ms,
    remaining: r.remaining_ms,
    maxMs: (r.settings?.max_duration_s || 0) * 1000,
    at: performance.now(),
  };
}

function renderClock() {
  if (clockBase) {
    const dt = performance.now() - clockBase.at;
    const { state } = clockBase;
    let clockText;
    let remaining = clockBase.remaining;

    if (state === 'countdown') {
      const left = Math.max(0, clockBase.countdown - dt);
      clockText = left > 0 ? String(Math.ceil(left / 1000)) : 'GO';
    } else if (state === 'running') {
      let elapsed = clockBase.elapsed + dt;
      if (clockBase.maxMs) elapsed = Math.min(elapsed, clockBase.maxMs);
      clockText = formatTime(elapsed, 2);
      if (remaining != null) remaining = Math.max(0, remaining - dt);
    } else {
      clockText = formatTime(clockBase.elapsed, 3);
    }
    setText(ui.clock, clockText);
    setText(ui.remaining, remaining == null ? '—' : formatTime(remaining, 0));
  }
  requestAnimationFrame(renderClock);
}

// ---------------------------------------------------------------- race state

function stateDetail(r) {
  const gate = r.settings.start_mode === 'gate';
  switch (r.state) {
    case 'idle':
      return gate ? 'Ready – gate start mode' : 'Ready – automatic start mode';
    case 'countdown':
      return gate ? 'Countdown – then waiting for first gate passage' : 'Countdown – timer starts at zero';
    case 'armed': return 'Armed – timer starts on the first gate passage';
    case 'running': return 'Race running – every gate passage records a lap';
    case 'paused': return 'Paused – gate passages are ignored';
    case 'finished':
      return r.finish_reason === 'time_limit' ? 'Finished – time limit reached' : 'Finished – stopped';
    default: return '';
  }
}

function onRace(r) {
  if (race && r.version < race.version) return; // stale (e.g. late POST reply)
  const newLap = race && race.race_id === r.race_id && r.lap_count > race.lap_count;
  race = r;
  applyClock(r);
  document.body.dataset.state = r.state;

  setText(ui.badge, STATE_LABELS[r.state] || r.state.toUpperCase());
  setText(ui.detail, stateDetail(r));
  setText(ui.lapCount, String(r.lap_count));
  setText(ui.lastLap, formatLap(r.last_lap?.lap_ms));
  setText(ui.bestLap, r.best_lap ? `${formatLap(r.best_lap.lap_ms)} (#${r.best_lap.number})` : '—');

  // Buttons
  const active = ['countdown', 'armed', 'running', 'paused'].includes(r.state);
  ui.start.disabled = active;
  ui.pause.disabled = !['running', 'paused'].includes(r.state);
  setText(ui.pause, r.state === 'paused' ? 'Resume' : 'Pause');
  ui.pause.classList.toggle('btn-resume', r.state === 'paused');
  ui.stop.disabled = !active;
  ui.reset.disabled = active || (r.state === 'idle' && r.lap_count === 0);
  if (ui.reset.disabled) disarmReset();

  renderLaps(r);
  renderSettings(r);
  renderIgnored(r.ignored);
  setText($('#i-race'), STATE_LABELS[r.state] || r.state);

  if (newLap) {
    ui.clock.classList.remove('flash');
    void ui.clock.offsetWidth; // restart the animation
    ui.clock.classList.add('flash');
  }
}

function renderLaps(r) {
  const key = `${r.race_id}:${r.lap_count}:${r.best_lap?.number}`;
  if (key === renderedLapsKey) return;
  renderedLapsKey = key;

  const tbody = document.createElement('tbody');
  tbody.id = 'lap-rows';
  if (!r.laps.length) {
    const row = tbody.insertRow();
    row.className = 'empty';
    const cell = row.insertCell();
    cell.colSpan = 3;
    cell.textContent = r.state === 'armed' ? 'Waiting for the first gate passage…' : 'No laps yet';
  }
  const bestNumber = r.best_lap?.number;
  for (let i = r.laps.length - 1; i >= 0; i -= 1) {
    const lap = r.laps[i];
    const row = tbody.insertRow();
    if (lap.number === bestNumber && r.laps.length > 1) row.className = 'best';
    row.insertCell().textContent = String(lap.number);
    row.insertCell().textContent = formatLap(lap.lap_ms);
    row.insertCell().textContent = formatTime(lap.total_ms);
  }
  ui.lapRows.replaceWith(tbody);
  ui.lapRows = tbody;
}

function renderSettings(r) {
  const locked = !['idle', 'finished'].includes(r.state);
  ui.fieldset.disabled = locked;
  setText(ui.hint, locked ? 'Locked during a race' : (settingsDirty ? 'Unsaved changes' : ''));
  if (settingsDirty || ui.form.contains(document.activeElement)) return;
  const s = r.settings;
  ui.form.start_mode.value = s.start_mode;
  ui.form.countdown_s.value = s.countdown_s;
  ui.form.max_duration_s.value = s.max_duration_s;
  ui.form.min_lap_s.value = s.min_lap_s;
}

function renderIgnored(ignored) {
  const parts = Object.entries(ignored || {})
    .map(([reason, count]) => `${IGNORE_LABELS[reason] || reason}: ${count}`);
  setText($('#i-ignored'), parts.length ? parts.join(' · ') : 'none');
}

// --------------------------------------------------------------------- sensor

function onSensor(s) {
  if (!s) return;
  let status;
  if (!s.available) status = 'Unavailable';
  else if (s.fault) status = 'Fault';
  else status = 'OK';
  setText($('#i-sensor'), s.error ? `${status} – ${s.error}` : status);
  setText($('#i-beam'), s.beam ? (s.beam === 'broken' ? 'BROKEN' : 'clear') : '—');
  $('#i-beam').className = s.beam === 'broken' ? 'beam-broken' : '';
  setText($('#i-gpio'), `BCM ${s.pin} · ${s.level || '—'}`);
  const ago = s.last_trigger_ago_ms == null ? '' : ` · last ${formatDuration(Math.floor(s.last_trigger_ago_ms / 1000))} ago`;
  setText($('#i-triggers'), s.trigger_count == null ? '—' : `${s.trigger_count}${ago}`);

  const fault = s.fault || (!s.available ? (s.error || 'Gate sensor unavailable') : null);
  ui.fault.hidden = !fault;
  if (fault) setText(ui.fault, `Sensor problem: ${fault}`);
}

// ----------------------------------------------------------------- live link

function setConnected(ok) {
  ui.conn.classList.toggle('conn-ok', ok);
  setText(ui.conn, ok ? 'Live' : 'Disconnected – retrying…');
  if (!ok) document.body.dataset.state = 'connecting';
  else if (race) document.body.dataset.state = race.state;
}

function connect() {
  if (eventSource) eventSource.close();
  eventSource = new EventSource('/api/events');
  lastMessageAt = performance.now();
  eventSource.addEventListener('state', (e) => {
    lastMessageAt = performance.now();
    const data = JSON.parse(e.data);
    setConnected(true);
    onRace(data.race);
    onSensor(data.sensor);
  });
  eventSource.addEventListener('status', (e) => {
    lastMessageAt = performance.now();
    const data = JSON.parse(e.data);
    setConnected(true);
    if (race && data.race.version === race.version) applyClock(data.race);
    onSensor(data.sensor);
  });
  eventSource.onerror = () => setConnected(false);
}

setInterval(() => {
  if (performance.now() - lastMessageAt > STALE_AFTER_MS) {
    setConnected(false);
    connect();
  }
}, 1000);

// -------------------------------------------------------------------- commands

async function post(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await response.json(); } catch { /* non-JSON error */ }
  if (data.race) onRace(data.race);
  if (!response.ok || !data.ok) throw new Error(data.error || `Server returned ${response.status}`);
  return data;
}

async function command(name) {
  try {
    await post(`/api/race/${name}`);
  } catch (error) {
    showMessage(error.message);
  }
}

function disarmReset() {
  resetConfirmUntil = 0;
  ui.reset.classList.remove('confirm');
  setText(ui.reset, 'Clear laps');
}

ui.start.addEventListener('click', () => command('start'));
ui.pause.addEventListener('click', () => command(race?.state === 'paused' ? 'resume' : 'pause'));
ui.stop.addEventListener('click', () => command('stop'));
ui.reset.addEventListener('click', () => {
  if (performance.now() < resetConfirmUntil) {
    disarmReset();
    command('reset');
    return;
  }
  resetConfirmUntil = performance.now() + RESET_CONFIRM_MS;
  ui.reset.classList.add('confirm');
  setText(ui.reset, 'Tap again to clear');
  setTimeout(() => { if (performance.now() >= resetConfirmUntil) disarmReset(); }, RESET_CONFIRM_MS + 50);
});

ui.form.addEventListener('input', () => {
  settingsDirty = true;
  setText(ui.hint, 'Unsaved changes');
});

ui.form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const values = {
    start_mode: ui.form.start_mode.value,
    countdown_s: Number(ui.form.countdown_s.value),
    max_duration_s: Number(ui.form.max_duration_s.value),
    min_lap_s: Number(ui.form.min_lap_s.value),
  };
  for (const [key, value] of Object.entries(values)) {
    if (key !== 'start_mode' && (ui.form[key].value === '' || !Number.isFinite(value))) {
      showMessage('Please fill in all settings with numbers.');
      return;
    }
  }
  try {
    settingsDirty = false;
    await post('/api/settings', values);
    if (document.activeElement) document.activeElement.blur();
    if (race) renderSettings(race);
    setText(ui.hint, 'Saved');
  } catch (error) {
    settingsDirty = true;
    showMessage(error.message);
  }
});

// ---------------------------------------------------------------- system info

async function refreshSystem() {
  try {
    const response = await fetch('/api/system', { cache: 'no-store' });
    const data = await response.json();
    const s = data.system;
    setText($('#i-host'), s.hostname || '—');
    setText($('#i-ip'), s.ip_addresses?.length ? s.ip_addresses.join(', ') : '—');
    setText($('#i-wifi'), s.wifi_ssid || '—');
    setText($('#i-model'), s.model || '—');
    setText($('#i-temp'), s.cpu_temp_c == null ? '—' : `${s.cpu_temp_c.toFixed(1)} °C`);
    setText($('#i-load'), s.load_avg ? s.load_avg.join(' / ') : '—');
    setText($('#i-uptime'), `${formatDuration(s.uptime_s)} (server ${formatDuration(s.server_uptime_s)})`);
  } catch {
    /* shown as disconnected by the SSE watchdog */
  }
}

connect();
refreshSystem();
setInterval(refreshSystem, SYSTEM_POLL_MS);
requestAnimationFrame(renderClock);
