"""抓 9 個公開行情指標的每日收盤價，存成 data/<代號>.csv。

只用 Python 內建功能（公司網路擋 pip）。執行方式：
    python -I scripts/fetch_quotes.py --where 家裡
--where 是「在哪個網路跑的」，會記進 data/fetch_log.csv，方便比較公司／家裡／GitHub Actions。

每個 CSV 欄位：date,close,source
抓不到的指標不會覆蓋舊檔，只在 fetch_log 記一筆 FAIL。

歷史資料：CSV 不存在或加 --full 時，從 Yahoo 抓「全部歷史」（每天一筆）；平常只抓近 2 年。
台股兩個指標：較早的歷史用 Yahoo 補，近 13 個月以證交所官方數字為準（同一天會覆蓋 Yahoo）。
另外抓國發會景氣燈號（每月）→ data/ndc_signal.csv（month,score,light,leading,coincident），每次整份覆蓋。
"""
import argparse
import csv
import datetime as dt
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
UA = {"User-Agent": "Mozilla/5.0 (market_weather personal dashboard)"}
TWSE_MONTHS = 13   # 證交所一次給一個月；抓全部歷史時抓近 13 個月
TWSE_MONTHS_DAILY = 2   # 平常每天只抓近 2 個月（請求少，比較不會被擋）
TPE = dt.timezone(dt.timedelta(hours=8))   # 一律用台灣時間（GitHub Actions 的機器是 UTC）
TWSE_PAUSE = 3     # 證交所請求太快會被暫時封鎖，每次間隔 3 秒

FULL_THRESHOLD = 1000   # CSV 少於這麼多天，就自動抓全部歷史

# 台股指標的歷史補資料來源（Yahoo 代號）
BACKFILL = {"taiex": "^TWII", "2330": "2330.TW"}

# 代號 → (顯示名稱, 來源函式名稱, 來源參數)
INDICATORS = {
    "taiex":  ("台股加權指數", "twse_index", None),
    "2330":   ("台積電 2330", "twse_stock", "2330"),
    "tsm":    ("台積電 ADR (TSM)", "yahoo", "TSM"),
    "spx":    ("標普 500", "yahoo", "^GSPC"),
    "qqq":    ("QQQ", "yahoo", "QQQ"),
    "smh":    ("SMH 半導體 ETF", "yahoo", "SMH"),
    "sox":    ("費城半導體指數 (SOX)", "yahoo", "^SOX"),   # 2026-11-23 加：觀察名單
    "nvda":   ("輝達 NVDA", "yahoo", "NVDA"),              # 2026-11-23 加：觀察名單
    "vix":    ("VIX 恐慌指數", "yahoo", "^VIX"),
    "us10y":  ("美國 10 年期公債殖利率", "yahoo", "^TNX"),
    "usdtwd": ("美元兌台幣", "yahoo", "TWD=X"),
    "gold":   ("黃金（紐約期貨，美元／盎司）", "yahoo", "GC=F"),   # 2026-11-22 加：只看走勢，和其他資產比較
    "btc":    ("比特幣（美元）", "yahoo", "BTC-USD"),           # 週末也有交易
}

# 本益比（證交所 BWIBBU，每日、2005-09 起）：代號 → (顯示名稱, 股票代號)。存 data/<代號>.csv，欄位 date,close,source（close＝本益比）
PE_SERIES = {
    "2330_pe": ("台積電本益比", "2330"),
}
PE_START = "200509"   # 證交所本益比資料從 94 年 9 月開始


def get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def roc_to_date(s):
    """民國日期 '115/11/02' → '2026-11-02'"""
    y, m, d = s.strip().split("/")
    return f"{int(y) + 1911:04d}-{int(m):02d}-{int(d):02d}"


def num(s):
    return float(s.replace(",", ""))


def twse_date(s):
    """證交所日期，新舊格式都吃：'115/11/02'、'115年11月20日'、'099年01月29日' → '2026-11-20'"""
    y, m, d = re.findall(r"\d+", s)[:3]
    return f"{int(y) + 1911:04d}-{int(m):02d}-{int(d):02d}"


def months_since(ym):
    """從 ym（'200509'）到這個月共幾個月"""
    today = dt.datetime.now(TPE).date()
    return (today.year - int(ym[:4])) * 12 + today.month - int(ym[4:]) + 1


def fetch_twse_pe(code, months):
    """證交所『個股日本益比、殖利率及股價淨值比』：每日本益比。欄位順序新舊不同，用欄位名稱找。
    本益比是 '-' 或 0（例如虧損）的日子跳過。"""
    rows, skipped = {}, []
    pause = TWSE_PAUSE + (2 if months > TWSE_MONTHS else 0)   # 補長歷史時放慢一點
    for i, ym in enumerate(m for m in month_starts(months) if m[:6] >= PE_START):
        if i:
            time.sleep(pause)
        js = None
        for wait in (0, 15, 60):   # 證交所偶爾回傳空白或網頁（請求太密）→ 等一下再試，最多 3 次
            time.sleep(wait)
            try:
                js = json.loads(get(f"https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU?date={ym}&stockNo={code}&response=json"))
                break
            except (ValueError, OSError):
                js = None
        if js is None:
            skipped.append(ym[:6])
            continue
        if js.get("stat") != "OK":
            continue
        idx = js["fields"].index("本益比")
        for r in js["data"]:
            try:
                v = num(r[idx])
            except ValueError:
                continue
            if v > 0:
                rows[twse_date(r[0])] = v
    if skipped:
        print(f"     ⚠ 這些月份重試 3 次仍抓不到，先跳過（下次 --full 會再補）：{', '.join(skipped)}")
    return rows, "TWSE BWIBBU"


def month_starts(n):
    today = dt.datetime.now(TPE).date()
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append(f"{y:04d}{m:02d}01")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def fetch_twse_index(_, months=TWSE_MONTHS):
    """證交所『市場成交資訊』：每日加權指數收盤。"""
    rows = {}
    for i, ym in enumerate(month_starts(months)):
        if i:
            time.sleep(TWSE_PAUSE)
        js = json.loads(get(f"https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?date={ym}&response=json"))
        if js.get("stat") != "OK":
            continue
        idx = js["fields"].index("發行量加權股價指數")
        for r in js["data"]:
            rows[roc_to_date(r[0])] = num(r[idx])
    return rows, "TWSE FMTQIK"


def fetch_twse_stock(code, months=TWSE_MONTHS):
    """證交所『個股日成交資訊』：每日收盤價。"""
    rows = {}
    for i, ym in enumerate(month_starts(months)):
        if i:
            time.sleep(TWSE_PAUSE)
        js = json.loads(get(f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date={ym}&stockNo={code}&response=json"))
        if js.get("stat") != "OK":
            continue
        idx = js["fields"].index("收盤價")
        for r in js["data"]:
            try:
                rows[roc_to_date(r[0])] = num(r[idx])
            except ValueError:   # 停牌等情況收盤價是 '--'
                pass
    return rows, "TWSE STOCK_DAY"


def fetch_yahoo(symbol, full=False):
    """Yahoo Finance chart API（非正式服務，格式可能改）。
    full=True 抓全部歷史。注意：range=max 會被 Yahoo 自動改成每月一筆，所以要用 period1／period2。"""
    q = "period1=0&period2=9999999999" if full else "range=2y"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.request.quote(symbol)}?{q}&interval=1d"
    res = json.loads(get(url, timeout=90))["chart"]["result"][0]
    meta = res["meta"]
    offset = meta.get("gmtoffset", 0)
    closes = res["indicators"]["quote"][0]["close"]
    rows = {}
    for ts, c in zip(res["timestamp"], closes):
        if c is None:
            continue
        d = dt.datetime.fromtimestamp(ts + offset, dt.timezone.utc).date().isoformat()
        rows[d] = round(c, 4)
    # 盤中還沒收盤的那一根不要（不能把盤中價當收盤）
    period = meta.get("currentTradingPeriod", {}).get("regular", {})
    if period and time.time() < period.get("end", 0) and rows:
        today_local = dt.datetime.fromtimestamp(period["start"] + offset, dt.timezone.utc).date().isoformat()
        rows.pop(today_local, None)
    return rows, f"Yahoo {symbol}"


SOURCES = {"twse_index": fetch_twse_index, "twse_stock": fetch_twse_stock, "yahoo": fetch_yahoo}


# 國發會「景氣指標及燈號」（政府資料開放平台 dataset 6099，每月一筆、1984 年起有燈號）。
# 國發會自己的網站 index.ndc.gov.tw 擋程式（403），所以用開放平台的 ZIP。下載網址每次從平台查，查不到用這個。
NDC_DATASET = "https://data.gov.tw/api/v2/rest/dataset/6099"
NDC_ZIP_FALLBACK = ("https://ws.ndc.gov.tw/Download.ashx?u=LzAwMS9hZG1pbmlzdHJhdG9yLzEwL3JlbGZpbGUvNTc4MS82MzkyL2VhMjM1YmQ5LWQwNTItNGE2OS1hYmZjLWQ1Yzc4NWQzZDBlMi56aXA%3d"
                    "&n=5pmv5rCj5oyH5qiZ5Y%2bK54eI6JmfLnppcA%3d%3d&icon=.zip")
NDC_FILE = "景氣指標與燈號.csv"
NDC_LIGHTS = {"藍", "黃藍", "綠", "黃紅", "紅"}


def fetch_ndc():
    """回傳 [(月份 'YYYY-MM', 分數或 '', 燈號或 '', 領先指標不含趨勢, 同時指標不含趨勢)]。
    國發會每月會回頭修正舊資料，所以每次整份覆蓋。"""
    import io, zipfile
    url = NDC_ZIP_FALLBACK
    try:
        for d in json.loads(get(NDC_DATASET))["result"].get("distribution", []):
            if d.get("resourceDownloadUrl", "").lower().endswith("zip") or d.get("resourceFormat") == "ZIP":
                url = d["resourceDownloadUrl"]
                break
    except Exception:
        pass
    z = zipfile.ZipFile(io.BytesIO(get(url, timeout=90)))
    name = next(n for n in z.namelist() if n.split("/")[-1] == NDC_FILE)   # 不能用 endswith：會抓到 schema-景氣指標與燈號.csv
    out = []
    for r in csv.DictReader(io.StringIO(z.read(name).decode("utf-8-sig"))):
        ym = r["Date"].strip()
        if not re.fullmatch(r"\d{6}", ym):
            continue
        score, light = r["景氣對策信號綜合分數"].strip(), r["景氣對策信號"].strip()
        if not score.isdigit() or light not in NDC_LIGHTS:
            score, light = "", ""
        lead, coin = r["領先指標不含趨勢指數"].strip(), r["同時指標不含趨勢指數"].strip()
        out.append((f"{ym[:4]}-{ym[4:]}", score, light,
                    f"{float(lead):.2f}" if lead else "", f"{float(coin):.2f}" if coin else ""))
    if not any(s for _, s, *_ in out):
        raise ValueError("ZIP 裡找不到燈號資料")
    return out


def save_ndc(rows):
    with (DATA_DIR / "ndc_signal.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["month", "score", "light", "leading", "coincident"])
        w.writerows(rows)


# 標普 500 本益比（multpl.com，第三方，每月一筆、1871 年起）。用過去 12 個月盈餘算；
# 最近幾個月的盈餘還沒公布完，是用預估盈餘算的「估計值」（表上標 †），之後會被修正，所以每次整份覆蓋。
MULTPL_URL = "https://www.multpl.com/s-p-500-pe-ratio/table/by-month"


def fetch_multpl():
    """回傳 [(日期, 本益比, 是否估計)]，日期舊到新。最新一列是當月到目前為止（例如 11/20）。"""
    page = get(MULTPL_URL).decode("utf-8")
    out = []
    for d, cell in re.findall(r"<tr[^>]*>\s*<td>([^<]+)</td>\s*<td>(.*?)</td>", page, re.S):
        v = re.search(r"\d+(?:\.\d+)?\s*$", re.sub(r"<[^>]+>", " ", cell).strip())
        if not v:
            continue
        date = dt.datetime.strptime(d.strip(), "%b %d, %Y").date().isoformat()
        out.append((date, float(v.group()), "Estimate" in cell))
    if len(out) < 1000:
        raise ValueError(f"multpl 表格只有 {len(out)} 列，格式可能改了")
    return sorted(out)


def save_multpl(rows):
    with (DATA_DIR / "spx_pe.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")   # 統一 LF，和其他 CSV 一樣
        w.writerow(["date", "close", "source"])
        for d, v, est in rows:
            w.writerow([d, f"{v:.2f}", "multpl 估計" if est else "multpl"])


def save_csv(code, rows, source):
    path = DATA_DIR / f"{code}.csv"
    old = {}
    if path.exists():   # 保留舊資料，新抓到的同一天覆蓋
        with path.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                old[r["date"]] = (r["close"], r["source"])
    for d, c in rows.items():
        old[d] = (c, source)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")   # 統一 LF：Linux（GitHub）和 Windows 寫出來一樣
        w.writerow(["date", "close", "source"])
        for d in sorted(old):
            w.writerow([d, *old[d]])
    return len(old)


def count_rows(code):
    path = DATA_DIR / f"{code}.csv"
    if not path.exists():
        return 0
    with path.open(encoding="utf-8") as f:
        return sum(1 for _ in f) - 1


def log(where, code, source, status, last_date, rows, note=""):
    path = DATA_DIR / "fetch_log.csv"
    new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")   # 統一 LF：Linux（GitHub）和 Windows 寫出來一樣
        if new:
            w.writerow(["run_at", "where", "indicator", "source", "status", "last_date", "rows", "note"])
        w.writerow([dt.datetime.now(TPE).strftime("%Y-%m-%d %H:%M:%S"), where, code, source, status, last_date, rows, note])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--where", default="未標示", help="在哪個網路跑：公司／家裡／GitHub Actions")
    ap.add_argument("--only", nargs="*", help="只抓這幾個代號（測試用）")
    ap.add_argument("--full", action="store_true", help="重抓全部歷史（第一次會自動做）")
    args = ap.parse_args()
    DATA_DIR.mkdir(exist_ok=True)

    ok = 0
    for code, (name, kind, param) in INDICATORS.items():
        if args.only and code not in args.only:
            continue
        full = args.full or count_rows(code) < FULL_THRESHOLD
        try:
            if full and code in BACKFILL:   # 台股：先用 Yahoo 補早期歷史，下面證交所再覆蓋近期
                hist, hsrc = fetch_yahoo(BACKFILL[code], full=True)
                save_csv(code, hist, hsrc)
                print(f"     {name:<18} 歷史補到 {min(hist)}（{hsrc}）")
            if kind == "yahoo":
                rows, source = fetch_yahoo(param, full=full)
            else:
                try:
                    rows, source = SOURCES[kind](param, TWSE_MONTHS if full else TWSE_MONTHS_DAILY)
                except Exception as e:   # 證交所被擋（例如 GitHub 的美國機器）→ 改用 Yahoo 備案
                    print(f"     {name:<18} 證交所失敗（{e!r}"[:120] + "），改用 Yahoo 備案")
                    log(args.where, code, kind, "FAIL", "", 0, repr(e)[:200])
                    rows, source = fetch_yahoo(BACKFILL[code])
            if not rows:
                raise ValueError("回傳沒有資料")
            total = save_csv(code, rows, source)
            last = max(rows)
            log(args.where, code, source, "OK", last, total)
            print(f"OK   {name:<18} 最新 {last}  {rows[last]}  （共 {total} 天）")
            ok += 1
        except Exception as e:
            log(args.where, code, kind, "FAIL", "", 0, repr(e)[:200])
            print(f"FAIL {name:<18} {e!r}"[:200])

    for code, (name, stock) in PE_SERIES.items():
        if args.only and code not in args.only:
            continue
        full = args.full or count_rows(code) < FULL_THRESHOLD
        try:
            if full:
                print(f"     {name:<18} 補歷史（{PE_START[:4]} 年起每月一次、間隔 {TWSE_PAUSE} 秒，"
                      f"約 {months_since(PE_START) * (TWSE_PAUSE + 3) // 60} 分鐘）…")
            rows, source = fetch_twse_pe(stock, months_since(PE_START) if full else TWSE_MONTHS_DAILY)
            if not rows:
                raise ValueError("回傳沒有資料")
            total = save_csv(code, rows, source)
            last = max(rows)
            log(args.where, code, source, "OK", last, total)
            print(f"OK   {name:<18} 最新 {last}  {rows[last]}  （共 {total} 天）")
            ok += 1
        except Exception as e:
            log(args.where, code, "twse_pe", "FAIL", "", 0, repr(e)[:200])
            print(f"FAIL {name:<18} {e!r}"[:200])

    if not args.only or "spx_pe" in args.only:   # 標普 500 本益比（multpl.com，每月）
        try:
            rows = fetch_multpl()
            save_multpl(rows)
            est = sum(1 for r in rows if r[2])
            log(args.where, "spx_pe", "multpl.com", "OK", rows[-1][0], len(rows), f"最近 {est} 筆是估計值")
            print(f"OK   {'標普 500 本益比':<18} 最新 {rows[-1][0]}  {rows[-1][1]}（{'估計' if rows[-1][2] else '實際'}）  （共 {len(rows)} 個月，最近 {est} 筆估計）")
            ok += 1
        except Exception as e:
            log(args.where, "spx_pe", "multpl.com", "FAIL", "", 0, repr(e)[:200])
            print(f"FAIL {'標普 500 本益比':<18} {e!r}"[:200])

    if not args.only or "ndc_signal" in args.only:   # 國發會景氣燈號（每月）
        try:
            rows = fetch_ndc()
            save_ndc(rows)
            last = [r for r in rows if r[1]][-1]
            log(args.where, "ndc_signal", "data.gov.tw 6099", "OK", last[0], len(rows))
            print(f"OK   {'國發會景氣燈號':<18} 最新 {last[0]}  {last[1]} 分 {last[2]}  （共 {len(rows)} 個月）")
            ok += 1
        except Exception as e:
            log(args.where, "ndc_signal", "data.gov.tw 6099", "FAIL", "", 0, repr(e)[:200])
            print(f"FAIL {'國發會景氣燈號':<18} {e!r}"[:200])
    print(f"\n完成：{ok} 個成功。紀錄在 data/fetch_log.csv")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
