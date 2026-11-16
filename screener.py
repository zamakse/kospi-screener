"""코스피 연속 음봉 스크리너 (일봉·주봉)

- 음봉: 종가 < 시가 (전일 대비 하락 여부와 무관)
- 조건: 가장 최근 봉부터 거꾸로 세어 음봉이 THRESHOLD개 이상 연속
- 기준 시세: 네이버 금융 일봉(KRX 정규장 종가, 수정주가). NXT 시간외 체결은 반영하지 않음
"""
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

THRESHOLD = 6                      # 연속 음봉 최소 개수
MIN_TRADING_VALUE = 1_000_000_000  # 이 금액(원) 미만 거래대금은 '저유동성'으로 분리
DAYS = 90                          # 종목당 가져올 일봉 수 (주봉 약 18개 분량)
WORKERS = 8
EXCLUDE_NAME = re.compile(r"스팩|리츠|인프라|맥쿼리|ETN|ETF")

KST = timezone(timedelta(hours=9))
S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"


def get(url, **kw):
    for i in range(3):
        try:
            r = S.get(url, timeout=15, **kw)
            r.raise_for_status()
            return r
        except Exception:
            if i == 2:
                raise
            time.sleep(1.5 * (i + 1))


# ---------- 종목 목록 ----------
def listing_mobile_api():
    out, page = {}, 1
    while True:
        j = get(f"https://m.stock.naver.com/api/stocks/marketValue/KOSPI?page={page}&pageSize=100").json()
        stocks = j.get("stocks") or []
        if not stocks:
            break
        for s in stocks:
            if s.get("stockEndType", "stock") != "stock":
                continue
            cap = str(s.get("marketValue", "")).replace(",", "")
            out[s["itemCode"]] = {
                "name": s["stockName"],
                # 네이버 표기 단위: 억원
                "market_cap": int(cap) * 100_000_000 if cap.isdigit() else None,
            }
        if page * 100 >= int(j.get("totalCount", 0)):
            break
        page += 1
    return out


def listing_html():
    out = {}
    for page in range(1, 80):
        html = get(f"https://finance.naver.com/sise/sise_market_sum.naver?sosok=0&page={page}").content.decode("euc-kr", "replace")
        found = re.findall(r'/item/main\.naver\?code=(\w{6})"[^>]*class="tltle"[^>]*>([^<]+)<', html)
        new = [c for c, _ in found if c not in out]
        if not new:
            break
        for c, n in found:
            out[c] = {"name": n.strip(), "market_cap": None}
    return out


def get_listing():
    errors = []
    for fn in (listing_mobile_api, listing_html):
        try:
            d = fn()
            if len(d) > 300:
                return d, fn.__name__
            errors.append(f"{fn.__name__}: {len(d)}건뿐")
        except Exception as e:
            errors.append(f"{fn.__name__}: {e!r}")
    raise RuntimeError("종목 목록 수집 실패: " + " / ".join(errors))


def is_target(code, name):
    """보통주만: 6자리 코드, 끝자리 0(우선주 제외), 스팩·리츠 등 제외"""
    return bool(re.fullmatch(r"[0-9A-Z]{5}0", code)) and not EXCLUDE_NAME.search(name)


# ---------- 시세 ----------
def parse_bars(xml):
    bars = []
    for d, o, h, l, c, v in re.findall(r'data="(\d{8})\|([\d.]+)\|([\d.]+)\|([\d.]+)\|([\d.]+)\|(\d+)"', xml):
        bars.append({"date": d, "open": float(o), "high": float(h), "low": float(l), "close": float(c), "volume": int(v)})
    return bars


def fetch_daily(code):
    url = f"https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=day&count={DAYS}&requestType=0"
    return parse_bars(get(url).text)


def to_weekly(bars):
    weeks = {}
    for b in bars:
        if b["volume"] == 0:
            continue
        y, w, _ = datetime.strptime(b["date"], "%Y%m%d").isocalendar()
        k = (y, w)
        if k not in weeks:
            weeks[k] = dict(b)
        else:
            wk = weeks[k]
            wk["high"] = max(wk["high"], b["high"])
            wk["low"] = min(wk["low"], b["low"])
            wk["close"] = b["close"]
            wk["volume"] += b["volume"]
            wk["date"] = b["date"]          # 주의 마지막 거래일
    return [weeks[k] for k in sorted(weeks)]


def bearish_streak(bars):
    """최근 봉부터 거꾸로 연속 음봉 수. 거래량 0(거래정지)·도지·양봉에서 끊김."""
    n = 0
    for b in reversed(bars):
        if b["volume"] > 0 and b["close"] < b["open"]:
            n += 1
        else:
            break
    return n


def describe(bars, n):
    first = bars[-n]
    base = bars[-n - 1]["close"] if len(bars) > n else first["open"]
    last = bars[-1]["close"]
    return {
        "streak": n,
        "capped": n == len(bars),            # 가져온 봉 전부가 음봉(실제는 더 길 수 있음)
        "start_date": first["date"],
        "drop_pct": round((last / base - 1) * 100, 2) if base else None,
        "close": last,
    }


def fmt_date(d):
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def main():
    now = datetime.now(KST)
    listing, source = get_listing()
    targets = {c: v for c, v in listing.items() if is_target(c, v["name"])}
    market_date = fetch_daily("005930")[-1]["date"]   # 삼성전자 최신 봉 = 시장 최신 거래일

    def work(code):
        try:
            return code, fetch_daily(code), None
        except Exception as e:
            return code, None, repr(e)

    with ThreadPoolExecutor(WORKERS) as ex:
        rows = list(ex.map(work, targets))

    daily, weekly, failed, stale = [], [], [], 0
    for code, bars, err in rows:
        if err or not bars:
            failed.append(code)
            continue
        if bars[-1]["date"] != market_date or bars[-1]["volume"] == 0:
            stale += 1                       # 거래정지 등
            continue
        info = targets[code]
        tv = int(bars[-1]["close"] * bars[-1]["volume"])
        common = {"code": code, "name": info["name"], "trading_value": tv,
                  "market_cap": info["market_cap"], "low_liquidity": tv < MIN_TRADING_VALUE}
        for dest, series in ((daily, bars), (weekly, to_weekly(bars))):
            n = bearish_streak(series)
            if n >= THRESHOLD:
                dest.append({**common, **describe(series, n)})

    key = lambda r: (r["low_liquidity"], -r["streak"], -r["trading_value"])
    daily.sort(key=key)
    weekly.sort(key=key)

    if len(failed) > len(targets) * 0.2:
        raise RuntimeError(f"시세 수집 실패 {len(failed)}/{len(targets)}건 — 결과를 신뢰할 수 없어 중단")

    result = {
        "generated_at": now.isoformat(timespec="seconds"),
        "market_date": fmt_date(market_date),
        "is_today": fmt_date(market_date) == now.strftime("%Y-%m-%d"),
        "threshold": THRESHOLD,
        "basis": "네이버 금융 일봉 (KRX 정규장 종가, 수정주가). NXT 시간외 미반영. 주봉은 일봉을 주 단위로 묶어 계산하며 마지막 봉은 진행 중인 이번 주",
        "min_trading_value": MIN_TRADING_VALUE,
        "listing_source": source,
        "universe_count": len(targets),
        "skipped_suspended": stale,
        "failed_count": len(failed),
        "daily": daily,
        "weekly": weekly,
    }

    out = Path(__file__).parent / "results"
    out.mkdir(exist_ok=True)
    text = json.dumps(result, ensure_ascii=False, indent=1)
    (out / "latest.json").write_text(text, encoding="utf-8")
    (out / f"{result['market_date']}.json").write_text(text, encoding="utf-8")
    (out / "latest.md").write_text(to_markdown(result), encoding="utf-8")
    print(f"{result['market_date']} 대상 {len(targets)} / 일봉 {len(daily)} / 주봉 {len(weekly)} / 실패 {len(failed)}")


def to_markdown(r):
    lines = [f"# 코스피 연속 음봉 {r['threshold']}개 이상 — {r['market_date']}", "",
             f"대상 {r['universe_count']}종목, 수집 실패 {r['failed_count']}, 거래정지 제외 {r['skipped_suspended']}", ""]
    for title, key in (("일봉", "daily"), ("주봉", "weekly")):
        lines += [f"## {title} ({len(r[key])}종목)", "", "| 종목 | 코드 | 연속 | 시작일 | 누적등락 | 종가 | 거래대금(억) | 비고 |", "|---|---|---|---|---|---|---|---|"]
        for x in r[key]:
            lines.append(f"| {x['name']} | {x['code']} | {x['streak']}{'+' if x['capped'] else ''} | {fmt_date(x['start_date'])} | {x['drop_pct']}% | {x['close']:,.0f} | {x['trading_value'] / 1e8:,.0f} | {'저유동성' if x['low_liquidity'] else ''} |")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
