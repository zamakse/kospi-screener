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

# 조건은 config.json 에서 바꿉니다 (아래는 파일이 없을 때의 기본값)
CONFIG = {
    "연속_음봉_최소": 6,              # 최근 봉부터 연속 음봉이 이 개수 이상
    "누적등락률_상한_퍼센트": 0,      # 연속 구간 누적 등락률이 이 값 미만인 종목만 (null 이면 미적용)
    "거래대금_하한_원": 1_000_000_000,  # 미만이면 '저유동성'으로 분리
    "구간_봉수": 10,                  # 누적 음봉: 최근 N개 봉 중
    "구간_음봉_최소": None,           # 음봉이 K개 이상 (null 이면 누적 음봉 검색 끔)
}
_cfg = Path(__file__).parent / "config.json"
if _cfg.exists():
    CONFIG.update(json.loads(_cfg.read_text(encoding="utf-8")))
THRESHOLD = int(CONFIG["연속_음봉_최소"])
MAX_DROP = CONFIG["누적등락률_상한_퍼센트"]
MIN_TRADING_VALUE = int(CONFIG["거래대금_하한_원"])
WINDOW = int(CONFIG["구간_봉수"])
WINDOW_MIN = CONFIG["구간_음봉_최소"]
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


def bearish_in_window(bars, window):
    """최근 window개 봉 중 음봉 수(연속 여부 무관)와 그 구간 누적 등락률."""
    w = [b for b in bars if b["volume"] > 0][-window:]
    cnt = sum(1 for b in w if b["close"] < b["open"])
    prev = [b for b in bars if b["volume"] > 0][-window - 1:-window]
    base = prev[0]["close"] if prev else (w[0]["open"] if w else None)
    drop = round((w[-1]["close"] / base - 1) * 100, 2) if w and base else None
    return cnt, len(w), drop


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

    daily, weekly, daily_win, weekly_win, failed, stale = [], [], [], [], [], 0
    filtered_by_drop = 0
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
        for dest, dest_win, series in ((daily, daily_win, bars), (weekly, weekly_win, to_weekly(bars))):
            cnt, wlen, wdrop = bearish_in_window(series, WINDOW)
            n = bearish_streak(series)
            if n >= THRESHOLD:
                d = describe(series, n)
                if MAX_DROP is not None and d["drop_pct"] is not None and d["drop_pct"] >= MAX_DROP:
                    filtered_by_drop += 1
                else:
                    dest.append({**common, **d, "bearish_in_window": cnt})
            if WINDOW_MIN is not None and wlen == WINDOW and cnt >= int(WINDOW_MIN):
                dest_win.append({**common, "bearish_in_window": cnt, "window": WINDOW, "streak": n,
                                 "drop_pct": wdrop, "close": series[-1]["close"]})

    key = lambda r: (r["low_liquidity"], -r["streak"], -r["trading_value"])
    daily.sort(key=key)
    weekly.sort(key=key)
    wkey = lambda r: (r["low_liquidity"], -r["bearish_in_window"], -r["trading_value"])
    daily_win.sort(key=wkey)
    weekly_win.sort(key=wkey)

    if len(failed) > len(targets) * 0.2:
        raise RuntimeError(f"시세 수집 실패 {len(failed)}/{len(targets)}건 — 결과를 신뢰할 수 없어 중단")

    result = {
        "generated_at": now.isoformat(timespec="seconds"),
        "market_date": fmt_date(market_date),
        "is_today": fmt_date(market_date) == now.strftime("%Y-%m-%d"),
        "threshold": THRESHOLD,
        "config": CONFIG,
        "excluded_by_drop_filter": filtered_by_drop,
        "basis": "네이버 금융 일봉 (KRX 정규장 종가, 수정주가). NXT 시간외 미반영. 주봉은 일봉을 주 단위로 묶어 계산하며 마지막 봉은 진행 중인 이번 주",
        "min_trading_value": MIN_TRADING_VALUE,
        "listing_source": source,
        "universe_count": len(targets),
        "skipped_suspended": stale,
        "failed_count": len(failed),
        "daily": daily,
        "weekly": weekly,
        "daily_window": daily_win,
        "weekly_window": weekly_win,
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
             f"생성 {r['generated_at']} · 대상 {r['universe_count']}종목, 수집 실패 {r['failed_count']}, 거래정지 제외 {r['skipped_suspended']}, 누적등락률 조건으로 제외 {r['excluded_by_drop_filter']}", ""]
    for title, key in (("일봉", "daily"), ("주봉", "weekly")):
        lines += [f"## {title} ({len(r[key])}종목)", "", "| 종목 | 코드 | 연속 | 시작일 | 누적등락 | 종가 | 거래대금(억) | 비고 |", "|---|---|---|---|---|---|---|---|"]
        for x in r[key]:
            lines.append(f"| {x['name']} | {x['code']} | {x['streak']}{'+' if x['capped'] else ''} | {fmt_date(x['start_date'])} | {x['drop_pct']}% | {x['close']:,.0f} | {x['trading_value'] / 1e8:,.0f} | {'저유동성' if x['low_liquidity'] else ''} |")
        lines.append("")
    if r["config"]["구간_음봉_최소"] is not None:
        for title, key in (("일봉", "daily_window"), ("주봉", "weekly_window")):
            lines += [f"## 누적 음봉 {title}: 최근 {r['config']['구간_봉수']}봉 중 {r['config']['구간_음봉_최소']}개 이상 ({len(r[key])}종목)", "",
                      "| 종목 | 코드 | 음봉 수 | 현재 연속 | 구간 등락 | 종가 | 거래대금(억) | 비고 |", "|---|---|---|---|---|---|---|---|"]
            for x in r[key]:
                lines.append(f"| {x['name']} | {x['code']} | {x['bearish_in_window']}/{x['window']} | {x['streak']} | {x['drop_pct']}% | {x['close']:,.0f} | {x['trading_value'] / 1e8:,.0f} | {'저유동성' if x['low_liquidity'] else ''} |")
            lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
