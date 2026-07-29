/* Local AI usage dashboard. No external dependencies - charts are hand-rolled SVG. */
'use strict';

const DOW = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const TABS = [
  ['overview', 'Overview', 'Headline meters across both assistants'],
  ['activity', 'Activity', 'When the work happens'],
  ['cost', 'Cost & Models', 'Estimated spend, token mix, cache economics'],
  ['projects', 'Projects', 'Where the tokens went'],
  ['tools', 'Tools', 'Every tool call, error and denial'],
  ['code', 'Files & Code', 'Lines written and files touched'],
  ['sessions', 'Sessions', 'Session shape and the heaviest runs'],
  ['cursor', 'Cursor', 'Local Cursor history — no usage metering'],
  ['data', 'Data & Sources', 'Provenance, ingest history, caveats'],
];

/* Series colours live in CSS so both themes stay in one place. */
let PALETTE = [];
function readPalette() {
  const cs = getComputedStyle(document.documentElement);
  PALETTE = ['--c1', '--c2', '--c3', '--c4', '--c5', '--c6']
    .map(v => cs.getPropertyValue(v).trim()).filter(Boolean);
  if (!PALETTE.length) PALETTE = ['#0F6E78', '#B5822B', '#4A6FA5', '#A85742', '#5C8250', '#7A5580'];
  return PALETTE;
}
const tone = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim() || '#888';

let DATA = null;
let activeTab = location.hash.slice(1) || 'overview';

/* ------------------------------------------------------------------ utils */
const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function num(n) {
  n = Number(n) || 0;
  const a = Math.abs(n);
  if (a >= 1e12) return (n / 1e12).toFixed(2) + 'T';
  if (a >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e8 ? 0 : 1) + 'M';
  if (a >= 1e3) return (n / 1e3).toFixed(a >= 1e5 ? 0 : 1) + 'k';
  return String(Math.round(n));
}
const int = (n) => (Number(n) || 0).toLocaleString();
function usd(n) {
  n = Number(n) || 0;
  if (Math.abs(n) >= 1000) return '$' + n.toLocaleString(undefined, { maximumFractionDigits: 0 });
  if (Math.abs(n) >= 1) return '$' + n.toFixed(2);
  return '$' + n.toFixed(4);
}
function dur(ms) {
  ms = Number(ms) || 0;
  if (ms < 1000) return Math.round(ms) + 'ms';
  const s = ms / 1000;
  if (s < 90) return s.toFixed(1) + 's';
  const m = s / 60;
  if (m < 90) return m.toFixed(1) + 'm';
  const h = m / 60;
  if (h < 48) return h.toFixed(1) + 'h';
  return (h / 24).toFixed(1) + 'd';
}
const pct = (n) => (Number(n) || 0).toFixed(1) + '%';
const shortDate = (d) => (d || '').slice(5);
/* Local YYYY-MM-DD. toISOString() would convert to UTC and shift the day. */
const isoLocal = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;

/* --------------------------------------------------------------- tooltip */
const tip = $('#tip');
function showTip(evt, title, rows) {
  tip.innerHTML = `<div class="t-title">${esc(title)}</div>` +
    rows.map(([k, v]) => `<div class="t-row"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('');
  tip.classList.add('on');
  moveTip(evt);
}
function moveTip(evt) {
  const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
  let x = evt.clientX + pad, y = evt.clientY + pad;
  if (x + w > innerWidth - 8) x = evt.clientX - w - pad;
  if (y + h > innerHeight - 8) y = evt.clientY - h - pad;
  tip.style.left = Math.max(4, x) + 'px';
  tip.style.top = Math.max(4, y) + 'px';
}
const hideTip = () => tip.classList.remove('on');

/* ------------------------------------------------------------- svg utils */
function svg(tag, attrs = {}) {
  const e = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const k in attrs) if (attrs[k] != null) e.setAttribute(k, attrs[k]);
  return e;
}
function niceTicks(max, count = 4) {
  if (!(max > 0)) return [0, 1];
  const raw = max / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
  const out = [];
  for (let v = 0; v <= max + step * 0.001; v += step) out.push(v);
  if (out[out.length - 1] < max) out.push(out[out.length - 1] + step);
  return out;
}

/**
 * Multi-series line / area / stacked-area chart.
 * series: [{name, values:[..], color?}]  labels: x axis labels
 */
function lineChart(host, { series, labels, height = 230, fmt = num, stacked = false, area = false }) {
  host.innerHTML = '';
  if (!series.length || !labels.length) { host.innerHTML = '<div class="empty">No data in range</div>'; return; }
  const W = Math.max(320, host.clientWidth || 640), H = height;
  const M = { t: 10, r: 12, b: 26, l: 52 };
  const iw = W - M.l - M.r, ih = H - M.t - M.b;

  const n = labels.length;
  const stacks = [];
  if (stacked) {
    let acc = new Array(n).fill(0);
    for (const s of series) {
      const lo = acc.slice();
      acc = acc.map((v, i) => v + (Number(s.values[i]) || 0));
      stacks.push({ lo, hi: acc.slice() });
    }
  }
  const max = stacked
    ? Math.max(1, ...(stacks.length ? stacks[stacks.length - 1].hi : [1]))
    : Math.max(1, ...series.flatMap(s => s.values.map(v => Number(v) || 0)));
  const ticks = niceTicks(max);
  const top = ticks[ticks.length - 1] || 1;
  const X = (i) => M.l + (n === 1 ? iw / 2 : (i / (n - 1)) * iw);
  const Y = (v) => M.t + ih - (Math.max(0, Number(v) || 0) / top) * ih;

  const root = svg('svg', { class: 'chart', width: W, height: H, viewBox: `0 0 ${W} ${H}` });

  for (const t of ticks) {
    root.appendChild(svg('line', { x1: M.l, x2: W - M.r, y1: Y(t), y2: Y(t), stroke: 'var(--rule)', 'stroke-width': 1 }));
    const lb = svg('text', { x: M.l - 7, y: Y(t) + 4, 'text-anchor': 'end', fill: 'var(--ink-3)', 'font-size': 10.5 });
    lb.textContent = fmt(t);
    root.appendChild(lb);
  }

  series.forEach((s, si) => {
    const color = s.color || PALETTE[si % PALETTE.length];
    const pts = s.values.map((v, i) => [X(i), Y(stacked ? stacks[si].hi[i] : v)]);
    if (stacked || area) {
      const base = stacked ? stacks[si].lo.map((v, i) => [X(i), Y(v)]).reverse()
                           : [[X(n - 1), Y(0)], [X(0), Y(0)]];
      const d = 'M' + pts.map(p => p.join(',')).join('L') + 'L' + base.map(p => p.join(',')).join('L') + 'Z';
      root.appendChild(svg('path', { d, fill: color, 'fill-opacity': stacked ? 0.55 : 0.16, stroke: 'none' }));
    }
    root.appendChild(svg('path', {
      d: 'M' + pts.map(p => p.join(',')).join('L'),
      fill: 'none', stroke: color,
      // In a stack the fills carry the story; heavy strokes just add noise.
      'stroke-width': stacked ? 1.1 : 1.9,
      'stroke-linejoin': 'round', 'stroke-linecap': 'round',
    }));
    // Emphasise where the series ends — the latest reading is the one that matters.
    const last = pts[pts.length - 1];
    if (last && !stacked) {
      root.appendChild(svg('circle', {
        cx: last[0], cy: last[1], r: 3.2, fill: color,
        stroke: 'var(--panel)', 'stroke-width': 1.5,
      }));
    }
  });

  // x labels: at most ~8
  const step = Math.max(1, Math.ceil(n / 8));
  for (let i = 0; i < n; i += step) {
    const t = svg('text', { x: X(i), y: H - 8, 'text-anchor': 'middle', fill: 'var(--ink-3)', 'font-size': 10.5 });
    t.textContent = labels[i];
    root.appendChild(t);
  }

  // hover targets
  const marker = svg('line', { y1: M.t, y2: M.t + ih, stroke: 'var(--ink-3)', 'stroke-width': 1, 'stroke-dasharray': '3 3', opacity: 0 });
  root.appendChild(marker);
  const band = iw / Math.max(1, n - 1);
  for (let i = 0; i < n; i++) {
    const r = svg('rect', {
      x: X(i) - band / 2, y: M.t, width: Math.max(band, 6), height: ih, fill: 'transparent',
    });
    r.addEventListener('mouseenter', (e) => {
      marker.setAttribute('x1', X(i)); marker.setAttribute('x2', X(i)); marker.setAttribute('opacity', 1);
      showTip(e, labels[i], series.map((s, si) => [s.name, fmt(s.values[i])]).filter(r => r[1] !== '0'));
    });
    r.addEventListener('mousemove', moveTip);
    r.addEventListener('mouseleave', () => { marker.setAttribute('opacity', 0); hideTip(); });
    root.appendChild(r);
  }
  host.appendChild(root);

  if (series.length > 1) {
    const lg = document.createElement('div');
    lg.className = 'legend';
    lg.innerHTML = series.map((s, i) =>
      `<span><i style="background:${s.color || PALETTE[i % PALETTE.length]}"></i>${esc(s.name)}</span>`).join('');
    host.appendChild(lg);
  }
}

/** Vertical bar chart (single series). */
function barChart(host, { labels, values, height = 200, fmt = num, color = PALETTE[0], tipLabel = 'value' }) {
  host.innerHTML = '';
  if (!labels.length) { host.innerHTML = '<div class="empty">No data</div>'; return; }
  const W = Math.max(320, host.clientWidth || 640), H = height;
  const M = { t: 10, r: 10, b: 24, l: 48 };
  const iw = W - M.l - M.r, ih = H - M.t - M.b;
  const max = Math.max(1, ...values.map(v => Number(v) || 0));
  const ticks = niceTicks(max);
  const top = ticks[ticks.length - 1] || 1;
  const bw = iw / labels.length;
  const root = svg('svg', { class: 'chart', width: W, height: H, viewBox: `0 0 ${W} ${H}` });

  for (const t of ticks) {
    const y = M.t + ih - (t / top) * ih;
    root.appendChild(svg('line', { x1: M.l, x2: W - M.r, y1: y, y2: y, stroke: 'var(--rule)' }));
    const lb = svg('text', { x: M.l - 7, y: y + 4, 'text-anchor': 'end', fill: 'var(--ink-3)', 'font-size': 10.5 });
    lb.textContent = fmt(t);
    root.appendChild(lb);
  }
  labels.forEach((lab, i) => {
    const v = Number(values[i]) || 0;
    const h = (v / top) * ih;
    const r = svg('rect', {
      x: M.l + i * bw + bw * 0.15, y: M.t + ih - h,
      width: bw * 0.7, height: Math.max(h, v > 0 ? 1 : 0), fill: color, rx: 2,
    });
    r.addEventListener('mouseenter', (e) => showTip(e, String(lab), [[tipLabel, fmt(v)]]));
    r.addEventListener('mousemove', moveTip);
    r.addEventListener('mouseleave', hideTip);
    root.appendChild(r);
    const stepL = Math.max(1, Math.ceil(labels.length / 12));
    if (i % stepL === 0) {
      const t = svg('text', { x: M.l + i * bw + bw / 2, y: H - 7, 'text-anchor': 'middle', fill: 'var(--ink-3)', 'font-size': 10.5 });
      t.textContent = lab;
      root.appendChild(t);
    }
  });
  host.appendChild(root);
}

/** Horizontal ranked bars (HTML, so labels wrap nicely). */
function hbars(host, rows, { fmt = num, max = null, color = PALETTE[0], sub = null } = {}) {
  if (!rows.length) { host.innerHTML = '<div class="empty">No data</div>'; return; }
  const m = max ?? Math.max(...rows.map(r => Number(r.value) || 0), 1);
  host.innerHTML = `<table><tbody>${rows.map(r => {
    const v = Number(r.value) || 0;
    const w = Math.max(0.5, (v / m) * 100);
    return `<tr>
      <td class="name" title="${esc(r.label)}">${esc(r.label)}</td>
      <td class="bar-cell from-left" style="width:62%">
        <div class="fill" style="width:${w}%;background:${color};opacity:.22"></div>
        <span class="mono">${esc(fmt(v))}${sub && r.sub != null ? ` <span style="color:var(--ink-3)">${esc(r.sub)}</span>` : ''}</span>
      </td></tr>`;
  }).join('')}</tbody></table>`;
}

/** Weekday x hour punch card. */
function punchcard(host, cells, { valueKey = 'api_calls' } = {}) {
  host.innerHTML = '';
  const grid = {};
  let max = 0;
  for (const c of cells) {
    const v = Number(c[valueKey]) || 0;
    grid[`${c.dow}:${c.hour}`] = v;
    if (v > max) max = v;
  }
  if (!max) { host.innerHTML = '<div class="empty">No data</div>'; return; }
  const W = Math.max(420, host.clientWidth || 700);
  const left = 38, topPad = 16, cell = (W - left - 8) / 24, H = topPad + 7 * cell + 16;
  const root = svg('svg', { class: 'chart', width: W, height: H, viewBox: `0 0 ${W} ${H}` });

  for (let h = 0; h < 24; h += 3) {
    const t = svg('text', { x: left + h * cell + cell / 2, y: 11, 'text-anchor': 'middle', fill: 'var(--ink-3)', 'font-size': 10 });
    t.textContent = h;
    root.appendChild(t);
  }
  for (let d = 0; d < 7; d++) {
    const t = svg('text', { x: left - 7, y: topPad + d * cell + cell / 2 + 4, 'text-anchor': 'end', fill: 'var(--ink-3)', 'font-size': 10.5 });
    t.textContent = DOW[d];
    root.appendChild(t);
    for (let h = 0; h < 24; h++) {
      const v = grid[`${d}:${h}`] || 0;
      const rad = v ? Math.max(2, Math.sqrt(v / max) * (cell / 2 - 1.5)) : 0;
      root.appendChild(svg('rect', {
        x: left + h * cell + 1, y: topPad + d * cell + 1,
        width: cell - 2, height: cell - 2, rx: 3,
        fill: 'var(--panel-2)',
      }));
      if (!v) continue;
      const c = svg('circle', {
        cx: left + h * cell + cell / 2, cy: topPad + d * cell + cell / 2,
        r: rad, fill: PALETTE[0], 'fill-opacity': 0.35 + 0.65 * (v / max),
      });
      c.addEventListener('mouseenter', (e) => showTip(e, `${DOW[d]} ${h}:00`, [[valueKey, int(v)]]));
      c.addEventListener('mousemove', moveTip);
      c.addEventListener('mouseleave', hideTip);
      root.appendChild(c);
    }
  }
  host.appendChild(root);
}

/** GitHub-style contribution calendar. */
function calendar(host, daily, { key = 'total_tokens', fmt = num } = {}) {
  host.innerHTML = '';
  if (!daily.length) { host.innerHTML = '<div class="empty">No data</div>'; return; }
  const byDate = new Map(daily.map(d => [d.date, d]));
  const first = new Date(daily[0].date + 'T00:00:00');
  const last = new Date(daily[daily.length - 1].date + 'T00:00:00');
  const start = new Date(first);
  start.setDate(start.getDate() - ((start.getDay() + 6) % 7)); // back to Monday
  const days = Math.round((last - start) / 86400000) + 1;
  const weeks = Math.ceil(days / 7);
  const cell = 13, gap = 3, left = 30, topPad = 14;
  const W = left + weeks * (cell + gap) + 6, H = topPad + 7 * (cell + gap) + 16;
  const max = Math.max(1, ...daily.map(d => Number(d[key]) || 0));
  const root = svg('svg', { class: 'chart', width: W, height: H, viewBox: `0 0 ${W} ${H}` });

  for (let d = 0; d < 7; d += 2) {
    const t = svg('text', { x: left - 5, y: topPad + d * (cell + gap) + cell - 2, 'text-anchor': 'end', fill: 'var(--ink-3)', 'font-size': 9.5 });
    t.textContent = DOW[d];
    root.appendChild(t);
  }
  let lastMonth = -1;
  for (let i = 0; i < days; i++) {
    const dt = new Date(start); dt.setDate(start.getDate() + i);
    const iso = isoLocal(dt);
    const w = Math.floor(i / 7), dow = i % 7;
    const rec = byDate.get(iso);
    const v = rec ? Number(rec[key]) || 0 : 0;
    const x = left + w * (cell + gap), y = topPad + dow * (cell + gap);
    if (dt.getMonth() !== lastMonth && dow <= 1) {
      lastMonth = dt.getMonth();
      const t = svg('text', { x, y: 9, fill: 'var(--ink-3)', 'font-size': 9.5 });
      t.textContent = dt.toLocaleString(undefined, { month: 'short' });
      root.appendChild(t);
    }
    const alpha = v ? 0.18 + 0.82 * Math.sqrt(v / max) : 0;
    const r = svg('rect', {
      x, y, width: cell, height: cell, rx: 3,
      fill: v ? PALETTE[0] : 'var(--panel-2)', 'fill-opacity': v ? alpha : 1,
    });
    if (rec) {
      r.addEventListener('mouseenter', (e) => showTip(e, iso, [
        ['tokens', num(rec.total_tokens)],
        ['you typed', int(rec.prompts)],
        ['model replies', int(rec.api_calls)],
        ['sessions', int(rec.sessions)], ['cost', usd(rec.cost_usd)]]));
      r.addEventListener('mousemove', moveTip);
      r.addEventListener('mouseleave', hideTip);
    }
    root.appendChild(r);
  }
  const wrap = document.createElement('div');
  wrap.className = 'chart-scroll';
  wrap.appendChild(root);
  host.appendChild(wrap);
}

/** Donut with a centre total. */
function donut(host, slices, { fmt = num, centreLabel = '' } = {}) {
  host.innerHTML = '';
  const total = slices.reduce((a, s) => a + (Number(s.value) || 0), 0);
  if (!total) { host.innerHTML = '<div class="empty">No data</div>'; return; }
  const size = 190, r = 78, ir = 50, cx = size / 2, cy = size / 2;
  const root = svg('svg', { class: 'chart', width: size, height: size, viewBox: `0 0 ${size} ${size}` });
  let a0 = -Math.PI / 2;
  slices.forEach((s, i) => {
    const frac = (Number(s.value) || 0) / total;
    if (frac <= 0) return;
    const a1 = a0 + frac * Math.PI * 2;
    const large = frac > 0.5 ? 1 : 0;
    const p = (ang, rad) => [cx + Math.cos(ang) * rad, cy + Math.sin(ang) * rad];
    const [x0, y0] = p(a0, r), [x1, y1] = p(a1, r);
    const [x2, y2] = p(a1, ir), [x3, y3] = p(a0, ir);
    const path = svg('path', {
      d: `M${x0},${y0}A${r},${r} 0 ${large} 1 ${x1},${y1}L${x2},${y2}A${ir},${ir} 0 ${large} 0 ${x3},${y3}Z`,
      fill: s.color || PALETTE[i % PALETTE.length],
    });
    path.addEventListener('mouseenter', (e) => showTip(e, s.label, [['value', fmt(s.value)], ['share', pct(frac * 100)]]));
    path.addEventListener('mousemove', moveTip);
    path.addEventListener('mouseleave', hideTip);
    root.appendChild(path);
    a0 = a1;
  });
  const t1 = svg('text', { x: cx, y: cy - 2, 'text-anchor': 'middle', fill: 'var(--ink)', 'font-size': 17, 'font-weight': 650 });
  t1.textContent = fmt(total);
  root.appendChild(t1);
  if (centreLabel) {
    const t2 = svg('text', { x: cx, y: cy + 14, 'text-anchor': 'middle', fill: 'var(--ink-3)', 'font-size': 10.5 });
    t2.textContent = centreLabel;
    root.appendChild(t2);
  }
  const wrap = document.createElement('div');
  wrap.style.cssText = 'display:flex;gap:16px;align-items:center;flex-wrap:wrap';
  wrap.appendChild(root);
  const lg = document.createElement('div');
  lg.className = 'legend';
  lg.style.cssText = 'flex-direction:column;gap:5px;margin:0';
  lg.innerHTML = slices.map((s, i) =>
    `<span><i style="background:${s.color || PALETTE[i % PALETTE.length]}"></i>${esc(s.label)} <b style="color:var(--ink)">${esc(fmt(s.value))}</b></span>`).join('');
  wrap.appendChild(lg);
  host.appendChild(wrap);
}

/* --------------------------------------------------------------- widgets */
function kpi(label, value, sub) {
  return `<div class="metric"><div class="m-label">${esc(label)}</div>
    <div class="m-value">${esc(value)}</div>
    ${sub ? `<div class="m-note">${sub}</div>` : ''}</div>`;
}
function card(title, hint, bodyHtml, cls = '') {
  return `<div class="panel ${cls}"><h3>${esc(title)}</h3>
    ${hint ? `<p class="hint">${hint}</p>` : ''}<div class="body">${bodyHtml}</div></div>`;
}
/** Sortable table. cols: [{k, label, fmt, cls, bar}] */
function table(rows, cols, { sortKey = null, max = 200 } = {}) {
  if (!rows || !rows.length) return '<div class="empty">No data</div>';
  const id = 't' + Math.random().toString(36).slice(2, 8);
  const maxes = {};
  for (const c of cols) if (c.bar) maxes[c.k] = Math.max(...rows.map(r => Number(r[c.k]) || 0), 1);
  const body = rows.slice(0, max).map(r => '<tr>' + cols.map(c => {
    const raw = r[c.k];
    const val = c.fmt ? c.fmt(raw, r) : esc(raw ?? '');
    const cls = (c.cls || '') + (c.num ? ' num' : '');
    if (c.bar) {
      const w = Math.max(0.5, ((Number(raw) || 0) / maxes[c.k]) * 100);
      return `<td class="bar-cell ${cls}"><div class="fill" style="width:${w}%"></div><span>${val}</span></td>`;
    }
    return `<td class="${cls}"${c.k === 'name' || c.title ? ` title="${esc(raw ?? '')}"` : ''}>${val}</td>`;
  }).join('') + '</tr>').join('');
  return `<div class="tw"><table id="${id}" data-sort="${esc(sortKey || '')}">
    <thead><tr>${cols.map((c, i) =>
      `<th class="${c.num ? 'num' : ''}" data-col="${i}">${esc(c.label)}</th>`).join('')}</tr></thead>
    <tbody>${body}</tbody></table>
    ${rows.length > max ? `<p class="hint">showing top ${max} of ${int(rows.length)}</p>` : ''}</div>`;
}

/* Attach sorting to every rendered table. */
function wireTables(root) {
  root.querySelectorAll('table[data-sort]').forEach(tbl => {
    tbl.querySelectorAll('th[data-col]').forEach(th => {
      th.addEventListener('click', () => {
        const idx = +th.dataset.col;
        const asc = th.classList.contains('sorted') && !th.classList.contains('asc');
        tbl.querySelectorAll('th').forEach(o => o.classList.remove('sorted', 'asc'));
        th.classList.add('sorted');
        if (asc) th.classList.add('asc');
        const rows = [...tbl.tBodies[0].rows];
        const val = (tr) => {
          const txt = tr.cells[idx].innerText.trim().replace(/[$,%]/g, '');
          const m = txt.match(/^-?[\d.]+/);
          if (!m) return txt.toLowerCase();
          let n = parseFloat(m[0]);
          if (/k$/i.test(txt)) n *= 1e3; else if (/M$/.test(txt)) n *= 1e6;
          else if (/B$/.test(txt)) n *= 1e9; else if (/T$/.test(txt)) n *= 1e12;
          return n;
        };
        rows.sort((a, b) => {
          const x = val(a), y = val(b);
          const r = typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y));
          return asc ? r : -r;
        });
        rows.forEach(r => tbl.tBodies[0].appendChild(r));
      });
    });
  });
}

/* Charts are declared in HTML as <div data-chart="id"> then filled after mount. */
const pending = [];
const chartSlot = (id) => `<div data-chart="${id}"></div>`;
function draw(id, fn) { pending.push([id, fn]); }
function flushCharts(root) {
  for (const [id, fn] of pending) {
    const host = root.querySelector(`[data-chart="${id}"]`);
    if (host) try { fn(host); } catch (e) { host.innerHTML = `<div class="empty">chart error: ${esc(e.message)}</div>`; }
  }
  pending.length = 0;
}

/* ---------------------------------------------------------------- tabs */
function renderOverview() {
  const c = DATA.claude, o = c.overview, st = c.streaks, cur = DATA.cursor.overview;
  const warn = DATA.meta.pricing_warnings || [];
  let html = '';

  if (warn.length) {
    html += `<div class="note warn"><b>Cost figures are estimates.</b> No verified price for
      ${warn.map(w => `<span class="mono">${esc(w.model)}</span>`).join(', ')}.
      Edit <span class="mono">aidash/pricing.json</span> to match your plan, then reload.</div>`;
  }

  html += `<div class="metrics">
    ${kpi('Est. spend', usd(o.cost_usd), `${int(o.assistant_messages)} billed API calls`)}
    ${kpi('Total tokens', num(o.total_tokens), `${num(o.output_tokens)} generated`)}
    ${kpi('Cache hit rate', pct(o.cache_hit_pct), `${num(o.cache_read)} tokens re-read`)}
    ${kpi('Sessions', int(o.sessions), `across ${int(o.projects)} projects`)}
    ${kpi('Tool calls', int(o.tool_calls), `${pct(o.tool_error_rate)} errored`)}
    ${kpi('Lines written', num(o.lines_added), `${num(o.lines_removed)} removed · ${int(o.files_touched)} files`)}
    ${kpi('Prompts you typed', int(o.prompts),
      `avg ${int(Math.round(o.avg_prompt_chars))} chars · ${int((o.prompt_records || 0) - (o.prompts || 0))} more were automated`)}
    ${kpi('Active days', int(o.active_days), `streak ${st.current}d · best ${st.longest}d`)}
    ${kpi('Model time', dur(o.total_turn_ms), `${int(o.turns)} turns · avg ${dur(o.avg_turn_ms)}`)}
    ${kpi('Thinking blocks', num(o.thinking_blocks), 'reasoning text is not stored locally')}
    ${kpi('Permission denials', int(o.denials), 'tools you rejected or blocked')}
    ${kpi('Cursor sessions', int(cur.sessions), `${num(cur.messages)} messages`)}
  </div>`;

  html += card('Daily token usage', `${esc(o.first_day || '?')} → ${esc(o.last_day || '?')}, local time`,
    chartSlot('ov-daily'), 'full');
  draw('ov-daily', h => lineChart(h, {
    labels: c.daily.map(d => shortDate(d.date)),
    series: [
      { name: 'cache read', values: c.daily.map(d => d.cache_read) },
      { name: 'cache write', values: c.daily.map(d => d.cache_write) },
      { name: 'output', values: c.daily.map(d => d.output_tokens) },
      { name: 'input', values: c.daily.map(d => d.input_tokens) },
    ], stacked: true, height: 260,
  }));

  html += `<div class="grid cols-2">
    ${card('Activity calendar', 'Tokens per day', chartSlot('ov-cal'))}
    ${card('Spend by model', 'Estimated, from local pricing table', chartSlot('ov-model'))}
  </div>`;
  draw('ov-cal', h => calendar(h, c.daily));
  draw('ov-model', h => donut(h, c.models.map(m => ({ label: m.model, value: m.cost_usd })),
    { fmt: usd, centreLabel: 'estimated' }));

  html += `<div class="grid cols-2">
    ${card('Busiest projects', 'By total tokens', chartSlot('ov-proj'))}
    ${card('Most-used tools', 'Call count', chartSlot('ov-tools'))}
  </div>`;
  draw('ov-proj', h => hbars(h, c.projects.slice(0, 12).map(p => ({ label: p.project, value: p.total_tokens })), { color: PALETTE[1] }));
  draw('ov-tools', h => hbars(h, c.tools.slice(0, 12).map(t => ({ label: t.name, value: t.calls })), { fmt: int, color: PALETTE[2] }));

  return html;
}

function renderActivity() {
  const c = DATA.claude;
  // Two charts, not one: what the person did and what the model did differ by
  // ~100x, so sharing a y-axis would flatten the human series into the floor.
  let html = `<div class="grid cols-2">
    ${card('What you did', 'Prompts you actually typed, and sessions started',
      chartSlot('ac-human'))}
    ${card('What the model did', 'Billed API responses and tool calls — note the scale',
      chartSlot('ac-model'))}
  </div>`;
  draw('ac-human', h => lineChart(h, {
    labels: c.daily.map(d => shortDate(d.date)),
    series: [
      { name: 'prompts you typed', values: c.daily.map(d => d.prompts) },
      { name: 'sessions', values: c.daily.map(d => d.sessions), color: PALETTE[2] },
    ], fmt: int, height: 220,
  }));
  draw('ac-model', h => lineChart(h, {
    labels: c.daily.map(d => shortDate(d.date)),
    series: [
      { name: 'model replies', values: c.daily.map(d => d.api_calls), color: PALETTE[1] },
      { name: 'tool calls', values: c.daily.map(d => d.tool_uses), color: PALETTE[3] },
    ], fmt: int, height: 220,
  }));

  html += `<div class="grid cols-2">
    ${card('When the work happens', 'Model replies by weekday and hour (local time)', chartSlot('ac-punch'))}
    ${card('Hour of day', 'Model replies across the whole range', chartSlot('ac-hour'))}
  </div>`;
  draw('ac-punch', h => punchcard(h, c.punchcard));
  draw('ac-hour', h => barChart(h, {
    labels: c.hourly.map(r => r.hour), values: c.hourly.map(r => r.api_calls),
    fmt: int, tipLabel: 'model replies',
  }));

  html += `<div class="grid cols-2">
    ${card('Turn duration', 'Average wall-clock seconds per turn, per day', chartSlot('ac-turn'))}
    ${card('How long your prompts are', 'Average characters per typed prompt, per day', chartSlot('ac-prompt'))}
  </div>`;
  draw('ac-turn', h => lineChart(h, {
    labels: c.misc.turns_daily.map(r => shortDate(r.date)),
    series: [{ name: 'avg turn (s)', values: c.misc.turns_daily.map(r => r.avg_turn_s), color: PALETTE[3] }],
    fmt: (v) => (Number(v) || 0).toFixed(0) + 's', area: true,
  }));
  draw('ac-prompt', h => lineChart(h, {
    labels: c.prompts.daily.map(r => shortDate(r.date)),
    series: [{ name: 'avg chars per prompt', values: c.prompts.daily.map(r => r.avg_chars), color: PALETTE[5] }],
    fmt: int, area: true,
  }));

  html += `<div class="grid cols-2">
    ${card('Where prompts came from',
      'A "user" turn in the transcript is often not you — SDK runs, task notifications and sub-agent instructions all land there.',
      table(c.prompts.by_source,
      [{ k: 'source', label: 'Source' },
       { k: 'who', label: 'Origin', fmt: (v) => `<span class="chip ${v === 'you' ? 'ok' : ''}">${esc(v)}</span>` },
       { k: 'n', label: 'Count', num: true, fmt: int, bar: true },
       { k: 'avg_chars', label: 'Avg chars', num: true, fmt: int }]))}
    ${card('Your longest prompts', '', table(c.prompts.longest,
      [{ k: 'date', label: 'Date' }, { k: 'chars', label: 'Chars', num: true, fmt: int },
       { k: 'preview', label: 'Preview', cls: 'name', fmt: (v) => `<span class="mono">${esc((v || '').slice(0, 90))}</span>` }]))}
  </div>`;

  html += card('Session activity streaks', '',
    `<div class="metrics">
      ${kpi('Current streak', c.streaks.current + ' days', '')}
      ${kpi('Longest streak', c.streaks.longest + ' days', '')}
      ${kpi('Days with activity', int(c.streaks.days), `${esc(c.streaks.first || '')} → ${esc(c.streaks.last || '')}`)}
    </div>`);
  return html;
}

function renderCost() {
  const c = DATA.claude, o = c.overview;
  const billedIn = (o.input_tokens || 0) + (o.cache_write || 0);
  let html = `<div class="note info">Costs are computed locally from token counts in your
    transcripts multiplied by <span class="mono">aidash/pricing.json</span> — they are an
    <b>estimate</b>, not billing data. Cache writes are charged at
    1.25× (5&nbsp;min) / 2× (1&nbsp;h) input, cache reads at 0.1× input.</div>`;

  html += `<div class="metrics">
    ${kpi('Estimated total', usd(o.cost_usd), '')}
    ${kpi('Per active day', usd(o.avg_cost_per_active_day), `${int(o.active_days)} days`)}
    ${kpi('Input tokens', num(o.input_tokens), 'fresh, uncached')}
    ${kpi('Cache writes', num(o.cache_write), 'charged above input rate')}
    ${kpi('Cache reads', num(o.cache_read), 'charged at 10% of input')}
    ${kpi('Output tokens', num(o.output_tokens), 'the expensive side')}
    ${kpi('Cache leverage', (billedIn ? ((o.cache_read || 0) / billedIn).toFixed(1) : '0') + '×',
      'read vs newly-billed input')}
    ${kpi('Web calls', int((o.web_search_calls || 0) + (o.web_fetch_calls || 0)),
      `${int(o.web_search_calls)} search · ${int(o.web_fetch_calls)} fetch`)}
  </div>`;

  html += card('Estimated spend per day', '', chartSlot('co-daily'), 'full');
  draw('co-daily', h => lineChart(h, {
    labels: c.daily.map(d => shortDate(d.date)),
    series: [{ name: 'cost', values: c.daily.map(d => d.cost_usd), color: PALETTE[3] }],
    fmt: usd, area: true, height: 230,
  }));

  html += card('By model', 'Sortable — click a column header', table(c.models, [
    { k: 'model', label: 'Model', cls: 'mono' },
    { k: 'price_confidence', label: 'Price', fmt: (v) => {
        const cls = v === 'official' ? 'ok' : v === 'assumed' ? 'warn' : 'bad';
        return `<span class="chip ${cls}">${esc(v)}</span>`; } },
    { k: 'messages', label: 'API calls', num: true, fmt: int },
    { k: 'sessions', label: 'Sessions', num: true, fmt: int },
    { k: 'input_tokens', label: 'Input', num: true, fmt: num },
    { k: 'output_tokens', label: 'Output', num: true, fmt: num },
    { k: 'cache_write', label: 'Cache W', num: true, fmt: num },
    { k: 'cache_read', label: 'Cache R', num: true, fmt: num },
    { k: 'total_tokens', label: 'Total', num: true, fmt: num, bar: true },
    { k: 'avg_output_tokens', label: 'Avg out', num: true, fmt: int },
    { k: 'cost_usd', label: 'Est. cost', num: true, fmt: usd, bar: true },
  ]), 'full');

  html += `<div class="grid cols-2">
    ${card('Token mix', 'Where the tokens go', chartSlot('co-mix'))}
    ${card('Tokens by model', '', chartSlot('co-model-tok'))}
  </div>`;
  draw('co-mix', h => donut(h, [
    { label: 'cache read', value: o.cache_read }, { label: 'cache write', value: o.cache_write },
    { label: 'output', value: o.output_tokens }, { label: 'input', value: o.input_tokens },
  ], { centreLabel: 'tokens' }));
  draw('co-model-tok', h => hbars(h, c.models.map(m => ({ label: m.model, value: m.total_tokens })), { color: PALETTE[1] }));

  const lg = c.legacy;
  if (lg && lg.daily.length) {
    html += card('Recovered history (pruned transcripts)',
      `Claude Code deletes old transcripts but keeps a rollup in
       <span class="mono">stats-cache.json</span>. These
       <b>${int(lg.days_only_in_cache)}</b> day(s) predate your oldest surviving transcript
       (${esc(lg.live_first_day || '?')}) and are <b>not</b> included in the totals above.`,
      table(lg.daily, [
        { k: 'date', label: 'Date' },
        { k: 'messages', label: 'Messages', num: true, fmt: int, bar: true },
        { k: 'sessions', label: 'Sessions', num: true, fmt: int },
        { k: 'tool_calls', label: 'Tool calls', num: true, fmt: int },
        { k: 'tokens', label: 'Tokens', num: true, fmt: num, bar: true },
      ], { max: 60 }), 'full');
  }
  return html;
}

function renderProjects() {
  const c = DATA.claude;
  let html = card('Projects', 'Every directory you have run Claude Code in', table(c.projects, [
    { k: 'project', label: 'Project', cls: 'name' },
    { k: 'sessions', label: 'Sessions', num: true, fmt: int },
    { k: 'messages', label: 'API calls', num: true, fmt: int },
    { k: 'active_days', label: 'Days', num: true, fmt: int },
    { k: 'total_tokens', label: 'Tokens', num: true, fmt: num, bar: true },
    { k: 'cost_usd', label: 'Est. cost', num: true, fmt: usd, bar: true },
    { k: 'tool_calls', label: 'Tools', num: true, fmt: int },
    { k: 'lines_added', label: '+Lines', num: true, fmt: int },
    { k: 'lines_removed', label: '−Lines', num: true, fmt: int },
    { k: 'files_touched', label: 'Files', num: true, fmt: int },
    { k: 'turn_ms', label: 'Model time', num: true, fmt: dur },
    { k: 'last_day', label: 'Last active' },
  ]), 'full');

  html += `<div class="grid cols-2">
    ${card('Cost share', '', chartSlot('pr-cost'))}
    ${card('Lines added', '', chartSlot('pr-lines'))}
  </div>`;
  draw('pr-cost', h => donut(h, c.projects.slice(0, 9).map(p => ({ label: p.project, value: p.cost_usd })),
    { fmt: usd, centreLabel: 'est. spend' }));
  draw('pr-lines', h => hbars(h, c.projects.slice(0, 14).map(p => ({ label: p.project, value: p.lines_added })),
    { fmt: int, color: PALETTE[2] }));

  html += `<div class="grid cols-2">
    ${card('Git branches', 'Where the work happened', table(c.misc.branches,
      [{ k: 'git_branch', label: 'Branch', cls: 'mono' },
       { k: 'messages', label: 'API calls', num: true, fmt: int, bar: true },
       { k: 'sessions', label: 'Sessions', num: true, fmt: int }]))}
    ${card('Claude Code versions', 'Which build produced the messages', table(c.misc.versions,
      [{ k: 'version', label: 'Version', cls: 'mono' },
       { k: 'messages', label: 'API calls', num: true, fmt: int, bar: true },
       { k: 'first_day', label: 'First seen' }, { k: 'last_day', label: 'Last seen' }], { max: 20 }))}
  </div>`;
  return html;
}

function renderTools() {
  const c = DATA.claude, o = c.overview;
  let html = `<div class="metrics">
    ${kpi('Tool calls', int(o.tool_calls), '')}
    ${kpi('Errors', int(o.tool_errors), pct(o.tool_error_rate) + ' of calls')}
    ${kpi('Interrupted', int(o.interrupted), 'you stopped it mid-run')}
    ${kpi('Denied', int(o.denials), 'permission refused')}
    ${kpi('MCP calls', int(o.mcp_calls), 'external servers')}
    ${kpi('Sub-agents', int((c.misc.subagents || []).reduce((a, s) => a + s.runs, 0)), 'Agent tool runs')}
  </div>`;

  html += card('Every tool',
    `Latency is the transcript gap between request and result. When a call waits on a
     permission prompt that gap includes <b>your</b> idle time, so the mean is skewed —
     prefer the median.`,
    table(c.tools, [
    { k: 'name', label: 'Tool', cls: 'mono name' },
    { k: 'calls', label: 'Calls', num: true, fmt: int, bar: true },
    { k: 'errors', label: 'Errors', num: true, fmt: (v, r) => {
        const p = r.calls ? (v / r.calls) * 100 : 0;
        return `${int(v)}${v ? ` <span class="chip ${p > 20 ? 'bad' : p > 5 ? 'warn' : ''}">${p.toFixed(0)}%</span>` : ''}`; } },
    { k: 'denied', label: 'Denied', num: true, fmt: int },
    { k: 'median_latency_ms', label: 'Median', num: true, fmt: (v) => v == null ? '—' : dur(v) },
    { k: 'avg_latency_ms', label: 'Mean', num: true, fmt: (v) => v == null ? '—' : dur(v) },
    { k: 'max_latency_ms', label: 'Max', num: true, fmt: (v) => v == null ? '—' : dur(v) },
    { k: 'avg_result_chars', label: 'Avg result', num: true, fmt: num },
    { k: 'total_result_chars', label: 'Total out', num: true, fmt: num, bar: true },
    { k: 'lines_added', label: '+Lines', num: true, fmt: int },
  ]), 'full');

  html += card('Tool usage over time', 'Top tools, calls per day', chartSlot('tl-daily'), 'full');
  draw('tl-daily', h => {
    const td = c.tool_daily;
    const dates = [...new Set(td.rows.map(r => r.date))].sort();
    const idx = new Map(dates.map((d, i) => [d, i]));
    const series = td.tools.map(name => {
      const vals = new Array(dates.length).fill(0);
      for (const r of td.rows) if (r.name === name) vals[idx.get(r.date)] = r.calls;
      return { name, values: vals };
    });
    lineChart(h, { labels: dates.map(shortDate), series, fmt: int, stacked: true, height: 250 });
  });

  html += `<div class="grid cols-2">
    ${card('Shell commands', 'First program in each Bash/PowerShell invocation', table(c.bash.programs,
      [{ k: 'program', label: 'Program', cls: 'mono' },
       { k: 'calls', label: 'Calls', num: true, fmt: int, bar: true },
       { k: 'errors', label: 'Errors', num: true, fmt: int },
       { k: 'avg_latency_ms', label: 'Avg', num: true, fmt: dur }], { max: 30 }))}
    ${card('Permission denials', 'Tools that were blocked or rejected', table(c.denials.by_tool,
      [{ k: 'name', label: 'Tool', cls: 'mono name' }, { k: 'denial_kind', label: 'Kind' },
       { k: 'n', label: 'Count', num: true, fmt: int, bar: true }], { max: 25 }))}
  </div>`;

  html += `<div class="grid cols-2">
    ${card('MCP servers', 'Model Context Protocol tool usage', table(c.misc.mcp,
      [{ k: 'mcp_server', label: 'Server', cls: 'mono' },
       { k: 'calls', label: 'Calls', num: true, fmt: int, bar: true },
       { k: 'distinct_tools', label: 'Tools', num: true, fmt: int },
       { k: 'errors', label: 'Errors', num: true, fmt: int }]))}
    ${card('Sub-agents', 'Work delegated to the Agent tool', table(c.misc.subagents,
      [{ k: 'agent_type', label: 'Type' }, { k: 'resolved_model', label: 'Model', cls: 'mono' },
       { k: 'runs', label: 'Runs', num: true, fmt: int, bar: true },
       { k: 'total_tokens', label: 'Tokens', num: true, fmt: num },
       { k: 'avg_duration_ms', label: 'Avg time', num: true, fmt: dur },
       { k: 'tool_uses', label: 'Tools', num: true, fmt: int }]))}
  </div>`;

  html += `<div class="grid cols-2">
    ${card('Skills invoked', '', table(c.misc.skills,
      [{ k: 'skill', label: 'Skill', cls: 'mono' },
       { k: 'messages', label: 'API calls', num: true, fmt: int, bar: true },
       { k: 'tokens', label: 'Tokens', num: true, fmt: num },
       { k: 'cost_usd', label: 'Est. cost', num: true, fmt: usd }]))}
    ${card('Session events', 'Attachments, system notices, queue operations', table(c.misc.events,
      [{ k: 'kind', label: 'Kind' }, { k: 'subkind', label: 'Detail', cls: 'mono name' },
       { k: 'n', label: 'Count', num: true, fmt: int, bar: true }], { max: 30 }))}
  </div>`;
  return html;
}

function renderCode() {
  const c = DATA.claude, o = c.overview;
  let html = `<div class="metrics">
    ${kpi('Lines added', num(o.lines_added), '')}
    ${kpi('Lines removed', num(o.lines_removed), '')}
    ${kpi('Net lines', num((o.lines_added || 0) - (o.lines_removed || 0)), '')}
    ${kpi('Files touched', int(o.files_touched), 'distinct paths')}
    ${kpi('Churn ratio', ((o.lines_added || 0) ? ((o.lines_removed || 0) / o.lines_added).toFixed(2) : '0'),
      'removed per added')}
  </div>`;

  html += `<div class="grid cols-2">
    ${card('By file type', 'Edits and reads per extension', chartSlot('cd-ext'))}
    ${card('Lines added by type', '', chartSlot('cd-ext-lines'))}
  </div>`;
  draw('cd-ext', h => hbars(h, c.files.by_ext.slice(0, 14).map(e => ({ label: e.file_ext, value: e.touches })),
    { fmt: int, color: PALETTE[0] }));
  draw('cd-ext-lines', h => hbars(h, [...c.files.by_ext].sort((a, b) => b.lines_added - a.lines_added)
    .slice(0, 14).map(e => ({ label: e.file_ext, value: e.lines_added })), { fmt: int, color: PALETTE[2] }));

  html += card('File type detail', '', table(c.files.by_ext, [
    { k: 'file_ext', label: 'Extension', cls: 'mono' },
    { k: 'files', label: 'Files', num: true, fmt: int },
    { k: 'touches', label: 'Tool calls', num: true, fmt: int, bar: true },
    { k: 'lines_added', label: '+Lines', num: true, fmt: int, bar: true },
    { k: 'lines_removed', label: '−Lines', num: true, fmt: int },
  ]), 'full');

  html += card('Most-touched files', 'Reads and writes across all sessions', table(c.files.top_files, [
    { k: 'file_path', label: 'Path', cls: 'mono name', title: true },
    { k: 'touches', label: 'Touches', num: true, fmt: int, bar: true },
    { k: 'reads', label: 'Reads', num: true, fmt: int },
    { k: 'writes', label: 'Writes', num: true, fmt: int },
    { k: 'lines_added', label: '+Lines', num: true, fmt: int, bar: true },
    { k: 'lines_removed', label: '−Lines', num: true, fmt: int },
    { k: 'sessions', label: 'Sessions', num: true, fmt: int },
  ]), 'full');
  return html;
}

function renderSessions() {
  const c = DATA.claude;
  const shape = c.session_shape || [];
  let html = '';

  html += `<div class="grid cols-2">
    ${card('Session size distribution', 'Replies per session', chartSlot('se-hist'))}
    ${card('Reasoning effort', 'Effort tag on assistant messages', table(c.misc.effort,
      [{ k: 'effort', label: 'Effort' }, { k: 'messages', label: 'API calls', num: true, fmt: int, bar: true },
       { k: 'thinking_blocks', label: 'Thinking blocks', num: true, fmt: int }]))}
  </div>`;
  draw('se-hist', h => {
    const buckets = [1, 2, 5, 10, 25, 50, 100, 250, 500, 1e9];
    const names = ['1', '2-4', '5-9', '10-24', '25-49', '50-99', '100-249', '250-499', '500+'];
    const counts = new Array(names.length).fill(0);
    for (const s of shape) {
      for (let i = 0; i < names.length; i++) {
        if (s.messages >= buckets[i] && s.messages < buckets[i + 1]) { counts[i]++; break; }
      }
    }
    barChart(h, { labels: names, values: counts, fmt: int, tipLabel: 'sessions', color: PALETTE[1] });
  });

  html += card('Heaviest sessions', 'Sorted by tokens — click any header to re-sort', table(c.sessions, [
    { k: 'title', label: 'Title', cls: 'name', title: true },
    { k: 'project', label: 'Project' },
    { k: 'messages', label: 'API calls', num: true, fmt: int },
    { k: 'total_tokens', label: 'Tokens', num: true, fmt: num, bar: true },
    { k: 'cost_usd', label: 'Est. cost', num: true, fmt: usd, bar: true },
    { k: 'tool_uses', label: 'Tools', num: true, fmt: int },
    { k: 'sidechain_messages', label: 'Sub-agent', num: true, fmt: int },
    { k: 'git_branch', label: 'Branch', cls: 'mono' },
    { k: 'started', label: 'Started', fmt: (v) => esc((v || '').slice(0, 16).replace('T', ' ')) },
  ]), 'full');

  html += `<div class="grid cols-2">
    ${card('Stop reasons', 'Why each reply ended', table(c.misc.stop_reasons,
      [{ k: 'stop_reason', label: 'Reason', cls: 'mono' },
       { k: 'n', label: 'Count', num: true, fmt: int, bar: true }]))}
    ${card('Prompt origins', '', table(c.prompts.by_origin,
      [{ k: 'origin', label: 'Origin' }, { k: 'n', label: 'Count', num: true, fmt: int, bar: true }]))}
  </div>`;
  return html;
}

function renderCursor() {
  const cu = DATA.cursor, o = cu.overview;
  let html = `<div class="note info"><b>Cursor stores chats locally but not usage metering.</b>
    ${esc(o.token_note)} Message counts, tool calls, timings and edited lines are reliable.</div>`;

  html += `<div class="metrics">
    ${kpi('Chat sessions', int(o.sessions), `${int(o.subagent_sessions)} sub-agent`)}
    ${kpi('Messages', num(o.messages), `${int(o.user_messages)} from you`)}
    ${kpi('Tool calls', num(o.tool_calls), '')}
    ${kpi('Lines added', num(o.lines_added), `${num(o.lines_removed)} removed`)}
    ${kpi('Files changed', int(o.files_changed), '')}
    ${kpi('AI-authored lines', int(o.ai_lines),
      o.ai_lines_capped ? `rolling buffer (capped) · ${int(o.ai_files)} files`
                        : `tracked across ${int(o.ai_files)} files`)}
    ${kpi('Avg context used', pct(o.avg_context_pct), 'per session')}
    ${kpi('Active days', int(o.active_days), `${esc(o.first_day || '')} → ${esc(o.last_day || '')}`)}
  </div>`;

  html += card('Cursor activity per day',
    `Only <b>${pct(o.ts_exact_pct)}</b> of messages carry a real timestamp
     (${int(o.ts_exact)} of ${int(o.messages)}); the rest are dated from their chat's
     creation day, so a long-running chat lands entirely on its start date.`,
    chartSlot('cu-daily'), 'full');
  draw('cu-daily', h => lineChart(h, {
    labels: cu.daily.map(d => shortDate(d.date)),
    series: [
      { name: 'messages', values: cu.daily.map(d => d.messages), color: PALETTE[1] },
      { name: 'tool calls', values: cu.daily.map(d => d.tool_calls), color: PALETTE[5] },
    ], fmt: int, height: 230,
  }));

  html += `<div class="grid cols-2">
    ${card('Hour of day', `Exact timestamps only — ${int(o.ts_exact)} messages`, chartSlot('cu-hour'))}
    ${card('Session modes', 'agent vs chat vs edit', chartSlot('cu-mode'))}
  </div>`;
  draw('cu-hour', h => barChart(h, {
    labels: cu.hourly.map(r => r.hour), values: cu.hourly.map(r => r.messages),
    fmt: int, tipLabel: 'messages', color: PALETTE[1],
  }));
  draw('cu-mode', h => donut(h, cu.modes.map(m => ({ label: m.mode, value: m.sessions })),
    { fmt: int, centreLabel: 'sessions' }));

  html += `<div class="grid cols-2">
    ${card('Tools', '', table(cu.tools,
      [{ k: 'name', label: 'Tool', cls: 'mono name' },
       { k: 'calls', label: 'Calls', num: true, fmt: int, bar: true },
       { k: 'errors', label: 'Errors', num: true, fmt: int },
       { k: 'avg_duration_ms', label: 'Avg', num: true, fmt: dur }], { max: 40 }))}
    ${card('AI-authored lines by type', 'From Cursor’s own code-attribution tracker', table(cu.ai_lines.by_ext,
      [{ k: 'file_ext', label: 'Extension', cls: 'mono' },
       { k: 'lines', label: 'Lines', num: true, fmt: int, bar: true },
       { k: 'files', label: 'Files', num: true, fmt: int }], { max: 25 }))}
  </div>`;

  html += card('Projects', '', table(cu.projects, [
    { k: 'project', label: 'Project', cls: 'name' },
    { k: 'sessions', label: 'Sessions', num: true, fmt: int },
    { k: 'messages', label: 'Messages', num: true, fmt: int, bar: true },
    { k: 'lines_added', label: '+Lines', num: true, fmt: int, bar: true },
    { k: 'lines_removed', label: '−Lines', num: true, fmt: int },
    { k: 'files_changed', label: 'Files', num: true, fmt: int },
    { k: 'last_day', label: 'Last active' },
  ]), 'full');

  html += card('Biggest chats', '', table(cu.sessions, [
    { k: 'name', label: 'Name', cls: 'name', title: true },
    { k: 'project', label: 'Project' },
    { k: 'mode', label: 'Mode' },
    { k: 'message_count', label: 'Messages', num: true, fmt: int, bar: true },
    { k: 'lines_added', label: '+Lines', num: true, fmt: int, bar: true },
    { k: 'files_changed', label: 'Files', num: true, fmt: int },
    { k: 'context_usage_pct', label: 'Context', num: true, fmt: (v) => v == null ? '' : pct(v) },
    { k: 'date', label: 'Started' },
  ]), 'full');
  return html;
}

function renderData() {
  const m = DATA.meta, c = DATA.claude;
  let html = card('Where this comes from', '', `
    <table><tbody>
      <tr><td>Claude Code</td><td class="mono">${esc(m.claude_dir)}</td>
          <td>transcripts, <span class="mono">stats-cache.json</span></td></tr>
      <tr><td>Cursor</td><td class="mono">${esc(m.cursor_dir || 'not found')}</td>
          <td><span class="mono">state.vscdb</span>, <span class="mono">conversation-search.db</span></td></tr>
      <tr><td>This database</td><td class="mono">${esc(m.db_path)}</td><td>rebuildable at any time</td></tr>
    </tbody></table>`, 'full');

  html += card('Ingest history', 'Incremental runs only read bytes appended since last time',
    table(m.runs, [
      { k: 'source', label: 'Source' }, { k: 'note', label: 'Mode' },
      { k: 'files_read', label: 'Files read', num: true, fmt: int },
      { k: 'files_seen', label: 'Files seen', num: true, fmt: int },
      { k: 'rows_added', label: 'Rows', num: true, fmt: int },
      { k: 'finished_at', label: 'Finished', fmt: (v) => esc((v || '').slice(0, 19).replace('T', ' ')) },
    ]), 'full');

  const warn = m.pricing_warnings || [];
  html += card('Pricing table', 'Only "official" entries are verified list prices',
    warn.length
      ? `<p class="hint">Unverified: ${warn.map(w =>
          `<span class="mono">${esc(w.model)}</span> (${esc(w.confidence)})`).join(', ')}.
          Edit <span class="mono">aidash/pricing.json</span> and re-run
          <span class="mono">python ingest.py --full</span> to recompute costs.</p>`
      : '<p class="hint">All models priced with verified rates.</p>',
    'full');

  html += card('Counts in the current filter', '', `<div class="metrics">
    ${kpi('Assistant messages', int(c.overview.assistant_messages), '')}
    ${kpi('Sessions', int(c.overview.sessions), '')}
    ${kpi('Tool calls', int(c.overview.tool_calls), '')}
    ${kpi('Prompts', int(c.overview.prompts), '')}
    ${kpi('Cursor messages', int(DATA.cursor.overview.messages), '')}
  </div>`, 'full');

  html += card('Notes & caveats', '', `<ul>
    <li><b>One API response is written to the transcript as many lines</b> — one per content
        block (thinking, text, each tool_use) — and the <span class="mono">usage</span> block is
        copied onto <i>every</i> line. Here that's ${int(c.overview.assistant_blocks)} lines for
        only <b>${int(c.overview.assistant_messages)}</b> real API calls. Tokens are therefore
        counted once per <span class="mono">message_id</span>; summing the raw lines would
        inflate every total by ~2.3× (up to 22× on a single response).</li>
    <li>Claude Code's own <span class="mono">stats-cache.json</span> appears to sum the raw
        lines, so the "recovered history" figures on the Cost tab are inflated the same way and
        are not comparable to the deduplicated totals.</li>
    <li><b>"Model replies" ≠ "your prompts".</b> A billed API call happens on every step of
        an agent loop, so the model replies far more often than you type. Here:
        <b>${int(c.overview.assistant_messages)}</b> model replies against
        <b>${int(c.overview.prompts)}</b> prompts you actually typed. A transcript's
        <span class="mono">user</span> turns also carry SDK-injected prompts, task
        notifications and sub-agent instructions — those
        (${int((c.overview.prompt_records || 0) - (c.overview.prompts || 0))} of them) are
        counted separately, never as yours.</li>
    <li>Token counts come straight from each assistant message's <span class="mono">usage</span> block —
        those are real, reported by the API.</li>
    <li>Cost is <b>derived</b>, never read from disk. Accuracy depends entirely on the pricing table.</li>
    <li>Sub-agent and workflow transcripts (nested under <span class="mono">&lt;session&gt;/subagents/</span>)
        are included; they carry their own billable usage.</li>
    <li>Cursor's local <span class="mono">tokenCount</span> is zero on most messages, so Cursor
        token totals are partial and no cost is estimated for it.</li>
    <li>Dates and hours are bucketed in <b>local</b> time.</li>
  </ul>`, 'full');
  return html;
}

const RENDERERS = {
  overview: renderOverview, activity: renderActivity, cost: renderCost,
  projects: renderProjects, tools: renderTools, code: renderCode,
  sessions: renderSessions, cursor: renderCursor, data: renderData,
};

/* ---------------------------------------------------------------- shell */
function showTab(name) {
  if (!RENDERERS[name]) name = 'overview';
  activeTab = name;
  readPalette();
  history.replaceState(null, '', '#' + name);
  const meta = TABS.find(t => t[0] === name);
  if (meta) {
    $('#page-title').textContent = meta[1];
    $('#page-sub').textContent = meta[2];
  }
  document.querySelectorAll('#tabs button').forEach(b =>
    b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('section.tab').forEach(s =>
    s.classList.toggle('active', s.dataset.tab === name));
  const host = $(`section[data-tab="${name}"]`);
  pending.length = 0;
  host.innerHTML = RENDERERS[name]();
  flushCharts(host);
  wireTables(host);
  scrollTo({ top: 0 });
}

function buildTabs() {
  $('#tabs').innerHTML = TABS.map(([k, label]) =>
    `<button data-tab="${k}" type="button">${esc(label)}</button>`).join('');
  $('#tabs').addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (b) showTab(b.dataset.tab);
  });
}

function currentFilters() {
  const q = new URLSearchParams();
  const range = $('#range').value;
  if (range === 'custom') {
    if ($('#since').value) q.set('since', $('#since').value);
    if ($('#until').value) q.set('until', $('#until').value);
  } else if (range !== 'all') {
    const d = new Date();
    d.setDate(d.getDate() - (+range - 1));
    q.set('since', isoLocal(d));
  }
  if ($('#project').value) q.set('project', $('#project').value);
  return q;
}

async function load() {
  $('#loading').hidden = false;
  $('#app').hidden = true;
  const res = await fetch('/api/data?' + currentFilters().toString());
  DATA = await res.json();

  const sel = $('#project');
  if (sel.options.length <= 1) {
    for (const p of DATA.projects) {
      const o = document.createElement('option');
      o.value = o.textContent = p;
      sel.appendChild(o);
    }
  }
  const ov = DATA.claude.overview;
  $('#rail-span').textContent = ov.first_day ? `${ov.first_day} → ${ov.last_day}` : 'no data';

  $('#loading').hidden = true;
  $('#app').hidden = false;
  showTab(activeTab);
}

function init() {
  buildTabs();
  $('#range').addEventListener('change', () => {
    const custom = $('#range').value === 'custom';
    $('#f-since').hidden = $('#f-until').hidden = !custom;
    if (!custom) load();
  });
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => showTab(activeTab));
  $('#since').addEventListener('change', load);
  $('#until').addEventListener('change', load);
  $('#project').addEventListener('change', load);
  $('#refresh').addEventListener('click', async () => {
    const b = $('#refresh');
    b.disabled = true; b.textContent = 'Refreshing…';
    try {
      await fetch('/api/refresh', { method: 'POST' });
      await load();
    } catch (e) {
      alert('Refresh failed: ' + e.message);
    } finally {
      b.disabled = false; b.textContent = 'Refresh data';
    }
  });
  addEventListener('resize', () => { clearTimeout(window._rz); window._rz = setTimeout(() => showTab(activeTab), 220); });
  load().catch(e => { $('#loading').textContent = 'Failed to load: ' + e.message; });
}

init();
