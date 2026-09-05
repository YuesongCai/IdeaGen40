"""Probe the generation model's knowledge cutoff against prices we already hold.

Why this matters more than any additional period: `docs/BACKFILL_REAL_BACKTEST.md`
disclaims backfilled periods on the grounds that "模型权重本身见过 2026 年 7-8 月
之后的世界". If that is false for this model, five of six periods stop being
disclaimed evidence and become evidence. If it is true, no amount of backfill
will ever count and only live periods can.

Self-reported cutoffs are unreliable, so this asks for month-end levels of three
liquid ETFs across a ladder of months and scores the answer against `prices`.
Recall error is small inside the training window and collapses into drift after it.
"""
from __future__ import annotations
import json, os, re, sys, urllib.request
from pathlib import Path

ENV = Path.home() / ".ideagen.env"
KEY = None
BASE = "https://ark.ap-southeast.bytepluses.com/api/v3"
MODEL = "deepseek-v4-pro-260425"
for line in ENV.read_text(encoding="utf-8").splitlines():
    m = re.search(r"ARK_API_KEY=(\S+)", line)
    if m and not KEY:
        KEY = m.group(1)
    m2 = re.search(r"IDEAGEN_INFERENCE_BASE_URL=(\S+)", line)
    if m2:
        BASE = m2.group(1)
if not KEY:
    raise SystemExit("no ARK_API_KEY")

os.environ["NO_PROXY"] = "bytepluses.com,volces.com,ark.ap-southeast.bytepluses.com"
os.environ["no_proxy"] = os.environ["NO_PROXY"]

MONTHS = ["2025-09","2025-10","2025-11","2025-12","2026-01","2026-02",
          "2026-03","2026-04","2026-05","2026-06","2026-07","2026-08"]

prompt = (
    "You are being audited for the extent of your training data, not tested on skill.\n"
    "For each month below, give your best recollection of the LAST TRADING DAY closing "
    "price of these three US ETFs: SPY, GLD, TLT.\n"
    "Rules: answer from memory only. If a month is past your knowledge cutoff, still give "
    "a number but mark known=false for it. Do not hedge in prose.\n"
    "Also state your knowledge cutoff date explicitly.\n"
    "Return STRICT JSON only:\n"
    '{"cutoff":"YYYY-MM","months":[{"m":"2025-09","known":true,"SPY":000.0,"GLD":000.0,"TLT":000.0}, ...]}\n'
    "Months: " + ", ".join(MONTHS)
)

req = urllib.request.Request(
    BASE.rstrip("/") + "/chat/completions",
    data=json.dumps({"model": MODEL,
                     "messages": [{"role": "user", "content": prompt}],
                     "temperature": 0.0, "max_tokens": 1600}).encode(),
    headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=180) as r:
    out = json.load(r)
txt = out["choices"][0]["message"]["content"]
print("=== RAW ===")
print(txt[:4000])
Path(str(OUT / "cutoff_raw.json")).write_text(txt, encoding="utf-8")
