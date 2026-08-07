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

GLOSSARY = {
 "TIS": "Tactical Impact Score，战术冲击潜力。= 0.15·D + 0.25·A + 0.25·B + 0.35·N。回答「这个主题值不值得研究」，不回答「现在该不该买」。≥75 核心 / ≥60 重要 / ≥45 观察 / 更低是背景。",
 "D": "讨论覆盖。这个主题在 3 日窗口里被多少条独立来源提到，对数标定到当窗口最响的主题。100 = 全场最被讨论。",
 "A": "三日升温。讨论是不是在窗口后段变密。60% 看三天的重心偏向哪一天，40% 看当日热度在该主题自己过去 20 天分布里的百分位。高 = 正在加速。",
 "B": "关键争议。围绕同一个预注册问题，看多和看空的观点是否都存在，以及一手/卖方/策展三层来源之间是否结论对立。高 = 还有预期差；低 = 已是共识。",
 "N": "最新变化。30% 新事实广度 + 40% 意外程度（主题指标最大单日涨跌 ÷ 自身日波动）+ 30% 因果深度（叙事 25 / 价格 50 / 订单收入 75 / 已实现利润或政策落地 100）。高 = 真有新东西。",
 "M": "市场验证，独立维度，不进 TIS。主题的预注册价格指标是否已经按预期方向动了。0–29 尚未定价 / 30–59 早期 / 60–79 已有确认 / 80+ 交易成熟。低 = 便宜但没人信；高 = 已经被交易过。",
 "C": "拥挤度，v0.4 新增，独立维度。45% 60日动量百分位 + 30% 距 52 周高点 + 25% 低波动溢价（高位且安静最挤）。≥80 强制「等待回踩」并把仓位砍半。",
 "hurdle": "持有期门槛收益（%）。= (无风险 + 流动性溢价) × 月数 ÷ 12。无风险取 Olive 美元货币基金货架的中位 7 日年化——不交易时钱真正能拿到的收益。",
 "中心赔率": "Opportunity Ratio。= Σ概率×max(情景回报−hurdle,0) ÷ Σ概率×max(hurdle−情景回报,0)。>1 表示超过门槛的加权收益大于低于门槛的加权缺口。已扣双边成本。",
 "保守赔率": "同上，但用保守情景（下调上涨概率与幅度、扩大下跌幅度）。v0.3 用它定 S/A/B/C 评级。",
 "评级": "绝对评级：保守赔率≥1.5→S，≥1.0→A，中心赔率≥1.0→B，否则 C。沿用 v0.3 规则。",
 "相对分位": "同一批 40 条内按保守赔率的四分位，Q1 最好。绝对评级会随 hurdle 口径整体漂移，这个不会。",
 "幅度校验": "情景幅度是否落在标的自身已实现波动的 [0.35σ, 2.60σ] 内。wide = 写得太夸张，narrow = 等于没观点。只标记不否决，但会进 outcome 供事后检验。",
 "σ_h": "标的自身已实现年化波动 × √(月数/12)，即该期限的一个标准差。情景幅度都以它为单位。",
 "当日组合": "当天那 40 条自己成一个 1000 万美元的独立盘：等权买入、持有到期限、单独计价。每天一盘，所以哪天的想法好可以直接比。",
 "超额": "组合收益 − SPY 同期收益。",
 "排序能力": "Spearman ρ：引擎给的赔率排序与后来真实收益的秩相关。ρ>0 表示排序有信息，ρ≈0 表示排序没用。",
 "Brier": "三分类（上涨/基准/下跌）概率的平方误差，越小越好。均匀先验（各 1/3）是 0.667。",
 "技能分": "1 − Brier ÷ 0.667。>0 表示概率比瞎猜有信息，<0 表示比瞎猜还差。",
 "字典覆盖": "当天语料里能被至少一个已注册主题命名的比例。主题字典最初是代码里冻结的 16 条，实测只能命名约 54% 的语料——GLP-1 与医保准入、韩国科技股重估、人形机器人、光模块出口管制、央行购金 当时都是有来源的真辩论，字典却没有词。所以字典改成「16 条种子 + 只能追加的注册表」，每天从零匹配语料里挖候选。这个数字下降就说明发现机制跟不上语料；固定主题列表按构造报不出这个数。",
 "新主题": "从语料里发现、而非最初写死的主题。注册日不可回填，且只能给注册日及以后的日子打分——否则「发现主题」就等于事后挑一个已经涨完的东西定义成主题，再为自己排序准确而自豪。",
 "冷启动": "注册未满 20 天的主题。A 因子的强度项要比较当日热度在该主题自己过去 20 天分布里的百分位，新主题没有这段历史，所以那一项暂时算不出来。标出来是为了让「发现的主题是不是系统性更差」变成可测量的问题。",
 "溯源": "智堡是客户端渲染的 SPA，逐篇没有固定网页链接（/report/<id> 之类全返回同一个空壳），所以这里不存永久链接——猜一个等于给一条指向空页的假引用。真正的凭据是可复现的 API 检索式 + 内容 sha1，加上已验证可达的图表 URL。",
 "生成器": "rules:v0.4 = 规则引擎（用于补齐每日历史，thesis 是模板）；claude-code = Claude 按契约手写；seed-import = 2026-07-27 的原始 PM pack。"
}

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
.crossnote{font-family:var(--mono);font-size:13px;font-weight:600;color:var(--ink-2)}
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
/* Cells opt in to wrapping; everything else stays on one line and the wrapper
   scrolls. `prose` additionally clamps to three lines so one long narrative cannot
   make the row hundreds of pixels tall — the full text sits on the td's title. */
td.prose{white-space:normal;max-width:560px;min-width:320px}
td.prose>span{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;
  overflow:hidden}
td.wrap{white-space:normal;max-width:250px;min-width:140px}
td.wrap .small,td.prose .small{white-space:normal}

/* ---- expandable rows: progressive disclosure instead of one long scroll ---- */
tbody tr.exp{cursor:pointer}
tbody tr.exp:hover{background:var(--surface-2)}
tbody tr.exp td:first-child{position:relative;padding-left:22px}
tbody tr.exp td:first-child::before{content:"▸";position:absolute;left:8px;
  color:var(--ink-3);font-size:9px;top:9px;transition:transform .12s ease}
tbody tr.exp[aria-expanded="true"] td:first-child::before{transform:rotate(90deg)}
tbody tr.exp[aria-expanded="true"]{background:var(--accent-soft)}
tr.detail>td{background:var(--surface-2);padding:0;
  border-bottom:2px solid var(--rule-strong);position:sticky;left:0}
.dw{padding:16px 18px 18px;white-space:normal;
  width:min(1120px, calc(100vw - 300px))}
@media (max-width:860px){.dw{width:calc(100vw - 40px)}}
.dw h4{margin:0 0 3px;font-size:13px;font-family:var(--serif)}
.dw .lead{font-size:13px;color:var(--ink);margin:0 0 12px;max-width:74ch}
.dsec{margin-top:15px}
.dsec>h5{margin:0 0 6px;font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;
  color:var(--ink-3);font-weight:600}
.chain{display:grid;gap:0}
.chain>div{display:grid;grid-template-columns:24px 48px minmax(0,1fr);gap:9px;
  align-items:baseline;font-size:12px;padding:6px 0;border-top:1px dotted var(--rule)}
.chain>div:first-child{border-top:none}
.chain b{font-family:var(--mono);color:var(--accent);font-size:12.5px}
.chain .sc{font-family:var(--mono);font-variant-numeric:tabular-nums;font-weight:600}
.chain .wy{color:var(--ink-2);line-height:1.55;min-width:0;overflow-wrap:anywhere}
.src{display:grid;gap:0}
/* minmax(0,1fr) so the title column can actually shrink; a bare 1fr next to an
   auto column lets the nowrap metadata push the whole panel past the viewport. */
.src>div{font-size:12px;padding:7px 0;border-top:1px dotted var(--rule);
  display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:start}
.src>div:first-child{border-top:none}
.src .t{color:var(--ink);line-height:1.5;min-width:0}
.src .m{font-family:var(--mono);font-size:10.5px;color:var(--ink-3);
  text-align:right;white-space:normal;word-break:break-word;max-width:220px}
@media (max-width:900px){
  .src>div{grid-template-columns:1fr}
  .src .m{text-align:left;max-width:none}
}
.chartgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(258px,1fr));
  gap:12px}
.chartgrid figure{margin:0;background:var(--surface);border:1px solid var(--rule);
  border-radius:4px;padding:10px 11px}
.chartgrid img{width:100%;height:auto;border:1px solid var(--rule);border-radius:3px;
  margin:6px 0}
.scen table{min-width:0;width:auto;font-size:12px}
.scen td,.scen th{padding:3px 14px 3px 0;border:none;white-space:nowrap;
  background:none}
.scen th{font-size:10.5px;color:var(--ink-3);cursor:default}
.kvgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));
  gap:0 18px;font-size:12px}
.kvgrid>div{display:flex;justify-content:space-between;gap:8px;padding:4px 0;
  border-bottom:1px dotted var(--rule)}
.kvgrid span:first-child{color:var(--ink-3)}
.kvgrid span:last-child{font-family:var(--mono)}
.weak{opacity:.72}
.hintline{font-size:11.5px;color:var(--ink-3);margin:9px 0 0;line-height:1.5}
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
/* A theme the corpus produced rather than one the author pre-imagined. Worth
   marking on the row: it is younger than the seed set and its A intensity term
   has no own history yet, so it is not quite like-for-like with its neighbours. */
.tag.new{margin-left:6px;padding:0 5px;border-radius:3px;
  background:var(--accent-soft);color:var(--accent);font-weight:600}
/* The ⓘ marker. The bubble itself is NOT a child: table wrappers use
   overflow-x:auto and sticky headers create their own stacking contexts, so an
   absolutely-positioned child gets clipped and truncated. A single fixed-position
   layer appended to the document body, placed by JS, escapes all of them. */
.hint{display:inline-flex;align-items:center;justify-content:center;width:14px;
  height:14px;border-radius:50%;border:1px solid var(--rule-strong);
  color:var(--ink-3);font-size:9px;font-weight:700;cursor:help;margin-left:4px;
  vertical-align:1px;flex-shrink:0;font-family:var(--sans);
  background:var(--surface);line-height:1}
.hint:hover,.hint:focus-visible{border-color:var(--accent);color:var(--accent);
  outline:none}
#tip{position:fixed;z-index:9999;max-width:min(360px,86vw);background:var(--ink);
  color:var(--paper);padding:10px 12px;border-radius:6px;font-size:12px;
  line-height:1.6;box-shadow:0 6px 22px rgba(0,0,0,.32);pointer-events:none;
  opacity:0;visibility:hidden;transition:opacity .09s ease;font-weight:400;
  font-family:var(--sans);text-align:left}
#tip.on{opacity:1;visibility:visible}
#tip b{color:#fff;display:block;margin-bottom:2px;font-family:var(--mono)}
/* ---- cockpit ---- */
.kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(136px,1fr));gap:10px}
.kpi>div{background:var(--surface);border:1px solid var(--rule);border-radius:4px;
  padding:11px 13px;box-shadow:var(--shadow)}
.kpi .v{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:20px;
  font-weight:600;line-height:1.15;letter-spacing:-.01em}
.kpi .k{font-size:10.5px;color:var(--ink-3);display:flex;align-items:center;
  margin-bottom:3px}
.kpi .s{font-size:10.5px;color:var(--ink-3);margin-top:2px}

.cal{display:grid;grid-template-columns:repeat(auto-fill,minmax(104px,1fr));gap:7px}
@media (max-width:520px){.cal{grid-template-columns:repeat(auto-fill,minmax(88px,1fr))}}
.cal button{all:unset;cursor:pointer;background:var(--surface);
  border:1px solid var(--rule);border-radius:4px;padding:9px 10px;
  display:flex;flex-direction:column;gap:2px;position:relative;overflow:hidden}
.cal button:hover{border-color:var(--accent)}
.cal button[aria-current="true"]{border-color:var(--accent);
  box-shadow:0 0 0 1px var(--accent)}
.cal button::after{content:"";position:absolute;left:0;right:0;bottom:0;height:3px}
.cal button.up::after{background:var(--up)} .cal button.down::after{background:var(--down)}
.cal button.flat::after{background:var(--rule-strong)}
.cal .d{font-family:var(--mono);font-size:11px;color:var(--ink-3)}
.cal .r{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:16px;
  font-weight:600;line-height:1.1}
.cal .x{font-size:10px;color:var(--ink-3)}
.cal .pend{color:var(--ink-3);font-size:11px;font-family:var(--sans)}

.glossbtn{all:unset;cursor:pointer;font-size:11px;color:var(--side-ink-2);
  padding:6px 12px;border-radius:5px;display:flex;gap:8px;align-items:center}
.glossbtn:hover{background:rgba(255,255,255,.05);color:var(--side-ink)}
dialog{border:1px solid var(--rule);border-radius:6px;background:var(--surface);
  color:var(--ink);max-width:680px;width:92vw;padding:0;box-shadow:0 12px 40px rgba(0,0,0,.3)}
dialog::backdrop{background:rgba(0,0,0,.45)}
dialog header{display:flex;justify-content:space-between;align-items:center;
  padding:14px 18px;border-bottom:1px solid var(--rule)}
dialog h3{margin:0;font-family:var(--serif);font-size:17px}
dialog .body{padding:6px 18px 18px;max-height:70vh;overflow-y:auto}
dialog dt{font-weight:600;font-size:13px;margin-top:13px;font-family:var(--mono)}
dialog dd{margin:2px 0 0;font-size:12.5px;color:var(--ink-2);line-height:1.6}
dialog button.x{all:unset;cursor:pointer;font-size:20px;color:var(--ink-3);
  padding:0 4px;line-height:1}
dialog button.x:hover{color:var(--ink)}

.tw{overflow-x:auto;border:1px solid var(--rule);border-radius:4px;
  background:var(--surface);box-shadow:var(--shadow)}
/* Dense blotters have up to 17 columns. Letting the browser squeeze them makes
   cells wrap one character per line and rows 240px tall; the wrapper already has
   overflow-x:auto, so the table should keep its natural width and scroll. */
table{border-collapse:collapse;width:100%;min-width:max-content;font-size:12.5px}
/* No position:sticky here: inside an overflow-x container it pins to the wrapper,
   not the viewport, which just adds a stacking context for no benefit. */
thead th{background:var(--surface-2);text-align:left;
  padding:8px 10px;border-bottom:1px solid var(--rule-strong);font-weight:600;
  font-size:10.5px;letter-spacing:.04em;color:var(--ink-2);white-space:nowrap;
  cursor:pointer;user-select:none}
thead th:hover{color:var(--accent)}
thead th[data-nosort]{cursor:default}
thead th[data-sort="asc"]::after{content:" ▲";font-size:8px}
thead th[data-sort="desc"]::after{content:" ▼";font-size:8px}
tbody td{padding:7px 10px;border-bottom:1px solid var(--rule);vertical-align:top;
  white-space:nowrap}
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
let view = 'cockpit', cur = P.meta.today;

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
/* A drawdown is never positive, so a signed "+0.00%" reads wrong, and zero must
   not be tinted as if it were a loss. */
const ddPct = v => (v === null || v === undefined) ? '—'
  : (v === 0 ? '0.00%' : (v * 100).toFixed(2) + '%');
const ddCls = v => (v ? 'down' : 'flat');
const day = P.days;
const dIdx = () => D.indexOf(cur);
const G = P.glossary || {};

/* A first-time reader should not have to know what D/A/B/N mean. Every term that
   is not self-evident gets an ⓘ next to it, hoverable and keyboard-focusable, plus
   a full glossary in the sidebar. */
let TIP = null;
function tipEl() {
  if (!TIP) { TIP = E('div', { id: 'tip', role: 'tooltip' }); document.body.append(TIP); }
  return TIP;
}
function showTip(anchor, term, text) {
  const t = tipEl();
  t.replaceChildren(E('b', {}, term), document.createTextNode(text));
  t.classList.add('on');
  const r = anchor.getBoundingClientRect(), b = t.getBoundingClientRect();
  const M = 8;
  let x = r.left + r.width / 2 - b.width / 2;
  x = Math.max(M, Math.min(x, window.innerWidth - b.width - M));
  // above by default; flip below when there is not enough room
  let y = r.top - b.height - 9;
  if (y < M) y = r.bottom + 9;
  t.style.left = x + 'px';
  t.style.top = y + 'px';
}
function hideTip() { if (TIP) TIP.classList.remove('on'); }
window.addEventListener('scroll', hideTip, { passive: true, capture: true });
window.addEventListener('resize', hideTip);
document.addEventListener('keydown', e => { if (e.key === 'Escape') hideTip(); });

const hint = k => {
  if (!G[k]) return null;
  const el = E('span', { cls: 'hint', tabindex: '0', role: 'button',
                         'aria-label': k + '：' + G[k] }, 'i');
  const show = () => showTip(el, k, G[k]);
  el.addEventListener('mouseenter', show);
  el.addEventListener('focus', show);
  el.addEventListener('mouseleave', hideTip);
  el.addEventListener('blur', hideTip);
  // a header cell sorts on click; the ⓘ must not also sort it
  el.addEventListener('click', e => { e.stopPropagation(); e.preventDefault(); show(); });
  el.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') { e.stopPropagation(); e.preventDefault(); show(); }
  });
  return el;
};
const lbl = (text, k) => E('span', {}, text, hint(k === undefined ? text : k));

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
      const BLANK = /^(—|-|\s*)$/;
      rows.sort((a, b) => {
        const g = r => { const c = r.cells[i]; return c.dataset.v ?? c.textContent.trim(); };
        const x = g(a), y = g(b);
        // empty cells always sink, whichever direction is active
        const xb = x === 'null' || BLANK.test(x), yb = y === 'null' || BLANK.test(y);
        if (xb !== yb) return xb ? 1 : -1;
        if (xb && yb) return 0;
        const xn = parseFloat(x), yn = parseFloat(y);
        const c = (!isNaN(xn) && !isNaN(yn)) ? xn - yn
          : String(x).localeCompare(String(y), 'zh');
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

/* A table whose rows open in place. Used instead of stacking a summary table and
   a separate detail section: the reasoning and the sources for a row belong under
   that row, not at the bottom of the page. */
function expTable(cols, rows, detailFn) {
  const t = E('table', { 'data-sortable': '' },
    E('thead', {}, E('tr', {}, cols.map(c =>
      E('th', { cls: c.n ? 'n' : '', 'data-nosort': c.nosort ? '' : null }, c.h)))),
    E('tbody', {}));
  const tb = t.tBodies[0];
  rows.forEach((r, idx) => {
    const tr = E('tr', { cls: 'exp', tabindex: '0', role: 'button',
                         'aria-expanded': 'false' },
      r.cells.map((cell, i) => E('td', {
        cls: [cols[i].n ? 'n' : '', cols[i].cls || '',
              cell && cell.cls || ''].join(' ').trim(),
        title: cell && cell.title || null,
        'data-v': cell && cell.v !== undefined && cell.v !== null ? cell.v : null,
      }, cell && cell.el ? cell.el : (cell && cell.t !== undefined ? cell.t : cell))));
    const det = E('tr', { cls: 'detail', hidden: '' },
      E('td', { colspan: String(cols.length) },
        E('div', { cls: 'dw' })));
    let built = false;
    const toggle = () => {
      const open = tr.getAttribute('aria-expanded') === 'true';
      if (!open && !built) { det.querySelector('.dw').append(detailFn(r.data, idx)); built = true; }
      tr.setAttribute('aria-expanded', open ? 'false' : 'true');
      det.hidden = open;
    };
    tr.addEventListener('click', e => {
      if (e.target.closest('a,.hint')) return;   // links and ⓘ keep their own job
      toggle();
    });
    tr.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
    });
    tb.append(tr, det);
  });
  return E('div', { cls: 'tw' }, sortable(t));
}

const dsec = (title, ...body) => E('div', { cls: 'dsec' },
  E('h5', {}, title), ...body);
const kvg = pairs => E('div', { cls: 'kvgrid' },
  pairs.filter(Boolean).map(([k, v]) => E('div', {},
    E('span', {}, k), E('span', {}, v))));

/* One source, rendered where it is cited. */
function srcRow(x) {
  const meta = [];
  if (x.resolved) {
    meta.push(`T${x.tier}`, x.line);
    if (x.institution) meta.push(x.institution);
    if (x.published_at) meta.push(x.published_at.slice(0, 16).replace('T', ' '));
  }
  return E('div', {},
    E('div', { cls: 't' },
      E('span', {}, x.title || x.doc_id),
      x.resolved === false ? E('span', { cls: 'muted small' }, '　（' + (x.note || '') + '）') : null,
      x.retrieval ? E('div', { cls: 'm', style: 'text-align:left;margin-top:2px' },
        x.doc_id + '　' + x.retrieval + (x.hash ? '　sha1 ' + x.hash : '')) : null,
      (x.assets && x.assets.length)
        ? E('div', { style: 'margin-top:3px' },
            ...x.assets.map((a, i) => E('a', {
              href: a.url, target: '_blank', rel: 'noopener', cls: 'tag',
              style: 'margin-right:8px' }, (i ? '' : '') + '图' + (i + 1))))
        : null),
    E('div', { cls: 'm' }, meta.join(' · ')));
}

function chartFig(c) {
  const f = E('figure', { cls: c.weak ? 'weak' : '' },
    E('div', { style: 'font-weight:600;font-size:12.5px' }, c.title),
    E('div', { cls: 'small muted' },
      `智堡图表库 · ${c.published_d} · ${(c.bytes / 1024).toFixed(0)}KB`
      + (c.weak ? ' · 关键词弱匹配' : '')));
  if (P.meta.embed_images)
    f.append(E('img', { src: c.url, alt: c.title, loading: 'lazy' }));
  f.append(E('p', { cls: 'small', style: 'margin:4px 0 0;color:var(--ink-2)' },
    (c.caption || '').slice(0, 260) + ((c.caption || '').length > 260 ? '…' : '')),
    E('a', { href: c.url, target: '_blank', rel: 'noopener', cls: 'tag' }, '原图 ↗'));
  return f;
}

function table(cols, rows) {
  const t = E('table', { 'data-sortable': '' },
    E('thead', {}, E('tr', {}, cols.map(c =>
      E('th', { cls: c.n ? 'n' : '', 'data-nosort': c.nosort ? '' : null }, c.h)))),
    E('tbody', {}, rows.map(r => E('tr', {}, r.map((cell, i) =>
      E('td', { cls: [cols[i].n ? 'n' : '', cols[i].cls || '',
                      cell && cell.cls || ''].join(' ').trim(),
                title: cell && cell.title || null,
                'data-v': cell && cell.v !== undefined && cell.v !== null
                  ? cell.v : null },
        cell && cell.el ? cell.el : (cell && cell.t !== undefined ? cell.t : cell)))))));
  return E('div', { cls: 'tw' }, sortable(t));
}

/* ============================================================ 概览 */
function viewCockpit() {
  const o = P.overview;
  const wrap = E('div');
  wrap.append(E('h1', { cls: 't' }, '概览'));
  wrap.append(E('p', { cls: 'lede' },
    `${o.n_days} 天，每天 40 条，共 ${o.n_ideas} 条想法、${o.n_filled} 条真的成交了。`
    + '每天那一批自己成一个独立组合，所以下面每一格就是「那天的想法到今天为止怎么样」。'));

  const kpi = (k, v, cls, sub, gk) => E('div', {},
    E('div', { cls: 'k' }, k, gk ? hint(gk) : null),
    E('div', { cls: 'v ' + (cls || '') }, v),
    sub ? E('div', { cls: 's' }, sub) : null);

  wrap.append(sec('到目前为止', '样本还短，这些数字是口径正确的证据，不是结论',
    E('div', { cls: 'kpi' },
      kpi('跑赢 SPY 的天数', `${o.beat} / ${o.n_scored}`,
          o.beat * 2 >= o.n_scored ? 'up' : 'down',
          `平均超额 ${pc(o.avg_excess)}`, '超额'),
      kpi('每日组合平均收益', pc(o.avg_ret), sgn(o.avg_ret),
          `中位 ${pc(o.median_ret)}`),
      kpi('最好的一天', pc(o.best && o.best.v), sgn(o.best && o.best.v),
          o.best && o.best.d),
      kpi('最差的一天', pc(o.worst && o.worst.v), sgn(o.worst && o.worst.v),
          o.worst && o.worst.d),
      kpi('最大回撤', ddPct(o.worst_dd), ddCls(o.worst_dd), '所有组合里最深的一次'),
      kpi('想法等权收益', pc(o.idea_equal_weight), sgn(o.idea_equal_weight),
          `${o.idea_scored} 条可评分 · 胜率 ${pcu(o.idea_hit, 0)}`),
      kpi('跑赢基准比例', pcu(o.beat_bench_rate, 0), null, '逐条 vs SPY'),
      kpi('排序能力 ρ', n2(o.rank_rho, 3), sgn(o.rank_rho),
          '中心期望回报 vs 实际', '排序能力'),
      kpi('概率技能分', n2(o.skill, 3), sgn(o.skill),
          '> 0 优于瞎猜', '技能分'),
      kpi('语料', o.corpus_total.toLocaleString(),
          null, `条来源 · ${o.charts_total} 张图表`))));

  /* ---- calendar ---- */
  const cal = E('div', { cls: 'cal' });
  for (const x of o.days) {
    const pend = x.sessions === 0;
    cal.append(E('button', {
      cls: pend ? 'flat' : sgn(x.cum_ret),
      'aria-current': x.d === cur ? 'true' : 'false',
      title: `${x.d}\n${x.top_theme || ''}\n成交 ${x.filled}/${x.n_ideas}`,
      on: { click: () => { cur = x.d; setView('book'); } },
    },
      E('span', { cls: 'd' }, x.d.slice(5)),
      pend ? E('span', { cls: 'pend' }, '未成交')
           : E('span', { cls: 'r ' + sgn(x.cum_ret) }, pc(x.cum_ret)),
      E('span', { cls: 'x' }, pend ? `${x.n_ideas} 条已下单`
          : `vs SPY ${pc(x.excess)}`),
      E('span', { cls: 'x' }, `${x.sessions} 天 · ${x.filled} 成交`)));
  }
  wrap.append(sec('收益日历', '点任意一天跳到那天的组合；底部色条 红=涨 绿=跌', cal));

  /* ---- bars ---- */
  wrap.append(sec('每日组合收益', '同一口径下逐日对比，虚线是各自区间的 SPY',
    E('figure', { cls: 'panel' }, barChart(o.days),
      E('figcaption', {},
        '每根柱子是那一天 40 条想法组成的独立组合到今天为止的收益；'
        + '灰点是同期 SPY。柱子低于灰点＝跑输指数。'))));

  /* ---- per-day rationale ---- */
  wrap.append(sec('每天在赌什么', '当天的主线与执行口径，一行一天',
    table([{ h: '日期' }, { h: lbl('生成器') },
           { h: '当日冲击最高的主题', cls: 'wrap' },
           { h: lbl('TIS'), n: 1 }, { h: '可直接执行', n: 1 },
           { h: '收益', n: 1 }, { h: lbl('超额'), n: 1 },
           { h: '主线', cls: 'prose', nosort: 1 }],
      [...o.days].reverse().map(x => [
        { el: E('a', { href: '#book/' + x.d,
                       on: { click: e => { e.preventDefault(); cur = x.d; setView('book'); } } },
            x.d) },
        { el: E('span', { cls: 'tag' },
            x.generator.startsWith('rules') ? '规则'
              : x.generator.startsWith('seed') ? '原始 pack' : 'Claude') },
        x.top_theme || '—',
        { v: x.top_tis, t: n2(x.top_tis, 1) },
        { v: x.executable, t: x.executable },
        { v: x.cum_ret, cls: sgn(x.cum_ret), t: pc(x.cum_ret) },
        { v: x.excess, cls: sgn(x.excess), t: pc(x.excess) },
        { title: x.narrative || '',
          el: E('span', { cls: 'small' }, x.narrative || '—') }]))));

  wrap.append(skillBlocks());
  return wrap;
}

function barChart(days) {
  const W = 1160, H = 210, PL = 48, PR = 14, PT = 12, PB = 34;
  const iw = W - PL - PR, ih = H - PT - PB;
  const vals = days.flatMap(x => [x.cum_ret || 0, x.spy || 0]);
  let lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
  const pad = Math.max((hi - lo) * .14, .002); lo -= pad; hi += pad;
  const Y = v => PT + (1 - (v - lo) / (hi - lo)) * ih;
  const bw = Math.min(46, iw / Math.max(days.length, 1) * .58);
  const X = i => PL + (i + .5) * (iw / Math.max(days.length, 1));
  let g = '';
  for (let k = 0; k <= 4; k++) {
    const v = lo + (hi - lo) * k / 4, y = Y(v), z = Math.abs(v) < 1e-9;
    g += `<line x1="${PL}" y1="${y.toFixed(1)}" x2="${PL + iw}" y2="${y.toFixed(1)}"
      stroke="var(--${z ? 'rule-strong' : 'rule'})" stroke-width="${z ? 1 : .6}"/>
      <text x="${PL - 7}" y="${(y + 3.5).toFixed(1)}" text-anchor="end" font-size="10"
      font-family="var(--mono)" fill="var(--ink-3)">${(v * 100).toFixed(1)}%</text>`;
  }
  days.forEach((x, i) => {
    const v = x.cum_ret || 0, y0 = Y(0), y1 = Y(v);
    const col = v > 0 ? 'var(--up)' : v < 0 ? 'var(--down)' : 'var(--rule-strong)';
    g += `<rect x="${(X(i) - bw / 2).toFixed(1)}" y="${Math.min(y0, y1).toFixed(1)}"
      width="${bw.toFixed(1)}" height="${Math.max(Math.abs(y1 - y0), 1).toFixed(1)}"
      fill="${col}" rx="1.5"/>`;
    if (x.spy !== null && x.spy !== undefined)
      g += `<circle cx="${X(i).toFixed(1)}" cy="${Y(x.spy).toFixed(1)}" r="2.6"
        fill="var(--ink-3)"/>`;
    // with a month of days the axis cannot carry every label; thin it out
    const every = days.length > 22 ? 3 : days.length > 14 ? 2 : 1;
    if (i % every === 0 || i === days.length - 1)
      g += `<text x="${X(i).toFixed(1)}" y="${H - 19}" text-anchor="middle" font-size="9.5"
        font-family="var(--mono)" fill="var(--ink-3)">${x.d.slice(5)}</text>`;
    if (days.length <= 22)
      g += `<text x="${X(i).toFixed(1)}" y="${H - 7}" text-anchor="middle" font-size="9"
        font-family="var(--mono)" fill="${col}">${(v * 100).toFixed(1)}</text>`;
  });
  const box = document.createElement('div');
  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img"
    aria-label="每日组合收益">${g}</svg>`;
  return box.firstElementChild;
}


/* Cross-day skill and attribution: these read every outcome ever recorded, so they
   belong in the cockpit, not under one particular day's portfolio. */
function skillBlocks() {
  const a = P.attribution;
  const out = E('div');
  if (!a || !a.scored) return out;
  const rk = a.ranking, cal = a.calibration;
  out.append(sec('想法层面能力',
    `${a.scored}/${a.n} 条可评分（${a.unmarkable} 条无法盯市，${a.too_fresh} 条持有期不足 1 天）`
    + ' · 跨全部天，剔除仓位规模影响',
    E('div', { cls: 'grid g3' },
      E('div', { cls: 'panel' }, E('div', { cls: 'eyebrow' }, '等权收益'),
        E('div', { cls: 'big sm ' + sgn(a.equal_weight_ret) }, pc(a.equal_weight_ret)),
        row('中位', pc(a.median_ret)),
        row('胜率', pcu(a.hit_rate, 0)),
        row('超额均值', pc(a.excess_mean), sgn(a.excess_mean)),
        row('跑赢基准', pcu(a.beat_bench_rate, 0))),
      E('div', { cls: 'panel' },
        E('div', { cls: 'eyebrow' }, '排序能力 ρ', hint('排序能力')),
        E('p', { cls: 'small muted', style: 'margin:3px 0 9px' },
          '赔率排序是否预测了真实收益。ρ > 0 表示有效。'),
        ...Object.values(rk).map(v =>
          row(v.label, n2(v.rho_vs_realized, 3), sgn(v.rho_vs_realized)))),
      E('div', { cls: 'panel' },
        E('div', { cls: 'eyebrow' }, '概率校准', hint('Brier')),
        E('p', { cls: 'small muted', style: 'margin:3px 0 9px' },
          '技能分 = 1 − Brier ÷ 均匀先验；> 0 表示优于瞎猜。'),
        row('Brier 中心 / 保守',
          `${n2(cal.brier_central, 3)} / ${n2(cal.brier_conservative, 3)}`),
        E('div', { cls: 'row' }, E('span', {}, '技能分', hint('技能分')),
          E('span', { cls: sgn(cal.skill_central) },
            `${n2(cal.skill_central, 3)} / ${n2(cal.skill_conservative, 3)}`)),
        row('情景实现', Object.entries(cal.scenario_realised_pct || {})
          .map(([k, v]) => `${k} ${Math.round(v * 100)}%`).join(' · ') || '—')))));

  const labels = { grade: '绝对评级', grade_rel: '相对分位', horizon: '期限',
    theme: '宏观主题', vol_check: '情景幅度校验', filled: '是否成交' };
  out.append(sec('分档归因', '哪一类 idea 在赚钱 · 跨全部天',
    E('div', { cls: 'grid g2' }, Object.entries(labels).map(([k, lab]) => {
      const bk = (a.buckets || {})[k]; if (!bk) return null;
      return E('div', {},
        E('div', { cls: 'eyebrow', style: 'margin-bottom:6px' }, '按' + lab),
        table([{ h: lab, cls: 'wrap' }, { h: 'n', n: 1 }, { h: '均值', n: 1 },
               { h: '胜率', n: 1 }, { h: '超额', n: 1 }],
          Object.entries(bk).map(([kk, v]) => [
            kk === '1' ? '已成交' : kk === '0' ? '未成交' : kk,
            { v: v.n, t: v.n },
            { v: v.mean, cls: sgn(v.mean), t: pc(v.mean) },
            { v: v.hit, t: pcu(v.hit, 0) },
            { v: v.excess, cls: sgn(v.excess), t: pc(v.excess) }])));
    }))));
  return out;
}

/* ============================================================ 日报 */
function viewReport(dd) {
  const wrap = E('div');
  const r = dd.report, b = dd.batch;
  if (!r && !b) return E('div', { cls: 'empty' },
    cur + ' 没有日报。左右方向键换一天，或点侧边栏的日期。');

  wrap.append(E('h1', { cls: 't' }, `${cur} 战术宏观日报`));
  if (b && b.narrative) wrap.append(E('p', { cls: 'lede' }, b.narrative));

  /* ---------- 1. themes: one row per theme, opens to its own reasoning ---------- */
  if (r) {
    const cols = [
      { h: '宏观主题', cls: 'wrap' },
      { h: lbl('冲击潜力', 'TIS'), n: 1 },
      { h: '因子', nosort: 1 },
      { h: lbl('D'), n: 1 }, { h: lbl('A'), n: 1 },
      { h: lbl('B'), n: 1 }, { h: lbl('N'), n: 1 },
      { h: lbl('入价 M', 'M'), n: 1 }, { h: lbl('拥挤 C', 'C'), n: 1 },
      { h: '证据', n: 1 }, { h: '想法', n: 1 }, { h: '分级' }];
    const ideasByTheme = {};
    for (const i of (b ? b.ideas : [])) (ideasByTheme[i.theme_id] ||= []).push(i);
    const rows = r.themes.map(t => ({
      data: { t, ideas: ideasByTheme[t.id] || [] },
      cells: [
        { el: E('div', {}, E('strong', {}, t.label),
            t.origin === 'discovered' ? E('span', { cls: 'tag new',
              title: '语料发现的新主题，' + (t.registered_d || '') + ' 注册；'
                   + '不属于最初 16 条种子，只能从注册日起打分' }, '新') : null,
            t.cold_start ? E('span', { cls: 'tag',
              title: '注册未满 20 天，A 的强度项还没有自身历史可比' }, '冷启动') : null,
            E('div', { cls: 'small muted' }, t.stage + ' · ' + t.crowd)) },
        { v: t.tis, el: E('div', {}, E('strong', {}, n2(t.tis, 1)),
            E('div', { cls: 'meter' + (t.tis >= 60 ? ' hot' : '') },
              E('i', { style: `width:${Math.min(t.tis || 0, 100)}%` }))) },
        { el: fbars(t) },
        { v: t.d, t: n2(t.d, 0) }, { v: t.a, t: n2(t.a, 0) },
        { v: t.b, t: n2(t.b, 0) }, { v: t.n, t: n2(t.n, 0) },
        { v: t.m, t: n2(t.m, 0) }, { v: t.c, t: n2(t.c, 0) },
        { v: t.n_items, t: t.n_items },
        { v: (ideasByTheme[t.id] || []).length, t: (ideasByTheme[t.id] || []).length },
        { el: E('div', {}, E('span', { cls: 'pill t-' + t.tier }, t.tier),
            t.confidence !== 'ok' ? E('div', { cls: 'tag' }, '低置信') : null) }],
    }));
    wrap.append(sec(E('span', {}, '宏观主题', hint('TIS')),
      '点任意一行展开：预注册关键结果、四个因子各自怎么算出来的、该主题的原始证据与图表',
      expTable(cols, rows, themeDetail)));
  }

  /* ---------- 2. ideas: classification lives in the row, not in a tree ---------- */
  if (b) {
    const v = b.validation;
    wrap.append(sec(`${b.n} 条交易想法`,
      `${b.batch_id} · ${b.generator} · 校验${v.pass ? '通过' : '未通过'}`
      + `（${v.errors || 0} error / ${v.warnings || 0} warning）`
      + ' · 点任意一行展开：论点、风险、三情景、进出场与来源',
      v.failed && v.failed.length
        ? E('div', { cls: 'panel', style: 'padding:0;margin-bottom:12px' },
            v.failed.map(f => E('div', { cls: 'alert ' + (f.severity === 'error' ? 'action' : 'warn') },
              E('span', { cls: 'k' }, f.check),
              E('span', { cls: 'small' }, JSON.stringify(f.detail))))) : null,
      ideaTable(b)));
  }

  /* ---------- 3. corpus: a fold, not the main event ---------- */
  if (r) {
    const c = r.corpus;
    wrap.append(E('details', {},
      E('summary', {}, `当日语料底稿 · ${c.total.toLocaleString()} 条来源条目`),
      E('div', { cls: 'panel', style: 'margin-top:8px' },
        E('p', { cls: 'small muted', style: 'margin:0 0 10px' },
          `观察窗口 ${c.window[0]} → ${c.window[2]}。逐条证据挂在它支撑的那个主题下面`
          + '（展开主题行即可看），这里只放总量与分布。'),
        E('div', { cls: 'kvgrid' },
          ...c.window.map(x => E('div', {}, E('span', {}, x),
            E('span', {}, (c.by_day[x] || 0).toLocaleString() + ' 条'))),
          ...Object.entries(c.by_tier).map(([k, n]) => E('div', {},
            E('span', {}, k + ' 层'), E('span', {}, n.toLocaleString()))),
          ...Object.entries(c.by_line).map(([k, n]) => E('div', {},
            E('span', {}, k), E('span', {}, n.toLocaleString())))),
        reachBlock(c.reach),
        E('p', { cls: 'hintline' }, G['溯源']))));
  }
  return wrap;
}

/* ---- how much of the corpus the theme dictionary can even name ----
   Shown because it is the one number a fixed theme list cannot report about
   itself. Everything under 100% is corpus that no idea can cite, however well
   sourced it is. */
function reachBlock(x) {
  if (!x) return null;
  const box = E('div', { style: 'margin-top:12px' });
  box.append(E('div', { cls: 'small' },
    E('strong', {}, '字典可命名 '),
    E('span', { cls: x.pct >= 60 ? 'up' : (x.pct >= 45 ? '' : 'down') },
      x.pct + '%'),
    E('span', { cls: 'muted' },
      `　${x.matched.toLocaleString()} / ${x.items.toLocaleString()} 条`
      + `　按 ${x.registered} 个已注册主题　零匹配 ${x.unmatched.toLocaleString()}`),
    hint('字典覆盖')));
  if ((x.candidates || []).length) {
    box.append(E('p', { cls: 'small muted', style: 'margin:8px 0 4px' },
      '零匹配语料里出现的、字典还没有词的辩论（已过准入门槛，等人判定是否注册）：'),
      E('div', { cls: 'kvgrid' }, ...x.candidates.map(c => E('div', {},
        E('span', {}, c.terms.slice(0, 4).join(' · ')),
        E('span', { cls: 'muted' },
          `${c.n_docs}篇 / ${c.n_institutions}家 / ${c.n_days}天 / lift ${c.lift}`)))));
  }
  return box;
}

/* ---- what opens when you click a theme ---- */
function themeDetail(d) {
  const t = d.t, box = E('div');
  box.append(E('h4', {}, t.label),
    E('p', { cls: 'lead' }, E('strong', {}, '预注册关键结果　'), t.key_question));

  box.append(dsec('推理链 · 每个因子是怎么算出来的',
    E('div', { cls: 'chain' },
      ...['D', 'A', 'B', 'N', 'M', 'C'].map(k => {
        const x = (t.trail || {})[k] || {};
        return E('div', {},
          E('b', {}, k), E('span', { cls: 'sc' }, n2(x.score, 1)),
          E('span', { cls: 'wy' }, x.why || '—'));
      })),
    E('p', { cls: 'hintline' },
      `TIS = 0.15·D + 0.25·A + 0.25·B + 0.35·N = ${n2(t.tis, 1)}（${t.tier}）。`
      + `M 与 C 是独立维度，不进 TIS：当前 ${t.stage} / ${t.crowd}。`
      + `方向 ${t.direction} 取自语料净立场，不取自价格——否则 M 会自我验证。`)));

  if (t.charts && t.charts.length) box.append(dsec(
    `原始图表 · ${t.charts.length} 张`,
    E('div', { cls: 'chartgrid' }, t.charts.map(chartFig)),
    E('p', { cls: 'hintline' },
      '图与解读均来自智堡图表库；与本主题的关联是用冻结词典做的关键词匹配，'
      + '标「弱匹配」的只命中一个词，请自行折扣。')));

  if (t.evidence && t.evidence.length) box.append(dsec(
    `证据 · ${t.evidence.length} 条（共 ${t.n_items} 条，${t.n_sources} 个独立来源）`,
    E('div', { cls: 'src' }, t.evidence.map(e => srcRow({
      ...e, resolved: true,
      title: (e.stance > 0 ? '↑ ' : e.stance < 0 ? '↓ ' : '· ') + e.title,
    }))),
    E('p', { cls: 'hintline' },
      '↑ / ↓ 是该条对关键结果的立场；深度分 100 已实现利润或政策落地、75 订单收入、'
      + '50 市场价格、25 叙事。' + G['溯源'])));

  if (d.ideas.length) box.append(dsec(
    `由此产生的 ${d.ideas.length} 条想法`,
    E('div', { cls: 'kvgrid' }, d.ideas.map(i => E('div', {},
      E('span', {}, i.tool + '　' + (i.signal_label || i.asset || '')),
      E('span', {}, `${i.action}　${n2(i.pos_init, 2)}%　赔率 ${n2(i.or_c)}`))))));
  return box;
}

function fbars(t) {
  return E('div', { cls: 'fbars' }, ['d', 'a', 'b', 'n'].map(k => {
    const v = t[k];
    return E('i', { style: `height:${v == null ? 2 : Math.max(2, Math.round(v / 100 * 24))}px`,
                    title: `${k.toUpperCase()}=${n2(v, 1)}` });
  }));
}

function ideaTable(b) {
  const cols = [
    { h: '工具', cls: 'wrap' },
    { h: '宏观主题', cls: 'wrap' },
    { h: '传导主线', cls: 'wrap' },
    { h: '资产信号', cls: 'wrap' },
    { h: '动作' }, { h: lbl('评级') },
    { h: lbl('中心赔率'), n: 1 }, { h: lbl('保守赔率'), n: 1 },
    { h: lbl('hurdle'), n: 1 }, { h: '参考价', n: 1 },
    { h: '进场', n: 1 }, { h: '止损', n: 1 }, { h: '止盈', n: 1 },
    { h: '仓位', n: 1 }, { h: '已实现', n: 1 }, { h: lbl('超额'), n: 1 }];
  const rows = b.ideas.map(i => ({
    data: i,
    cells: [
      { el: E('div', {}, E('strong', { cls: 'mono' }, i.tool),
          E('div', { cls: 'small muted' }, i.desc || '')) },
      i.theme || '—',
      { el: E('span', { cls: 'small' }, i.transmission || '—') },
      { el: E('span', { cls: 'small' }, i.signal_label || i.asset || '—') },
      i.action,
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
      { v: i.realized, cls: sgn(i.realized), t: pc(i.realized) },
      { v: i.excess, cls: sgn(i.excess), t: pc(i.excess) }],
  }));
  return expTable(cols, rows, ideaDetail);
}

/* ---- what opens when you click a ticker ---- */
function ideaDetail(i) {
  const box = E('div');
  box.append(E('h4', {}, `${i.tool}　${i.desc || ''}`),
    E('p', { cls: 'lead' }, i.view));
  if (i.thesis) box.append(dsec('论点',
    E('p', { style: 'margin:0;font-size:12.5px;line-height:1.65;max-width:78ch' }, i.thesis)));
  if (i.risk) box.append(dsec('最可能怎么错',
    E('p', { style: 'margin:0;font-size:12.5px;line-height:1.65;max-width:78ch' }, i.risk)));

  const scen = (lab, o) => E('tr', {},
    E('td', {}, lab),
    ...o.p.map((x, k) => E('td', {}, `${x}%　${n2(o.r[k], 2)}%`)));
  box.append(dsec('三情景（概率　持有期回报）',
    E('div', { cls: 'scen' }, E('table', {},
      E('thead', {}, E('tr', {}, E('th', {}, ''), E('th', {}, '上涨'),
        E('th', {}, '基准'), E('th', {}, '下跌'))),
      E('tbody', {}, scen('中心', i.central), scen('保守', i.conservative)))),
    E('p', { cls: 'hintline' },
      `σ_${i.horizon === '6个月' ? '6m' : '1m'} = ${n2(i.sigma_h)}%，`
      + `幅度校验 ${i.vol_check}。` + G['幅度校验'])));

  box.append(dsec('执行口径', kvg([
    ['期限', `${i.horizon}（到期 ${i.horizon_end}）`],
    ['参考价', `${n2(i.ref_price)}（${i.ref_price_d}）`],
    ['进场', i.entry_lo ? `${n2(i.entry_lo)}–${n2(i.entry_hi)}`
      : i.entry_break ? `突破 ${n2(i.entry_break)}` : '首个可成交收盘价'],
    ['thesis stop', n2(i.stop_px)],
    ['止盈', n2(i.take_lo)],
    ['仓位', `初始 ${n2(i.pos_init, 2)}%　上限 ${n2(i.pos_max, 2)}%`],
    ['hurdle', n2(i.hurdle) + '%'],
    ['中心 / 保守赔率', `${n2(i.or_c)} / ${n2(i.or_k)}`],
    ['中心期望回报', n2(i.ev_c) + '%'],
    ['代码', i.code || '—'],
    i.gate ? ['建仓前置条件', i.gate] : null,
  ])));

  if (i.fills && i.fills.length) box.append(dsec('成交与盯市',
    E('div', { cls: 'kvgrid' }, i.fills.map(f => E('div', {},
      E('span', {}, (BOOKS[f.book_id] ? BOOKS[f.book_id].label : f.book_id)),
      E('span', {}, f.status === 'closed'
        ? `${f.opened_d} @${n2(f.avg_px)} → ${f.closed_d} @${n2(f.close_px)}（${f.exit_reason}）`
        : `${f.opened_d} @${n2(f.avg_px)}　在场`))))));
  else box.append(dsec('成交与盯市',
    E('p', { cls: 'small muted', style: 'margin:0' },
      i.realized === null || i.realized === undefined
        ? '尚未成交。进场条件未被触及，或这一天的收盘价还没到位。'
        : `未在组合中成交；已实现 ${pc(i.realized)} 是按收盘价的反事实读数。`)));

  const src = i.sources_resolved || [];
  box.append(dsec(`来源 · ${src.length} 条`,
    src.length ? E('div', { cls: 'src' }, src.map(srcRow))
      : E('p', { cls: 'small muted', style: 'margin:0' }, '无'),
    E('p', { cls: 'hintline' }, G['溯源'])));
  return box;
}

const lv = (k, v) => E('div', {}, E('span', {}, k), E('span', {}, v));

/* ============================================================ 组合 */
function viewBook(dd) {
  const wrap = E('div');
  const co = (P.cohorts || {})[cur];

  wrap.append(E('h1', { cls: 't' }, `${cur} 当日组合`, hint('当日组合')));
  wrap.append(E('p', { cls: 'lede' },
    '当天那 40 条自己成一个独立的盘：等权买入、持有到期限、单独计价。'
    + '每天一盘，所以哪天的想法好、哪天的差，可以直接比。'));

  if (!co) {
    wrap.append(E('div', { cls: 'empty' },
      cur + ' 没有批次，所以没有当日组合。'));
    return wrap;
  }

  /* ---- today's cohort ---- */
  const o = co.orders, pp = co.positions;
  const notYet = (o.filled || 0) === 0 && (o.pending || 0) > 0;

  wrap.append(E('div', { cls: 'grid g2', style: 'margin-bottom:20px' },
    E('div', { cls: 'panel' },
      E('div', { cls: 'eyebrow' }, `${co.batch_id} · ${co.generator}`),
      E('div', { cls: 'big ' + sgn(co.cum_ret) }, pc(co.cum_ret)),
      E('div', { cls: 'small muted', style: 'margin:3px 0 10px' },
        notYet
          ? `${co.n_ideas} 条已下单，等 ${cur} 美股收盘成交`
          : `${usd(co.equity)} · 持有 ${co.sessions} 个交易日`),
      row('SPY 同期', pc(co.spy)),
      row('超额 vs SPY', pc(co.excess), sgn(co.excess)),
      row('最大回撤', ddPct(co.max_dd), ddCls(co.max_dd)),
      row('成交 / 下单', `${o.filled || 0} / ${o.n || 0}`),
      row('在场 / 已平', `${pp.open_n || 0} / ${pp.closed_n || 0}`)),
    E('figure', { cls: 'panel' },
      E('div', { cls: 'eyebrow', style: 'margin-bottom:6px' }, '当日组合净值'),
      co.curve.length > 1
        ? lineChart({ [cur]: co.curve }, { h: 168, w: 560 })
        : E('div', { cls: 'note', style: 'margin:0' },
            '还没有成交，所以还没有净值曲线。批次已下单，等这一天的收盘价到位后自动成交。'))));

  /* ---- this day's holdings ---- */
  const held = P.positions.filter(x => x.book === co.book && x.kind !== 'order');
  if (held.length) {
    wrap.append(sec('当日持仓', `${held.length} 个仓位，全部来自 ${cur} 这一批想法`,
      posTable(held)));
  }
  const ords = P.positions.filter(x => x.book === co.book && x.kind === 'order'
    && x.status !== 'filled');
  if (ords.length) wrap.append(sec('未成交',
    '进场条件没被触及，或这一天的收盘价还没到位', orderTable(ords)));

  /* ---- every day, side by side ---- */
  const rows = Object.values(P.cohorts).sort((a, b) => a.as_of < b.as_of ? 1 : -1);
  wrap.append(sec('每天一览', 
    `${rows.length} 天，每天一个独立组合。这是「30 天后回来看」的那张表`,
    table([{ h: '日期' }, { h: lbl('生成器') }, { h: '条', n: 1 }, { h: '成交', n: 1 },
           { h: '持有(交易日)', n: 1 }, { h: '收益', n: 1 }, { h: 'SPY 同期', n: 1 },
           { h: lbl('超额'), n: 1 }, { h: '最大回撤', n: 1 }, { h: '在场', n: 1 }],
      rows.map(c => [
        { el: E('a', { href: '#book/' + c.as_of,
                       on: { click: e => { e.preventDefault(); go(c.as_of); } },
                       style: c.as_of === cur ? 'font-weight:700' : '' }, c.as_of) },
        { el: E('span', { cls: 'tag' },
            c.generator.startsWith('rules') ? '规则' :
            c.generator.startsWith('seed') ? '原始 pack' : 'Claude') },
        { v: c.n_ideas, t: c.n_ideas },
        { v: c.orders.filled || 0, t: c.orders.filled || 0 },
        { v: c.sessions, t: c.sessions },
        { v: c.cum_ret, cls: sgn(c.cum_ret), t: pc(c.cum_ret) },
        { v: c.spy, t: pc(c.spy) },
        { v: c.excess, cls: sgn(c.excess), t: pc(c.excess) },
        { v: c.max_dd, cls: ddCls(c.max_dd), t: ddPct(c.max_dd) },
        { v: c.n_open, t: c.n_open }]))));

  const scored = rows.filter(c => c.sessions > 0 && c.excess !== null);
  if (scored.length >= 2) {
    const beat = scored.filter(c => c.excess > 0).length;
    const avg = scored.reduce((a, c) => a + c.excess, 0) / scored.length;
    wrap.append(E('div', { cls: 'note', style: 'margin:-16px 0 30px' },
      E('b', {}, '到目前　'),
      `${scored.length} 天里有 ${beat} 天跑赢 SPY，平均超额 ${pc(avg)}。`
      + (scored.length < 20 ? '样本还太短，不能下结论。' : '')));
  }

  /* ---- commingled books, demoted ---- */
  wrap.append(E('details', {},
    E('summary', {}, '混合账户口径（所有天的想法放进同一个盘）'),
    E('p', { cls: 'small muted', style: 'margin:6px 0 12px' },
      '这两个盘把每天的想法累加进同一个账户，回答的是「这套流程对账户净值做了什么」，'
      + '不是「某一天的想法好不好」。两者差额是仓位管理的贡献。'),
    E('div', { cls: 'grid g2' }, Object.entries(dd.books).map(([id, v]) => {
      const meta = BOOKS[id];
      return E('div', { cls: 'panel' },
        E('div', { cls: 'eyebrow' }, meta.label),
        E('div', { cls: 'big sm ' + sgn(v.cum_ret) }, pc(v.cum_ret)),
        E('div', { cls: 'small muted', style: 'margin:2px 0 9px' }, meta.desc),
        row('SPY 同期', pc(v.spy)),
        row('超额 vs SPY', pc(v.excess), sgn(v.excess)),
        row('最大回撤', ddPct(v.max_dd), ddCls(v.max_dd)),
        row('净敞口 / 在场', `${pcu(v.gross, 0)} / ${v.n_open}`));
    })),
    E('figure', { cls: 'panel', style: 'margin-top:12px' },
      E('div', { cls: 'legend' },
        ...[['守纪律组合', 'var(--accent)'], ['无脑全买组合', 'var(--up)'],
            ['SPY', 'var(--ink-3)'], ['ACWI', 'var(--down)']].map(([n, c]) =>
          E('span', {}, E('b', { style: `background:${c}` }), n))),
      lineChart(Object.fromEntries(Object.entries(P.curves).map(
        ([k, v]) => [k, v.filter(x => x[0] <= cur).map(x => [x[0], x[1]])]))))));

  if (dd.alerts.length) wrap.append(sec('当日告警',
    '止损临近 / 主题前提被价格否定 / 拥挤度跳升 / 临近到期',
    E('div', { cls: 'panel', style: 'padding:0' },
      dd.alerts.map(x => E('div', { cls: 'alert ' + x.level },
        E('span', { cls: 'k' }, x.kind), E('span', {}, x.message))))));

  return wrap;
}

function orderTable(list) {
  return table([{ h: '工具' }, { h: '主题' }, { h: '期限' }, { h: '动作' },
                { h: '类型' }, { h: '区间 / 触发', n: 1 }, { h: '参考价', n: 1 },
                { h: '计划金额', n: 1 }, { h: '状态' }],
    list.map(o => [
      { el: E('strong', { cls: 'mono' }, o.tool) }, o.theme, o.horizon, o.action,
      { el: E('span', { cls: 'tag' }, o.order_kind) },
      { v: o.band_hi ?? o.trigger, cls: 'small',
        t: o.band_lo ? `${n2(o.band_lo)}–${n2(o.band_hi)}`
          : o.trigger ? `>${n2(o.trigger)}` : '收盘' },
      { v: o.ref_price, t: n2(o.ref_price) },
      { v: o.notional, t: usd(o.notional) },
      { el: E('span', { cls: 'pill ' + (o.status === 'pending' ? 'lv-info' : 'lv-warn') },
          o.status === 'pending' ? '待成交' : '已失效') }]));
}

function posTable(list) {
  const cols = [{ h: '工具', cls: 'wrap' }, { h: '代码' }, { h: '主题', cls: 'wrap' },
    { h: '期限' }, { h: lbl('评级') }, { h: '建仓日' },
    { h: '成本价', n: 1 }, { h: '现价', n: 1 }, { h: '投入', n: 1 },
    { h: '盈亏', n: 1 }, { h: '盈亏 $', n: 1 },
    { h: '止损', n: 1 }, { h: '止盈', n: 1 }, { h: '到期日' }, { h: '状态' }];
  const rows = list.map(p2 => ({
    data: p2,
    cells: [
      { el: E('div', {}, E('strong', { cls: 'mono' }, p2.tool),
          E('div', { cls: 'small muted' }, p2.desc || '')) },
      { el: E('span', { cls: 'tag' }, p2.code) },
      p2.theme, p2.horizon,
      { el: E('span', { cls: 'pill g-' + p2.grade }, p2.grade) },
      p2.opened_d,
      { v: p2.avg_px, t: n2(p2.avg_px) }, { v: p2.px, t: n2(p2.px) },
      { v: p2.cost, t: usd(p2.cost) },
      { v: p2.pnl_pct, cls: sgn(p2.pnl_pct), t: pc(p2.pnl_pct) },
      { v: p2.pnl_usd, cls: sgn(p2.pnl_usd), t: usd(p2.pnl_usd) },
      { v: p2.stop_px, cls: 'small', t: n2(p2.stop_px) },
      { v: p2.take_px, cls: 'small', t: n2(p2.take_px) },
      p2.horizon_end,
      { el: p2.status === 'open' ? E('span', { cls: 'pill lv-info' }, '在场')
          : E('span', { cls: 'pill lv-warn' },
              ({ stop: '止损', take: '止盈', horizon: '到期' })[p2.exit_reason]
                || p2.exit_reason) }],
  }));
  // clicking a holding shows the idea that produced it, sourced and all
  return expTable(cols, rows, posDetail);
}

function posDetail(p2) {
  const b = (day[p2.as_of] || {}).batch;
  const idea = b ? b.ideas.find(x => x.uid === p2.idea_uid) : null;
  if (idea) {
    const box = ideaDetail(idea);
    box.insertBefore(dsec('这个仓位', kvg([
      ['所属组合', (BOOKS[p2.book] && BOOKS[p2.book].label) || p2.book],
      ['建仓', `${p2.opened_d} @${n2(p2.avg_px)}`],
      ['当前', p2.status === 'closed'
        ? `${p2.closed_d} @${n2(p2.close_px || p2.px)}（${p2.exit_reason}）`
        : `@${n2(p2.px)}　在场`],
      ['盈亏', `${pc(p2.pnl_pct)}　${usd(p2.pnl_usd)}`],
      ['区间高 / 低', `${n2(p2.peak_px)} / ${n2(p2.trough_px)}`],
      ['距止损', p2.stop_px && p2.px
        ? pc(p2.px / p2.stop_px - 1) : '—'],
    ])), box.children[2] || null);
    return box;
  }
  return E('div', {}, E('p', { cls: 'small muted', style: 'margin:0' },
    `这个仓位来自 ${p2.as_of} 的批次，但该批次的想法数据不在当前 payload 中。`));
}

const row = (k, v, cls) => E('div', { cls: 'row' },
  E('span', {}, k), E('span', { cls: cls || '' }, v));
const sec = (h, sub, ...body) => E('section', { cls: 'sec' },
  E('h2', {}, h), sub ? E('p', { cls: 'sub' }, sub) : null, ...body);
function glossary() {
  const dl = E('dl', {});
  for (const [k, v] of Object.entries(G)) {
    dl.append(E('dt', {}, k));
    dl.append(E('dd', {}, v));
  }
  const dlg = E('dialog', { id: 'gloss' },
    E('header', {}, E('h3', {}, '名词表'),
      E('button', { cls: 'x', 'aria-label': '关闭',
                    on: { click: () => dlg.close() } }, '×')),
    E('div', { cls: 'body' }, dl));
  return dlg;
}

/* ============================================================ shell */
function render() {
  const dd = day[cur];
  $('#view').replaceChildren(
    view === 'cockpit' ? viewCockpit()
      : view === 'report' ? viewReport(dd) : viewBook(dd));
  document.querySelectorAll('.nav button').forEach(b =>
    b.setAttribute('aria-current', b.dataset.view === view ? 'page' : 'false'));
  document.querySelectorAll('.datelist button').forEach(b =>
    b.setAttribute('aria-current', b.dataset.d === cur ? 'true' : 'false'));
  // The cockpit aggregates every day at once; a date picker there would imply a
  // selection that does not affect anything on screen.
  const crossDay = view === 'cockpit';
  $('#datenav').hidden = crossDay;
  $('#crossnote').hidden = !crossDay;
  $('#curd').textContent = cur;
  $('#reld').textContent = relLabel(cur);
  $('#prev').disabled = dIdx() <= 0;
  $('#next').disabled = dIdx() >= D.length - 1;
  // the sidebar shows *this day's* cohort — a cumulative blend across days would
  // answer a question nobody asked
  const co = (P.cohorts || {})[cur];
  const ss = $('#sidestats');
  ss.replaceChildren();
  if (co) {
    ss.append(E('div', { cls: 'sidestat' }, E('span', {}, '当日收益'),
      E('b', { cls: sgn(co.cum_ret) }, pc(co.cum_ret))));
    ss.append(E('div', { cls: 'sidestat' }, E('span', {}, 'SPY 同期'),
      E('b', {}, pc(co.spy))));
    ss.append(E('div', { cls: 'sidestat' }, E('span', {}, '超额'),
      E('b', { cls: sgn(co.excess) }, pc(co.excess))));
    ss.append(E('div', { cls: 'sidestat' }, E('span', {}, '持有'),
      E('b', {}, co.sessions + ' 天')));
  } else {
    ss.append(E('div', { cls: 'sidestat' },
      E('span', { cls: 'muted' }, '这天没有批次'), E('b', {}, '—')));
  }
  $('#topkv').textContent = dd.batch
    ? `${dd.batch.n} 条想法 · ${dd.batch.generator.startsWith('rules') ? '规则生成' : dd.batch.generator.startsWith('seed') ? '原始 pack' : 'Claude 生成'}`
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
  else if (e.key === '0') setView('cockpit');
  else if (e.key === '1') setView('report');
  else if (e.key === '2') setView('book');
});

(function boot() {
  const m = /^#(cockpit|report|book)\/(\d{4}-\d{2}-\d{2})$/.exec(location.hash);
  if (m && day[m[2]]) { view = m[1]; cur = m[2]; }
  else if (location.hash === '#cockpit') view = 'cockpit';
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
  document.body.append(glossary());
  $('#openGloss').addEventListener('click', () => $('#gloss').showModal());
  render();
})();
"""


def build(con, out: Path | None = None, artifact: bool = False,
          embed_images: bool | None = None) -> Path:
    """Render the dashboard.

    `embed_images` decides whether Wisburg chart images are hotlinked into the
    page. Locally that is just viewing them; on the public GitHub Pages build it
    would republish a subscription service's charts at an indexable URL, so the
    public build shows the title, the platform's interpretation and a link
    instead. Defaults to `config.EMBED_IMAGES_LOCAL`.
    """
    pl = payload.build(con)
    pl["meta"]["embed_images"] = (config.EMBED_IMAGES_LOCAL if embed_images is None
                                  else bool(embed_images))
    pl["glossary"] = GLOSSARY
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
      <button data-view="cockpit" aria-current="page">
        <span class="ic">▤</span>概览<span class="k">0</span></button>
      <button data-view="report" aria-current="false">
        <span class="ic">◈</span>日报<span class="k">1</span></button>
      <button data-view="book" aria-current="false">
        <span class="ic">◱</span>组合<span class="k">2</span></button>
      <button class="glossbtn" id="openGloss" type="button">
        <span class="ic">?</span>名词表</button>
    </nav>

    <div class="side-sec">
      <h4>当日组合</h4>
      <div id="sidestats"></div>
    </div>

    <div class="side-sec">
      <h4>日期</h4>
      <div class="datelist" id="datelist"></div>
    </div>

    <div class="side-foot">
      <span class="kbd">←</span> <span class="kbd">→</span> 换日期 ·
      <span class="kbd">0</span> <span class="kbd">1</span> <span class="kbd">2</span> 换视图<br>
      不确定的名词旁边都有 <span class="kbd">i</span><br><br>
      方法论 v{m['methodology']} · 词典 v{m['lexicon']}<br>
      行情至 US {m['px_through'].get('US', '')} / HK {m['px_through'].get('HK', '')}<br>
      生成 {m['generated_at'][:16].replace('T', ' ')} HKT<br><br>
      模拟盘，非投资建议。
    </div>
  </aside>

  <main>
    <div class="topbar">
      <div class="datenav" id="datenav">
        <button id="prev" title="前一天（←）" aria-label="前一天">‹</button>
        <span class="cur" id="curd">—</span>
        <button id="next" title="后一天（→）" aria-label="后一天">›</button>
        <span class="rel" id="reld"></span>
      </div>
      <div class="crossnote" id="crossnote" hidden>跨全部日期汇总</div>
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
