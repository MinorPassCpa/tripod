"""
자가진단 — 이 리포지토리가 원본과 동일하게 작동하는지 증명한다.

  python3 src/selftest.py

통과하면 계정을 옮기든, 몇 년이 지나든 같은 시스템이라는 뜻이다.
네트워크를 쓰지 않으므로 어디서든 돌릴 수 있다.
CI 에서 매일 돌면서, 코드나 데이터가 조용히 어긋나는 순간 즉시 실패한다.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import fetch  # noqa: E402
from engine import build, STATE_LABEL, target_text  # noqa: E402
from backtest import run, stats  # noqa: E402

FAILS = []
PASSES = []


def check(label, got, want, tol=0.0, unit=""):
    ok = (got == want) if tol == 0 else (abs(got - want) <= tol)
    line = f"{label}: {got}{unit}" + ("" if ok else f"  ≠ 기대값 {want}{unit}")
    (PASSES if ok else FAILS).append(line)


# ── 1. 데이터 무결성 ────────────────────────────────────────────
# 1989-12-01 ~ 2026-09-04 구간은 절대 바뀌지 않는다. 그 구간만 지문을 찍는다.
FROZEN_END = "20260904"
FROZEN_ROWS = 9257
FROZEN_BYTES = 210449
FROZEN_CHECKSUM = 1376847300   # h = h*31 + ord(c), 32비트


def frozen_fingerprint():
    lines = []
    with open(os.path.join(ROOT, "data", "market.csv")) as f:
        next(f)
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            date, ndx, vix, _irx = (line.split(",") + ["", "", ""])[:4]
            if date > FROZEN_END:
                break
            lines.append(f"{date},{ndx},{vix}")
    text = "\n".join(lines)
    h = 0
    for ch in text:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return len(lines), len(text), h


n, b, h = frozen_fingerprint()
check("과거구간 행수", n, FROZEN_ROWS)
check("과거구간 바이트", b, FROZEN_BYTES)
check("과거구간 체크섬", h, FROZEN_CHECKSUM)

# ── 2. 파서 — 실제 응답 샘플로 검증 ─────────────────────────────
YAHOO_SAMPLE = json.dumps({"chart": {"result": [{
    "meta": {"symbol": "^NDX"},
    "timestamp": [1788183000, 1788269400, 1788355800, 1788442200, 1788528600],
    "indicators": {"quote": [{"close": [29456.970703125, 29077.220703125,
                                        29143.330078125, 29482.3203125,
                                        29544.150390625]}]}}], "error": None}})
FRED_SAMPLE = ("observation_date,NASDAQ100\n2026-09-01,29456.97\n"
               "2026-09-02,.\n2026-09-03,29482.32\n")
CBOE_SAMPLE = "DATE,OPEN,HIGH,LOW,CLOSE\n2026-09-03,15.25,15.44,14.23,14.32\n"

y = fetch.parse_yahoo(YAHOO_SAMPLE)
check("yahoo 파서 · 행수", len(y), 5)
check("yahoo 파서 · 최종 종가", round(y[max(y)], 2), 29544.15)
check("yahoo 파서 · 최종 날짜", max(y), "20260904")

fr = fetch.parse_fred(FRED_SAMPLE)
check("fred 파서 · 결측(.) 제외 후 행수", len(fr), 2)
check("fred 파서 · 최종 종가", fr["20260903"], 29482.32)

import csv as _csv           # noqa: E402
import io as _io             # noqa: E402
cb = {r["DATE"].replace("-", ""): float(r["CLOSE"])
      for r in _csv.DictReader(_io.StringIO(CBOE_SAMPLE))}
check("cboe 파서 · VIX 종가", cb["20260903"], 14.32)

# 두 소스가 어긋나면 반드시 예외가 나야 한다
try:
    fetch.agree([("a", {"20260904": 100.0}), ("b", {"20260904": 105.0})],
                "20260904", "테스트")
    check("소스 불일치 감지", "예외 없음", "예외 발생")
except fetch.FetchError:
    check("소스 불일치 감지", "예외 발생", "예외 발생")

# ── 3. 35년 백테스트 골든값 ─────────────────────────────────────
cfg, rows = build()
cfg["backtest"]["end"] = FROZEN_END          # 데이터가 늘어나도 골든값은 고정
curve, trades, bench = run(rows, cfg, cfg["backtest"]["start"], FROZEN_END)
s = stats(curve)
check("백테스트 CAGR", round(s["cagr"] * 100, 1), 33.2, 0.05, "%")
check("백테스트 MDD", round(s["mdd"] * 100, 1), -61.9, 0.05, "%")
check("백테스트 울서지수", round(s["ulcer"], 1), 29.9, 0.05)
check("백테스트 소르티노", round(s["sortino"], 2), 1.17, 0.005)
check("백테스트 매매횟수", len(trades), 283)
for k, cagr, mdd in (("QQQ", 15.3, -82.8), ("QLD", 19.6, -98.8), ("TQQQ", 16.3, -100.0)):
    bs = stats(bench[k])
    check(f"{k} CAGR", round(bs["cagr"] * 100, 1), cagr, 0.05, "%")
    check(f"{k} MDD", round(bs["mdd"] * 100, 1), mdd, 0.05, "%")

# ── 4. 규칙 판정 — 알려진 날짜의 상태 ───────────────────────────
idx = {r["date"]: r for r in rows}
for d, want in (("20260904", "UP_RISKON"),     # 촬영 직후 — 3배 유지
                ("20260728", "UP_RISKOFF"),    # 낙폭 -9.45%, 문턱을 아슬하게 돌파
                ("20260330", "UP_RISKOFF"),    # 영상이 언급한 '올해 3월' 감속
                ("20250407", "DOWN_FEAR"),     # 2025-04 관세 충격 → 현금
                ("20200316", "DOWN_FEAR"),     # 코로나 폭락 → 현금
                ("20081010", "DOWN_FEAR"),     # 금융위기 → 현금
                ("20001215", "DOWN_FEAR"),     # 닷컴 붕괴 → 현금
                ("20020102", "DOWN_FEAR"),     # 닷컴 바닥 구간
                ("19981001", "UP_RISKOFF")):   # LTCM — 상승장이지만 VIX 37 → 1.5배
    got = idx[d]["state"] if d in idx else "해당 날짜 없음"
    check(f"규칙 판정 {d}", got, want)

# ── 결과 ────────────────────────────────────────────────────────
print(f"통과 {len(PASSES)}건")
for line in PASSES:
    print("  ✓ " + line)
if FAILS:
    print(f"\n실패 {len(FAILS)}건")
    for line in FAILS:
        print("  ✗ " + line)
    print("\n원본과 동작이 달라졌습니다. 데이터나 코드가 바뀌지 않았는지 확인하세요.")
    sys.exit(1)
cur = [r for r in rows if r["state"]][-1]
print(f"\n전부 통과 — 원본과 동일한 시스템입니다.")
print(f"현재 상태: {cur['date']} · {STATE_LABEL[cur['state']]} · {target_text(cur['target'])}")
