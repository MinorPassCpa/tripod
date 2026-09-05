"""
시세 수집기 — 외부 패키지 없이 표준 라이브러리만 사용.

소스 우선순위
  나스닥100(^NDX) : Yahoo chart API (query1 → query2)  →  FRED NASDAQ100 CSV
  VIX(^VIX)       : Yahoo chart API (query1 → query2)  →  GitHub datasets/finance-vix (CBOE 원본)

  stooq 는 의도적으로 뺐다 — 다운로드 엔드포인트가 HTTP 200 과 함께 "Access denied" 를
  돌려주는 것을 실제로 확인했다(2026-09). 조용히 틀릴 소스는 안 쓰는 편이 낫다.

세 가지 안전장치
  1. 두 개 이상 소스가 잡히면 서로 대조한다(0.5% 이상 벌어지면 실패 처리).
  2. 하루 변동폭 15% 초과, 과거 날짜, 비정상 VIX는 거부한다.
  3. 어떤 이유로든 확신이 없으면 데이터를 쓰지 않고 0이 아닌 종료코드로 죽는다.
     조용히 틀린 값을 남기는 것보다 워크플로가 실패해서 메일이 오는 편이 낫다.

사용법:  python3 src/fetch.py            # data/market.csv 갱신
         python3 src/fetch.py --dry-run  # 저장하지 않고 무엇을 받았는지만 출력
"""
import csv
import io
import json
import os
import sys
import urllib.request
import urllib.parse
import urllib.error
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKET = os.path.join(ROOT, "data", "market.csv")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TIMEOUT = 30

MAX_DAILY_MOVE = 0.15      # 지수 하루 변동 한계
MAX_SOURCE_DIFF = 0.005    # 소스 간 허용 오차 0.5%
VIX_RANGE = (1.0, 200.0)


class FetchError(RuntimeError):
    pass


def _get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


# ── 소스별 구현 ────────────────────────────────────────────────

def yahoo(symbol, rng="3mo", host="query1"):
    """{'YYYYMMDD': close} 최근 구간."""
    url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol)}?range={rng}&interval=1d")
    return parse_yahoo(_get(url), symbol)


def parse_yahoo(text, symbol="?"):
    j = json.loads(text)
    res = (j.get("chart") or {}).get("result")
    if not res:
        raise FetchError(f"yahoo {symbol}: 빈 응답 {str(j)[:200]}")
    ts = res[0]["timestamp"]
    cl = res[0]["indicators"]["quote"][0]["close"]
    out = {}
    for t, c in zip(ts, cl):
        if c is None:
            continue
        d = datetime.datetime.utcfromtimestamp(t).strftime("%Y%m%d")
        out[d] = float(c)
    if not out:
        raise FetchError(f"yahoo {symbol}: 종가 없음")
    return out


def fred(series="NASDAQ100", days=120):
    """세인트루이스 연준 FRED 의 일간 지수 시계열."""
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)
    url = ("https://fred.stlouisfed.org/graph/fredgraph.csv"
           f"?id={series}&cosd={start}&coed={end}")
    return parse_fred(_get(url, {"Accept-Encoding": "identity"}), series)


def parse_fred(text, series="?"):
    lines = text.strip().splitlines()
    if not lines or "," not in lines[0]:
        raise FetchError(f"fred {series}: 예상과 다른 응답 {text[:120]}")
    out = {}
    for r in csv.DictReader(io.StringIO(text)):
        vals = list(r.values())
        d, v = vals[0], vals[1]
        if v in (".", "", None):
            continue
        out[d.replace("-", "")] = float(v)
    if not out:
        raise FetchError(f"fred {series}: 종가 없음")
    return out


def cboe_vix_mirror():
    url = "https://raw.githubusercontent.com/datasets/finance-vix/main/data/vix-daily.csv"
    out = {}
    for r in csv.DictReader(io.StringIO(_get(url))):
        if r.get("CLOSE"):
            out[r["DATE"].replace("-", "")] = float(r["CLOSE"])
    if not out:
        raise FetchError("cboe mirror: 종가 없음")
    return out


def gather(sources):
    """[(이름, 호출가능객체)] → ([(이름, dict)], [(이름, 사유)])"""
    ok, bad = [], []
    for name, fn in sources:
        try:
            ok.append((name, fn()))
        except Exception as e:
            bad.append((name, f"{type(e).__name__}: {e}"[:180]))
    return ok, bad


def agree(series, date, label):
    """여러 소스의 같은 날짜 값을 대조하고 대표값을 돌려준다."""
    vals = [(n, s[date]) for n, s in series if date in s]
    if not vals:
        return None, []
    base = vals[0][1]
    for n, v in vals[1:]:
        if base and abs(v / base - 1) > MAX_SOURCE_DIFF:
            raise FetchError(
                f"{label} 소스 불일치: " + ", ".join(f"{n2}={v2}" for n2, v2 in vals))
    return base, vals


# ── 메인 ───────────────────────────────────────────────────────

def main():
    dry = "--dry-run" in sys.argv
    rows = list(csv.DictReader(open(MARKET)))
    last = rows[-1]

    ndx_ok, ndx_bad = gather([("yahoo-q1", lambda: yahoo("^NDX", host="query1")),
                              ("yahoo-q2", lambda: yahoo("^NDX", host="query2")),
                              ("fred", lambda: fred("NASDAQ100"))])
    vix_ok, vix_bad = gather([("yahoo-q1", lambda: yahoo("^VIX", host="query1")),
                              ("yahoo-q2", lambda: yahoo("^VIX", host="query2")),
                              ("cboe-mirror", cboe_vix_mirror)])

    if not ndx_ok:
        raise FetchError("나스닥100 소스를 하나도 받지 못했습니다 — " + "; ".join(
            f"{n}: {r}" for n, r in ndx_bad))
    if not vix_ok:
        raise FetchError("VIX 소스를 하나도 받지 못했습니다 — " + "; ".join(
            f"{n}: {r}" for n, r in vix_bad))

    # 저장된 마지막 날짜 이후의 새 거래일만 취한다
    new_dates = sorted(d for d in set().union(*[set(s) for _, s in ndx_ok])
                       if d > last["date"])

    report = {"stored_last": last["date"], "ndx_sources": [n for n, _ in ndx_ok],
              "vix_sources": [n for n, _ in vix_ok], "failed": ndx_bad + vix_bad,
              "appended": [], "skipped": []}

    prev_close = float(last["ndx"])
    for d in new_dates:
        ndx, ndx_vals = agree(ndx_ok, d, f"NDX {d}")
        if ndx is None:
            continue
        # VIX 는 하루 늦게 올라오는 소스가 있으므로 없으면 빈칸으로 두고 다음 실행에서 채운다
        try:
            vix, _ = agree(vix_ok, d, f"VIX {d}")
        except FetchError as e:
            report["skipped"].append(f"{d}: {e}")
            vix = None

        move = ndx / prev_close - 1
        if abs(move) > MAX_DAILY_MOVE:
            raise FetchError(f"{d} 지수 변동 {move*100:.1f}% — 데이터 오류로 판단하고 중단합니다 "
                             f"(직전 {prev_close}, 수신 {ndx})")
        if vix is not None and not (VIX_RANGE[0] < vix < VIX_RANGE[1]):
            raise FetchError(f"{d} VIX {vix} 범위 이탈")

        rows.append({"date": d, "ndx": f"{ndx:g}",
                     "vix": f"{vix:g}" if vix is not None else "", "irx": ""})
        report["appended"].append({"date": d, "ndx": ndx, "vix": vix,
                                   "sources": [n for n, _ in ndx_vals]})
        prev_close = ndx

    # 비어 있던 과거 VIX 채우기 / 임시값 교정 (CBOE 원본 우선)
    auth = dict(vix_ok[-1][1]) if vix_ok[-1][0] == "cboe-mirror" else {}
    for _, s in vix_ok:
        for d, v in s.items():
            auth.setdefault(d, v)
    filled = corrected = 0
    for r in rows:
        a = auth.get(r["date"])
        if a is None:
            continue
        a = f"{a:g}"
        if not r.get("vix"):
            r["vix"] = a
            filled += 1
        elif r["vix"] != a and abs(float(r["vix"]) / float(a) - 1) > 1e-9:
            r["vix"] = a
            corrected += 1
    report["vix_filled"] = filled
    report["vix_corrected"] = corrected

    # ── 최근 값 재검증 ────────────────────────────────────────
    # FRED 는 미국 동부시간 밤늦게 갱신돼 실행 시점에는 하루 뒤처져 있다.
    # 그래서 다음 실행 때 최근 며칠치를 독립 소스와 다시 대조한다.
    # 규칙상 체결이 T+1 이므로, 이 재검증은 그 값으로 실제 매매하기 전에 끝난다.
    recheck = []
    stored = {r["date"]: r for r in rows[-6:]}
    for d, r in stored.items():
        for name, s in ndx_ok:
            if d not in s:
                continue
            diff = s[d] / float(r["ndx"]) - 1
            if abs(diff) > MAX_SOURCE_DIFF:
                raise FetchError(
                    f"{d} 저장된 지수 {r['ndx']} 와 {name} 의 {s[d]} 가 "
                    f"{diff*100:+.2f}% 어긋납니다 — 사람이 확인해야 합니다")
            if abs(diff) > 1e-9:
                recheck.append({"date": d, "source": name,
                                "stored": float(r["ndx"]), "source_value": s[d],
                                "diff_pct": round(diff * 100, 4)})
    report["recheck_notes"] = recheck

    if not dry:
        tmp = MARKET + ".tmp"
        with open(tmp, "w") as f:
            f.write("date,ndx,vix,irx\n")
            for r in rows:
                f.write(f"{r['date']},{r['ndx']},{r.get('vix','')},{r.get('irx','')}\n")
        os.replace(tmp, MARKET)
    report["new_last"] = rows[-1]["date"]
    report["rows"] = len(rows)
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"::error::시세 수집 실패 — {e}", file=sys.stderr)
        sys.exit(1)
