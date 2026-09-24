/* Sanctions ingestion console. Vanilla JS, no inline scripts (CSP script-src 'self').
 * Untrusted strings are only ever inserted with textContent / text nodes - never innerHTML. */
(() => {
  'use strict';

  // ================================================================ basics
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const BODY = document.body;
  const ROLE_RANK = { viewer: 0, operator: 1, reviewer: 2, admin: 3 };
  const can = (role) => (ROLE_RANK[BODY.dataset.role] ?? -1) >= ROLE_RANK[role];

  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') n.className = v;
      else if (k === 'text') n.textContent = String(v);
      else if (k.startsWith('on') && typeof v === 'function') n.addEventListener(k.slice(2), v);
      else if (k === 'dataset') Object.assign(n.dataset, v);
      else if (k === 'href') n.setAttribute('href', safeHref(v));
      else if (v === true) n.setAttribute(k, '');
      else n.setAttribute(k, String(v));
    }
    for (const c of kids.flat(Infinity)) {
      if (c === null || c === undefined || c === false) continue;
      n.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return n;
  }

  function safeHref(u) {
    const s = String(u || '');
    if (s.startsWith('/') && !s.startsWith('//')) return s;
    try {
      const url = new URL(s);
      return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : '#';
    } catch (_) {
      return '#';
    }
  }

  function extLink(url, label) {
    return el('a', { href: url, target: '_blank', rel: 'noopener noreferrer' }, label || url);
  }

  function clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
    return node;
  }

  function mount(target, ...nodes) {
    const t = typeof target === 'string' ? $(target) : target;
    if (!t) return null;
    clear(t);
    t.append(...nodes.flat(Infinity).filter((n) => n !== null && n !== undefined && n !== false));
    return t;
  }

  async function api(path, opts = {}) {
    const init = { method: opts.method || 'GET', headers: { Accept: 'application/json', ...(opts.headers || {}) } };
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    const res = await fetch(path, init);
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (_) { data = text; }
    if (!res.ok) {
      let msg = `${res.status} ${res.statusText}`;
      if (data && typeof data === 'object') {
        if (Array.isArray(data.detail)) msg = data.detail.map((d) => `${(d.loc || []).slice(1).join('.')}: ${d.msg}`).join('; ');
        else if (data.detail) msg = String(data.detail);
      }
      const err = new Error(msg);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function debounce(fn, ms) {
    let t = null;
    return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  }

  function store(key, value) {
    try {
      if (value === undefined) return sessionStorage.getItem(key);
      if (value === null) sessionStorage.removeItem(key); else sessionStorage.setItem(key, value);
    } catch (_) { /* storage unavailable: behave statelessly */ }
    return null;
  }

  // ================================================================ formatting
  const NF = new Intl.NumberFormat();
  const fmtNum = (n) => (n === null || n === undefined || n === '' ? '—' : NF.format(Number(n)));
  const fmtCompact = (n) => new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(n);
  const fmtPct = (x, d = 1) => (x === null || x === undefined ? '—' : `${(Number(x) * 100).toFixed(d)}%`);
  const fmtUsd = (x, d = 2) => (x === null || x === undefined ? '—' : `$${Number(x).toFixed(d)}`);
  const short = (s, n = 12) => (s ? String(s).slice(0, n) : '—');

  function fmtTime(iso, tz) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    const o = { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' };
    if (tz) { try { return d.toLocaleString(undefined, { ...o, timeZone: tz, timeZoneName: 'short' }); } catch (_) { /* bad tz */ } }
    return d.toLocaleString(undefined, o);
  }

  function fmtAgo(iso) {
    if (!iso) return '—';
    const diff = (new Date(iso).getTime() - Date.now()) / 1000;
    const a = Math.abs(diff);
    let v;
    if (a < 60) v = `${Math.round(a)} s`;
    else if (a < 3600) v = `${Math.round(a / 60)} min`;
    else if (a < 172800) v = `${(a / 3600).toFixed(1)} h`;
    else v = `${Math.round(a / 86400)} d`;
    return diff >= 0 ? `in ${v}` : `${v} ago`;
  }

  function fmtDue(iso) {
    if (!iso) return '—';
    const diff = (new Date(iso).getTime() - Date.now()) / 1000;
    if (diff >= 0) return fmtAgo(iso);
    return diff > -120 ? 'due now' : `overdue ${fmtAgo(iso).replace(' ago', '')}`;
  }

  function fmtMarker(m) {
    if (!m) return null;
    return /^\d{4}-\d{2}-\d{2}T/.test(m) ? fmtTime(m) : m;
  }

  function fmtHours(h) {
    if (h === null || h === undefined) return '—';
    const x = Number(h);
    if (x < 1) return `${Math.round(x * 60)} min`;
    if (x < 48) return `${x.toFixed(1)} h`;
    return `${(x / 24).toFixed(1)} d`;
  }

  function fmtDur(sec) {
    if (sec === null || sec === undefined) return '—';
    const s = Number(sec);
    if (s < 90) return `${s.toFixed(0)} s`;
    if (s < 5400) return `${(s / 60).toFixed(1)} min`;
    return `${(s / 3600).toFixed(1)} h`;
  }

  function fmtBytes(b) {
    if (b === null || b === undefined) return '—';
    const u = ['B', 'KB', 'MB', 'GB'];
    let x = Number(b);
    let i = 0;
    while (x >= 1024 && i < u.length - 1) { x /= 1024; i += 1; }
    return `${x.toFixed(i ? 1 : 0)} ${u[i]}`;
  }

  function fmtMinutes(m) {
    if (!m) return '—';
    const x = Number(m);
    if (x % 1440 === 0) return x === 1440 ? 'day' : `${x / 1440} days`;
    if (x % 60 === 0) return x === 60 ? 'hour' : `${x / 60} h`;
    return `${x} min`;
  }

  const scheduleText = (s) => (s.schedule_kind === 'CRON'
    ? `cron ${s.cron_expr || '?'} (${s.timezone || 'UTC'})` : `every ${fmtMinutes(s.cadence_minutes)}`);

  // ================================================================ badges (status colour never alone: icon + label)
  const BADGES = {
    health: {
      healthy: ['good', '✓', 'Healthy'], degraded: ['warning', '!', 'Degraded'],
      stale_warning: ['warning', '!', 'Stale (warning)'], stale: ['critical', '✕', 'Stale'],
      never_succeeded: ['serious', '!', 'Never succeeded'], paused: ['', '‖', 'Paused'],
      disabled: ['', '⏻', 'Disabled'], draft: ['', '✎', 'Draft'],
    },
    run: {
      QUEUED: ['info', '…', 'Queued'], RUNNING: ['info', '▶', 'Running'], SUCCEEDED: ['good', '✓', 'Succeeded'],
      NO_CHANGE: ['good', '✓', 'No change'], DRY_RUN_OK: ['good', '✓', 'Dry run OK'],
      HELD: ['warning', '‖', 'Held for review'], QUARANTINED: ['serious', '!', 'Quarantined'],
      FAILED: ['critical', '✕', 'Failed'], ABANDONED: ['serious', '↺', 'Abandoned'], CANCELLED: ['', '–', 'Cancelled'],
    },
    breaker: { CLOSED: ['good', '✓', 'Closed'], HALF_OPEN: ['warning', '!', 'Half-open'], OPEN: ['critical', '✕', 'Open'] },
    severity: {
      FAIL: ['critical', '✕', 'Fail'], PAGE: ['critical', '✕', 'Page'], WARN: ['warning', '!', 'Warn'],
      INFO: ['info', 'i', 'Info'], OK: ['good', '✓', 'OK'],
    },
    source: {
      ACTIVE: ['good', '✓', 'Active'], PAUSED: ['warning', '‖', 'Paused'], DISABLED: ['', '⏻', 'Disabled'],
      DRAFT: ['', '✎', 'Draft'],
    },
    proposal: {
      PENDING: ['info', '…', 'Pending'], APPROVED: ['good', '✓', 'Approved'], APPLIED: ['good', '✓', 'Applied'],
      REJECTED: ['', '✕', 'Rejected'], EXPIRED: ['', '–', 'Expired'], ACTIVE: ['good', '✓', 'Active'],
      SUPERSEDED: ['', '–', 'Superseded'], PENDING_APPROVAL: ['warning', '…', 'Awaiting approval'],
    },
    incident: { OPEN: ['critical', '!', 'Open'], ACK: ['warning', '✓', 'Acknowledged'], RESOLVED: ['good', '✓', 'Resolved'] },
    cycle: {
      SUCCEEDED: ['good', '✓', 'Succeeded'], COMPLETED: ['good', '✓', 'Completed'], RUNNING: ['info', '▶', 'Running'],
      FAILED: ['critical', '✕', 'Failed'], FALLBACK: ['warning', '↺', 'Autopilot fallback'],
      SKIPPED: ['', '–', 'Skipped'], BUDGET_EXCEEDED: ['warning', '$', 'Over budget'], TIMEOUT: ['serious', '!', 'Timed out'],
      REFUSED: ['', '–', 'Declined'],
    },
    version: {
      PUBLISHED: ['good', '✓', 'Published'], SUPERSEDED: ['', '–', 'Superseded'], VALIDATED: ['info', '…', 'Validated'],
      HELD: ['warning', '‖', 'Held'], QUARANTINED: ['serious', '!', 'Quarantined'], REJECTED: ['', '✕', 'Rejected'],
    },
  };

  function badge(kind, value) {
    const v = value === null || value === undefined ? '' : String(value);
    const spec = (BADGES[kind] || {})[v] || ['', '', v.replaceAll('_', ' ').toLowerCase() || '—'];
    return el('span', { class: `badge ${spec[0]}`.trim() }, spec[1] ? el('span', { class: 'ico', 'aria-hidden': 'true' }, spec[1]) : null, spec[2]);
  }

  const tag = (text) => el('span', { class: 'tag' }, text);

  // ================================================================ generic table / key-value / meter
  function table(columns, rows, opts = {}) {
    if (!rows || !rows.length) return el('div', { class: 'empty' }, opts.empty || 'Nothing to show.');
    const thead = el('thead', {}, el('tr', {}, columns.map((c) => el('th', { class: c.num ? 'num' : null }, c.label))));
    const tbody = el('tbody');
    for (const r of rows) {
      if (r && r.__group) {
        tbody.append(el('tr', { class: 'group' }, el('td', { colspan: columns.length }, r.__group)));
        continue;
      }
      const tr = el('tr', { class: opts.onRowClick ? 'clickable' : null });
      for (const c of columns) {
        const v = c.render ? c.render(r) : r[c.key];
        tr.append(el('td', { class: c.num ? 'num' : null }, v === null || v === undefined || v === '' ? '—' : v));
      }
      if (opts.onRowClick) {
        tr.tabIndex = 0;
        tr.addEventListener('click', (e) => { if (!e.target.closest('button, a, input, select')) opts.onRowClick(r); });
        tr.addEventListener('keydown', (e) => { if (e.key === 'Enter') opts.onRowClick(r); });
      }
      tbody.append(tr);
    }
    return el('div', { class: 'table-wrap' }, el('table', {}, thead, tbody));
  }

  function fillTbody(tableSel, columns, rows, opts = {}) {
    const tb = $(`${tableSel} tbody`);
    if (!tb) return;
    clear(tb);
    const built = table(columns, rows, opts);
    if (built.classList.contains('empty')) {
      tb.append(el('tr', {}, el('td', { colspan: columns.length }, built)));
      return;
    }
    tb.append(...$$('tbody > tr', built));
  }

  function kv(pairs) {
    const dl = el('dl', { class: 'kv' });
    for (const [k, v] of pairs) {
      if (v === undefined) continue;
      dl.append(el('dt', {}, k), el('dd', {}, v === null || v === '' ? '—' : v));
    }
    return dl;
  }

  function meter(value, floor, status) {
    const pct = Math.max(0, Math.min(1, Number(value) || 0));
    const cls = status === 'FAIL' ? 'fail' : status === 'WARN' ? 'warn' : '';
    const m = el('div', { class: `meter ${cls}`.trim(), role: 'img',
      'aria-label': `fill ${fmtPct(value)}${floor !== null && floor !== undefined ? `, floor ${fmtPct(floor)}` : ''}` });
    const fill = el('span', { class: 'fill' });
    fill.style.width = `${pct * 100}%`;
    m.append(fill);
    if (floor !== null && floor !== undefined) {
      const f = el('span', { class: 'floor', title: `floor ${fmtPct(floor)}` });
      f.style.left = `calc(${Math.min(1, Number(floor)) * 100}% - 1px)`;
      m.append(f);
    }
    return el('div', { class: 'meter-cell' }, m, el('span', { class: 'pct' }, fmtPct(value)));
  }

  function jsonBlock(obj) {
    return el('pre', { class: 'json' }, JSON.stringify(obj, null, 2));
  }

  function errorBox(e) {
    return el('div', { class: 'error' }, `Could not load: ${e.message || e}`);
  }

  // ================================================================ dialogs (built in JS; shared by pages)
  function formDialog({ title, intro, fields, submitLabel = 'Save', onSubmit, onReady }) {
    const dlg = el('dialog');
    const form = el('form', { method: 'dialog' });
    const inputs = {};
    const rows = {};
    form.append(el('h2', {}, title));
    if (intro) form.append(el('p', { class: 'sub' }, intro));
    for (const f of fields) {
      let input;
      if (f.type === 'select') {
        input = el('select', { id: `dlg-${f.id}` }, (f.options || []).map((o) => {
          const [val, label] = Array.isArray(o) ? o : [o, o];
          return el('option', { value: val, selected: String(val) === String(f.value) }, label);
        }));
      } else if (f.type === 'checkbox') {
        input = el('input', { type: 'checkbox', id: `dlg-${f.id}`, checked: !!f.value });
      } else if (f.type === 'textarea') {
        input = el('textarea', { id: `dlg-${f.id}` });
        input.value = f.value || '';
      } else {
        input = el('input', { type: f.type || 'text', id: `dlg-${f.id}`, min: f.min, max: f.max, step: f.step,
          maxlength: f.maxlength || 500, placeholder: f.placeholder });
        if (f.value !== undefined && f.value !== null) input.value = f.value;
      }
      inputs[f.id] = input;
      const row = f.type === 'checkbox'
        ? el('label', { class: 'row' }, input, f.label)
        : el('label', { class: 'field' }, f.label, input);
      if (f.hidden) row.classList.add('hidden');
      rows[f.id] = row;
      form.append(row);
      if (f.help) form.append(el('p', { class: 'sub' }, f.help));
    }
    const err = el('p', { class: 'error', role: 'alert' });
    const cancel = el('button', { type: 'button' }, 'Cancel');
    const submit = el('button', { class: 'primary', type: 'submit' }, submitLabel);
    form.append(err, el('div', { class: 'row' }, el('span', { class: 'spacer' }), cancel, submit));
    dlg.append(form);
    const close = () => { dlg.close(); dlg.remove(); };
    cancel.addEventListener('click', close);
    dlg.addEventListener('cancel', () => setTimeout(() => dlg.remove(), 0));
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      err.textContent = '';
      submit.disabled = true;
      const values = {};
      for (const [k, i] of Object.entries(inputs)) values[k] = i.type === 'checkbox' ? i.checked : i.value.trim();
      try {
        await onSubmit(values);
        close();
      } catch (e) {
        err.textContent = e.message || String(e);
      } finally {
        submit.disabled = false;
      }
    });
    document.body.append(dlg);
    if (onReady) onReady(inputs, rows);
    dlg.showModal();
    return { dlg, inputs, rows };
  }

  function openStatusDialog(src, target, after) {
    const labels = { ACTIVE: src.status === 'DISABLED' ? 'Enable' : 'Resume', PAUSED: 'Pause', DISABLED: 'Disable' };
    const notes = [];
    if (src.is_core && target !== 'ACTIVE') notes.push('This is a core list: while it is not active a banner shows on every page and a warning alert repeats.');
    if (target === 'DISABLED') notes.push('Disabled sources raise no staleness alerts and are never pulled until re-enabled by an admin.');
    formDialog({
      title: `${labels[target]} ${src.display_name || src.source_id}`,
      intro: notes.join(' ') || null,
      submitLabel: labels[target],
      fields: [
        { id: 'pause_hours', label: 'Pause for (hours; blank = until resumed)', type: 'number', min: 0.25, step: 0.25, hidden: target !== 'PAUSED' },
        { id: 'reason', label: target === 'ACTIVE' ? 'Reason (optional)' : 'Reason (required)', maxlength: 500 },
      ],
      onSubmit: async (v) => {
        if (target !== 'ACTIVE' && v.reason.length < 3) throw new Error('Give a reason (at least 3 characters).');
        const body = { status: target, reason: v.reason || null };
        if (target === 'PAUSED' && v.pause_hours) body.pause_hours = Number(v.pause_hours);
        await api(`/api/sources/${encodeURIComponent(src.source_id)}/status`, { method: 'POST', body });
        banners();
        if (after) after();
      },
    });
  }

  function openRunDialog(src, after) {
    const isEnrichment = src.kind === 'ENRICHMENT';
    const modes = [['normal', 'Normal'], ['force_refetch', 'Force re-fetch (ignore conditional GET / same hash)'],
      ['dry_run', 'Dry run (validate, never publish)']];
    if (src.kind === 'STRUCTURED_LIST' || src.kind === 'CURATED_LIST') modes.push(['reparse', 'Re-parse an archived file (no download)']);
    if (src.status === 'DRAFT') modes.splice(0, 2);
    formDialog({
      title: `Run ${src.display_name || src.source_id} now`,
      intro: src.status === 'DRAFT' ? 'Draft sources can only be dry-run until they are activated.' : null,
      submitLabel: 'Queue run',
      fields: [
        { id: 'mode', label: 'Mode', type: 'select', options: modes, value: modes[0][0] },
        { id: 'reparse_sha256', label: 'Archived file', type: 'select', options: [], hidden: true },
        { id: 'limit', label: 'Limit (enrichment subjects)', type: 'number', min: 1, hidden: !isEnrichment },
        { id: 'reason', label: 'Reason (required)', maxlength: 500 },
        { id: 'override', label: 'Override the politeness interval (admin; pulls stay at least 5 minutes apart)', type: 'checkbox', hidden: !can('admin') },
      ],
      onReady: (inputs, rows) => {
        let loaded = false;
        inputs.mode.addEventListener('change', async () => {
          const rp = inputs.mode.value === 'reparse';
          rows.reparse_sha256.classList.toggle('hidden', !rp);
          if (rp && !loaded) {
            loaded = true;
            try {
              const d = await api(`/api/sources/${encodeURIComponent(src.source_id)}`);
              const arts = d.archived_artifacts || [];
              mount(inputs.reparse_sha256, arts.length
                ? arts.map((a) => el('option', { value: a.sha256 }, `v${a.seq} · ${a.status} · ${fmtTime(a.created_at)} · ${short(a.sha256)}`))
                : [el('option', { value: '' }, 'No archived files yet')]);
            } catch (e) {
              mount(inputs.reparse_sha256, el('option', { value: '' }, `Could not load: ${e.message}`));
            }
          }
        });
      },
      onSubmit: async (v) => {
        if (v.reason.length < 3) throw new Error('Give a reason (at least 3 characters).');
        const body = { mode: v.mode, reason: v.reason, override_min_interval: !!v.override };
        if (v.mode === 'reparse') {
          if (!v.reparse_sha256) throw new Error('Pick an archived file to re-parse.');
          body.reparse_sha256 = v.reparse_sha256;
        }
        if (v.limit) body.limit = Number(v.limit);
        const res = await api(`/api/sources/${encodeURIComponent(src.source_id)}/runs`, { method: 'POST', body });
        if (after) after(res); else window.location.href = `/ui/runs/${res.run_id}`;
      },
    });
  }

  function noteDialog(title, submitLabel, onSubmit) {
    formDialog({ title, submitLabel, fields: [{ id: 'note', label: 'Note', maxlength: 1000 }], onSubmit });
  }

  // ================================================================ live progress (SSE <- Postgres LISTEN/NOTIFY)
  const live = { handlers: new Set(), resyncs: new Set(), es: null };
  /** fn(progressEvent) for every notification; resync() whenever the stream (re)connects - reload state from the
   * API then, because anything that happened while not listening was not delivered. */
  function onProgress(fn, resync) {
    live.handlers.add(fn);
    if (resync) live.resyncs.add(resync);
    if (!live.es && 'EventSource' in window) {
      live.es = new EventSource('/api/stream/runs');
      live.es.addEventListener('ready', () => {
        BODY.dataset.live = 'on';
        live.resyncs.forEach((r) => { try { r(); } catch (err) { console.error(err); } });
      });
      live.es.addEventListener('error', () => { BODY.dataset.live = 'off'; });
      live.es.addEventListener('progress', (e) => {
        let d;
        try { d = JSON.parse(e.data); } catch (_) { return; }
        live.handlers.forEach((h) => { try { h(d); } catch (err) { console.error(err); } });
      });
    }
  }
  const TERMINAL = new Set(['SUCCEEDED', 'NO_CHANGE', 'DRY_RUN_OK', 'HELD', 'QUARANTINED', 'FAILED', 'ABANDONED', 'CANCELLED']);
  const PIPELINE = ['FETCH', 'ARCHIVE', 'VALIDATE_FILE', 'PARSE', 'VALIDATE_DATA', 'DIFF_PUBLISH'];

  function stepsFor(kind) {
    if (kind === 'RELEASE_HELD') return ['DIFF_PUBLISH'];
    if (kind === 'NOTICE_SYNC') return ['SYNC'];
    if (kind === 'ENRICHMENT_BATCH') return ['ENRICH'];
    return PIPELINE;
  }

  const SUCCESS = new Set(['SUCCEEDED', 'NO_CHANGE', 'DRY_RUN_OK']);

  /** done: optional Set of step names with a DONE checkpoint (run detail); otherwise inferred from the current step. */
  function stepPills(kind, current, status, done) {
    const steps = stepsFor(kind);
    const idx = steps.indexOf(current);
    return el('div', { class: 'steps', 'aria-label': `step ${current || 'not started'}` }, steps.map((s, i) => {
      let cls = 'step';
      const isDone = done ? done.has(s) : (SUCCESS.has(status) || i < idx);
      if (isDone) cls += ' done';
      else if (s === current && !SUCCESS.has(status)) cls += TERMINAL.has(status) ? ' current stopped' : ' current';
      return el('span', { class: cls, title: isDone ? 'completed' : s === current ? `stopped here (${status})` : 'not reached' },
        s.replaceAll('_', ' ').toLowerCase());
    }));
  }

  function progressBar(pct) {
    const p = Math.max(0, Math.min(100, Number(pct) || 0));
    const bar = el('div', { class: 'progress', role: 'progressbar', 'aria-valuemin': 0, 'aria-valuemax': 100, 'aria-valuenow': p.toFixed(0) });
    const s = el('span');
    s.style.width = `${p}%`;
    bar.append(s);
    return bar;
  }

  function progressFacts(r) {
    const bits = [];
    if (r.bytes_done) bits.push(`${fmtBytes(r.bytes_done)}${r.bytes_total ? ` of ${fmtBytes(r.bytes_total)}` : ''}`);
    if (r.records_done) bits.push(`${fmtNum(r.records_done)}${r.records_expected ? ` of ${fmtNum(r.records_expected)}` : ''} records`);
    if (r.items_done) bits.push(`${fmtNum(r.items_done)}${r.items_total ? ` of ${fmtNum(r.items_total)}` : ''} items`);
    if (r.retries) bits.push(`${r.retries} retr${r.retries === 1 ? 'y' : 'ies'}`);
    const eta = r.eta_s ?? r.eta_seconds;
    if (eta) bits.push(`ETA ${fmtDur(eta)}`);
    return bits.join(' · ');
  }

  function liveCard(r) {
    const step = r.step || r.current_step;
    return el('div', { class: 'run-live', dataset: { runId: r.run_id } },
      el('div', { class: 'row' },
        el('strong', {}, r.source_id), tag((r.run_kind || 'LIST_INGEST').replaceAll('_', ' ').toLowerCase()),
        badge('run', r.status), el('span', { class: 'spacer' }),
        el('span', { class: 'sub' }, r.pct !== undefined && r.pct !== null ? `${Number(r.pct).toFixed(0)}%` : ''),
        el('a', { href: `/ui/runs/${r.run_id}` }, 'Details')),
      stepPills(r.run_kind, step, r.status),
      progressBar(r.pct),
      el('div', { class: 'sub', style: 'margin-top:4px' }, progressFacts(r)));
  }

  function miniProgress(r) {
    if (!r) return null;
    const step = r.step || r.current_step;
    return el('div', { class: 'mini-progress' },
      el('span', {}, `${r.status === 'QUEUED' ? 'queued' : (step || 'starting').replaceAll('_', ' ').toLowerCase()}${r.pct ? ` · ${Number(r.pct).toFixed(0)}%` : ''}`),
      progressBar(r.pct));
  }

  // ================================================================ charts (Chart.js; dataviz rules)
  const charts = {};
  const redraws = {};
  const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const SERIES = ['--series-1', '--series-2', '--series-3'];

  function chartDefaults() {
    if (!window.Chart) return;
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    Chart.defaults.font.size = 12;
    Chart.defaults.color = cssVar('--text-secondary');
  }

  function baseOptions({ stacked = false, yFormat = fmtCompact, tipFormat = fmtNum, yLabel } = {}) {
    return {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: cssVar('--surface-1'), titleColor: cssVar('--text-primary'), bodyColor: cssVar('--text-secondary'),
          borderColor: cssVar('--border-strong'), borderWidth: 1, padding: 10, boxPadding: 4, usePointStyle: true,
          callbacks: { label: (c) => ` ${c.dataset.label}: ${tipFormat(c.parsed.y)}` },
        },
      },
      scales: {
        x: { stacked, grid: { display: false }, border: { color: cssVar('--axis') },
          ticks: { color: cssVar('--muted'), maxRotation: 0, autoSkip: true, maxTicksLimit: 8 } },
        y: { stacked, beginAtZero: true, grid: { color: cssVar('--grid'), lineWidth: 1 }, border: { display: false },
          ticks: { color: cssVar('--muted'), callback: (v) => yFormat(v), maxTicksLimit: 6 },
          title: yLabel ? { display: true, text: yLabel, color: cssVar('--text-secondary') } : { display: false } },
      },
    };
  }

  function lineDataset(label, data, slot) {
    const c = cssVar(SERIES[slot]);
    return { label, data, borderColor: c, backgroundColor: c, borderWidth: 2, tension: 0,
      pointRadius: data.length < 2 ? 4 : 0, pointHoverRadius: 5, pointHoverBorderWidth: 2,
      pointHoverBorderColor: cssVar('--surface-1'), pointHitRadius: 12 };
  }

  function barDataset(label, data, slot, stacked) {
    return { label, data, backgroundColor: cssVar(SERIES[slot]), borderColor: cssVar('--surface-1'),
      borderWidth: stacked ? { top: 2 } : 0, borderSkipped: 'start', borderRadius: 4, maxBarThickness: 24 };
  }

  /** spec: {type:'line'|'bar', labels, series:[{name, values}], stacked?, yFormat?, tipFormat?, yLabel?, title} */
  function drawChart(canvasId, spec) {
    const canvas = document.getElementById(canvasId);
    if (!canvas || !window.Chart) return;
    const render = () => {
      if (charts[canvasId]) charts[canvasId].destroy();
      chartDefaults();
      const series = spec.series.slice(0, SERIES.length);
      const datasets = series.map((s, i) => (spec.type === 'line' ? lineDataset(s.name, s.values, i) : barDataset(s.name, s.values, i, !!spec.stacked)));
      canvas.setAttribute('role', 'img');
      canvas.setAttribute('aria-label', `${spec.title || 'chart'}; the table view has the same data`);
      charts[canvasId] = new Chart(canvas, { type: spec.type, data: { labels: spec.labels, datasets }, options: baseOptions(spec) });
    };
    redraws[canvasId] = render;
    render();
  }

  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => Object.values(redraws).forEach((f) => f()));

  function legend(target, names) {
    const t = typeof target === 'string' ? $(target) : target;
    if (!t) return;
    if (names.length < 2) { clear(t); return; }
    mount(t, names.slice(0, SERIES.length).map((n, i) => {
      const key = el('span', { class: 'key', 'aria-hidden': 'true' });
      key.style.background = `var(${SERIES[i]})`;
      return el('span', {}, key, n);
    }));
  }

  function chartTable(labels, series, fmt = fmtNum, labelHead = '') {
    const cols = [{ label: labelHead, render: (r) => r.label }, ...series.map((s, i) => ({ label: s.name, num: true, render: (r) => fmt(r.values[i]) }))];
    return table(cols, labels.map((l, j) => ({ label: l, values: series.map((s) => s.values[j]) })));
  }

  function wireToggles() {
    $$('[data-toggle]').forEach((b) => {
      b.addEventListener('click', () => {
        const key = b.dataset.toggle;
        const box = $(`#${key}-chart-box`);
        const tbl = $(`#${key}-table`);
        if (!box || !tbl) return;
        const showTable = tbl.classList.contains('hidden');
        tbl.classList.toggle('hidden', !showTable);
        box.classList.toggle('hidden', showTable);
        b.textContent = showTable ? 'Chart view' : 'Table view';
      });
    });
  }

  // ================================================================ safe minimal markdown (answers from the analyst agent)
  function inlineMd(text) {
    const frag = document.createDocumentFragment();
    const re = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*)/g;
    let last = 0;
    let m;
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) frag.append(document.createTextNode(text.slice(last, m.index)));
      const tok = m[0];
      if (tok.startsWith('**')) frag.append(el('strong', {}, tok.slice(2, -2)));
      else if (tok.startsWith('`')) frag.append(el('code', {}, tok.slice(1, -1)));
      else frag.append(el('em', {}, tok.slice(1, -1)));
      last = m.index + tok.length;
    }
    if (last < text.length) frag.append(document.createTextNode(text.slice(last)));
    return frag;
  }

  function renderMarkdown(md) {
    const out = document.createDocumentFragment();
    const lines = String(md || '').replace(/\r/g, '').split('\n');
    const isTable = (l) => /^\s*\|.*\|\s*$/.test(l);
    const isUl = (l) => /^\s*[-*•]\s+/.test(l);
    const isOl = (l) => /^\s*\d+[.)]\s+/.test(l);
    const isHead = (l) => /^#{1,6}\s+/.test(l);
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i += 1; continue; }
      if (isTable(line)) {
        const rows = [];
        while (i < lines.length && isTable(lines[i])) { rows.push(lines[i]); i += 1; }
        const cells = (l) => l.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
        const body = rows.filter((r) => !/^\s*\|?\s*:?-{2,}/.test(r));
        const head = cells(body.shift() || '');
        out.append(el('div', { class: 'table-wrap' }, el('table', {},
          el('thead', {}, el('tr', {}, head.map((h) => el('th', {}, inlineMd(h))))),
          el('tbody', {}, body.map((r) => el('tr', {}, cells(r).map((c) => el('td', { class: /^[-+]?[\d.,]+%?$/.test(c) ? 'num' : null }, inlineMd(c)))))))));
        continue;
      }
      if (isUl(line) || isOl(line)) {
        const ordered = isOl(line);
        const list = el(ordered ? 'ol' : 'ul');
        while (i < lines.length && (ordered ? isOl(lines[i]) : isUl(lines[i]))) {
          list.append(el('li', {}, inlineMd(lines[i].replace(ordered ? /^\s*\d+[.)]\s+/ : /^\s*[-*•]\s+/, ''))));
          i += 1;
        }
        out.append(list);
        continue;
      }
      if (isHead(line)) {
        const level = Math.min(4, Math.max(3, line.match(/^#+/)[0].length + 2));
        out.append(el(`h${level}`, {}, inlineMd(line.replace(/^#+\s+/, ''))));
        i += 1;
        continue;
      }
      const para = [];
      while (i < lines.length && lines[i].trim() && !isTable(lines[i]) && !isUl(lines[i]) && !isOl(lines[i]) && !isHead(lines[i])) {
        para.push(lines[i].trim());
        i += 1;
      }
      out.append(el('p', {}, inlineMd(para.join(' '))));
    }
    return out;
  }

  // ================================================================ banners (every page)
  async function banners(ov) {
    try {
      const d = ov || await api('/api/overview');
      const items = [];
      if (d.maintenance && d.maintenance.enabled) {
        items.push(el('div', { class: 'banner warn', role: 'status' }, el('span', { class: 'ico' }, '!'),
          el('span', {}, `Maintenance mode is on — no new pulls are started.${d.maintenance.reason ? ` Reason: ${d.maintenance.reason}` : ''}`)));
      }
      for (const s of d.core_disabled || []) {
        items.push(el('div', { class: 'banner crit', role: 'alert' }, el('span', { class: 'ico' }, '✕'),
          el('span', {}, `Core list ${s.source_id} is ${String(s.status).toLowerCase()} — screening coverage is reduced.${s.status_reason ? ` Reason: ${s.status_reason}` : ''}`)));
      }
      mount('#banners', items);
      return d;
    } catch (e) {
      mount('#banners', el('div', { class: 'banner crit' }, el('span', { class: 'ico' }, '✕'), el('span', {}, `API unavailable: ${e.message}`)));
      return null;
    }
  }

  // ================================================================ page: overview
  async function initOverview() {
    const render = async () => {
      const d = await banners();
      if (!d) return;
      const srcs = d.sources || [];
      const active = new Map((d.active_runs || []).map((r) => [r.source_id, r]));
      const count = (fn) => srcs.filter(fn).length;
      const activeSrc = srcs.filter((s) => s.status === 'ACTIVE');
      const inc = d.open_incidents || {};
      const tiles = [
        ['Healthy sources', `${count((s) => s.health === 'healthy')} / ${activeSrc.length}`, 'active sources'],
        ['Stale or failing', count((s) => ['stale', 'stale_warning', 'never_succeeded', 'degraded'].includes(s.health)), 'need attention'],
        ['Runs in progress', (d.active_runs || []).length, 'queued or running'],
        ['Open incidents', (inc.PAGE || 0) + (inc.WARN || 0) + (inc.INFO || 0), `${inc.PAGE || 0} page · ${inc.WARN || 0} warn`],
        ['Pending reviews', d.pending_reviews ?? 0, 'proposals'],
        ['Held removals', d.held_removals ?? 0, 'still blocking'],
        ['LLM cost today', fmtUsd(d.llm_cost_today_usd), 'supervisor + analyst'],
      ];
      mount('#kpis', tiles.map(([l, v, h]) => el('div', { class: 'tile' }, el('div', { class: 'label' }, l), el('div', { class: 'value' }, v), el('div', { class: 'hint' }, h))));
      const snap = d.snapshot;
      mount('#snapshot-info', snap ? `Screening snapshot #${snap.snapshot_id} · ${fmtAgo(snap.created_at)}` : 'No screening snapshot yet');
      const rows = [];
      let lvl = null;
      for (const s of srcs) {
        if (s.level !== lvl) {
          lvl = s.level;
          rows.push({ __group: lvl === 1 ? 'Level 1 — official lists' : 'Level 2 — notices & free enrichment' });
        }
        rows.push(s);
      }
      fillTbody('#sources-table', [
        { label: 'Source', render: (s) => el('span', {}, el('a', { href: `/ui/manage/sources/${s.source_id}` }, s.display_name), s.is_core ? el('span', {}, ' ', tag('core')) : null) },
        { label: 'Health', render: (s) => badge('health', s.health) },
        { label: 'Since success', num: true, render: (s) => fmtHours(s.hours_since_success) },
        { label: 'Since change', num: true, render: (s) => fmtHours(s.hours_since_change) },
        { label: 'Breaker', render: (s) => badge('breaker', s.breaker_state) },
        { label: 'Records', num: true, render: (s) => fmtNum(s.record_count) },
        { label: 'Version', render: (s) => (s.current_version_seq ? el('span', { title: s.publication_marker || '' }, `v${s.current_version_seq}`, s.publication_marker ? el('div', { class: 'sub' }, fmtMarker(s.publication_marker)) : null) : '—') },
        { label: 'Next due', render: (s) => (s.status === 'ACTIVE' ? fmtDue(s.next_due_at) : '—') },
        { label: 'Active run', render: (s) => el('div', { dataset: { liveSource: s.source_id } }, miniProgress(active.get(s.source_id))) },
      ], rows);
      mount('#updated-at', `updated ${fmtTime(new Date().toISOString())}`);
      const a = d.last_agent_cycle;
      mount('#agent-last', a ? [
        el('div', { class: 'row' }, badge('cycle', a.status), a.fallback_used ? tag('autopilot fallback') : null, el('span', { class: 'sub' }, fmtAgo(a.started_at))),
        el('p', {}, a.summary || a.error || 'No summary recorded.'),
        kv([['Trigger', a.trigger], ['Tool calls', `${fmtNum(a.tool_calls)} (${fmtNum(a.denied_calls)} denied by guards)`],
          ['Tokens', `${fmtNum(a.input_tokens)} in · ${fmtNum(a.output_tokens)} out`], ['Cost', fmtUsd(a.cost_usd, 4)]]),
        el('a', { href: '/ui/agent' }, 'All agent activity'),
      ] : el('div', { class: 'empty' }, 'The supervisor agent has not run yet (autopilot handles routine pulls).'));
    };
    const renderIncidents = async () => {
      try {
        const rows = await api('/api/incidents?status=OPEN');
        const acks = await api('/api/incidents?status=ACK');
        const incAction = (r, verb, label) => el('button', { class: 'small', onclick: () => noteDialog(`${label} incident #${r.incident_id}`, label, async (v) => {
          await api(`/api/incidents/${r.incident_id}/${verb}`, { method: 'POST', body: { note: v.note } });
          renderIncidents();
        }) }, label);
        mount('#incidents', table([
          { label: 'Severity', render: (r) => badge('severity', r.severity) },
          { label: 'Incident', render: (r) => el('div', {}, el('strong', {}, r.title),
            el('div', { class: 'sub' }, r.diagnosis || r.summary || ''),
            el('div', { class: 'sub' }, `${r.source_id || 'system'} · opened ${fmtAgo(r.opened_at)}${r.status === 'ACK' ? ' · acknowledged' : ''}`)) },
          { label: '', render: (r) => (can('operator') ? el('div', { class: 'row actions' },
            r.status === 'OPEN' ? incAction(r, 'ack', 'Acknowledge') : null, incAction(r, 'resolve', 'Resolve')) : null) },
        ], [...rows, ...acks], { empty: 'No open incidents.' }));
      } catch (e) { mount('#incidents', errorBox(e)); }
    };
    await Promise.all([render().catch((e) => mount('#kpis', errorBox(e))), renderIncidents()]);
    const refresh = debounce(render, 1500);
    onProgress((p) => {
      const cell = $(`[data-live-source="${CSS.escape(p.source_id || '')}"]`);
      if (TERMINAL.has(p.status)) { refresh(); return; }
      if (cell) mount(cell, miniProgress({ ...p, current_step: p.step }));
    }, refresh);
    setInterval(render, 60000);
  }

  // ================================================================ page: runs (list + live)
  async function initRuns() {
    banners();
    if (BODY.dataset.runId) { initRunDetail(BODY.dataset.runId); return; }
    const liveRuns = new Map();
    const renderLive = () => mount('#live-runs', liveRuns.size ? [...liveRuns.values()].map(liveCard) : el('div', { class: 'empty' }, 'No runs in progress.'));
    const loadLive = async () => {
      const d = await api('/api/overview');
      liveRuns.clear();
      (d.active_runs || []).forEach((r) => liveRuns.set(r.run_id, r));
      renderLive();
      const sel = $('#f-source');
      if (sel.options.length <= 1) (d.sources || []).forEach((s) => sel.append(el('option', { value: s.source_id }, s.display_name)));
    };
    const loadHistory = async () => {
      const q = new URLSearchParams({ limit: '200' });
      if ($('#f-source').value) q.set('source_id', $('#f-source').value);
      if ($('#f-status').value) q.set('status', $('#f-status').value);
      const rows = await api(`/api/runs?${q}`);
      fillTbody('#runs-table', [
        { label: 'Queued', render: (r) => fmtTime(r.queued_at) },
        { label: 'Source', key: 'source_id' },
        { label: 'Kind', render: (r) => tag(String(r.run_kind).replaceAll('_', ' ').toLowerCase()) },
        { label: 'Trigger', render: (r) => String(r.trigger).toLowerCase() },
        { label: 'By', key: 'requested_by' },
        { label: 'Status', render: (r) => badge('run', r.status) },
        { label: 'Duration', num: true, render: (r) => fmtDur(r.duration_seconds) },
        { label: '+ / ~ / −', num: true, render: (r) => (r.added === null && r.changed === null && r.removed === null ? '—' : `${fmtNum(r.added || 0)} / ${fmtNum(r.changed || 0)} / ${fmtNum(r.removed || 0)}`) },
        { label: 'Error', render: (r) => (r.error_class ? el('span', { title: r.error_detail || '' }, r.error_class) : null) },
      ], rows, { onRowClick: (r) => { window.location.href = `/ui/runs/${r.run_id}`; }, empty: 'No runs match.' });
    };
    $('#f-source').addEventListener('change', loadHistory);
    $('#f-status').addEventListener('change', loadHistory);
    await loadLive().catch((e) => mount('#live-runs', errorBox(e)));
    await loadHistory().catch((e) => mount('#runs-table tbody', el('tr', {}, el('td', { colspan: 9 }, errorBox(e)))));
    const refreshHistory = debounce(loadHistory, 1000);
    onProgress((p) => {
      if (TERMINAL.has(p.status)) { liveRuns.delete(p.run_id); renderLive(); refreshHistory(); return; }
      const prev = liveRuns.get(p.run_id) || {};
      liveRuns.set(p.run_id, { ...prev, ...p, current_step: p.step || prev.current_step });
      if (p.status === 'QUEUED') refreshHistory();
      renderLive();
    }, () => { loadLive().catch(() => {}); refreshHistory(); });
  }

  async function initRunDetail(runId) {
    const load = async () => {
      const d = await api(`/api/runs/${encodeURIComponent(runId)}`);
      const r = d.run;
      const cancel = $('#cancel-run');
      const activeRun = r.status === 'QUEUED' || r.status === 'RUNNING';
      cancel.classList.toggle('hidden', !(activeRun && can('operator')));
      cancel.onclick = () => noteDialog('Cancel this run?', 'Cancel run', async () => {
        await api(`/api/runs/${encodeURIComponent(runId)}/cancel`, { method: 'POST' });
        load();
      });
      const p = r.progress || {};
      mount('#run-head',
        el('div', { class: 'row' }, el('h2', { style: 'margin:0' }, el('a', { href: `/ui/manage/sources/${r.source_id}` }, r.source_id)),
          tag(String(r.run_kind).replaceAll('_', ' ').toLowerCase()), badge('run', r.status), el('span', { class: 'spacer' }),
          el('span', { class: 'sub mono' }, r.run_id)),
        activeRun ? [stepPills(r.run_kind, r.current_step, r.status), progressBar(p.pct), el('div', { class: 'sub' }, progressFacts({ ...p }))]
          : stepPills(r.run_kind, r.current_step, r.status, new Set(d.steps.filter((s) => s.status === 'DONE').map((s) => s.step))),
        kv([
          ['Trigger', `${String(r.trigger).toLowerCase()} by ${r.requested_by || 'system'}`], ['Reason', r.reason],
          ['Options', Object.keys(r.options || {}).length ? JSON.stringify(r.options) : null],
          ['Queued / started / finished', `${fmtTime(r.queued_at)} / ${fmtTime(r.started_at)} / ${fmtTime(r.finished_at)}`],
          ['Attempt', r.attempt], ['Config version', r.config_version],
          ['Resumed from', r.resumed_from_run_id ? el('a', { href: `/ui/runs/${r.resumed_from_run_id}` }, short(r.resumed_from_run_id)) : null],
          ['Agent cycle', r.agent_cycle_id ? el('a', { href: '/ui/agent' }, short(r.agent_cycle_id)) : null],
          ['Raw file (sha256)', r.raw_sha256 ? el('span', { class: 'mono' }, r.raw_sha256) : null],
          ['Error', r.error_class ? el('span', { class: 'error' }, `${r.error_class}: ${r.error_detail || ''}`) : null],
          ['Summary', r.summary && Object.keys(r.summary).length ? el('span', { class: 'mono' }, JSON.stringify(r.summary)) : null],
        ]));
      mount('#run-steps', table([
        { label: 'Step', render: (s) => s.step.replaceAll('_', ' ').toLowerCase() },
        { label: 'Attempt', num: true, key: 'attempt' },
        { label: 'Status', render: (s) => badge('run', s.status === 'DONE' ? 'SUCCEEDED' : s.status) },
        { label: 'Took', num: true, render: (s) => (s.finished_at && s.started_at ? fmtDur((new Date(s.finished_at) - new Date(s.started_at)) / 1000) : '—') },
        { label: 'Detail', render: (s) => (s.error ? el('span', { class: 'error' }, s.error) : (s.detail && Object.keys(s.detail).length ? el('details', {}, el('summary', {}, Object.keys(s.detail).slice(0, 4).join(', ')), jsonBlock(s.detail)) : null)) },
      ], d.steps, { empty: 'No steps started yet.' }));
      const v = d.version;
      mount('#run-version', v ? [
        el('div', { class: 'row' }, el('strong', {}, `Version ${v.seq}`), badge('version', v.status)),
        kv([['Records', fmtNum(v.record_count)], ['By type', v.counts_by_type ? Object.entries(v.counts_by_type).map(([k, n]) => `${k.toLowerCase()} ${fmtNum(n)}`).join(' · ') : null],
          ['Publication marker', v.publication_marker], ['Published at source', fmtTime(v.published_at_source)],
          ['Released by', v.released_by]]),
        d.changes.length ? table([{ label: 'Change', key: 'change_type' }, { label: 'Entity type', key: 'entity_type' }, { label: 'Count', num: true, render: (c) => fmtNum(c.n) }], d.changes) : null,
        v.validation_report ? el('details', {}, el('summary', {}, 'Validation report'), jsonBlock(v.validation_report)) : null,
      ] : el('div', { class: 'empty' }, 'No list version (notice/enrichment runs, or not yet validated).'));
      mount('#run-evidence', table([
        { label: '#', num: true, key: 'attempt' },
        { label: 'When', render: (e) => fmtTime(e.fetched_at) },
        { label: 'HTTP', num: true, render: (e) => (e.not_modified ? '304' : e.http_status) },
        { label: 'Final URL', render: (e) => el('span', { class: 'mono' }, e.final_url || e.requested_url) },
        { label: 'Redirects', num: true, render: (e) => (Array.isArray(e.redirect_chain) ? e.redirect_chain.length : 0) },
        { label: 'Size', num: true, render: (e) => fmtBytes(e.size_bytes) },
        { label: 'Took', num: true, render: (e) => (e.duration_ms !== null ? `${fmtNum(e.duration_ms)} ms` : '—') },
        { label: 'sha256', render: (e) => (e.sha256 ? el('span', { class: 'mono', title: e.sha256 }, short(e.sha256)) : null) },
        { label: 'TLS leaf', render: (e) => (e.tls_leaf_sha256 ? el('span', { class: 'mono', title: e.tls_leaf_sha256 }, short(e.tls_leaf_sha256)) : null) },
        { label: 'Error', render: (e) => (e.error_class ? el('span', { class: 'error', title: e.error_detail || '' }, e.error_class) : null) },
      ], d.evidence, { empty: 'No fetch attempts (re-parse, release or resumed run).' }));
      mount('#run-issues', issuesTable(d.issues));
    };
    await load().catch((e) => mount('#run-head', errorBox(e)));
    const reload = debounce(load, 800);
    onProgress((p) => { if (p.run_id === runId) reload(); }, reload);
  }

  function issuesTable(rows) {
    return table([
      { label: 'Severity', render: (i) => badge('severity', i.severity) },
      { label: 'Category', render: (i) => String(i.category).replaceAll('_', ' ').toLowerCase() },
      { label: 'Source', key: 'source_id' },
      { label: 'Field', render: (i) => [i.entity_type, i.field].filter(Boolean).join('.') || null },
      { label: 'Count', num: true, render: (i) => fmtNum(i.count) },
      { label: 'Detail', key: 'message' },
      { label: 'Last seen', render: (i) => fmtAgo(i.last_seen) },
    ], rows, { empty: 'No data-quality issues.' });
  }

  // ================================================================ page: data quality
  async function initQuality() {
    banners();
    wireToggles();
    const sel = $('#q-source');
    const sources = await api('/api/sources').catch(() => []);
    const lists = sources.filter((s) => s.kind === 'STRUCTURED_LIST' || s.kind === 'CURATED_LIST');
    sel.append(el('option', { value: '' }, 'All lists'));
    lists.forEach((s) => sel.append(el('option', { value: s.source_id }, s.display_name)));
    const first = lists.find((s) => s.current_version_seq);
    if (first) sel.value = first.source_id;
    const load = async () => {
      const src = sel.value;
      const days = $('#q-days').value;
      const qs = src ? `?source_id=${encodeURIComponent(src)}` : '';
      const name = src ? (lists.find((s) => s.source_id === src) || {}).display_name || src : 'all lists';
      // trend (one series: the title names it, no legend)
      if (src) {
        const t = await api(`/api/quality/trend?source_id=${encodeURIComponent(src)}&days=${days}`);
        mount('#trend-title', `Records per published version — ${name}`);
        const labels = t.map((r) => `v${r.seq} · ${fmtTime(r.published_at)}`);
        const series = [{ name: 'Records', values: t.map((r) => r.record_count) }];
        $('#trend-chart-box').classList.toggle('hidden', !t.length || !$('#trend-table').classList.contains('hidden'));
        if (t.length) drawChart('trend-chart', { type: 'line', labels, series, title: `records per version, ${name}` });
        mount('#trend-table', t.length ? chartTable(labels, series, fmtNum, 'Version') : el('div', { class: 'empty' }, 'No published versions in this period.'));
        if (!t.length) $('#trend-table').classList.remove('hidden');
      } else {
        mount('#trend-title', 'Records per published version');
        $('#trend-chart-box').classList.add('hidden');
        mount('#trend-table', el('div', { class: 'empty' }, 'Pick a single list to see its record-count trend.'));
        $('#trend-table').classList.remove('hidden');
      }
      // change volume per day, stacked; RELIST folded into "Added" so the palette stays at 3 validated slots
      const ch = await api(`/api/quality/changes?days=${days}${src ? `&source_id=${encodeURIComponent(src)}` : ''}`);
      const days_ = [...new Set(ch.map((r) => r.day))].sort();
      const pick = (types) => days_.map((d) => ch.filter((r) => r.day === d && types.includes(r.change_type)).reduce((a, r) => a + r.n, 0));
      const series = [{ name: 'Added / relisted', values: pick(['ADD', 'RELIST']) }, { name: 'Changed', values: pick(['CHANGE']) }, { name: 'Removed', values: pick(['REMOVE']) }];
      const labels = days_.map((d) => new Date(`${d}T00:00:00`).toLocaleDateString(undefined, { day: '2-digit', month: 'short' }));
      legend('#changes-legend', series.map((s) => s.name));
      if (days_.length) drawChart('changes-chart', { type: 'bar', stacked: true, labels, series, title: `changes per day, ${name}` });
      $('#changes-chart-box').classList.toggle('hidden', !days_.length || !$('#changes-table').classList.contains('hidden'));
      mount('#changes-table', days_.length ? chartTable(labels, series, fmtNum, 'Day') : el('div', { class: 'empty' }, 'No changes in this period.'));
      if (!days_.length) $('#changes-table').classList.remove('hidden');
      // fill rates (table with meters: more classes than a chart can carry)
      const fr = await api(`/api/quality/fill-rates${qs}`);
      const order = { FAIL: 0, WARN: 1, OK: 2 };
      fr.sort((a, b) => a.source_id.localeCompare(b.source_id) || (order[a.status] ?? 3) - (order[b.status] ?? 3)
        || a.entity_type.localeCompare(b.entity_type) || a.field.localeCompare(b.field));
      const frRows = [];
      let grp = null;
      for (const r of fr) {
        if (r.source_id !== grp) {
          grp = r.source_id;
          const bad = fr.filter((x) => x.source_id === grp && x.status === 'FAIL').length;
          frRows.push({ __group: el('span', {}, `${r.source_id} · version ${r.seq} `, badge('version', r.version_status),
            bad ? el('span', { class: 'sub' }, `  ${bad} field(s) below floor`) : null) });
        }
        frRows.push(r);
      }
      mount('#fill-rates', table([
        { label: 'Entity type', render: (r) => String(r.entity_type).toLowerCase() },
        { label: 'Field', key: 'field' },
        { label: 'Fill rate (| = floor)', render: (r) => meter(r.fill_rate, r.floor, r.status) },
        { label: 'Floor', num: true, render: (r) => (r.floor === null ? '—' : fmtPct(r.floor, 0)) },
        { label: 'Previous', num: true, render: (r) => fmtPct(r.previous_fill_rate) },
        { label: 'Check', render: (r) => (r.status === 'FAIL'
          ? el('span', { class: 'badge critical' }, el('span', { class: 'ico', 'aria-hidden': 'true' }, '✕'), 'Below floor')
          : r.status === 'WARN' ? el('span', { class: 'badge warning' }, el('span', { class: 'ico', 'aria-hidden': 'true' }, '!'), 'Near floor')
            : r.floor === null ? el('span', { class: 'sub' }, 'no floor') : badge('severity', 'OK')) },
      ], frRows, { empty: 'No fill-rate metrics yet (measured on the first parsed file).' }));
      const mx = await api(`/api/quality/metrics${qs}`);
      const isRate = (m) => /(_rate|_share|_pass|_pct|pass_rate)$/.test(m);
      mount('#metrics', table([
        { label: 'Source', key: 'source_id' },
        { label: 'Entity type', render: (r) => String(r.entity_type || '').toLowerCase() },
        { label: 'Metric', render: (r) => String(r.metric).replaceAll('_', ' ') },
        { label: 'Field', key: 'field' },
        { label: 'Value', num: true, render: (r) => (isRate(r.metric) ? fmtPct(r.value) : fmtNum(r.value)) },
      ], mx, { empty: 'No other metrics.' }));
      mount('#issues', issuesTable(await api(`/api/quality/issues${qs}`)));
      const cat = await api(`/api/quality/field-catalog${qs}`);
      mount('#catalog', table([
        { label: 'Source', key: 'source_id' },
        { label: 'Canonical field', render: (r) => el('span', { class: 'mono' }, r.canonical_field) },
        { label: 'Entity types', render: (r) => (r.entity_types || []).map((t) => tag(String(t).toLowerCase())) },
        { label: 'Publisher path', render: (r) => (r.source_path ? el('span', { class: 'mono' }, r.source_path) : null) },
        { label: 'Description', key: 'description' },
        { label: 'Floor', num: true, render: (r) => (r.fill_floor === null ? '—' : fmtPct(r.fill_floor, 0)) },
        { label: 'Notes', key: 'notes' },
      ], cat, { empty: 'Field catalog is empty — run `sanctions-agent sources import`.' }));
    };
    const run = () => load().catch((e) => mount('#fill-rates', errorBox(e)));
    sel.addEventListener('change', run);
    $('#q-days').addEventListener('change', run);
    await run();
  }

  // ================================================================ page: enrichment & evidence
  async function initEnrichment() {
    banners();
    try {
      const d = await api('/api/enrichment/coverage');
      mount('#coverage', table([
        { label: 'Provider', key: 'provider' },
        { label: 'List', key: 'source_id' },
        { label: 'Entity type', render: (r) => String(r.entity_type || '').toLowerCase() },
        { label: 'Match status', render: (r) => String(r.status).replaceAll('_', ' ').toLowerCase() },
        { label: 'Matches', num: true, render: (r) => fmtNum(r.n) },
        { label: 'Current records', num: true, render: (r) => fmtNum(r.current_records) },
        { label: 'Share of records', render: (r) => meter(r.share_of_records, null, 'OK') },
      ], d.coverage, { empty: 'No enrichment results yet.' }));
      mount('#evidence', table([
        { label: 'List', key: 'source_id' },
        { label: 'Change', render: (r) => String(r.change_type).toLowerCase() },
        { label: 'Changes', num: true, render: (r) => fmtNum(r.change_events) },
        { label: 'With legal notice', num: true, render: (r) => fmtNum(r.with_legal_evidence) },
        { label: 'Coverage', render: (r) => meter(r.change_events ? r.with_legal_evidence / r.change_events : 0, null, 'OK') },
      ], d.evidence, { empty: 'No changes in the last 90 days.' }));
      mount('#holds', table([
        { label: 'List', key: 'source_id' },
        { label: 'Status', render: (r) => String(r.status).replaceAll('_', ' ').toLowerCase() },
        { label: 'Records', num: true, render: (r) => fmtNum(r.n) },
        { label: 'Oldest', render: (r) => fmtAgo(r.oldest) },
      ], d.removal_holds, { empty: 'No removal candidates.' }));
      mount('#notices', table([
        { label: 'Published', render: (r) => r.published_on || '—' },
        { label: 'Provider', key: 'provider' },
        { label: 'Title', render: (r) => (r.url ? extLink(r.url, r.title || r.url) : r.title) },
        { label: 'Actions', render: (r) => (r.action_types || []).map((a) => tag(String(a).toLowerCase())) },
        { label: 'Linked records', num: true, render: (r) => fmtNum(r.links) },
        { label: 'Extraction', render: (r) => (r.extraction_status ? String(r.extraction_status).toLowerCase() : null) },
      ], d.notices, { empty: 'No notices collected yet.' }));
    } catch (e) { mount('#coverage', errorBox(e)); }
  }

  // ================================================================ page: agent activity
  async function initAgent() {
    banners();
    wireToggles();
    const showCycle = async (id) => {
      try {
        const d = await api(`/api/agent/cycles/${encodeURIComponent(id)}`);
        const c = d.cycle;
        const report = c.report || {};
        mount('#cycle-detail',
          el('div', { class: 'row' }, el('strong', {}, c.agent_name || 'supervisor'), badge('cycle', c.status), c.fallback_used ? tag('autopilot fallback') : null,
            el('span', { class: 'sub' }, fmtTime(c.started_at))),
          el('p', {}, report.summary || c.error || '—'),
          kv([['Why the agent ran', (c.gate_reasons || []).join(', ') || 'scheduled review'], ['Model', c.model],
            ['Tokens', `${fmtNum(c.input_tokens)} in (${fmtNum(c.cached_tokens)} cached) · ${fmtNum(c.output_tokens)} out`],
            ['Cost', fmtUsd(c.cost_usd, 4)], ['Trace', c.trace_id ? el('span', { class: 'mono' }, c.trace_id) : null]]),
          el('h2', { style: 'margin-top:12px' }, 'Tool calls'),
          table([
            { label: '#', num: true, key: 'seq' },
            { label: 'Tool', render: (a) => el('span', { class: 'mono' }, a.tool) },
            { label: 'Guard', render: (a) => (a.guard_verdict === 'DENIED' ? badge('severity', 'WARN') : badge('severity', 'OK')) },
            { label: 'Args', render: (a) => el('span', { class: 'mono' }, JSON.stringify(a.args || {}).slice(0, 160)) },
            { label: 'Result', render: (a) => (a.error ? el('span', { class: 'error' }, a.error) : el('span', { class: 'mono' }, JSON.stringify(a.result_summary || {}).slice(0, 200))) },
            { label: 'ms', num: true, render: (a) => fmtNum(a.duration_ms) },
          ], d.actions, { empty: 'No tool calls in this cycle.' }),
          d.runs.length ? [el('h2', { style: 'margin-top:12px' }, 'Runs it started'),
            table([{ label: 'Source', key: 'source_id' }, { label: 'Status', render: (r) => badge('run', r.status) }], d.runs, { onRowClick: (r) => { window.location.href = `/ui/runs/${r.run_id}`; } })] : null);
      } catch (e) { mount('#cycle-detail', errorBox(e)); }
    };
    try {
      const d = await api('/api/agent/cycles?limit=200');
      fillTbody('#cycles-table', [
        { label: 'Started', render: (c) => fmtTime(c.started_at) },
        { label: 'Agent', key: 'agent_name' },
        { label: 'Trigger', render: (c) => String(c.trigger || '').toLowerCase() },
        { label: 'Status', render: (c) => el('span', {}, badge('cycle', c.status), c.fallback_used ? el('span', {}, ' ', tag('fallback')) : null) },
        { label: 'Tool calls', num: true, render: (c) => fmtNum(c.tool_calls) },
        { label: 'Denied', num: true, render: (c) => fmtNum(c.denied_calls) },
        { label: 'Tokens in/out', num: true, render: (c) => `${fmtCompact(c.input_tokens || 0)} / ${fmtCompact(c.output_tokens || 0)}` },
        { label: 'Cost', num: true, render: (c) => fmtUsd(c.cost_usd, 4) },
        { label: 'Summary', render: (c) => String(c.summary || c.error || '').slice(0, 140) },
      ], d.cycles, { onRowClick: (c) => showCycle(c.cycle_id), empty: 'No agent cycles yet.' });
      const labels = d.daily.map((r) => new Date(`${r.day}T00:00:00`).toLocaleDateString(undefined, { day: '2-digit', month: 'short' }));
      const series = [{ name: 'LLM cost (USD)', values: d.daily.map((r) => Number(r.cost_usd || 0)) }];
      if (d.daily.length) drawChart('cost-chart', { type: 'bar', labels, series, yFormat: (v) => fmtUsd(v), tipFormat: (v) => fmtUsd(v, 4), title: 'LLM spend per day' });
      else { $('#cost-chart-box').classList.add('hidden'); $('#cost-table').classList.remove('hidden'); }
      mount('#cost-table', d.daily.length ? chartTable(labels, series, (v) => fmtUsd(v, 4), 'Day') : el('div', { class: 'empty' }, 'No LLM spend in the last 30 days.'));
      if (d.cycles.length) showCycle(d.cycles[0].cycle_id);
    } catch (e) { mount('#cycle-detail', errorBox(e)); }
  }

  // ================================================================ page: review queue
  async function initReview() {
    banners();
    const RESOLUTIONS = [['CONFIRMED', 'Confirm removal (stop blocking)'], ['STILL_LISTED_ELSEWHERE', 'Still listed elsewhere (keep blocking via the other list)'],
      ['REJECTED_ID_CHANGE', 'Not a removal — the publisher changed the ID'], ['REJECTED_PARSER', 'Parser problem — keep blocking until re-parsed']];
    const ADMIN_KINDS = new Set(['CONFIG_CHANGE', 'SOURCE_ACTIVATION']);
    const load = async () => {
      const q = new URLSearchParams({ status: $('#r-status').value });
      if ($('#r-kind').value) q.set('kind', $('#r-kind').value);
      const rows = await api(`/api/proposals?${q}`);
      if (!rows.length) { mount('#proposals', el('div', { class: 'card empty' }, 'Nothing waiting for review.')); return; }
      mount('#proposals', rows.map((p) => {
        const needs = ADMIN_KINDS.has(p.kind) ? 'admin' : 'reviewer';
        const mine = p.proposed_by === BODY.dataset.user;
        const quotes = Array.isArray(p.verbatim_quotes) ? p.verbatim_quotes : [];
        const ver = p.verification || {};
        const card = el('div', { class: 'proposal' },
          el('div', { class: 'row' }, el('h3', {}, p.title), tag(String(p.kind).replaceAll('_', ' ').toLowerCase()), badge('proposal', p.status),
            el('span', { class: 'spacer' }), el('span', { class: 'sub' }, `#${p.change_id} · ${fmtAgo(p.created_at)}`)),
          kv([['Source', p.source_id], ['Proposed by', p.proposed_by], ['Rationale', p.rationale],
            ['Reviewed', p.reviewed_by ? `${p.reviewed_by} · ${fmtTime(p.reviewed_at)}${p.review_comment ? ` — ${p.review_comment}` : ''}` : undefined]]),
          (p.evidence_urls || []).length ? el('div', { style: 'margin-top:8px' }, el('strong', {}, 'Evidence: '), p.evidence_urls.map((u) => el('div', {}, extLink(u)))) : null,
          quotes.length ? el('div', { style: 'margin-top:8px' }, el('strong', {}, 'Verbatim quotes'),
            quotes.map((q) => el('blockquote', {}, typeof q === 'string' ? q : `${q.field ? `${q.field}: ` : ''}${q.quote || JSON.stringify(q)}`))) : null,
          Object.keys(ver).length ? el('div', { style: 'margin-top:8px' }, el('strong', {}, 'Automatic checks: '),
            ver.ok === false ? badge('severity', 'FAIL') : ver.ok === true ? badge('severity', 'OK') : null,
            el('details', {}, el('summary', {}, 'Verifier output'), jsonBlock(ver))) : null,
          el('details', { style: 'margin-top:6px' }, el('summary', {}, 'Proposed change (payload)'), jsonBlock(p.payload)));
        if (p.status === 'PENDING') {
          if (!can(needs)) {
            card.append(el('p', { class: 'sub' }, `A ${needs} decides this proposal.`));
          } else if (mine) {
            card.append(el('p', { class: 'sub' }, 'You made this proposal — a different person must decide it (maker-checker).'));
          } else {
            const comment = el('input', { placeholder: 'Comment (recorded in the audit log)', maxlength: 2000 });
            const res = p.kind === 'REMOVAL_CONFIRMATION'
              ? el('select', {}, RESOLUTIONS.map(([v, l]) => el('option', { value: v, selected: v === (p.payload || {}).recommendation }, l))) : null;
            const err = el('span', { class: 'error' });
            const decide = async (approve, btn) => {
              err.textContent = '';
              if (!approve && comment.value.trim().length < 3) { err.textContent = 'Give a reason to reject.'; return; }
              btn.disabled = true;
              try {
                await api(`/api/proposals/${p.change_id}/decide`, { method: 'POST', body: { approve, comment: comment.value.trim() || null, resolution: res ? res.value : null } });
                load();
              } catch (e) { err.textContent = e.message; btn.disabled = false; }
            };
            const ok = el('button', { class: 'primary' }, 'Approve');
            const no = el('button', { class: 'danger' }, 'Reject');
            ok.addEventListener('click', () => decide(true, ok));
            no.addEventListener('click', () => decide(false, no));
            card.append(el('div', { class: 'row', style: 'margin-top:10px' }, res, comment, ok, no, err));
          }
        }
        return card;
      }));
    };
    const run = () => load().catch((e) => mount('#proposals', errorBox(e)));
    $('#r-status').addEventListener('change', run);
    $('#r-kind').addEventListener('change', run);
    await run();
  }

  // ================================================================ page: ask (aggregate-only Q&A)
  function initAsk() {
    banners();
    const chat = $('#chat');
    const input = $('#ask-input');
    let n = 0;
    const addUser = (q) => chat.append(el('div', { class: 'msg user' }, q));
    const addBot = (res) => {
      n += 1;
      const box = el('div', { class: 'msg bot' });
      if (res.refused) box.append(el('div', { class: 'row' }, badge('cycle', 'REFUSED'), el('span', { class: 'sub' }, 'Row-level question — this assistant answers only with aggregates.')));
      box.append(renderMarkdown(res.answer_markdown));
      const c = res.chart;
      if (c && Array.isArray(c.series) && c.series.length && Array.isArray(c.labels) && c.labels.length) {
        const id = `ask-chart-${n}`;
        const series = c.series.map((s) => ({ name: s.name, values: s.values }));
        const tbl = el('div', { class: 'table-wrap', id: `ask-${n}-table` }, chartTable(c.labels, series, fmtNum, ''));
        if (series.length <= SERIES.length) {
          const boxC = el('div', { class: 'chart-box', id: `ask-${n}-chart-box` }, el('canvas', { id }));
          tbl.classList.add('hidden');
          const toggle = el('button', { class: 'view-toggle' }, 'Table view');
          toggle.addEventListener('click', () => {
            const showTable = tbl.classList.contains('hidden');
            tbl.classList.toggle('hidden', !showTable);
            boxC.classList.toggle('hidden', showTable);
            toggle.textContent = showTable ? 'Chart view' : 'Table view';
          });
          const leg = el('div', { class: 'chart-legend' });
          box.append(el('div', { class: 'row', style: 'margin-top:8px' }, el('strong', {}, c.title || ''), el('span', { class: 'spacer' }), toggle), leg, boxC, tbl);
          chat.append(box);
          legend(leg, series.map((s) => s.name));
          drawChart(id, { type: c.type === 'line' ? 'line' : 'bar', labels: c.labels, series, yLabel: c.y_label, title: c.title });
        } else {
          box.append(el('strong', {}, c.title || ''), tbl);
        }
      }
      if ((res.citations || []).length) {
        box.append(el('div', { class: 'cites' }, 'Sources: ', res.citations.map((ci, i) => [i ? ' · ' : '', `${ci.source} (as of ${fmtTime(ci.as_of)})`])));
      }
      if (!box.isConnected) chat.append(box);
      box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    };
    const ask = async (q) => {
      if (!q || q.length < 2) return;
      addUser(q);
      input.value = '';
      const pending = el('div', { class: 'msg bot pending' }, 'Looking at the ingestion data…');
      chat.append(pending);
      const btn = $('#ask-form button');
      btn.disabled = true;
      try {
        const res = await api('/api/ask', { method: 'POST', body: { question: q, session_id: store('ask-session') || null } });
        if (res.session_id) store('ask-session', res.session_id);
        pending.remove();
        addBot(res);
      } catch (e) {
        pending.remove();
        chat.append(el('div', { class: 'msg bot' }, el('span', { class: 'error' }, `The assistant could not answer: ${e.message}`)));
      } finally {
        btn.disabled = false;
        input.focus();
      }
    };
    $('#ask-form').addEventListener('submit', (e) => { e.preventDefault(); ask(input.value.trim()); });
    $$('#examples .chip').forEach((c) => {
      c.tabIndex = 0;
      c.setAttribute('role', 'button');
      c.addEventListener('click', () => ask(c.textContent.trim()));
      c.addEventListener('keydown', (e) => { if (e.key === 'Enter') ask(c.textContent.trim()); });
    });
  }

  // ================================================================ page: manage — sources list
  async function initManageList() {
    const load = async () => {
      const [srcs, runs] = await Promise.all([api('/api/sources'), api('/api/runs?limit=500')]);
      const lastRun = new Map();
      for (const r of runs) if (!lastRun.has(r.source_id)) lastRun.set(r.source_id, r);
      const rows = [];
      let lvl = null;
      for (const s of srcs) {
        if (s.level !== lvl) { lvl = s.level; rows.push({ __group: lvl === 1 ? 'Level 1 — official lists' : 'Level 2 — notices & free enrichment' }); }
        rows.push(s);
      }
      fillTbody('#manage-table', [
        { label: 'Source', render: (s) => el('div', {}, el('a', { href: `/ui/manage/sources/${s.source_id}` }, s.display_name), s.is_core ? el('span', {}, ' ', tag('core')) : null,
          el('div', { class: 'sub mono' }, `${s.source_id} · ${s.adapter_type}`)) },
        { label: 'Status', render: (s) => el('div', {}, badge('source', s.status),
          s.paused_until ? el('div', { class: 'sub' }, `until ${fmtTime(s.paused_until)}`) : null,
          s.status_reason && s.status !== 'ACTIVE' ? el('div', { class: 'sub' }, s.status_reason) : null) },
        { label: 'Schedule', render: (s) => scheduleText(s) },
        { label: 'Next due', render: (s) => (s.status === 'ACTIVE' ? fmtDue(s.next_due_at) : '—') },
        { label: 'Last run', render: (s) => { const r = lastRun.get(s.source_id); return r ? el('a', { href: `/ui/runs/${r.run_id}` }, badge('run', r.status), ' ', fmtAgo(r.finished_at || r.queued_at)) : 'never'; } },
        { label: 'Health', render: (s) => badge('health', s.health) },
        { label: '', render: (s) => sourceActions(s, load) },
      ], rows, { empty: 'No sources configured — run `sanctions-agent sources import`.' });
    };
    await load().catch((e) => mount('#manage-table tbody', el('tr', {}, el('td', { colspan: 7 }, errorBox(e)))));
    const refresh = debounce(() => load().catch(() => {}), 1200);
    onProgress((p) => { if (TERMINAL.has(p.status) || p.status === 'QUEUED') refresh(); }, refresh);
  }

  function sourceActions(s, after, opts = {}) {
    const box = el('div', { class: 'row actions' });
    const b = (label, fn, cls) => el('button', { class: `small ${cls || ''}`.trim(), onclick: fn }, label);
    if (can('operator')) {
      if (s.status === 'ACTIVE') box.append(b('Run now', () => openRunDialog(s)));
      if (s.status === 'DRAFT') box.append(b('Dry run', () => openRunDialog(s)));
      if (s.status === 'ACTIVE') box.append(b('Pause', () => openStatusDialog(s, 'PAUSED', after)));
      if (s.status === 'PAUSED') box.append(b('Resume', () => openStatusDialog(s, 'ACTIVE', after)));
      if (can('admin') && (s.status === 'ACTIVE' || s.status === 'PAUSED')) box.append(b('Disable', () => openStatusDialog(s, 'DISABLED', after)));
      if (can('admin') && s.status === 'DISABLED') box.append(b('Enable', () => openStatusDialog(s, 'ACTIVE', after)));
    }
    if (!opts.noEdit) box.append(el('a', { class: 'btn small', href: `/ui/manage/sources/${s.source_id}` }, can('admin') ? 'Edit' : 'View'));
    return box;
  }

  // ================================================================ page: manage — source editor
  async function initSourceEdit(sourceId) {
    let d = null;
    const admin = can('admin');
    const tabs = $$('#tabs button');
    const showTab = (name) => {
      tabs.forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
      $$('[data-panel]').forEach((p) => p.classList.toggle('hidden', p.dataset.panel !== name));
      try { history.replaceState(null, '', `#${name}`); } catch (_) { /* ignore */ }
    };
    tabs.forEach((t) => t.addEventListener('click', () => showTab(t.dataset.tab)));
    if (window.location.hash && $(`[data-panel="${CSS.escape(window.location.hash.slice(1))}"]`)) showTab(window.location.hash.slice(1));
    if (!admin) $$('[data-panel] input, [data-panel] select, [data-panel] textarea, [data-panel] button.primary, #v-suggest, #v-add-floor').forEach((i) => { i.disabled = true; });

    const patch = async (body, errSel, okSel) => {
      mount(errSel, '');
      if (okSel) mount(okSel, '');
      const res = await api(`/api/sources/${encodeURIComponent(sourceId)}`, { method: 'PATCH', body: { ...body, expected_version: d.source.config_version } });
      const msg = res.pending_change_id
        ? `Security-sensitive change held for a second admin's approval (proposal #${res.pending_change_id}).`
        : res.applied ? `Saved as configuration version ${res.version}.` : (res.message || 'No change.');
      await load();
      return msg;
    };

    const scheduleBody = () => ({
      kind: $('#s-kind').value,
      cadence_minutes: $('#s-kind').value === 'INTERVAL' ? Number($('#s-cadence').value) || null : null,
      cron_expr: $('#s-kind').value === 'CRON' ? $('#s-cron').value.trim() : null,
      timezone: $('#s-tz').value.trim() || 'UTC',
      min_interval_minutes: Number($('#s-min').value) || 30,
      warn_staleness_hours: Number($('#s-warn').value) || 6,
      hard_max_staleness_hours: Number($('#s-hard').value) || 12,
    });
    const preview = debounce(async () => {
      if (!d) return;
      const kind = $('#s-kind').value;
      $('#s-cadence-row').classList.toggle('hidden', kind !== 'INTERVAL');
      $('#s-cron-row').classList.toggle('hidden', kind !== 'CRON');
      try {
        const p = await api(`/api/schedule/preview?adapter_type=${encodeURIComponent(d.source.adapter_type)}`, { method: 'POST', body: scheduleBody() });
        const tz = $('#s-tz').value.trim() || 'UTC';
        mount('#s-preview', p.valid
          ? [el('strong', {}, `${p.text}. `), 'Next runs: ', p.next_runs.map((t, i) => [i ? ' · ' : '', fmtTime(t, tz)])]
          : el('span', { class: 'error' }, p.error));
      } catch (e) { mount('#s-preview', el('span', { class: 'error' }, e.message)); }
    }, 350);
    ['#s-kind', '#s-cadence', '#s-cron', '#s-tz', '#s-min', '#s-warn', '#s-hard'].forEach((s) => $(s).addEventListener('input', preview));

    const floorsBox = $('#v-floors');
    const floorRow = (key = '', val = '', hint = '') => {
      const k = el('input', { placeholder: 'ENTITY_TYPE.field, e.g. PERSON.dob', value: key });
      const v = el('input', { type: 'number', min: 0, max: 1, step: 0.01, value: val });
      const rm = el('button', { type: 'button', title: 'Remove floor' }, 'Remove');
      const row = el('div', { class: 'floor-row' }, k, v, rm, el('span', { class: 'sub' }, hint));
      rm.addEventListener('click', () => row.remove());
      if (!admin) [k, v, rm].forEach((i) => { i.disabled = true; });
      floorsBox.append(row);
      return row;
    };

    const load = async () => {
      d = await api(`/api/sources/${encodeURIComponent(sourceId)}`);
      const s = d.source;
      const st = d.status || {};
      document.title = `${s.display_name} · Sanctions ingestion`;
      $('h1').textContent = s.display_name;
      mount('#src-head',
        el('div', { class: 'row' }, badge('source', s.status), badge('health', st.health), s.is_core ? tag('core list') : null,
          tag(`level ${s.level}`), el('span', { class: 'mono' }, `${s.source_id} · ${s.adapter_type}`), el('span', { class: 'spacer' }),
          sourceActions({ ...st, ...s }, load, { noEdit: true }), s.status === 'DRAFT' && admin ? el('button', { class: 'primary', onclick: () => requestActivation(s.source_id, load) }, 'Request activation') : null),
        kv([['Schedule', s.schedule_text], ['Next due', s.status === 'ACTIVE' ? `${fmtTime(s.next_due_at)} (${fmtDue(s.next_due_at)})` : '—'],
          ['Politeness floor (code)', `${fmtMinutes(d.hard_min_interval_minutes)} — schedules cannot go below this`],
          ['Last success / change', `${fmtAgo(st.last_success_at)} / ${fmtAgo(st.last_change_at)}`],
          ['Last run', d.runs.length ? el('a', { href: `/ui/runs/${d.runs[0].run_id}` }, badge('run', d.runs[0].status), ` ${fmtAgo(d.runs[0].finished_at || d.runs[0].queued_at)}${d.runs[0].error_class ? ` · ${d.runs[0].error_class}` : ''}`) : 'never'],
          ['Breaker', badge('breaker', s.breaker_state)], ['Configuration version', s.config_version],
          ['Status reason', s.status_reason], ['Licence', s.licence ? `${s.licence} (${String(s.licence_status || '').toLowerCase()})` : null]]));
      // schedule tab
      const sch = s.schedule || {};
      $('#s-kind').value = sch.kind || 'INTERVAL';
      $('#s-cadence').value = sch.cadence_minutes || '';
      $('#s-cron').value = sch.cron_expr || '';
      $('#s-tz').value = sch.timezone || 'UTC';
      $('#s-min').value = sch.min_interval_minutes || '';
      $('#s-min').min = d.hard_min_interval_minutes;
      $('#s-warn').value = sch.warn_staleness_hours || '';
      $('#s-hard').value = sch.hard_max_staleness_hours || '';
      $('#s-priority').value = s.priority || 5;
      mount('#s-floor', `This adapter's politeness floor is ${fmtMinutes(d.hard_min_interval_minutes)}: neither the cadence nor the minimum interval can be lower.`);
      preview();
      // config tab
      $('#c-json').value = JSON.stringify(s.config, null, 2);
      mount('#c-schema', JSON.stringify(d.config_schema, null, 2));
      // validation tab
      const val = (s.config || {}).validation || {};
      $('#v-min').value = val.min_records ?? '';
      $('#v-rem-abs').value = val.max_removed_abs ?? '';
      $('#v-rem-pct').value = val.max_removed_pct ?? '';
      $('#v-add-abs').value = val.max_added_abs ?? '';
      $('#v-add-pct').value = val.max_added_pct ?? '';
      $('#v-drift').value = val.drift_policy || 'warn';
      clear(floorsBox);
      Object.entries(val.fill_floors || {}).forEach(([k, v]) => floorRow(k, v));
      // history
      mount('#h-list', table([
        { label: 'Version', num: true, key: 'version' },
        { label: 'Status', render: (v) => badge('proposal', v.status) },
        { label: 'By', key: 'changed_by' },
        { label: 'When', render: (v) => fmtTime(v.changed_at) },
        { label: 'Reason', key: 'reason' },
        { label: 'Changed', render: (v) => (v.diff && Object.keys(v.diff).length ? el('details', {}, el('summary', {}, `${Object.keys(v.diff).length} path(s)`), jsonBlock(v.diff)) : null) },
        { label: '', render: (v) => (admin && v.status === 'SUPERSEDED' ? el('button', { onclick: () => formDialog({
          title: `Roll back to version ${v.version}?`, intro: 'Rollback creates a new configuration version with the old settings (sensitive fields still need a second admin).',
          submitLabel: 'Roll back', fields: [{ id: 'reason', label: 'Reason (required)' }],
          onSubmit: async (x) => {
            if (x.reason.length < 3) throw new Error('Give a reason.');
            await api(`/api/sources/${encodeURIComponent(sourceId)}/rollback/${v.version}`, { method: 'POST', body: { reason: x.reason, expected_version: d.source.config_version } });
            await load();
          },
        }) }, 'Roll back') : null) },
      ], d.config_versions));
      // runs
      mount('#r-list', table([
        { label: 'Queued', render: (r) => fmtTime(r.queued_at) },
        { label: 'Kind', render: (r) => tag(String(r.run_kind).replaceAll('_', ' ').toLowerCase()) },
        { label: 'Trigger', render: (r) => `${String(r.trigger).toLowerCase()} · ${r.requested_by || ''}` },
        { label: 'Status', render: (r) => badge('run', r.status) },
        { label: 'Duration', num: true, render: (r) => fmtDur(r.duration_seconds) },
        { label: 'Config v', num: true, key: 'config_version' },
        { label: 'Error', key: 'error_class' },
      ], d.runs, { onRowClick: (r) => { window.location.href = `/ui/runs/${r.run_id}`; }, empty: 'No runs yet.' }));
    };

    $('#s-save').addEventListener('click', async () => {
      const reason = $('#s-reason').value.trim();
      if (reason.length < 3) { mount('#s-error', 'Give a reason for the change.'); return; }
      try {
        const msg = await patch({ schedule: scheduleBody(), priority: Number($('#s-priority').value) || null, reason }, '#s-error');
        mount('#s-error', el('span', { class: 'ok-text' }, msg));
        $('#s-reason').value = '';
      } catch (e) { mount('#s-error', e.status === 409 ? 'Someone else changed this source — reload the page and re-apply your edit.' : e.message); }
    });
    $('#c-save').addEventListener('click', async () => {
      const reason = $('#c-reason').value.trim();
      mount('#c-ok', '');
      if (reason.length < 3) { mount('#c-error', 'Give a reason for the change.'); return; }
      let cfg;
      try { cfg = JSON.parse($('#c-json').value); } catch (e) { mount('#c-error', `Not valid JSON: ${e.message}`); return; }
      try {
        const msg = await patch({ config: cfg, reason }, '#c-error', '#c-ok');
        mount('#c-ok', msg);
        $('#c-reason').value = '';
      } catch (e) { mount('#c-error', e.status === 409 ? 'Someone else changed this source — reload and re-apply your edit.' : e.message); }
    });
    $('#v-add-floor').addEventListener('click', () => floorRow());
    $('#v-suggest').addEventListener('click', async () => {
      try {
        const sug = await api(`/api/sources/${encodeURIComponent(sourceId)}/suggest-floors`);
        const existing = new Map($$('.floor-row', floorsBox).map((r) => [$$('input', r)[0].value.trim(), r]));
        const keys = Object.keys(sug);
        if (!keys.length) { mount('#v-error', 'Not enough published versions to suggest floors yet.'); return; }
        for (const k of keys) {
          const hint = `lowest ${fmtPct(sug[k].lowest)} · highest ${fmtPct(sug[k].highest)} over ${sug[k].versions} version(s)`;
          const row = existing.get(k);
          if (row) { $$('input', row)[1].value = sug[k].suggested_floor; $('span.sub', row).textContent = hint; } else floorRow(k, sug[k].suggested_floor, hint);
        }
        mount('#v-error', el('span', { class: 'sub' }, 'Suggestions filled in — review, then save. Nothing is applied until you save.'));
      } catch (e) { mount('#v-error', e.message); }
    });
    $('#v-save').addEventListener('click', async () => {
      const reason = $('#v-reason').value.trim();
      if (reason.length < 3) { mount('#v-error', 'Give a reason for the change.'); return; }
      const floors = {};
      for (const r of $$('.floor-row', floorsBox)) {
        const [k, v] = $$('input', r);
        if (!k.value.trim()) continue;
        floors[k.value.trim()] = Number(v.value);
      }
      const num = (sel) => ($(sel).value === '' ? undefined : Number($(sel).value));
      const cfg = JSON.parse(JSON.stringify(d.source.config || {}));
      cfg.validation = { ...(cfg.validation || {}), min_records: num('#v-min'), max_removed_abs: num('#v-rem-abs'), max_removed_pct: num('#v-rem-pct'),
        max_added_abs: num('#v-add-abs'), max_added_pct: num('#v-add-pct'), drift_policy: $('#v-drift').value, fill_floors: floors };
      Object.keys(cfg.validation).forEach((k) => cfg.validation[k] === undefined && delete cfg.validation[k]);
      try {
        const msg = await patch({ config: cfg, reason }, '#v-error');
        mount('#v-error', el('span', { class: 'ok-text' }, msg));
        $('#v-reason').value = '';
      } catch (e) { mount('#v-error', e.status === 409 ? 'Someone else changed this source — reload and re-apply your edit.' : e.message); }
    });

    await load().catch((e) => mount('#src-head', errorBox(e)));
    const refresh = debounce(() => load().catch(() => {}), 1500);
    onProgress((p) => { if (p.source_id === sourceId && (TERMINAL.has(p.status) || p.status === 'QUEUED')) refresh(); }, refresh);
  }

  function requestActivation(sourceId, after) {
    formDialog({
      title: 'Request activation',
      intro: 'A second admin must approve activation in the review queue. Run a dry run first so they can see the result.',
      submitLabel: 'Request activation',
      fields: [{ id: 'reason', label: 'Reason (required)' }],
      onSubmit: async (v) => {
        if (v.reason.length < 3) throw new Error('Give a reason.');
        const r = await api(`/api/sources/${encodeURIComponent(sourceId)}/request-activation`, { method: 'POST', body: { reason: v.reason } });
        if (after) after(r);
      },
    });
  }

  // ================================================================ page: manage — add source wizard
  function templateFromSchema(schema) {
    const defs = schema.$defs || schema.definitions || {};
    const resolve = (s) => {
      if (s && s.$ref) return defs[s.$ref.split('/').pop()] || {};
      if (s && s.allOf && s.allOf.length === 1) return resolve(s.allOf[0]);
      return s || {};
    };
    const build = (raw, depth) => {
      const s = resolve(raw);
      if (s.default !== undefined) return s.default;
      if (depth > 4) return null;
      if (s.anyOf) { const nn = s.anyOf.find((x) => x.type !== 'null'); return nn ? build(nn, depth + 1) : null; }
      if (s.type === 'object' || s.properties) {
        const o = {};
        const req = new Set(s.required || []);
        for (const [k, p] of Object.entries(s.properties || {})) {
          const r = resolve(p);
          if (r.default !== undefined || req.has(k)) o[k] = build(p, depth + 1);
        }
        return o;
      }
      if (s.enum) return s.enum[0];
      switch (s.type) {
        case 'string': return '';
        case 'integer': case 'number': return s.minimum ?? 0;
        case 'boolean': return false;
        case 'array': return [];
        default: return null;
      }
    };
    return build(schema, 0);
  }

  async function initAddSource() {
    if (!can('admin')) {
      mount('#step1', el('h2', {}, 'Add source'), el('p', { class: 'sub' }, 'Only an admin can add sources.'));
      return;
    }
    let chosen = null;
    let created = null;
    const types = await api('/api/adapter-types');
    mount('#types', types.map((t) => {
      const b = el('button', { class: 'type-card', type: 'button' },
        el('span', { class: 't' }, t.type_id), el('span', { class: 'd' }, t.description),
        el('span', { class: 'd' }, `${String(t.kind).replaceAll('_', ' ').toLowerCase()} · level ${t.level} · floor ${fmtMinutes(t.hard_min_interval_minutes)}`));
      b.addEventListener('click', () => {
        chosen = t;
        $$('.type-card').forEach((c) => c.classList.toggle('selected', c === b));
        $('#step2').classList.remove('hidden');
        $('#n-min').value = t.hard_min_interval_minutes;
        $('#n-min').min = t.hard_min_interval_minutes;
        $('#n-cadence').value = Math.max(240, t.hard_min_interval_minutes);
        $('#n-config').value = JSON.stringify(templateFromSchema(t.config_schema) || {}, null, 2);
        $('#step2').scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
      return b;
    }));
    $('#n-create').addEventListener('click', async () => {
      mount('#n-error', '');
      let cfg;
      try { cfg = JSON.parse($('#n-config').value); } catch (e) { mount('#n-error', `Configuration is not valid JSON: ${e.message}`); return; }
      const body = {
        source_id: $('#n-id').value.trim(), display_name: $('#n-name').value.trim(), adapter_type: chosen.type_id, config: cfg,
        schedule: { kind: 'INTERVAL', cadence_minutes: Number($('#n-cadence').value), min_interval_minutes: Number($('#n-min').value) },
        is_core: $('#n-core').checked, priority: Number($('#n-priority').value) || 5, licence: $('#n-licence').value.trim() || null,
        reason: $('#n-reason').value.trim(),
      };
      if (body.reason.length < 3) { mount('#n-error', 'Give a reason.'); return; }
      try {
        await api('/api/sources', { method: 'POST', body });
        created = body.source_id;
        $('#step3').classList.remove('hidden');
        $('#n-create').disabled = true;
        mount('#n-status', el('span', { class: 'ok-text' }, `Created ${created} as DRAFT.`), ' ', el('a', { href: `/ui/manage/sources/${created}` }, 'Open editor'));
        $('#step3').scrollIntoView({ behavior: 'smooth', block: 'start' });
      } catch (e) { mount('#n-error', e.message); }
    });
    $('#n-dry').addEventListener('click', async () => {
      try {
        const res = await api(`/api/sources/${encodeURIComponent(created)}/runs`, { method: 'POST', body: { mode: 'dry_run', reason: 'Dry run from the add-source wizard' } });
        const runId = res.run_id;
        mount('#n-run', liveCard({ run_id: runId, source_id: created, run_kind: 'LIST_INGEST', status: 'QUEUED' }));
        const finish = async () => {
          const d = await api(`/api/runs/${runId}`);
          const v = d.version || {};
          mount('#n-run', el('div', { class: 'row' }, badge('run', d.run.status), el('a', { href: `/ui/runs/${runId}` }, 'Full run detail')),
            kv([['Records parsed', fmtNum(v.record_count)], ['By type', v.counts_by_type ? JSON.stringify(v.counts_by_type) : null],
              ['Fetch attempts', d.evidence.length], ['Error', d.run.error_class ? `${d.run.error_class}: ${d.run.error_detail || ''}` : null]]),
            d.issues.length ? issuesTable(d.issues) : el('p', { class: 'ok-text' }, 'No data-quality issues raised.'));
        };
        const resync = async () => {
          const d = await api(`/api/runs/${runId}`);
          if (TERMINAL.has(d.run.status)) finish();
        };
        onProgress((p) => {
          if (p.run_id !== runId) return;
          if (TERMINAL.has(p.status)) finish();
          else mount('#n-run', liveCard({ run_kind: 'LIST_INGEST', ...p, current_step: p.step }));
        }, () => { resync().catch(() => {}); });
      } catch (e) { mount('#n-run', errorBox(e)); }
    });
    $('#n-activate').addEventListener('click', () => requestActivation(created, (r) => mount('#n-status', el('span', { class: 'ok-text' }, `Activation requested (proposal #${r.proposal_id}). A second admin approves it in the review queue.`))));
  }

  // ================================================================ page: manage — global settings
  async function initSettings() {
    const admin = can('admin');
    if (!admin) $$('main input, main select, main button').forEach((i) => { i.disabled = true; });
    const s = await api('/api/settings');
    const mm = s.maintenance_mode || {};
    $('#m-on').checked = !!mm.enabled;
    $('#m-reason').value = mm.reason || '';
    $('#a-on').checked = !!s.agent_enabled;
    $('#a-model').value = s.agent_model || '';
    $('#a-effort').value = s.agent_reasoning_effort || 'low';
    $('#a-budget').value = s.agent_daily_budget_usd ?? '';
    $('#a-review').value = s.agent_health_review_minutes ?? '';
    const ch = new Set(s.alert_channels || []);
    $$('[data-ch]').forEach((c) => { c.checked = ch.has(c.dataset.ch); });
    $('#al-page').value = s.page_realert_minutes ?? '';
    $('#al-core').value = s.core_disabled_realert_hours ?? '';
    const put = async (pairs) => {
      mount('#set-ok', '');
      mount('#set-error', '');
      try {
        for (const [k, v] of pairs) await api(`/api/settings/${k}`, { method: 'PUT', body: { value: v } });
        mount('#set-ok', 'Saved.');
        banners();
      } catch (e) { mount('#set-error', e.message); }
    };
    $('#m-save').addEventListener('click', () => {
      if ($('#m-on').checked && $('#m-reason').value.trim().length < 3) { mount('#set-error', 'Give a reason for maintenance mode.'); return; }
      put([['maintenance_mode', { enabled: $('#m-on').checked, reason: $('#m-reason').value.trim() || null }]]);
    });
    $('#a-save').addEventListener('click', () => put([['agent_enabled', $('#a-on').checked], ['agent_model', $('#a-model').value.trim()],
      ['agent_reasoning_effort', $('#a-effort').value], ['agent_daily_budget_usd', Number($('#a-budget').value)],
      ['agent_health_review_minutes', Number($('#a-review').value)]]));
    $('#al-save').addEventListener('click', () => put([['alert_channels', $$('[data-ch]').filter((c) => c.checked).map((c) => c.dataset.ch)],
      ['page_realert_minutes', Number($('#al-page').value)], ['core_disabled_realert_hours', Number($('#al-core').value)]]));
  }

  async function initManage() {
    banners();
    if ($('#manage-table')) return initManageList();
    if (BODY.dataset.sourceId) return initSourceEdit(BODY.dataset.sourceId);
    if ($('#types')) return initAddSource();
    if ($('#m-on')) return initSettings().catch((e) => mount('#set-error', e.message));
    return null;
  }

  // ================================================================ boot
  const PAGES = { overview: initOverview, runs: initRuns, quality: initQuality, enrichment: initEnrichment, agent: initAgent,
    review: initReview, ask: initAsk, manage: initManage };
  const init = PAGES[BODY.dataset.page];
  if (init) Promise.resolve(init()).catch((e) => console.error(e));
})();
