"""Dashboard: two views over a date scrubber, in one self-contained HTML file.

Structure is deliberately narrow. A sidebar with exactly two destinations —

  日报    what the engine read today and what it concluded: macro narrative,
          theme scores, the theme→transmission→signal→instrument map, the day's
          40 ideas, and the citation trail behind them
  组合    the paper book: what got bought, at what, on what horizon, when it
          exits, and how it is doing

— plus a date rail. ← / → move a day; the whole payload is embedded so moving
between days is instant and works offline.

Design notes:
* Colour follows the HK/CN convention: red is up, green is down.
* Rendering is client-side because a date scrubber needs JS anyway; the payload is
  built server-side by `payload.py` so the numbers are computed once, in Python.
* No external requests: no font CDN, no chart library, no images.
* The citation trail carries titles and institutions, never body excerpts — the
  published page is public and the corpus is subscription research.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import analytics, config, db, lexicon, monitor, payload

CSS = """
:root{
  --paper:#EDF0F3; --surface:#FFFFFF; --surface-2:#F6F8FA; --sidebar:#131A24;
  --rule:#D7DEE5; --rule-strong:#B9C4CE;
  --ink:#161B22; --ink-2:#41505E; --ink-3:#6B7A89;
  --accent:#1F3A6E; --accent-soft:#E4EAF4;
  --up:#C8372B; --down:#1E8E5A; --warn:#9A6C1C; --flat:#6B7A89;
  --up-soft:#FBEAE7; --down-soft:#E6F4EC; --warn-soft:#FBF2DF;
  --side-ink:#E8EDF3; --side-ink-2:#93A2B4; --side-rule:#26313F;
  --side-active:#1F3A6E;
  --shadow:0 1px 0 rgba(22,27,34,.03), 0 1px 3px rgba(22,27,34,.06);
  --serif:"Songti SC","Source Han Serif SC","Noto Serif CJK SC",Georgia,serif;
  --sans:"PingFang SC","Hiragino Sans GB",-apple-system,BlinkMacSystemFont,
         "Segoe UI","Microsoft YaHei",sans-serif;
  --mono:"SF Mono","JetBrains Mono",ui-monospace,Menlo,Consolas,monospace;
}
@media (prefers-color-scheme: dark){:root{
  --paper:#0E1116; --surface:#161B22; --surface-2:#1C232C; --sidebar:#0A0E13;
  --rule:#2A333E; --rule-strong:#3A4552;
  --ink:#E6EBF0; --ink-2:#B4C0CC; --ink-3:#8A97A5;
  --accent:#7A9CD8; --accent-soft:#1B2739;
  --up:#E05A4A; --down:#3FB97D; --warn:#D9A441;
  --up-soft:#2A1A18; --down-soft:#12291F; --warn-soft:#2A2113;
  --side-ink:#E6EBF0; --side-ink-2:#7C8A9A; --side-rule:#1C242F;
  --side-active:#24405F;
  --shadow:0 1px 0 rgba(0,0,0,.3), 0 1px 3px rgba(0,0,0,.4);
}}
:root[data-theme="dark"]{
  --paper:#0E1116; --surface:#161B22; --surface-2:#1C232C; --sidebar:#0A0E13;
  --rule:#2A333E; --rule-strong:#3A4552;
  --ink:#E6EBF0; --ink-2:#B4C0CC; --ink-3:#8A97A5;
  --accent:#7A9CD8; --accent-soft:#1B2739;
  --up:#E05A4A; --down:#3FB97D; --warn:#D9A441;
  --up-soft:#2A1A18; --down-soft:#12291F; --warn-soft:#2A2113;
  --side-ink:#E6EBF0; --side-ink-2:#7C8A9A; --side-rule:#1C242F;
  --side-active:#24405F;
  --shadow:0 1px 0 rgba(0,0,0,.3), 0 1px 3px rgba(0,0,0,.4);
}
:root[data-theme="light"]{
  --paper:#EDF0F3; --surface:#FFFFFF; --surface-2:#F6F8FA; --sidebar:#131A24;
  --rule:#D7DEE5; --rule-strong:#B9C4CE;
  --ink:#161B22; --ink-2:#41505E; --ink-3:#6B7A89;
  --accent:#1F3A6E; --accent-soft:#E4EAF4;
  --up:#C8372B; --down:#1E8E5A; --warn:#9A6C1C;
  --up-soft:#FBEAE7; --down-soft:#E6F4EC; --warn-soft:#FBF2DF;
  --side-ink:#E8EDF3; --side-ink-2:#93A2B4; --side-rule:#26313F;
  --side-active:#1F3A6E;
  --shadow:0 1px 0 rgba(22,27,34,.03), 0 1px 3px rgba(22,27,34,.06);
}

*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:var(--paper);color:var(--ink);font-family:var(--sans);
     font-size:14px;line-height:1.62;-webkit-font-smoothing:antialiased}
.num,.mono{font-family:var(--mono);font-variant-numeric:tabular-nums}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:2px}
a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}

/* ============================== shell ============================== */
.app{display:grid;grid-template-columns:236px 1fr;min-height:100vh}

aside{background:var(--sidebar);color:var(--side-ink);padding:20px 0 28px;
      position:sticky;top:0;height:100vh;overflow-y:auto;
      display:flex;flex-direction:column;gap:22px}
.brand{padding:0 18px}
.brand b{display:block;font-family:var(--serif);font-size:17px;letter-spacing:.01em}
.brand span{display:block;color:var(--side-ink-2);font-size:11px;margin-top:2px}

.nav{display:flex;flex-direction:column;gap:2px;padding:0 8px}
.nav button{all:unset;cursor:pointer;display:flex;align-items:center;gap:10px;
  padding:10px 12px;border-radius:5px;font-size:14px;color:var(--side-ink-2);
  font-weight:500}
.nav button:hover{background:rgba(255,255,255,.05);color:var(--side-ink)}
.nav button[aria-current="page"]{background:var(--side-active);color:#fff;
  font-weight:600}
.nav .k{font-family:var(--mono);font-size:10px;opacity:.5;margin-left:auto}
.nav .ic{width:16px;text-align:center;font-size:13px;opacity:.9}

.side-sec{padding:0 18px}
.side-sec h4{margin:0 0 8px;font-size:10.5px;letter-spacing:.13em;
  text-transform:uppercase;color:var(--side-ink-2);font-weight:600}
.datelist{display:flex;flex-direction:column;gap:1px;max-height:30vh;overflow-y:auto;
  margin:0 -6px}
.datelist button{all:unset;cursor:pointer;padding:5px 8px;border-radius:4px;
  font-family:var(--mono);font-size:12px;color:var(--side-ink-2);
  display:flex;justify-content:space-between;gap:8px}
.datelist button:hover{background:rgba(255,255,255,.05);color:var(--side-ink)}
.datelist button[aria-current="true"]{background:rgba(255,255,255,.1);
  color:var(--side-ink);font-weight:600}
.datelist .dot{width:6px;height:6px;border-radius:50%;background:var(--accent);
  align-self:center;flex-shrink:0;opacity:.85}
.datelist .dot.none{background:var(--side-rule)}

.sidestat{display:flex;justify-content:space-between;gap:8px;font-size:11.5px;
  padding:3px 0;border-top:1px solid var(--side-rule);color:var(--side-ink-2)}
.sidestat:first-of-type{border-top:none}
.sidestat b{font-family:var(--mono);font-weight:600;color:var(--side-ink)}
.side-foot{margin-top:auto;padding:0 18px;font-size:10.5px;color:var(--side-ink-2);
  line-height:1.5}

main{min-width:0;display:flex;flex-direction:column}
.topbar{position:sticky;top:0;z-index:10;background:var(--paper);
  border-bottom:1px solid var(--rule);padding:12px 26px;
  display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.datenav{display:flex;align-items:center;gap:6px}
.datenav button{all:unset;cursor:pointer;width:29px;height:29px;border-radius:5px;
  border:1px solid var(--rule-strong);display:grid;place-items:center;
  background:var(--surface);color:var(--ink-2);font-size:14px}
.datenav button:hover:not(:disabled){border-color:var(--accent);color:var(--accent)}
.datenav button:disabled{opacity:.32;cursor:not-allowed}
.datenav .cur{font-family:var(--mono);font-size:16px;font-weight:600;
  min-width:118px;text-align:center}
.datenav .rel{font-size:11px;color:var(--ink-3);min-width:52px}
.topbar .spacer{flex:1}
.topbar .kv{font-size:11.5px;color:var(--ink-3);font-family:var(--mono)}
.view{padding:22px 26px 72px;max-width:1280px}

/* ============================== bits ============================== */
h1.t{font-family:var(--serif);font-size:25px;margin:0 0 6px;line-height:1.25;
  text-wrap:balance}
.lede{color:var(--ink-2);font-size:14.5px;margin:0 0 20px;max-width:66ch}
.sec{margin:0 0 30px}
.sec>h2{font-family:var(--serif);font-size:17px;margin:0 0 3px;font-weight:600}
.sec>.sub{color:var(--ink-3);font-size:12px;margin:0 0 12px}
.panel{background:var(--surface);border:1px solid var(--rule);border-radius:4px;
  padding:15px 17px;box-shadow:var(--shadow)}
.grid{display:grid;gap:12px}
.g2{grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}
.g3{grid-template-columns:repeat(auto-fit,minmax(212px,1fr))}
.g4{grid-template-columns:repeat(auto-fit,minmax(158px,1fr))}
.eyebrow{font-size:10.5px;letter-spacing:.13em;text-transform:uppercase;
  color:var(--ink-3);font-weight:600}
.big{font-family:var(--mono);font-size:27px;font-weight:600;line-height:1.1;
  letter-spacing:-.02em}
.big.sm{font-size:20px}
.up{color:var(--up)} .down{color:var(--down)} .flat{color:var(--flat)}
.muted{color:var(--ink-3)} .small{font-size:11.5px}
.row{display:flex;justify-content:space-between;gap:10px;font-size:12px;
  padding:4px 0;border-top:1px dotted var(--rule)}
.row:first-of-type{border-top:none}
.row>span:first-child{color:var(--ink-3)}
.row>span:last-child{font-family:var(--mono);font-variant-numeric:tabular-nums}
.pill{display:inline-block;padding:1px 7px;border-radius:9px;font-size:10.5px;
  font-weight:600;border:1px solid transparent;white-space:nowrap}
.g-S{background:var(--accent);color:#fff}
.g-A{background:var(--accent-soft);color:var(--accent);border-color:var(--accent)}
.g-B{background:var(--surface-2);color:var(--ink-2);border-color:var(--rule-strong)}
.g-C{color:var(--ink-3);border-color:var(--rule-strong)}
.t-core{background:var(--up-soft);color:var(--up);border-color:var(--up)}
.t-important{background:var(--warn-soft);color:var(--warn);border-color:var(--warn)}
.t-watch{background:var(--surface-2);color:var(--ink-2);border-color:var(--rule-strong)}
.t-background{color:var(--ink-3);border-color:var(--rule)}
.lv-action{background:var(--up-soft);color:var(--up);border-color:var(--up)}
.lv-warn{background:var(--warn-soft);color:var(--warn);border-color:var(--warn)}
.lv-info{background:var(--surface-2);color:var(--ink-3);border-color:var(--rule-strong)}
.tag{font-family:var(--mono);font-size:10.5px;color:var(--ink-3)}

.tw{overflow-x:auto;border:1px solid var(--rule);border-radius:4px;
  background:var(--surface);box-shadow:var(--shadow)}
table{border-collapse:collapse;width:100%;font-size:12.5px}
thead th{position:sticky;top:0;background:var(--surface-2);text-align:left;
  padding:8px 10px;border-bottom:1px solid var(--rule-strong);font-weight:600;
  font-size:10.5px;letter-spacing:.04em;color:var(--ink-2);white-space:nowrap;
  cursor:pointer;user-select:none}
thead th:hover{color:var(--accent)}
thead th[data-nosort]{cursor:default}
thead th[data-sort="asc"]::after{content:" ▲";font-size:8px}
thead th[data-sort="desc"]::after{content:" ▼";font-size:8px}
tbody td{padding:7px 10px;border-bottom:1px solid var(--rule);vertical-align:top}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover{background:var(--surface-2)}
td.n,th.n{text-align:right;font-family:var(--mono);
  font-variant-numeric:tabular-nums;white-space:nowrap}

.meter{height:4px;background:var(--surface-2);border-radius:3px;overflow:hidden;
  border:1px solid var(--rule);margin-top:3px}
.meter i{display:block;height:100%;background:var(--accent)}
.meter.hot i{background:var(--up)} .meter.cool i{background:var(--down)}
.fbars{display:flex;gap:2.5px;align-items:flex-end;height:24px}
.fbars i{width:8px;background:var(--accent);opacity:.85;border-radius:1px 1px 0 0;
  display:block;min-height:2px}

.alert{display:flex;gap:10px;padding:9px 12px;border-bottom:1px solid var(--rule);
  border-left:3px solid var(--flat);align-items:flex-start;font-size:12.5px}
.alert:last-child{border-bottom:none}
.alert.action{border-left-color:var(--up);background:var(--up-soft)}
.alert.warn{border-left-color:var(--warn)}
.alert .k{font-family:var(--mono);font-size:10.5px;color:var(--ink-3);
  min-width:118px;flex-shrink:0}

.tmap{font-family:var(--mono);font-size:12px;line-height:1.85}
.tmap .th{font-family:var(--sans);font-weight:600;font-size:13px}
.tmap .tr{color:var(--ink-2)} .tmap .sg{color:var(--accent)}
.tmap .id{color:var(--ink-3)}
.tmap ul{list-style:none;margin:0;padding-left:17px;border-left:1px dotted var(--rule)}
.tmap>ul{padding-left:0;border:none}
.tmap li{margin:1px 0}

.idea{border:1px solid var(--rule);border-radius:4px;background:var(--surface);
  padding:13px 15px;box-shadow:var(--shadow)}
.idea header{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap;
  margin-bottom:5px}
.idea header b{font-size:14.5px;font-family:var(--mono)}
.idea .v{font-size:12.5px;color:var(--ink-2);margin:0 0 8px}
.idea .th{font-size:12px;color:var(--ink-3);margin:0 0 9px;line-height:1.6}
.idea .lv{display:grid;grid-template-columns:repeat(auto-fit,minmax(88px,1fr));
  gap:2px 12px;font-size:11.5px;border-top:1px dotted var(--rule);padding-top:8px}
.idea .lv div{display:flex;justify-content:space-between;gap:6px}
.idea .lv span:first-child{color:var(--ink-3)}
.idea .lv span:last-child{font-family:var(--mono)}

.note{background:var(--surface-2);border-left:3px solid var(--rule-strong);
  padding:11px 14px;font-size:12.5px;color:var(--ink-2);border-radius:0 4px 4px 0}
.note b{color:var(--ink)}
.empty{padding:44px 20px;text-align:center;color:var(--ink-3);
  border:1px dashed var(--rule-strong);border-radius:4px;background:var(--surface)}
figcaption{font-size:11.5px;color:var(--ink-3);margin-top:9px;line-height:1.55}
svg{display:block;max-width:100%;height:auto}
.legend{display:flex;gap:15px;flex-wrap:wrap;font-size:11.5px;color:var(--ink-2);
  margin:0 0 8px}
.legend b{display:inline-block;width:15px;height:2px;vertical-align:middle;
  margin-right:5px;border-radius:1px}
details>summary{cursor:pointer;font-size:12px;color:var(--accent);
  padding:5px 0;user-select:none}
.kbd{font-family:var(--mono);font-size:10.5px;border:1px solid var(--side-rule);
  padding:0 4px;border-radius:3px;background:rgba(255,255,255,.06)}

@media (max-width:860px){
  .app{grid-template-columns:1fr}
  aside{position:static;height:auto;flex-direction:row;flex-wrap:wrap;
        align-items:center;padding:12px 14px;gap:14px}
  aside .side-sec,aside .side-foot{display:none}
  .nav{flex-direction:row;padding:0}
  .view{padding:16px 14px 56px}
  .topbar{padding:10px 14px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
"""

JS = r"""
const P = window.__IG40__;
const D = P.meta.dates, BOOKS = P.meta.books;
let view = 'report', cur = P.meta.today;

/* ------------------------------------------------------------ helpers */
const $ = s => document.querySelector(s);
const E = (t, a = {}, ...kids) => {
  const n = document.createElement(t);
  for (const [k, v] of Object.entries(a)) {
    if (v === null || v === undefined) continue;
    if (k === 'cls') n.className = v;
    else if (k === 'html') n.innerHTML = v;
    else if (k === 'on') for (const [e, f] of Object.entries(v)) n.addEventListener(e, f);
    else n.setAttribute(k, v);
  }
  for (const c of kids.flat()) if (c !== null && c !== undefined && c !== false)
    n.append(c.nodeType ? c : document.createTextNode(String(c)));
  return n;
};
const pc = (v, nd = 2) => v === null || v === undefined ? '—'
  : (v * 100).toFixed(nd).replace(/^(?=[^-])/, '+') + '%';
const pcu = (v, nd = 2) => v === null || v === undefined ? '—' : (v * 100).toFixed(nd) + '%';
const n2 = (v, nd = 2) => v === null || v === undefined ? '—'
  : Number(v).toLocaleString('en-US', { minimumFractionDigits: nd, maximumFractionDigits: nd });
const usd = v => v === null || v === undefined ? '—'
  : '$' + Math.round(v).toLocaleString('en-US');
const sgn = v => v === null || v === undefined ? 'flat' : v > 0 ? 'up' : v < 0 ? 'down' : 'flat';
const day = P.days;
const dIdx = () => D.indexOf(cur);

function relLabel(d) {
  const t = P.meta.today;
  if (d === t) return '最新';
  const gap = Math.round((new Date(t) - new Date(d)) / 86400000);
  return gap + ' 天前';
}

/* ------------------------------------------------------------ chart */
function lineChart(series, opts = {}) {
  const W = opts.w || 1160, H = opts.h || 230;
  const pts = [];
  for (const s of Object.values(series)) for (const p of s) pts.push(p);
  if (pts.length < 2) return E('p', { cls: 'muted small' }, '数据不足，无法作图。');
  const xs = [...new Set(pts.map(p => p[0]))].sort();
  const xi = new Map(xs.map((d, i) => [d, i]));
  let lo = Math.min(...pts.map(p => p[1])), hi = Math.max(...pts.map(p => p[1]));
  const span = Math.max(hi - lo, .004); lo -= span * .13; hi += span * .13;
  const PL = 50, PR = 92, PT = 12, PB = 24, iw = W - PL - PR, ih = H - PT - PB;
  const X = d => PL + (xi.get(d) / Math.max(xs.length - 1, 1)) * iw;
  const Y = v => PT + (1 - (v - lo) / (hi - lo)) * ih;
  const col = { disciplined: 'var(--accent)', naive: 'var(--up)',
                SPY: 'var(--ink-3)', ACWI: 'var(--down)' };
  const dash = { SPY: '4 3', ACWI: '2 3' };
  let o = '';
  for (let k = 0; k <= 5; k++) {
    const v = lo + (hi - lo) * k / 5, y = Y(v), z = Math.abs(v) < 1e-9;
    o += `<line x1="${PL}" y1="${y.toFixed(1)}" x2="${PL + iw}" y2="${y.toFixed(1)}"
      stroke="var(--${z ? 'rule-strong' : 'rule'})" stroke-width="${z ? 1 : .6}"/>
      <text x="${PL - 7}" y="${(y + 3.5).toFixed(1)}" text-anchor="end" font-size="10"
      font-family="var(--mono)" fill="var(--ink-3)">${(v * 100).toFixed(1)}%</text>`;
  }
  for (const d of [xs[0], xs[xs.length >> 1], xs[xs.length - 1]])
    o += `<text x="${X(d).toFixed(1)}" y="${H - 7}" text-anchor="middle" font-size="10"
      font-family="var(--mono)" fill="var(--ink-3)">${d.slice(5)}</text>`;
  const labels = [];
  for (const [name, s] of Object.entries(series)) {
    if (s.length < 2) continue;
    const c = col[name] || 'var(--warn)';
    const main = name === 'disciplined' || name === 'naive';
    o += `<path d="${s.map((p, i) => (i ? 'L' : 'M') + X(p[0]).toFixed(1) + ',' + Y(p[1]).toFixed(1)).join(' ')}"
      fill="none" stroke="${c}" stroke-width="${main ? 2 : 1.3}"
      ${dash[name] ? `stroke-dasharray="${dash[name]}"` : ''} stroke-linejoin="round"/>`;
    const last = s[s.length - 1];
    o += `<circle cx="${X(last[0]).toFixed(1)}" cy="${Y(last[1]).toFixed(1)}" r="3" fill="${c}"/>`;
    labels.push({ x: X(last[0]) + 7, y: Y(last[1]), c, t: pc(last[1]) });
  }
  labels.sort((a, b) => a.y - b.y);
  const placed = [];
  for (const L of labels) {
    let y = L.y;
    for (const p of placed) if (Math.abs(y - p) < 12) y = p + 12;
    placed.push(y);
    if (Math.abs(y - L.y) > 1)
      o += `<line x1="${(L.x - 3).toFixed(1)}" y1="${L.y.toFixed(1)}"
        x2="${(L.x + 1).toFixed(1)}" y2="${y.toFixed(1)}" stroke="${L.c}"
        stroke-width=".7" opacity=".55"/>`;
    o += `<text x="${L.x.toFixed(1)}" y="${(y + 3.5).toFixed(1)}" font-size="10.5"
      font-family="var(--mono)" fill="${L.c}">${L.t}</text>`;
  }
  // Setting innerHTML on a container parses <svg> in the SVG namespace;
  // document.createElement('svg') would produce an inert HTML element instead.
  const box = document.createElement('div');
  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img"
    aria-label="累计收益曲线">${o}</svg>`;
  return box.firstElementChild;
}

function sortable(tbl) {
  tbl.querySelectorAll('thead th:not([data-nosort])').forEach((th, i) => {
    th.tabIndex = 0;
    const go = () => {
      const dir = th.dataset.sort === 'asc' ? 'desc' : 'asc';
      tbl.querySelectorAll('thead th').forEach(o => delete o.dataset.sort);
      th.dataset.sort = dir;
      const tb = tbl.tBodies[0], rows = [...tb.rows];
      rows.sort((a, b) => {
        const g = r => { const c = r.cells[i]; return c.dataset.v ?? c.textContent.trim(); };
        const x = g(a), y = g(b), xn = parseFloat(x), yn = parseFloat(y);
        const c = (!isNaN(xn) && !isNaN(yn)) ? xn - yn : String(x).localeCompare(String(y), 'zh');
        return dir === 'asc' ? c : -c;
      });
      rows.forEach(r => tb.appendChild(r));
    };
    th.addEventListener('click', go);
    th.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); }
    });
  });
  return tbl;
}

function table(cols, rows) {
  const t = E('table', { 'data-sortable': '' },
    E('thead', {}, E('tr', {}, cols.map(c =>
      E('th', { cls: c.n ? 'n' : '', 'data-nosort': c.nosort ? '' : null }, c.h)))),
    E('tbody', {}, rows.map(r => E('tr', {}, r.map((cell, i) =>
      E('td', { cls: [cols[i].n ? 'n' : '', cell && cell.cls || ''].join(' ').trim(),
                'data-v': cell && cell.v !== undefined ? cell.v : null },
        cell && cell.el ? cell.el : (cell && cell.t !== undefined ? cell.t : cell)))))));
  return E('div', { cls: 'tw' }, sortable(t));
}

/* ============================================================ 日报 */
function viewReport(dd) {
  const wrap = E('div');
  const r = dd.report, b = dd.batch;
  if (!r && !b) return E('div', { cls: 'empty' },
    cur + ' 没有日报。左右方向键换一天，或点侧边栏的日期。');

  wrap.append(E('h1', { cls: 't' }, `${cur} 战术宏观日报`));
  if (b && b.narrative)
    wrap.append(E('p', { cls: 'lede' }, b.narrative));
  else if (r)
    wrap.append(E('p', { cls: 'lede muted' },
      '这一天完成了主题打分，但没有生成想法批次，所以没有宏观主线叙述。'));

  /* ---- corpus strip ---- */
  if (r) {
    const c = r.corpus;
    wrap.append(sec('语料底稿', `观察窗口 ${c.window[0]} → ${c.window[2]}，共 ${c.total.toLocaleString()} 条来源条目`,
      E('div', { cls: 'grid g4' },
        E('div', { cls: 'panel' }, E('div', { cls: 'eyebrow' }, '窗口条目'),
          E('div', { cls: 'big sm' }, c.total.toLocaleString()),
          E('div', { cls: 'small muted' }, Object.entries(c.by_tier)
            .map(([k, v]) => `${k} ${v}`).join(' · '))),
        ...c.window.map(d => E('div', { cls: 'panel' },
          E('div', { cls: 'eyebrow' }, d.slice(5)),
          E('div', { cls: 'big sm' }, (c.by_day[d] || 0).toLocaleString()),
          E('div', { cls: 'small muted' }, '条')))),
      E('details', {},
        E('summary', {}, '按来源线拆分'),
        E('div', { cls: 'panel', style: 'margin-top:8px' },
          Object.entries(c.by_line).map(([k, v]) =>
            E('div', { cls: 'row' }, E('span', {}, k), E('span', {}, v)))))));
  }

  /* ---- theme table ---- */
  if (r) {
    const cols = [
      { h: '宏观主题 · 预注册关键结果' }, { h: '冲击潜力', n: 1 },
      { h: '因子', nosort: 1 }, { h: 'D', n: 1 }, { h: 'A', n: 1 },
      { h: 'B', n: 1 }, { h: 'N', n: 1 },
      { h: '入价程度 M', n: 1 }, { h: '拥挤度 C', n: 1 },
      { h: '主要争议' }, { h: '条目', n: 1 }, { h: '分级' }];
    const rows = r.themes.map(t => [
      { el: E('div', {}, E('strong', {}, t.label),
          E('div', { cls: 'small muted' }, t.key_question)) },
      { v: t.tis, el: E('div', {}, E('strong', {}, n2(t.tis, 1)),
          E('div', { cls: 'meter' + (t.tis >= 60 ? ' hot' : '') },
            E('i', { style: `width:${Math.min(t.tis || 0, 100)}%` }))) },
      { el: fbars(t) },
      { v: t.d, t: n2(t.d, 0) }, { v: t.a, t: n2(t.a, 0) },
      { v: t.b, t: n2(t.b, 0) }, { v: t.n, t: n2(t.n, 0) },
      { v: t.m, el: E('div', {}, n2(t.m, 0),
          E('div', { cls: 'small muted' }, t.stage || '')) },
      { v: t.c, el: E('div', {}, n2(t.c, 0),
          E('div', { cls: 'small muted' }, t.crowd || '')) },
      { el: E('span', { cls: 'small' }, t.debate || '—') },
      { v: t.n_items, t: t.n_items },
      { el: E('div', {}, E('span', { cls: 'pill t-' + t.tier }, t.tier),
          t.confidence !== 'ok' ? E('div', { cls: 'tag' }, '低置信') : null) }]);
    wrap.append(sec('六个宏观主题，冲击与入价并列',
      'TIS = 0.15·D + 0.25·A + 0.25·B + 0.35·N（沿用 v0.3 权重）；M 与 C 是独立维度，不进 TIS。柱状图依次为 D / A / B / N',
      table(cols, rows)));
  }

  /* ---- theme map ---- */
  if (r && b) wrap.append(sec('Theme Map',
    '宏观主题 └ 传导主线 └ 资产信号（方向｜期限）└ 交易想法与表达工具',
    E('div', { cls: 'panel tmap' }, themeMap(r, b))));

  /* ---- ideas ---- */
  if (b) {
    const v = b.validation;
    wrap.append(sec(`当日 ${b.n} 条交易想法`,
      `批次 ${b.batch_id} · 生成器 ${b.generator} · 校验 ${v.pass ? '通过' : '未通过'}（${v.errors || 0} error / ${v.warnings || 0} warning）· sha ${b.output_sha}`,
      v.failed && v.failed.length ? E('div', { cls: 'panel', style: 'padding:0;margin-bottom:12px' },
        v.failed.map(f => E('div', { cls: 'alert ' + (f.severity === 'error' ? 'action' : 'warn') },
          E('span', { cls: 'k' }, f.check),
          E('span', { cls: 'small' }, JSON.stringify(f.detail))))) : null,
      ideaTable(b)));

    const byTheme = {};
    for (const i of b.ideas) (byTheme[i.theme] ||= []).push(i);
    wrap.append(sec('逐条展开', '按宏观主题分组；每条的 thesis 都可追溯到当日语料',
      E('div', {}, Object.entries(byTheme).map(([th, list]) =>
        E('details', { open: '' },
          E('summary', {}, `${th} · ${list.length} 条`),
          E('div', { cls: 'grid g2', style: 'margin:8px 0 16px' },
            list.map(ideaCard)))))));
  }

  /* ---- evidence ---- */
  if (r && r.evidence.length) wrap.append(sec('来源底稿',
    `窗口内信号最高的 ${r.evidence.length} 条（Tier 1 一手优先）。只列标题与机构，正文留在本地 briefing`,
    table([{ h: 'doc_id' }, { h: 'T', n: 1 }, { h: '日期' }, { h: '来源线' },
           { h: '机构' }, { h: '标题' }],
      r.evidence.map(e => [
        { el: E('span', { cls: 'tag' }, e.doc_id) },
        { v: e.tier, t: 'T' + e.tier }, e.d, e.line,
        e.institution || '—',
        { el: e.url ? E('a', { href: e.url, target: '_blank', rel: 'noopener' }, e.title)
                    : E('span', {}, e.title) }]))));

  return wrap;
}

function fbars(t) {
  return E('div', { cls: 'fbars' }, ['d', 'a', 'b', 'n'].map(k => {
    const v = t[k];
    return E('i', { style: `height:${v == null ? 2 : Math.max(2, Math.round(v / 100 * 24))}px`,
                    title: `${k.toUpperCase()}=${n2(v, 1)}` });
  }));
}

function themeMap(r, b) {
  const ideasBySignal = {}, ideasByTheme = {};
  for (const i of b.ideas) {
    (ideasBySignal[i.signal_id] ||= []).push(i);
    (ideasByTheme[i.theme_id] ||= []).push(i);
  }
  const shown = r.themes.filter(t => ideasByTheme[t.id]);
  return E('ul', {}, shown.map(t => {
    const byTr = {};
    for (const s of t.signals) (byTr[s.transmission_id || '—'] ||= []).push(s);
    const trLabel = id => (t.transmissions.find(x => x.id === id) || {}).label || id;
    return E('li', {},
      E('span', { cls: 'th' }, t.label),
      E('span', { cls: 'id' }, `  TIS ${n2(t.tis, 1)} · M ${n2(t.m, 0)} · C ${n2(t.c, 0)}`),
      E('ul', {}, Object.entries(byTr).map(([tr, sigs]) =>
        E('li', {},
          E('span', { cls: 'tr' }, '└─ ' + trLabel(tr)),
          E('ul', {}, sigs.map(s =>
            E('li', {},
              E('span', { cls: 'sg' }, `└─ ${s.asset} ${s.direction}｜${s.horizon}`),
              E('ul', {}, (ideasBySignal[s.id] || []).map(i =>
                E('li', {}, E('span', { cls: 'id' },
                  `└─ ${i.tool}  ${i.action}  ${n2(i.pos_init, 2)}%  赔率 ${n2(i.or_c)}`)))))))))));
  }));
}

function ideaTable(b) {
  const cols = [{ h: '#', n: 1 }, { h: '工具' }, { h: '主题' }, { h: '期限' },
    { h: '动作' }, { h: '评级' }, { h: '中心赔率', n: 1 }, { h: '保守赔率', n: 1 },
    { h: 'hurdle', n: 1 }, { h: '参考价', n: 1 }, { h: '进场', n: 1 },
    { h: '止损', n: 1 }, { h: '止盈', n: 1 }, { h: '仓位', n: 1 },
    { h: '幅度校验' }, { h: '已实现', n: 1 }, { h: '超额', n: 1 }];
  return table(cols, b.ideas.map(i => [
    { v: i.rank, t: i.rank },
    { el: E('div', {}, E('strong', { cls: 'mono' }, i.tool),
        E('div', { cls: 'small muted' }, i.desc || '')) },
    i.theme, i.horizon, i.action,
    { el: E('span', {}, E('span', { cls: 'pill g-' + i.grade }, i.grade), ' ',
        E('span', { cls: 'tag' }, i.grade_rel || '')) },
    { v: i.or_c, t: n2(i.or_c) }, { v: i.or_k, t: n2(i.or_k) },
    { v: i.hurdle, t: n2(i.hurdle) + '%' },
    { v: i.ref_price, t: n2(i.ref_price) },
    { v: i.entry_hi ?? i.entry_break, cls: 'small',
      t: i.entry_lo ? `${n2(i.entry_lo)}–${n2(i.entry_hi)}`
        : i.entry_break ? `突破 ${n2(i.entry_break)}` : '收盘' },
    { v: i.stop_px, cls: 'small', t: n2(i.stop_px) },
    { v: i.take_lo, cls: 'small', t: n2(i.take_lo) },
    { v: i.pos_init, t: n2(i.pos_init, 2) + '%' },
    { el: E('span', { cls: 'tag' }, i.vol_check) },
    { v: i.realized, cls: sgn(i.realized), t: pc(i.realized) },
    { v: i.excess, cls: sgn(i.excess), t: pc(i.excess) }]));
}

function ideaCard(i) {
  return E('article', { cls: 'idea' },
    E('header', {}, E('b', {}, i.tool),
      E('span', { cls: 'pill g-' + i.grade }, i.grade),
      E('span', { cls: 'tag' }, `${i.horizon} · ${i.action}`),
      E('span', { cls: 'tag' }, i.asset || '')),
    E('p', { cls: 'v' }, i.view),
    E('p', { cls: 'th' }, i.thesis),
    i.risk ? E('p', { cls: 'th' }, E('strong', {}, '风险 '), i.risk) : null,
    E('div', { cls: 'lv' },
      lv('参考价', n2(i.ref_price)),
      lv('进场', i.entry_lo ? `${n2(i.entry_lo)}–${n2(i.entry_hi)}`
        : i.entry_break ? `突破 ${n2(i.entry_break)}` : '收盘'),
      lv('止损', n2(i.stop_px)), lv('止盈', n2(i.take_lo)),
      lv('中心赔率', n2(i.or_c)), lv('保守赔率', n2(i.or_k)),
      lv('hurdle', n2(i.hurdle) + '%'), lv('仓位', n2(i.pos_init, 2) + '%'),
      lv('到期', i.horizon_end), lv('σ_h', n2(i.sigma_h) + '%'),
      lv('情景', `${i.central.r.map(x => n2(x, 1)).join(' / ')}`),
      lv('概率', `${i.central.p.join(' / ')}`)),
    i.sources && i.sources.length
      ? E('div', { cls: 'small muted', style: 'margin-top:8px' },
          '来源 ' + i.sources.join(' · ')) : null);
}
const lv = (k, v) => E('div', {}, E('span', {}, k), E('span', {}, v));

/* ============================================================ 组合 */
function viewBook(dd) {
  const wrap = E('div');
  wrap.append(E('h1', { cls: 't' }, 'Paper Portfolio'));
  wrap.append(E('p', { cls: 'lede' },
    '两个组合共用同一批 idea，差别只在执行。两条线的差额就是仓位管理的贡献；组合与 SPY 的差额就是选股加择时的贡献。'));

  /* ---- verdict cards ---- */
  const cards = Object.entries(dd.books).map(([id, v]) => {
    const meta = BOOKS[id];
    return E('div', { cls: 'panel' },
      E('div', { cls: 'eyebrow' }, id),
      E('div', { style: 'font-family:var(--serif);font-weight:600;font-size:14px' }, meta.label),
      E('div', { cls: 'small muted', style: 'margin:2px 0 9px;min-height:32px' }, meta.desc),
      E('div', { cls: 'big ' + sgn(v.cum_ret) }, pc(v.cum_ret)),
      E('div', { cls: 'small muted', style: 'margin:3px 0 10px' },
        `${usd(v.equity)} · 至 ${v.d}`),
      row('SPY 同期', pc(v.spy)),
      row('超额 vs SPY', pc(v.excess), sgn(v.excess)),
      row('最大回撤', pc(v.max_dd), 'down'),
      row('净敞口 / 在场', `${pcu(v.gross, 0)} / ${v.n_open}`),
      row('现金 / 持仓', `${usd(v.cash)} / ${usd(v.mv)}`));
  });
  wrap.append(E('div', { cls: 'grid g2', style: 'margin-bottom:26px' }, cards));

  /* ---- curve ---- */
  const cs = {};
  for (const k of Object.keys(P.curves))
    cs[k] = P.curves[k].filter(p => p[0] <= cur).map(p => [p[0], p[1]]);
  wrap.append(sec('净值曲线', '累计收益，含双边成本与闲置现金的货币基金收益',
    E('figure', { cls: 'panel' },
      E('div', { cls: 'legend' },
        ...[['守纪律组合', 'var(--accent)'], ['无脑全买组合', 'var(--up)'],
            ['SPY', 'var(--ink-3)'], ['ACWI', 'var(--down)']].map(([n, c]) =>
          E('span', {}, E('b', { style: `background:${c}` }), n))),
      lineChart(cs),
      E('figcaption', {}, `截至 ${cur}。虚线为基准。`))));

  /* ---- holdings ---- */
  for (const bk of Object.keys(BOOKS)) {
    const held = P.positions.filter(p => p.book === bk && p.kind !== 'order'
      && p.opened_d && p.opened_d <= cur);
    const open = held.filter(p => p.status === 'open' || (p.closed_d && p.closed_d > cur));
    const closed = held.filter(p => p.status === 'closed' && p.closed_d <= cur);
    if (!held.length) continue;
    wrap.append(sec(`${BOOKS[bk].label} · 持仓`,
      `在场 ${open.length} · 已平 ${closed.length}`,
      posTable([...open, ...closed])));
  }

  /* ---- pending / expired orders ---- */
  const ords = P.positions.filter(p => p.kind === 'order' && p.as_of <= cur);
  if (ords.length) wrap.append(sec('未成交订单',
    '进场区间没被触及的 idea。钱留在现金里按货币基金收益计息——这些「没买到」也是结果的一部分',
    table([{ h: '组合' }, { h: '工具' }, { h: '主题' }, { h: '期限' }, { h: '动作' },
           { h: '类型' }, { h: '区间 / 触发', n: 1 }, { h: '参考价', n: 1 },
           { h: '计划金额', n: 1 }, { h: '挂单日' }, { h: '失效日' }, { h: '状态' }],
      ords.map(o => [
        BOOKS[o.book].label, { el: E('strong', { cls: 'mono' }, o.tool) },
        o.theme, o.horizon, o.action,
        { el: E('span', { cls: 'tag' }, o.order_kind) },
        { v: o.band_hi ?? o.trigger, cls: 'small',
          t: o.band_lo ? `${n2(o.band_lo)}–${n2(o.band_hi)}`
            : o.trigger ? `>${n2(o.trigger)}` : '收盘' },
        { v: o.ref_price, t: n2(o.ref_price) },
        { v: o.notional, t: usd(o.notional) },
        o.placed_d, o.expire_d || '—',
        { el: E('span', { cls: 'pill ' + (o.status === 'pending' ? 'lv-info' : 'lv-warn') },
            o.status === 'pending' ? '待成交' : '已失效') }]))));

  /* ---- skill ---- */
  const a = P.attribution;
  if (a && a.scored) {
    const rk = a.ranking, cal = a.calibration;
    wrap.append(sec('so far 如何', `${a.scored}/${a.n} 条可评分（${a.unmarkable} 条无法盯市，${a.too_fresh} 条持有期不足 1 个交易日）`,
      E('div', { cls: 'grid g3' },
        E('div', { cls: 'panel' }, E('div', { cls: 'eyebrow' }, '想法层面 · 等权'),
          E('div', { cls: 'big sm ' + sgn(a.equal_weight_ret) }, pc(a.equal_weight_ret)),
          E('div', { cls: 'small muted', style: 'margin:2px 0 9px' }, '剔除仓位规模影响'),
          row('中位收益', pc(a.median_ret)),
          row('胜率', pcu(a.hit_rate, 0)),
          row('超额均值', pc(a.excess_mean), sgn(a.excess_mean)),
          row('跑赢基准', pcu(a.beat_bench_rate, 0))),
        E('div', { cls: 'panel' }, E('div', { cls: 'eyebrow' }, '排序能力 · Spearman ρ'),
          E('p', { cls: 'small muted', style: 'margin:3px 0 9px' },
            '引擎给的赔率排序是否预测了真实收益。ρ > 0 表示排序有效。'),
          ...Object.values(rk).map(v =>
            row(v.label, n2(v.rho_vs_realized, 3), sgn(v.rho_vs_realized)))),
        E('div', { cls: 'panel' }, E('div', { cls: 'eyebrow' }, '概率校准 · Brier'),
          E('p', { cls: 'small muted', style: 'margin:3px 0 9px' },
            '技能分 = 1 − Brier ÷ 均匀先验；> 0 表示优于「三分之一各一」。'),
          row('Brier 中心 / 保守',
            `${n2(cal.brier_central, 3)} / ${n2(cal.brier_conservative, 3)}`),
          row('均匀基线', n2(cal.brier_uniform_baseline, 3)),
          row('技能分', `${n2(cal.skill_central, 3)} / ${n2(cal.skill_conservative, 3)}`,
            sgn(cal.skill_central)),
          row('情景实现', Object.entries(cal.scenario_realised_pct || {})
            .map(([k, v]) => `${k} ${Math.round(v * 100)}%`).join(' · ') || '—')))));

    const labels = { grade: '绝对评级', grade_rel: '相对分位', horizon: '期限',
      theme: '宏观主题', vol_check: '情景幅度校验', filled: '是否成交' };
    wrap.append(sec('分档归因', '哪一类 idea 在赚钱',
      E('div', { cls: 'grid g2' }, Object.entries(labels).map(([k, lab]) => {
        const bk = (a.buckets || {})[k]; if (!bk) return null;
        return E('div', {}, E('div', { cls: 'eyebrow', style: 'margin-bottom:6px' }, '按' + lab),
          table([{ h: lab }, { h: 'n', n: 1 }, { h: '均值', n: 1 },
                 { h: '胜率', n: 1 }, { h: '超额', n: 1 }],
            Object.entries(bk).map(([kk, v]) => [
              kk === '1' ? '已成交' : kk === '0' ? '未成交' : kk,
              { v: v.n, t: v.n },
              { v: v.mean, cls: sgn(v.mean), t: pc(v.mean) },
              { v: v.hit, t: pcu(v.hit, 0) },
              { v: v.excess, cls: sgn(v.excess), t: pc(v.excess) }])));
      }))));
  }

  /* ---- alerts ---- */
  if (dd.alerts.length) wrap.append(sec('当日告警',
    '止损临近 / 主题前提被价格否定 / 拥挤度跳升 / 临近到期',
    E('div', { cls: 'panel', style: 'padding:0' },
      dd.alerts.map(x => E('div', { cls: 'alert ' + x.level },
        E('span', { cls: 'k' }, x.kind), E('span', {}, x.message))))));

  return wrap;
}

function posTable(list) {
  const cols = [{ h: '工具' }, { h: '代码' }, { h: '主题' }, { h: '期限' },
    { h: '评级' }, { h: '建仓日' }, { h: '成本价', n: 1 }, { h: '现价', n: 1 },
    { h: '投入', n: 1 }, { h: '盈亏', n: 1 }, { h: '盈亏 $', n: 1 },
    { h: '止损', n: 1 }, { h: '止盈', n: 1 }, { h: '到期日' }, { h: '状态' }];
  return table(cols, list.map(p => [
    { el: E('div', {}, E('strong', { cls: 'mono' }, p.tool),
        E('div', { cls: 'small muted' }, p.view ? p.view.slice(0, 30) : '')) },
    { el: E('span', { cls: 'tag' }, p.code) },
    p.theme, p.horizon,
    { el: E('span', { cls: 'pill g-' + p.grade }, p.grade) },
    p.opened_d,
    { v: p.avg_px, t: n2(p.avg_px) }, { v: p.px, t: n2(p.px) },
    { v: p.cost, t: usd(p.cost) },
    { v: p.pnl_pct, cls: sgn(p.pnl_pct), t: pc(p.pnl_pct) },
    { v: p.pnl_usd, cls: sgn(p.pnl_usd), t: usd(p.pnl_usd) },
    { v: p.stop_px, cls: 'small', t: n2(p.stop_px) },
    { v: p.take_px, cls: 'small', t: n2(p.take_px) },
    p.horizon_end,
    { el: p.status === 'open' ? E('span', { cls: 'pill lv-info' }, '在场')
        : E('span', { cls: 'pill lv-warn' },
            ({ stop: '止损', take: '止盈', horizon: '到期' })[p.exit_reason] || p.exit_reason) }]));
}

const row = (k, v, cls) => E('div', { cls: 'row' },
  E('span', {}, k), E('span', { cls: cls || '' }, v));
const sec = (h, sub, ...body) => E('section', { cls: 'sec' },
  E('h2', {}, h), sub ? E('p', { cls: 'sub' }, sub) : null, ...body);

/* ============================================================ shell */
function render() {
  const dd = day[cur];
  $('#view').replaceChildren(view === 'report' ? viewReport(dd) : viewBook(dd));
  document.querySelectorAll('.nav button').forEach(b =>
    b.setAttribute('aria-current', b.dataset.view === view ? 'page' : 'false'));
  document.querySelectorAll('.datelist button').forEach(b =>
    b.setAttribute('aria-current', b.dataset.d === cur ? 'true' : 'false'));
  $('#curd').textContent = cur;
  $('#reld').textContent = relLabel(cur);
  $('#prev').disabled = dIdx() <= 0;
  $('#next').disabled = dIdx() >= D.length - 1;
  const b = dd.books[Object.keys(BOOKS)[0]];
  $('#topkv').textContent = dd.batch
    ? `${dd.batch.n} 条想法 · ${dd.batch.status}`
    : (dd.report ? '仅打分，无批次' : '无数据');
  history.replaceState(null, '', `#${view}/${cur}`);
  window.scrollTo({ top: 0 });
}
function go(d) { if (day[d]) { cur = d; render(); } }
function step(k) { const i = dIdx() + k; if (i >= 0 && i < D.length) go(D[i]); }
function setView(v) { view = v; render(); }

document.addEventListener('keydown', e => {
  if (e.target.matches('input,textarea')) return;
  if (e.key === 'ArrowLeft') { e.preventDefault(); step(-1); }
  else if (e.key === 'ArrowRight') { e.preventDefault(); step(1); }
  else if (e.key === '1') setView('report');
  else if (e.key === '2') setView('book');
});

(function boot() {
  const m = /^#(report|book)\/(\d{4}-\d{2}-\d{2})$/.exec(location.hash);
  if (m && day[m[2]]) { view = m[1]; cur = m[2]; }
  $('#prev').addEventListener('click', () => step(-1));
  $('#next').addEventListener('click', () => step(1));
  document.querySelectorAll('.nav button').forEach(b =>
    b.addEventListener('click', () => setView(b.dataset.view)));
  const dl = $('#datelist');
  for (const d of [...D].reverse()) {
    const has = !!day[d].batch;
    dl.append(E('button', { 'data-d': d, on: { click: () => go(d) } },
      E('span', {}, d),
      E('span', { cls: 'dot' + (has ? '' : ' none'),
                  title: has ? '有想法批次' : '仅打分' })));
  }
  const last = day[P.meta.today].books;
  const ss = $('#sidestats');
  for (const [id, v] of Object.entries(last))
    ss.append(E('div', { cls: 'sidestat' },
      E('span', {}, BOOKS[id].label.replace('组合', '')),
      E('b', { cls: sgn(v.cum_ret) }, pc(v.cum_ret))));
  const sp = Object.values(last)[0];
  if (sp) ss.append(E('div', { cls: 'sidestat' },
    E('span', {}, 'SPY 同期'), E('b', {}, pc(sp.spy))));
  render();
})();
"""


def build(con, out: Path | None = None, artifact: bool = False) -> Path:
    pl = payload.build(con)
    out = out or (config.WEB / "index.html")
    data = json.dumps(pl, ensure_ascii=False, separators=(",", ":"), default=str)

    m = pl["meta"]
    body = f"""
<div class="app">
  <aside>
    <div class="brand">
      <b>IdeaGen40</b>
      <span>战术交易想法实盘模拟</span>
    </div>

    <nav class="nav">
      <button data-view="report" aria-current="page">
        <span class="ic">◈</span>日报<span class="k">1</span></button>
      <button data-view="book" aria-current="false">
        <span class="ic">◱</span>组合<span class="k">2</span></button>
    </nav>

    <div class="side-sec">
      <h4>组合 · 至今</h4>
      <div id="sidestats"></div>
    </div>

    <div class="side-sec">
      <h4>日期</h4>
      <div class="datelist" id="datelist"></div>
    </div>

    <div class="side-foot">
      <span class="kbd">←</span> <span class="kbd">→</span> 换日期 ·
      <span class="kbd">1</span> <span class="kbd">2</span> 换视图<br><br>
      方法论 v{m['methodology']} · 词典 v{m['lexicon']}<br>
      行情至 US {m['px_through'].get('US', '')} / HK {m['px_through'].get('HK', '')}<br>
      生成 {m['generated_at'][:16].replace('T', ' ')} HKT<br><br>
      模拟盘，非投资建议。
    </div>
  </aside>

  <main>
    <div class="topbar">
      <div class="datenav">
        <button id="prev" title="前一天（←）" aria-label="前一天">‹</button>
        <span class="cur" id="curd">—</span>
        <button id="next" title="后一天（→）" aria-label="后一天">›</button>
        <span class="rel" id="reld"></span>
      </div>
      <div class="spacer"></div>
      <div class="kv" id="topkv"></div>
    </div>
    <div class="view" id="view"></div>
  </main>
</div>"""

    title = "IdeaGen40 · 战术交易想法实盘模拟"
    script = f'<script>window.__IG40__={data};</script>\n<script>{JS}</script>'
    if artifact:
        doc = f"<title>{title}</title>\n<style>{CSS}</style>\n{body}\n{script}"
    else:
        doc = (f"<!doctype html>\n<html lang=\"zh-Hans\"><head>\n"
               f'<meta charset="utf-8">\n'
               f'<meta name="viewport" content="width=device-width,initial-scale=1">\n'
               f"<title>{title}</title>\n<style>{CSS}</style>\n"
               f"</head><body>{body}\n{script}</body></html>")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    (out.parent / "report.json").write_text(
        json.dumps({"payload": pl, "digest": monitor.digest(con),
                    "report": analytics.full_report(con)},
                   ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return out
