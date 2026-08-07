"""Dashboard builder: one self-contained HTML file from the database.

Design notes, so later edits stay coherent:

* This is a UI, not a document — it is scanned and operated. The verdict comes
  first (two books, one number each, plus the exposure-matched excess that says
  whether the ideas or the sizing did the work), then the evidence, then the
  dense ledger.
* Colour follows the HK/CN market convention: red is up, green is down. That is
  the opposite of the Western default and the right call for the reader.
* Charts are rendered as SVG in Python, not drawn by client JS, so the page is
  correct with scripting disabled. JS only sorts tables and moves the section rail.
* No external requests of any kind: no font CDN, no chart library, no images.
"""

from __future__ import annotations

import html
import json
import math
import statistics as st
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import analytics, config, db, ideas as ideas_mod, lexicon, monitor, paper
from .sources import futu_px

# --------------------------------------------------------------------- tokens
CSS = """
:root{
  --paper:#EDF0F3; --surface:#FFFFFF; --surface-2:#F6F8FA;
  --rule:#D7DEE5; --rule-strong:#B9C4CE;
  --ink:#161B22; --ink-2:#41505E; --ink-3:#6B7A89;
  --accent:#1F3A6E; --accent-soft:#E4EAF4;
  --up:#C8372B; --down:#1E8E5A; --warn:#9A6C1C; --flat:#6B7A89;
  --up-soft:#FBEAE7; --down-soft:#E6F4EC; --warn-soft:#FBF2DF;
  --shadow:0 1px 0 rgba(22,27,34,.04), 0 1px 3px rgba(22,27,34,.06);
  --serif:"Songti SC","Source Han Serif SC","Noto Serif CJK SC","Songti TC",
          Georgia,"Times New Roman",serif;
  --sans:"PingFang SC","Hiragino Sans GB",-apple-system,BlinkMacSystemFont,
         "Segoe UI","Microsoft YaHei",sans-serif;
  --mono:"SF Mono","JetBrains Mono",ui-monospace,Menlo,Consolas,monospace;
}
@media (prefers-color-scheme: dark){
  :root{
    --paper:#0E1116; --surface:#161B22; --surface-2:#1C232C;
    --rule:#2A333E; --rule-strong:#3A4552;
    --ink:#E6EBF0; --ink-2:#B4C0CC; --ink-3:#8A97A5;
    --accent:#7A9CD8; --accent-soft:#1B2739;
    --up:#E05A4A; --down:#3FB97D; --warn:#D9A441; --flat:#8A97A5;
    --up-soft:#2A1A18; --down-soft:#12291F; --warn-soft:#2A2113;
    --shadow:0 1px 0 rgba(0,0,0,.3), 0 1px 3px rgba(0,0,0,.4);
  }
}
:root[data-theme="dark"]{
  --paper:#0E1116; --surface:#161B22; --surface-2:#1C232C;
  --rule:#2A333E; --rule-strong:#3A4552;
  --ink:#E6EBF0; --ink-2:#B4C0CC; --ink-3:#8A97A5;
  --accent:#7A9CD8; --accent-soft:#1B2739;
  --up:#E05A4A; --down:#3FB97D; --warn:#D9A441; --flat:#8A97A5;
  --up-soft:#2A1A18; --down-soft:#12291F; --warn-soft:#2A2113;
  --shadow:0 1px 0 rgba(0,0,0,.3), 0 1px 3px rgba(0,0,0,.4);
}
:root[data-theme="light"]{
  --paper:#EDF0F3; --surface:#FFFFFF; --surface-2:#F6F8FA;
  --rule:#D7DEE5; --rule-strong:#B9C4CE;
  --ink:#161B22; --ink-2:#41505E; --ink-3:#6B7A89;
  --accent:#1F3A6E; --accent-soft:#E4EAF4;
  --up:#C8372B; --down:#1E8E5A; --warn:#9A6C1C; --flat:#6B7A89;
  --up-soft:#FBEAE7; --down-soft:#E6F4EC; --warn-soft:#FBF2DF;
  --shadow:0 1px 0 rgba(22,27,34,.04), 0 1px 3px rgba(22,27,34,.06);
}

*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);
     font-size:14px;line-height:1.6;-webkit-font-smoothing:antialiased}
.wrap{max-width:1240px;margin:0 auto;padding:0 20px 72px}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:2px}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums}

/* ---- masthead ---- */
.masthead{border-bottom:2px solid var(--ink);margin-bottom:28px;padding:26px 0 14px}
.mh-top{display:flex;flex-wrap:wrap;align-items:flex-end;gap:16px;
        justify-content:space-between}
.mh-title{font-family:var(--serif);font-size:30px;line-height:1.15;margin:0;
          letter-spacing:.01em;text-wrap:balance}
.mh-sub{color:var(--ink-2);font-size:13px;margin:6px 0 0}
.mh-meta{display:flex;gap:18px;flex-wrap:wrap;font-size:12px;color:var(--ink-3);
         font-family:var(--mono)}
.eyebrow{font-size:11px;letter-spacing:.14em;text-transform:uppercase;
         color:var(--ink-3);font-family:var(--sans);font-weight:600}

/* ---- verdict ---- */
.verdict{display:grid;grid-template-columns:repeat(auto-fit,minmax(268px,1fr));
         gap:14px;margin:0 0 26px}
.vcard{background:var(--surface);border:1px solid var(--rule);border-radius:3px;
       padding:16px 18px;box-shadow:var(--shadow);position:relative}
.vcard::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;
               background:var(--accent);border-radius:3px 0 0 3px}
.vcard.is-up::before{background:var(--up)} .vcard.is-down::before{background:var(--down)}
.vcard h3{margin:0 0 2px;font-size:14px;font-family:var(--serif);font-weight:600}
.vcard .desc{color:var(--ink-3);font-size:11.5px;line-height:1.5;margin:0 0 12px;
             min-height:34px}
.big{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:32px;
     line-height:1;font-weight:600;letter-spacing:-.02em}
.big.up{color:var(--up)} .big.down{color:var(--down)}
.vrow{display:flex;justify-content:space-between;gap:10px;font-size:12px;
      padding:4px 0;border-top:1px dotted var(--rule)}
.vrow:first-of-type{border-top:none}
.vrow span:first-child{color:var(--ink-3)}
.vrow span:last-child{font-family:var(--mono);font-variant-numeric:tabular-nums}

/* ---- sections ---- */
section{margin:0 0 34px;scroll-margin-top:70px}
.sec-head{display:flex;align-items:baseline;gap:12px;
          border-bottom:1px solid var(--rule-strong);padding-bottom:6px;margin:0 0 14px}
.sec-head h2{font-family:var(--serif);font-size:19px;margin:0;font-weight:600}
.sec-head p{margin:0;color:var(--ink-3);font-size:12px}
.panel{background:var(--surface);border:1px solid var(--rule);border-radius:3px;
       padding:16px 18px;box-shadow:var(--shadow)}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:14px}
.grid3{display:grid;grid-template-columns:repeat(auto-fit,minmax(215px,1fr));gap:14px}

/* ---- tables ---- */
.tw{overflow-x:auto;border:1px solid var(--rule);border-radius:3px;
    background:var(--surface);box-shadow:var(--shadow)}
table{border-collapse:collapse;width:100%;font-size:12.5px}
thead th{position:sticky;top:0;background:var(--surface-2);text-align:left;
         padding:8px 10px;border-bottom:1px solid var(--rule-strong);
         font-weight:600;font-size:11px;letter-spacing:.04em;color:var(--ink-2);
         white-space:nowrap;cursor:pointer;user-select:none}
thead th:hover{color:var(--accent)}
thead th[data-nosort]{cursor:default}
thead th::after{content:"";opacity:.35;font-size:9px;margin-left:4px}
thead th[data-sort="asc"]::after{content:"▲";opacity:.9}
thead th[data-sort="desc"]::after{content:"▼";opacity:.9}
tbody td{padding:7px 10px;border-bottom:1px solid var(--rule);vertical-align:top}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover{background:var(--surface-2)}
td.n,th.n{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums;
          white-space:nowrap}
.up{color:var(--up)} .down{color:var(--down)} .flat{color:var(--flat)}
.muted{color:var(--ink-3)}
.small{font-size:11px}
.trunc{max-width:340px}

/* ---- chips ---- */
.pill{display:inline-block;padding:1px 7px;border-radius:9px;font-size:10.5px;
      font-weight:600;border:1px solid transparent;white-space:nowrap;
      font-family:var(--sans)}
.g-S{background:var(--accent);color:var(--surface)}
.g-A{background:var(--accent-soft);color:var(--accent);border-color:var(--accent)}
.g-B{background:var(--surface-2);color:var(--ink-2);border-color:var(--rule-strong)}
.g-C{background:transparent;color:var(--ink-3);border-color:var(--rule-strong)}
.lv-action{background:var(--up-soft);color:var(--up);border-color:var(--up)}
.lv-warn{background:var(--warn-soft);color:var(--warn);border-color:var(--warn)}
.lv-info{background:var(--surface-2);color:var(--ink-3);border-color:var(--rule-strong)}
.tag{font-family:var(--mono);font-size:10.5px;color:var(--ink-3)}

/* ---- alerts ---- */
.alert{display:flex;gap:10px;padding:9px 12px;border-bottom:1px solid var(--rule);
       border-left:3px solid var(--flat);align-items:flex-start}
.alert:last-child{border-bottom:none}
.alert.action{border-left-color:var(--up);background:var(--up-soft)}
.alert.warn{border-left-color:var(--warn)}
.alert .k{font-family:var(--mono);font-size:10.5px;color:var(--ink-3);
          min-width:132px;flex-shrink:0}

/* ---- meters ---- */
.meter{height:5px;background:var(--surface-2);border-radius:3px;overflow:hidden;
       border:1px solid var(--rule)}
.meter i{display:block;height:100%;background:var(--accent)}
.meter.hot i{background:var(--up)}
.factors{display:flex;gap:3px;align-items:flex-end;height:26px}
.factors i{width:9px;background:var(--accent);opacity:.85;border-radius:1px 1px 0 0;
           display:block}

/* ---- rail ---- */
.rail{position:sticky;top:0;z-index:20;background:var(--paper);
      border-bottom:1px solid var(--rule);margin-bottom:22px}
.rail nav{max-width:1240px;margin:0 auto;padding:9px 20px;display:flex;gap:16px;
          overflow-x:auto;font-size:12px}
.rail a{color:var(--ink-3);white-space:nowrap;padding:2px 0;border-bottom:2px solid transparent}
.rail a:hover,.rail a.on{color:var(--ink);border-bottom-color:var(--accent);
                         text-decoration:none}

figure{margin:0}
figcaption{font-size:11.5px;color:var(--ink-3);margin-top:8px;line-height:1.5}
svg{display:block;max-width:100%;height:auto}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:11.5px;color:var(--ink-2);
        margin:0 0 10px}
.legend b{display:inline-block;width:16px;height:2px;vertical-align:middle;
          margin-right:5px;border-radius:1px}
.note{background:var(--surface-2);border-left:3px solid var(--rule-strong);
      padding:11px 14px;font-size:12px;color:var(--ink-2);border-radius:0 3px 3px 0}
.note strong{color:var(--ink)}
footer{border-top:1px solid var(--rule);margin-top:40px;padding-top:16px;
       color:var(--ink-3);font-size:11.5px}
@media (max-width:640px){
  .mh-title{font-size:23px} .big{font-size:26px} .wrap{padding:0 14px 48px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
"""

JS = """
document.querySelectorAll('table[data-sortable]').forEach(function(t){
  t.querySelectorAll('thead th:not([data-nosort])').forEach(function(th,i){
    th.tabIndex=0;
    function go(){
      var dir = th.dataset.sort==='asc' ? 'desc':'asc';
      t.querySelectorAll('thead th').forEach(function(o){delete o.dataset.sort;});
      th.dataset.sort=dir;
      var tb=t.tBodies[0], rows=Array.prototype.slice.call(tb.rows);
      rows.sort(function(a,b){
        var x=a.cells[i], y=b.cells[i];
        var xv=x.dataset.v!==undefined?x.dataset.v:x.textContent.trim();
        var yv=y.dataset.v!==undefined?y.dataset.v:y.textContent.trim();
        var xn=parseFloat(xv), yn=parseFloat(yv);
        var both=!isNaN(xn)&&!isNaN(yn);
        var c = both ? xn-yn : String(xv).localeCompare(String(yv),'zh');
        return dir==='asc'? c : -c;
      });
      rows.forEach(function(r){tb.appendChild(r);});
    }
    th.addEventListener('click',go);
    th.addEventListener('keydown',function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();go();}});
  });
});
var links=Array.prototype.slice.call(document.querySelectorAll('.rail a'));
var secs=links.map(function(a){return document.querySelector(a.getAttribute('href'));});
function mark(){
  var y=window.scrollY+120, best=0;
  secs.forEach(function(s,i){ if(s&&s.offsetTop<=y) best=i; });
  links.forEach(function(a,i){ a.classList.toggle('on', i===best); });
}
window.addEventListener('scroll',mark,{passive:true}); mark();
"""


# --------------------------------------------------------------------- utils
def e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def pc(v: float | None, nd: int = 2, sign: bool = True) -> str:
    if v is None:
        return "—"
    return f"{v*100:+.{nd}f}%" if sign else f"{v*100:.{nd}f}%"


def cls(v: float | None) -> str:
    if v is None:
        return "flat"
    return "up" if v > 0 else ("down" if v < 0 else "flat")


def money(v: float | None) -> str:
    return "—" if v is None else f"${v:,.0f}"


def num(v: float | None, nd: int = 2) -> str:
    return "—" if v is None else f"{v:,.{nd}f}"


def _dv(v: Any) -> str:
    """data-v attribute so tables sort on the raw value, not the display string."""
    if v is None:
        return ' data-v="-999999"'
    return f' data-v="{e(v)}"'


# --------------------------------------------------------------------- charts
def line_chart(series: dict[str, list[tuple[str, float]]], height: int = 240,
               width: int = 1180, ylabel: str = "累计收益") -> str:
    """Multi-series step chart of cumulative return, with an emphasised endpoint."""
    pts = [(d, v) for s in series.values() for d, v in s]
    if len(pts) < 2:
        return '<p class="muted small">数据不足，无法作图。</p>'
    xs = sorted({d for d, _ in pts})
    xi = {d: i for i, d in enumerate(xs)}
    lo = min(v for _, v in pts)
    hi = max(v for _, v in pts)
    span = max(hi - lo, 0.004)
    lo -= span * 0.12
    hi += span * 0.12
    pad_l, pad_r, pad_t, pad_b = 52, 96, 14, 26
    iw = width - pad_l - pad_r
    ih = height - pad_t - pad_b

    def X(d: str) -> float:
        return pad_l + (xi[d] / max(len(xs) - 1, 1)) * iw

    def Y(v: float) -> float:
        return pad_t + (1 - (v - lo) / (hi - lo)) * ih

    colours = {"disciplined": "var(--accent)", "naive": "var(--up)",
               "SPY": "var(--ink-3)", "ACWI": "var(--down)"}
    dash = {"SPY": "4 3", "ACWI": "2 3"}
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" '
           f'aria-label="{e(ylabel)}曲线">']

    # zero line and gridlines
    steps = 5
    for k in range(steps + 1):
        v = lo + (hi - lo) * k / steps
        y = Y(v)
        strong = abs(v) < 1e-9
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l+iw}" y2="{y:.1f}" '
                   f'stroke="var(--{"rule-strong" if strong else "rule"})" '
                   f'stroke-width="{1 if strong else 0.6}"/>')
        out.append(f'<text x="{pad_l-8}" y="{y+3.5:.1f}" text-anchor="end" '
                   f'font-size="10" font-family="var(--mono)" fill="var(--ink-3)">'
                   f'{v*100:+.1f}%</text>')
    # x labels: first, middle, last
    for d in (xs[0], xs[len(xs) // 2], xs[-1]):
        out.append(f'<text x="{X(d):.1f}" y="{height-8}" text-anchor="middle" '
                   f'font-size="10" font-family="var(--mono)" fill="var(--ink-3)">'
                   f'{e(d[5:])}</text>')

    # Endpoint labels are placed last so collisions can be resolved: two series
    # ending a few basis points apart would otherwise overprint each other.
    labels: list[tuple[float, float, str, str]] = []
    for name, s in series.items():
        if len(s) < 2:
            continue
        col = colours.get(name, "var(--warn)")
        d_attr = " ".join(f"{'M' if i == 0 else 'L'}{X(d):.1f},{Y(v):.1f}"
                          for i, (d, v) in enumerate(s))
        da = f' stroke-dasharray="{dash[name]}"' if name in dash else ""
        out.append(f'<path d="{d_attr}" fill="none" stroke="{col}" '
                   f'stroke-width="{2 if name in ("disciplined", "naive") else 1.3}"'
                   f'{da} stroke-linejoin="round"/>')
        ld, lv = s[-1]
        out.append(f'<circle cx="{X(ld):.1f}" cy="{Y(lv):.1f}" r="3" fill="{col}"/>')
        labels.append((X(ld) + 7, Y(lv), col, f"{lv*100:+.2f}%"))

    MIN_GAP = 12.0
    labels.sort(key=lambda t: t[1])
    placed: list[float] = []
    for x, y, col, txt in labels:
        ny = y
        for py in placed:
            if abs(ny - py) < MIN_GAP:
                ny = py + MIN_GAP
        placed.append(ny)
        leader = ("" if abs(ny - y) < 1 else
                  f'<line x1="{x-3:.1f}" y1="{y:.1f}" x2="{x+1:.1f}" y2="{ny:.1f}" '
                  f'stroke="{col}" stroke-width="0.7" opacity=".55"/>')
        out.append(leader)
        out.append(f'<text x="{x:.1f}" y="{ny+3.5:.1f}" font-size="10.5" '
                   f'font-family="var(--mono)" fill="{col}">{txt}</text>')
    out.append("</svg>")
    return "".join(out)


def bar_row(label: str, value: float | None, lo: float, hi: float,
            fmt: str = "{:+.2%}") -> str:
    if value is None:
        return (f'<div class="vrow"><span>{e(label)}</span>'
                f'<span class="muted">—</span></div>')
    frac = 0.0 if hi == lo else max(0.0, min(1.0, (value - lo) / (hi - lo)))
    hot = " hot" if value < 0 else ""
    return (f'<div class="vrow"><span>{e(label)}</span>'
            f'<span>{fmt.format(value)}</span></div>'
            f'<div class="meter{hot}"><i style="width:{frac*100:.0f}%"></i></div>')


def factor_bars(f: dict) -> str:
    keys = ("D", "A", "B", "N")
    bars = []
    for k in keys:
        v = f.get(k)
        h = 0 if v is None else max(2, round(v / 100 * 26))
        bars.append(f'<i style="height:{h}px" title="{k}={num(v,1)}"></i>')
    return f'<div class="factors">{"".join(bars)}</div>'


# --------------------------------------------------------------------- sections
def _verdict(rep: dict) -> str:
    cards = []
    for bid, b in rep["books"].items():
        if b.get("empty"):
            continue
        mb = b.get("matched_benchmark") or {}
        cards.append(f"""
<article class="vcard is-{cls(b['cum_ret'])}">
  <div class="eyebrow">{e(bid)}</div>
  <h3>{e(b['label'])}</h3>
  <p class="desc">{e(b['desc'])}</p>
  <div class="big {cls(b['cum_ret'])}">{pc(b['cum_ret'])}</div>
  <div class="small muted" style="margin:4px 0 12px">
    {money(b['equity'])} · {e(b['from'])} → {e(b['to'])} · {b['sessions']} 个交易日</div>
  <div class="vrow"><span>SPY 同期</span><span>{pc(b['benchmarks'].get('SPY'))}</span></div>
  <div class="vrow"><span>超额 vs SPY</span>
    <span class="{cls(b['excess_vs_spy'])}">{pc(b['excess_vs_spy'])}</span></div>
  <div class="vrow"><span>敞口匹配基准<br><span class="small muted">{e(mb.get('label','—'))}</span></span>
    <span class="{cls(mb.get('excess'))}">{pc(mb.get('excess'))}</span></div>
  <div class="vrow"><span>最大回撤</span><span class="down">{pc(b['max_drawdown'])}</span></div>
  <div class="vrow"><span>净敞口 / 在场</span>
    <span>{b['gross']*100:.0f}% / {b['n_open']}</span></div>
  <div class="vrow"><span>成交率</span>
    <span>{'—' if b['orders']['fill_rate'] is None else f"{b['orders']['fill_rate']*100:.0f}%"}</span></div>
</article>""")
    return f'<div class="verdict">{"".join(cards)}</div>'


def _curves(con, rep: dict) -> str:
    series: dict[str, list[tuple[str, float]]] = {}
    for bid, b in rep["books"].items():
        if b.get("empty"):
            continue
        series[bid] = [(p["d"], p["cum_ret"] or 0.0) for p in b["curve"]]
    anchor = None
    for s in series.values():
        if s:
            anchor = s[0][0] if anchor is None else min(anchor, s[0][0])
    if anchor:
        for name, code in (("SPY", config.BENCHMARKS["SPY"]),
                           ("ACWI", config.BENCHMARKS["ACWI"])):
            base = futu_px.last_close_on_or_before(con, code, anchor)
            if not base:
                continue
            bars = futu_px.bars(con, code, anchor)
            series[name] = [(x["d"], x["close"] / base[1] - 1) for x in bars]

    legend = "".join(
        f'<span><b style="background:{c}"></b>{e(n)}</span>' for n, c in (
            ("守纪律组合", "var(--accent)"), ("无脑全买组合", "var(--up)"),
            ("SPY", "var(--ink-3)"), ("ACWI", "var(--down)")))
    return f"""
<figure class="panel">
  <div class="legend">{legend}</div>
  {line_chart(series)}
  <figcaption>两个组合共用同一批 idea，差别只在执行：守纪律按方法论仓位下限价单、
  带止损止盈与到期平仓，未成交的钱留在现金并按货币基金收益计息；无脑组合在首个
  可成交收盘价等权全买并持有到期。两条线的差额即「仓位管理」的贡献；组合与 SPY
  的差额即「选股 + 择时」的贡献。</figcaption>
</figure>"""


def _skill(rep: dict) -> str:
    ir = rep["ideas"]
    if not ir.get("scored"):
        return '<div class="panel"><p class="muted">尚无可评分的 outcome。</p></div>'
    rk = ir["ranking"]
    cal = ir["calibration"]
    rows = "".join(
        f'<div class="vrow"><span>{e(v["label"])}</span>'
        f'<span class="{cls(v["rho_vs_realized"])}">{num(v["rho_vs_realized"],3)}</span></div>'
        for v in rk.values())
    scen = " · ".join(f"{k} {v*100:.0f}%"
                      for k, v in (cal.get("scenario_realised_pct") or {}).items())
    return f"""
<div class="grid2">
  <div class="panel">
    <div class="eyebrow">想法层面 · 等权</div>
    <div class="big {cls(ir['equal_weight_ret'])}">{pc(ir['equal_weight_ret'])}</div>
    <div class="small muted" style="margin:4px 0 12px">
      {ir['scored']}/{ir['n']} 条可评分（{ir['unmarkable']} 条无法盯市）· 剔除仓位影响</div>
    <div class="vrow"><span>中位收益</span><span>{pc(ir['median_ret'])}</span></div>
    <div class="vrow"><span>胜率</span><span>{ir['hit_rate']*100:.0f}%</span></div>
    <div class="vrow"><span>超额均值</span>
      <span class="{cls(ir['excess_mean'])}">{pc(ir['excess_mean'])}</span></div>
    <div class="vrow"><span>跑赢基准比例</span>
      <span>{(ir['beat_bench_rate'] or 0)*100:.0f}%</span></div>
  </div>
  <div class="panel">
    <div class="eyebrow">排序能力 · Spearman ρ</div>
    <p class="small muted" style="margin:2px 0 10px">
      引擎给出的赔率排序，是否预测了后来的真实收益。ρ &gt; 0 表示排序有效。</p>
    {rows}
  </div>
  <div class="panel">
    <div class="eyebrow">概率校准 · Brier</div>
    <p class="small muted" style="margin:2px 0 10px">
      情景概率是否诚实。技能分 = 1 − Brier ÷ 均匀先验；&gt; 0 表示优于「三分之一各一」。</p>
    <div class="vrow"><span>Brier 中心 / 保守</span>
      <span>{num(cal['brier_central'],3)} / {num(cal['brier_conservative'],3)}</span></div>
    <div class="vrow"><span>均匀基线</span><span>{num(cal['brier_uniform_baseline'],3)}</span></div>
    <div class="vrow"><span>技能分 中心 / 保守</span>
      <span class="{cls(cal['skill_central'])}">{num(cal['skill_central'],3)} /
      {num(cal['skill_conservative'],3)}</span></div>
    <div class="vrow"><span>情景实现分布</span><span class="small">{e(scen) or '—'}</span></div>
  </div>
</div>"""


def _buckets(rep: dict) -> str:
    ir = rep["ideas"]
    if not ir.get("scored"):
        return ""
    labels = {"grade": "绝对评级", "grade_rel": "相对分位", "horizon": "期限",
              "instrument": "工具类型", "vol_check": "情景幅度校验",
              "filled": "是否成交", "theme": "宏观主题"}
    blocks = []
    for key, lab in labels.items():
        bk = ir["buckets"].get(key) or {}
        if not bk:
            continue
        rows = "".join(f"""<tr><td>{e(k)}</td><td class="n">{v['n']}</td>
<td class="n {cls(v['mean'])}"{_dv(v['mean'])}>{pc(v['mean'])}</td>
<td class="n">{v['hit']*100:.0f}%</td>
<td class="n {cls(v['excess'])}"{_dv(v['excess'])}>{pc(v['excess'])}</td></tr>"""
                       for k, v in bk.items())
        blocks.append(f"""
<div class="tw"><table data-sortable>
<caption class="eyebrow" style="text-align:left;padding:9px 10px 0">按{e(lab)}</caption>
<thead><tr><th>{e(lab)}</th><th class="n">n</th><th class="n">均值</th>
<th class="n">胜率</th><th class="n">超额</th></tr></thead>
<tbody>{rows}</tbody></table></div>""")
    return f'<div class="grid2">{"".join(blocks)}</div>'


def _batch_table(con) -> str:
    bid = ideas_mod.latest_batch(con)
    if not bid:
        return '<p class="muted">尚无批次。</p>'
    rows_in = ideas_mod.load_batch(con, bid)
    b = db.q1(con, "SELECT * FROM batches WHERE batch_id=?", (bid,))
    val = db.jl(b["validation"], {}) or {}
    out = db.q(con, "SELECT idea_uid, realized, excess, filled, exit_reason "
                    "FROM outcomes WHERE idea_uid IN (%s)"
               % ",".join("?" * len(rows_in)), [r["idea_uid"] for r in rows_in])
    om = {r["idea_uid"]: dict(r) for r in out}

    trs = []
    for r in rows_in:
        o = om.get(r["idea_uid"], {})
        entry = ("—" if r["entry_lo"] is None else
                 (f"{num(r['entry_lo'],2)}–{num(r['entry_hi'],2)}"
                  if r["entry_hi"] else f"≤{num(r['entry_lo'],2)}"))
        trs.append(f"""<tr>
<td class="n">{r['rank']}</td>
<td><strong>{e(r['tool'])}</strong><div class="small muted">{e(r['tool_desc'] or '')[:48]}</div></td>
<td><span class="tag">{e(r['instrument'])}</span></td>
<td>{e(r['theme'] or '')}</td>
<td>{e(r['horizon'])}</td>
<td><span class="pill g-{e(r['grade'])}">{e(r['grade'])}</span>
    <span class="tag">{e(r['grade_rel'] or '')}</span></td>
<td class="n"{_dv(r['or_c'])}>{num(r['or_c'])}</td>
<td class="n"{_dv(r['or_k'])}>{num(r['or_k'])}</td>
<td class="n"{_dv(r['ev_c'])}>{num(r['ev_c'])}%</td>
<td class="n"{_dv(r['hurdle'])}>{num(r['hurdle'])}%</td>
<td class="n">{num(r['ref_price'],2)}</td>
<td class="n small">{e(entry)}</td>
<td class="n small">{num(r['stop_px'],2)}</td>
<td><span class="tag">{e(r['vol_check'])}</span></td>
<td class="n {cls(o.get('realized'))}"{_dv(o.get('realized'))}>{pc(o.get('realized'))}</td>
<td class="n {cls(o.get('excess'))}"{_dv(o.get('excess'))}>{pc(o.get('excess'))}</td>
<td class="small muted">{e(o.get('exit_reason') or '')}</td>
</tr>""")
    checks = [c for c in val.get("checks", []) if not c["ok"]]
    warn = ("".join(f'<div class="alert {"action" if c["severity"]=="error" else "warn"}">'
                    f'<span class="k">{e(c["check"])}</span>'
                    f'<span class="small">{e(json.dumps(c["detail"], ensure_ascii=False))[:200]}</span>'
                    f'</div>' for c in checks)
            or '<div class="alert"><span class="k">all checks</span>'
               '<span class="small">全部通过</span></div>')
    return f"""
<div class="note" style="margin-bottom:12px">
  <strong>{e(bid)}</strong> · as_of {e(b['as_of'])} · 生成器 <code>{e(b['generator'])}</code>
  · {b['n_ideas']} 条 · 状态 {e(b['status'])} · 校验
  {'<span class="up">通过</span>' if val.get('pass') else '<span class="down">未通过</span>'}
  （{val.get('n_errors',0)} error / {val.get('n_warnings',0)} warning）
  · output_sha <code>{e((b['output_sha'] or '')[:12])}</code>
</div>
<div class="panel" style="padding:0;margin-bottom:12px">{warn}</div>
<div class="tw"><table data-sortable>
<thead><tr><th class="n">#</th><th>工具</th><th>类型</th><th>主题</th><th>期限</th>
<th>评级</th><th class="n">中心赔率</th><th class="n">保守赔率</th><th class="n">EV</th>
<th class="n">hurdle</th><th class="n">参考价</th><th class="n">进场</th>
<th class="n">止损</th><th>幅度校验</th><th class="n">已实现</th><th class="n">超额</th>
<th>离场</th></tr></thead>
<tbody>{''.join(trs)}</tbody></table></div>"""


def _positions(con) -> str:
    blocks = []
    for bid in config.BOOKS:
        ps = paper.positions(con, bid)
        if not ps:
            continue
        trs = []
        for p in ps:
            m = db.q1(con, "SELECT px, upnl_pct FROM mtm WHERE book_id=? AND pos_id=? "
                           "ORDER BY d DESC LIMIT 1", (bid, p["pos_id"]))
            live = (p["realized"] / p["cost"] if p["status"] == "closed" and p["cost"]
                    else (m["upnl_pct"] if m else None))
            px = p["close_px"] if p["status"] == "closed" else (m["px"] if m else None)
            trs.append(f"""<tr>
<td><strong>{e(p['tool'])}</strong></td>
<td><span class="tag">{e(p['code'])}</span></td>
<td>{e(p['theme'] or '')}</td>
<td>{e(p['hz'])}</td>
<td><span class="pill g-{e(p['grade'])}">{e(p['grade'])}</span></td>
<td class="n">{e(p['opened_d'])}</td>
<td class="n">{num(p['avg_px'],2)}</td>
<td class="n">{num(px,2)}</td>
<td class="n"{_dv(p['cost'])}>{money(p['cost'])}</td>
<td class="n {cls(live)}"{_dv(live)}>{pc(live)}</td>
<td class="n small">{num(p['stop_px'],2)}</td>
<td class="n small">{num(p['take_px'],2)}</td>
<td class="n small">{e(p['horizon_end'])}</td>
<td>{'<span class="pill lv-info">在场</span>' if p['status']=='open'
     else f'<span class="pill lv-warn">{e(p["exit_reason"])}</span>'}</td>
</tr>""")
        blocks.append(f"""
<div class="sec-head" style="margin-top:18px">
  <h2 style="font-size:15px">{e(config.BOOKS[bid]['label'])}</h2>
  <p>{len(ps)} 个仓位</p></div>
<div class="tw"><table data-sortable>
<thead><tr><th>工具</th><th>代码</th><th>主题</th><th>期限</th><th>评级</th>
<th class="n">建仓日</th><th class="n">成本价</th><th class="n">现价</th>
<th class="n">投入</th><th class="n">盈亏</th><th class="n">止损</th><th class="n">止盈</th>
<th class="n">到期</th><th>状态</th></tr></thead>
<tbody>{''.join(trs)}</tbody></table></div>""")
    return "".join(blocks) or '<p class="muted">尚无仓位。</p>'


def _themes(con) -> str:
    d = db.q1(con, "SELECT MAX(as_of) a FROM themes")
    if not d or not d["a"]:
        return '<p class="muted">尚无主题打分。运行 <code>ideagen score</code>。</p>'
    rows = db.q(con, "SELECT * FROM themes WHERE as_of=? ORDER BY tis DESC", (d["a"],))
    trs = []
    for r in rows:
        f = db.jl(r["factors"], {}) or {}
        trs.append(f"""<tr>
<td><strong>{e(r['label'])}</strong>
    <div class="small muted trunc">{e(r['key_question'] or '')}</div></td>
<td class="n"{_dv(r['tis'])}><strong>{num(r['tis'],1)}</strong>
    <div class="meter"><i style="width:{min(r['tis'] or 0,100):.0f}%"></i></div></td>
<td>{factor_bars({'D':r['d'],'A':r['a'],'B':r['b'],'N':r['n']})}</td>
<td class="n"{_dv(r['d'])}>{num(r['d'],0)}</td>
<td class="n"{_dv(r['a'])}>{num(r['a'],0)}</td>
<td class="n"{_dv(r['b'])}>{num(r['b'],0)}</td>
<td class="n"{_dv(r['n'])}>{num(r['n'],0)}</td>
<td class="n"{_dv(r['m'])}>{num(r['m'],0)}<div class="small muted">{e(f.get('stage',''))}</div></td>
<td class="n"{_dv(r['c'])}>{num(r['c'],0)}<div class="small muted">{e(f.get('crowding',''))}</div></td>
<td class="n">{r['n_items']}</td><td class="n">{r['n_sources']}</td>
<td><span class="pill g-{'S' if r['tier']=='core' else 'A' if r['tier']=='important' else 'B' if r['tier']=='watch' else 'C'}">{e(r['tier'])}</span>
    {'' if r['confidence']=='ok' else '<span class="tag">低置信</span>'}</td>
</tr>""")
    return f"""
<div class="note" style="margin-bottom:12px">
  打分日 <strong>{e(d['a'])}</strong>。<code>TIS = 0.15·D + 0.25·A + 0.25·B + 0.35·N</code>
  沿用 v0.3 权重以便对比；M（市场验证）与 C（拥挤度）是独立维度，不进 TIS。
  柱状图依次为 D / A / B / N。
</div>
<div class="tw"><table data-sortable>
<thead><tr><th>宏观主题 · 预注册关键结果</th><th class="n">TIS</th><th data-nosort>因子</th>
<th class="n">D</th><th class="n">A</th><th class="n">B</th><th class="n">N</th>
<th class="n">M</th><th class="n">C</th><th class="n">条目</th><th class="n">来源</th>
<th>分级</th></tr></thead>
<tbody>{''.join(trs)}</tbody></table></div>"""


def _alerts(con) -> str:
    rows = db.q(con, "SELECT * FROM alerts ORDER BY d DESC, "
                     "CASE level WHEN 'action' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END "
                     "LIMIT 40")
    if not rows:
        return '<div class="panel"><p class="muted">无告警。</p></div>'
    items = "".join(
        f'<div class="alert {e(r["level"])}">'
        f'<span class="k">{e(r["d"])} · {e(r["kind"])}</span>'
        f'<span>{e(r["message"])}</span></div>' for r in rows)
    return f'<div class="panel" style="padding:0">{items}</div>'


def _coverage(con, rep: dict) -> str:
    cv = rep["coverage"]
    runs = db.q(con, "SELECT * FROM runs ORDER BY started_at DESC LIMIT 6")
    rr = "".join(
        f'<div class="vrow"><span>{e(r["run_id"])} · {e(r["as_of"])}</span>'
        f'<span class="{"up" if r["status"]=="ok" else "down"}">{e(r["status"])}</span></div>'
        for r in runs) or '<div class="vrow"><span class="muted">尚无 run 记录</span><span></span></div>'
    lines = db.q(con, "SELECT line, tier, COUNT(*) n, MAX(published_d) last "
                      "FROM documents GROUP BY line ORDER BY n DESC")
    ll = "".join(
        f'<div class="vrow"><span>{e(config.SOURCE_LINES.get(r["line"],{}).get("label",r["line"]))}'
        f' <span class="tag">T{r["tier"]}</span></span>'
        f'<span>{r["n"]:,} · {e(r["last"])}</span></div>' for r in lines)
    return f"""
<div class="grid3">
  <div class="panel"><div class="eyebrow">语料</div>
    <div class="big">{cv['documents']['n']:,}</div>
    <div class="small muted">{cv['documents']['days']} 个发布日 ·
      {e(cv['documents']['a'])} → {e(cv['documents']['b'])}</div>
    <div style="margin-top:10px">{ll}</div></div>
  <div class="panel"><div class="eyebrow">行情</div>
    <div class="big">{cv['prices']['codes']}</div>
    <div class="small muted">标的 · {cv['prices']['bars']:,} 根日线 ·
      至 {e(cv['prices']['last'])}</div>
    <div style="margin-top:10px">
      <div class="vrow"><span>Olive NAV</span>
        <span>{cv['navs']['keys']} 只 / {cv['navs']['rows']} 点</span></div>
      <div class="vrow"><span>打分日</span><span>{cv['theme_days']}</span></div>
      <div class="vrow"><span>额度受限</span>
        <span>{len(futu_px.quota_blocked(con))} 个标的</span></div>
    </div></div>
  <div class="panel"><div class="eyebrow">最近运行</div>{rr}</div>
</div>"""


# --------------------------------------------------------------------- build
SECTIONS = [
    ("verdict", "结论"), ("curves", "净值曲线"), ("skill", "想法层面能力"),
    ("buckets", "分档归因"), ("batch", "最新批次"), ("positions", "持仓明细"),
    ("themes", "宏观主题打分"), ("alerts", "告警"), ("coverage", "数据与运行"),
]


def build(con, out: Path | None = None, artifact: bool = False) -> Path:
    """Render the dashboard.

    `artifact=True` emits body-only markup (a <title>, the <style>, the content,
    the <script>) with no document wrapper, which is what the Artifact publisher
    expects — it supplies the doctype/html/head/body itself.
    """
    rep = analytics.full_report(con)
    dg = monitor.digest(con)
    out = out or (config.WEB / "index.html")

    rail = "".join(f'<a href="#{k}">{e(v)}</a>' for k, v in SECTIONS)
    n_batches = len(rep["batches"])
    days = max((b.get("sessions") or 0) for b in rep["books"].values()) or 0

    body = f"""
<div class="rail"><nav>{rail}</nav></div>
<div class="wrap">
  <header class="masthead">
    <div class="mh-top">
      <div>
        <div class="eyebrow">IdeaGen40 · 战术交易想法实盘模拟</div>
        <h1 class="mh-title">每天 40 条想法，真的买下去会怎样</h1>
        <p class="mh-sub">Wisburg 多源语料 → v0.4 四因子打分 → 40 条 idea →
          Futu / Olive 实价模拟盘 → 逐日盯市与归因</p>
      </div>
      <div class="mh-meta">
        <span>方法论 v{e(rep['methodology'])}</span>
        <span>词典 v{e(lexicon.LEXICON_VERSION)}</span>
        <span>{n_batches} 个批次</span>
        <span>{days} 个交易日</span>
        <span>生成于 {e(rep['generated_at'][:16].replace('T',' '))} HKT</span>
      </div>
    </div>
  </header>

  <section id="verdict">
    <div class="sec-head"><h2>结论</h2>
      <p>同一批 idea，两种执行方式，与基准并列</p></div>
    {_verdict(rep)}
  </section>

  <section id="curves">
    <div class="sec-head"><h2>净值曲线</h2><p>累计收益，含成本与现金收益</p></div>
    {_curves(con, rep)}
  </section>

  <section id="skill">
    <div class="sec-head"><h2>想法层面能力</h2>
      <p>剔除仓位规模后，idea 本身值不值钱；以及引擎的排序与概率是否诚实</p></div>
    {_skill(rep)}
  </section>

  <section id="buckets">
    <div class="sec-head"><h2>分档归因</h2><p>哪一类 idea 在赚钱</p></div>
    {_buckets(rep)}
  </section>

  <section id="batch">
    <div class="sec-head"><h2>最新批次</h2><p>逐条 idea、赔率、进出场与已实现结果</p></div>
    {_batch_table(con)}
  </section>

  <section id="positions">
    <div class="sec-head"><h2>持仓明细</h2><p>两个组合的全部仓位与盈亏</p></div>
    {_positions(con)}
  </section>

  <section id="themes">
    <div class="sec-head"><h2>宏观主题打分</h2>
      <p>当日 D / A / B / N 与独立的 M（验证）、C（拥挤）</p></div>
    {_themes(con)}
  </section>

  <section id="alerts">
    <div class="sec-head"><h2>告警</h2>
      <p>止损临近、主题前提被价格否定、拥挤度跳升、临近到期</p></div>
    {_alerts(con)}
  </section>

  <section id="coverage">
    <div class="sec-head"><h2>数据与运行</h2><p>每天真的拉到了什么</p></div>
    {_coverage(con, rep)}
  </section>

  <footer>
    <p>模拟盘，非投资建议。所有成交均为规则化模拟：进场按限价区间与突破次日开盘、
    离场按收盘止损 / 盘中止盈 / 到期平仓，双边计入佣金与滑点，闲置现金按 Olive
    货币基金货架中位 7 日年化计息。Olive 基金仅在当日有 NAV 时进入组合，
    否则记为「已映射但不可盯市」并从 P&amp;L 中剔除。</p>
    <p>行情 Futu OpenD（US / HK，前复权日线）· 语料 Wisburg（8 条线，Tier 1–3）·
    产品货架 Olive / Nexus HK。</p>
  </footer>
</div>"""

    title = "IdeaGen40 · 战术交易想法实盘模拟"
    if artifact:
        doc = (f"<title>{title}</title>\n<style>{CSS}</style>\n"
               f"{body}\n<script>{JS}</script>")
    else:
        doc = f"""<!doctype html>
<html lang="zh-Hans"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>{CSS}</style>
</head><body>{body}<script>{JS}</script></body></html>"""

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")

    # Machine-readable twin, so the Feishu doc and any downstream tool read the
    # same numbers the page shows.
    (out.parent / "report.json").write_text(
        json.dumps({"report": rep, "digest": dg}, ensure_ascii=False, indent=1,
                   default=str), encoding="utf-8")
    return out
