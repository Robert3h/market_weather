"""抓 9 個公開行情指標的每日收盤價，存成 data/<代號>.csv。

只用 Python 內建功能（不安裝任何套件）。執行方式：
    python -I scripts/fetch_quotes.py
--where 是「在哪裡跑的」：本機（預設）或 GitHub Actions，會記進 data/fetch_log.csv。不記實際地點。

每個 CSV 欄位：date,close,source
抓不到的指標不會覆蓋舊檔，只在 fetch_log 記一筆 FAIL。

歷史資料：CSV 不存在或加 --full 時，從 Yahoo 抓「全部歷史」（每天一筆）；平常只抓近 2 年。
台股兩個指標：較早的歷史用 Yahoo 補，近 13 個月以證交所官方數字為準（同一天會覆蓋 Yahoo）。
另外抓國發會景氣燈號（每月）→ data/ndc_signal.csv（month,score,light,leading,coincident），每次整份覆蓋。
聯準會升降息（政策事件，2026-11-23）→ data/events_fed.csv（date,effective,market,text,url），每次整份重寫，已查過的聲明稿網址沿用。
台灣央行理監事會（政策事件，2026-11-24）→ data/events_cbc.csv（date,kind,market,text,url），kind＝升息／降息／維持／空白（判斷不出來）；
平常只看央行列表第 1 頁，已讀過的新聞稿不再讀。
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
    "otc":    ("櫃買指數", "tpex_index", None),            # 2026-11-24 加
    "spx":    ("標普 500", "yahoo", "^GSPC"),
    "dji":    ("道瓊工業指數", "yahoo", "^DJI"),           # 2026-11-24 加
    "ixic":   ("那斯達克綜合指數", "yahoo", "^IXIC"),      # 2026-11-24 加（QQQ 是那斯達克 100）
    "qqq":    ("QQQ", "yahoo", "QQQ"),
    "smh":    ("SMH 半導體 ETF", "yahoo", "SMH"),
    "sox":    ("費城半導體指數 (SOX)", "yahoo", "^SOX"),   # 2026-11-23 加：觀察名單
    "nvda":   ("輝達 NVDA", "yahoo", "NVDA"),              # 2026-11-23 加：觀察名單
    "vix":    ("VIX 恐慌指數", "yahoo", "^VIX"),
    "us10y":  ("美國 10 年期公債殖利率", "yahoo", "^TNX"),
    "tw10y":  ("台灣 10 年期公債殖利率", "tpex_bond10y", None),   # 2026-11-24 加
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


# 櫃買中心（2026-11-24 加）：櫃買指數「月查詢」一次一個月，有開高低收，1999 年下半年起；
# 公債殖利率曲線：每天一個 .xls（2005-11 起），取「10年(Year)」那一列＝10 年期指標公債等殖成交量加權平均殖利率。
TPEX = "https://www.tpex.org.tw"
TPEX_INDEX_START = "199906"
TPEX_BOND_START = "200511"
TPEX_PAUSE = 0.5   # 櫃買中心請求間隔（秒）


def tpex_get(path):
    """櫃買中心偶爾直接斷線（2026-11-25 補歷史時遇到）：等 10 秒重試，最多 3 次。"""
    for attempt in range(3):
        try:
            return get(TPEX + path, timeout=60)
        except Exception:
            if attempt == 2:
                raise
            time.sleep(10)


def tpex_json(path):
    j = json.loads(tpex_get(path))
    if "tables" not in j:
        raise ValueError(f"櫃買中心回應：{j.get('stat', j)}")
    return j["tables"][0].get("data", [])


def fetch_tpex_index(_, months):
    """櫃買指數收盤 {date: close}。months＝往回抓幾個月（含這個月）。"""
    rows = {}
    for ym in month_starts(months):
        for r in tpex_json(f"/www/zh-tw/indexInfo/inx?date={ym[:4]}/{ym[4:6]}/01&response=json"):
            rows[r[0].replace("/", "-")] = num(r[4])
        time.sleep(TPEX_PAUSE)
    return rows, "TPEx 櫃買指數"


def fetch_tpex_bond10y(_, months):
    """台灣 10 年期公債殖利率（%）{date: yield}。已經在 CSV 裡的日子不重抓（每天一個檔，歷史很多）。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))   # python -I 不會把程式所在資料夾放進搜尋路徑
    from xls_reader import read_sheets
    have = set()
    f = DATA_DIR / "tw10y.csv"
    if f.exists():
        with f.open(encoding="utf-8") as fh:
            have = {r["date"] for r in csv.DictReader(fh)}
    rows, n = {}, 0
    for ym in month_starts(months):
        try:
            files = tpex_json(f"/www/zh-tw/bond/govDaily2?date={ym[:4]}/{ym[4:6]}/01&fileCode=Curve&response=json")
        except ValueError:
            continue   # 那個月沒有資料
        time.sleep(TPEX_PAUSE)
        for roc, xls, _ods in files:
            date = roc_to_date(roc)
            if date in have:
                continue
            try:
                raw = tpex_get(xls)
            except Exception:
                save_csv("tw10y", rows, "TPEx 公債殖利率曲線")   # 重試都失敗：先把讀到的存起來，下次接著抓
                raise
            cells = read_sheets(raw)[0]
            time.sleep(TPEX_PAUSE)
            for (r, c), v in cells.items():
                if isinstance(v, str) and v.startswith("10年"):
                    y = cells.get((r, c + 1))
                    if isinstance(y, float) and 0 < y < 20:
                        rows[date] = round(y, 4)
                    break
            n += 1
            if n % 100 == 0:   # 補歷史要 2 小時：每 100 個檔先存一次，中途停掉下次會從這裡接著跑
                save_csv("tw10y", rows, "TPEx 公債殖利率曲線")
                print(f"     台債 10 年：已讀 {n} 個檔，到 {date}（已存檔）", flush=True)
    return rows, "TPEx 公債殖利率曲線"


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
    # Yahoo 有時候最後一天的日線收盤是空的（2026-11-23 QQQ、NVDA 收盤後幾小時仍是 None），
    # 但 meta 的 regularMarketPrice 已經是收盤價：確定那天已收盤（regularMarketTime ≥ 收盤時間）才補上。
    period = meta.get("currentTradingPeriod", {}).get("regular", {})
    ts_list = res.get("timestamp") or []
    if ts_list and closes and closes[-1] is None and period:
        last_d = dt.datetime.fromtimestamp(ts_list[-1] + offset, dt.timezone.utc).date().isoformat()
        mt, px = meta.get("regularMarketTime", 0), meta.get("regularMarketPrice")
        mt_d = dt.datetime.fromtimestamp(mt + offset, dt.timezone.utc).date().isoformat() if mt else ""
        if px and mt_d == last_d and mt >= period.get("end", 0) and last_d not in rows:
            rows[last_d] = round(px, 4)
    # 盤中還沒收盤的那一根不要（不能把盤中價當收盤）
    if period and time.time() < period.get("end", 0) and rows:
        today_local = dt.datetime.fromtimestamp(period["start"] + offset, dt.timezone.utc).date().isoformat()
        rows.pop(today_local, None)
    return rows, f"Yahoo {symbol}"


SOURCES = {"twse_index": fetch_twse_index, "twse_stock": fetch_twse_stock, "yahoo": fetch_yahoo,
           "tpex_index": fetch_tpex_index, "tpex_bond10y": fetch_tpex_bond10y}
TPEX_FULL = {"tpex_index": TPEX_INDEX_START, "tpex_bond10y": TPEX_BOND_START}   # 櫃買中心：全部歷史從這個月開始


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


# ---------- 政策事件：聯準會升降息（2026-11-23，點子清單第四節第 6 點的做法 ①） ----------
# 來源是聯準會自己的利率變動表（2003 年起），不是新聞：只有日期、幅度、目標區間這些客觀數字，中文用固定句型，不用翻譯。
# 表上的日期是「生效日」：2008 年起通常是宣布的隔天，更早是宣布當天。所以依序試前一天、當天，
#   哪天的聲明稿網址存在，就用那天當事件日（市場是在宣布那天反應）；都找不到就用生效日、連到利率表。
FED_TABLE = "https://www.federalreserve.gov/monetarypolicy/openmarket.htm"
FED_STATEMENT = "https://www.federalreserve.gov/newsevents/pressreleases/monetary{}{}.htm"
FED_OLD = "https://www.federalreserve.gov/boarddocs/press/monetary/{}/{}/default.htm"
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]


def url_ok(url):
    try:
        req = urllib.request.Request(url, headers=UA, method="HEAD")
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception:
        return False


def fed_frac(x):
    """4.25 → '4-1/4'、0.25 → '1/4'、1.00 → '1'：聲明稿寫利率的方式"""
    w, r = divmod(round(float(x) * 8), 8)
    part = {0: "", 1: "1/8", 2: "1/4", 3: "3/8", 4: "1/2", 5: "5/8", 6: "3/4", 7: "7/8"}[r]
    return f"{w}-{part}" if w and part else (part or str(w))


def says_level(url, level):
    """聲明稿真的寫了這次的利率水準才算（同一天可能有別的貨幣政策新聞稿，只看網址存不存在會認錯）。"""
    try:
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", get(url, timeout=20).decode("utf-8", "replace")))
    except Exception:
        return False
    text = re.sub("[‐‑‒–]", "-", text)   # 有些聲明稿用不換行連字號（U+2011），看起來一樣、字元不同
    a, _, b = level.partition("-")
    need = f"{fed_frac(a)} to {fed_frac(b)} percent" if b else f"{fed_frac(a)} percent"
    return "federal funds rate" in text and need in text


def fed_text(inc, dec, level):
    """'25','0','5.25-5.50' → '聯準會升息 0.25 個百分點（1 碼），目標區間 5.25–5.50%'"""
    bp = dec if dec not in ("0", "...") else inc
    lo, _, hi = bp.partition("-")
    pp = lambda b: f"{int(b) / 100:g}"
    size = f"{pp(lo)}～{pp(hi)} 個百分點" if hi else f"{pp(lo)} 個百分點（{int(lo) // 25} 碼）"
    word = "降息" if dec not in ("0", "...") else "升息"
    a, _, b = level.partition("-")
    lv = f"目標區間 {a}–{b}%" if b else f"目標利率 {a}%"
    return f"聯準會{word} {size}，{lv}"


def fetch_fed_events():
    """回傳 [(事件日, 生效日, 說明, 網址)]，舊到新。"""
    page = get(FED_TABLE).decode("utf-8")
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page)).replace("&nbsp;", " ").replace("\xa0", " ")
    parts = re.split(r"(\d{4}) Date Increase Decrease Level \(%\)", text)
    old = {}
    f = DATA_DIR / "events_fed.csv"
    if f.exists():   # 已經查過的聲明稿網址沿用，不用每天重問聯準會網站
        with f.open(encoding="utf-8") as fh:
            old = {r["effective"]: (r["date"], r["url"]) for r in csv.DictReader(fh)}
    out = []
    for year, body in zip(parts[1::2], parts[2::2]):
        body = body.split("Back to year navigation")[0]
        for mon, day, inc, dec, level in re.findall(
                r"(" + "|".join(MONTHS) + r") (\d{1,2})\s*\*?\s+(\S+)\s+(\S+)\s+(\d[\d.]*(?:-[\d.]+)?)", body):
            eff = dt.date(int(year), MONTHS.index(mon) + 1, int(day))
            if eff.isoformat() in old:
                date, url = old[eff.isoformat()]
            else:
                date, url = eff.isoformat(), FED_TABLE
                for cand in (eff - dt.timedelta(days=1), eff):
                    ymd = cand.strftime("%Y%m%d")
                    urls = [FED_STATEMENT.format(ymd, x) for x in "ab"] + [FED_OLD.format(cand.year, ymd)]   # 2006 年以前在舊網址
                    hit = next((u for u in urls if says_level(u, level)), None)
                    if hit:
                        date, url = cand.isoformat(), hit
                        break
            out.append((date, eff.isoformat(), fed_text(inc, dec, level), url))
    if len(out) < 40:
        raise ValueError(f"聯準會利率表只解析出 {len(out)} 筆，格式可能改了")
    return sorted(out)


def save_fed_events(rows):
    with (DATA_DIR / "events_fed.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["date", "effective", "market", "text", "url"])
        for d, eff, t, u in rows:
            w.writerow([d, eff, "全部", t, u])


# 台灣央行理監事會（政策事件，2026-11-24）：央行網站「理監事會議決議」列表 → 每則決議新聞稿 → 找重貼現率那一句。
# 只認「各調升／調降 X 個百分點 … 調整為／降為／提高至 Y%」和「維持」，判斷不出來的就略過（不硬猜）。
# 2001 年以前的新聞稿用中文數字（四．六二五％），先換成阿拉伯數字再找。
CBC_BASE = "https://www.cbc.gov.tw"
CBC_LIST = CBC_BASE + "/tw/lp-357-1-{}-20.html"
CBC_DIGITS = str.maketrans({**{c: str(i) for i, c in enumerate("〇一二三四五六七八九")},
                            **{c: str(i) for i, c in enumerate("０１２３４５６７８９")},
                            "○": "0", "．": ".", "‧": ".", "﹪": "%", "％": "%"})


def cbc_bp_text(pp):
    """0.125 → '0.125 個百分點（半碼）'；0.25 → '（1 碼）'"""
    q = round(float(pp) / 0.25, 3)
    size = "半碼" if q == 0.5 else f"{q:g} 碼"
    return f"{float(pp):g} 個百分點（{size}）"


def cbc_decision(url):
    """回傳 ('升息'／'降息'／'維持'／'', 中文說明)。"""
    t = get(url, timeout=60).decode("utf-8", "replace")
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t)).replace("&nbsp;", " ").translate(CBC_DIGITS)
    t = re.sub(r"(\d) +(?=[\d.%])|(?<=[\d.]) +(?=%)", r"\1", t)   # 「0.125 個」「2.00 ％」中間的空白
    for s in re.findall(r"[^。]*重貼現率[^。]*。", t):
        if "聯邦" in s or "FED" in s.upper():   # 講聯準會的句子不算
            continue
        m = re.search(r"(調升|調高|提高|調降|降低)[^。%]*?(\d+(?:\.\d+)?)\s*個?百分\s*點", s) or \
            re.search(r"(調升|調高|提高|調降|降低)重貼現率[^。]*?各(\d+(?:\.\d+)?)個?百分點", s)
        if m:
            new = re.search(r"(?:調整後分別為|調整為|降為|提高至|調降為)\s*(?:年息)?\s*(\d+(?:\.\d+)?)\s*%", s)
            if not new:
                continue
            word = "降息" if "降" in m.group(1) else "升息"
            return word, f"台灣央行{word} {cbc_bp_text(m.group(2))}，重貼現率 {float(new.group(1)):g}%"
        keep = re.search(r"維持[^。]*?年[息率]\s*(\d+(?:\.\d+)?)\s*%|維持[^。]*?年[息率][^。]*?為\s*(\d+(?:\.\d+)?)\s*%"
                         r"|年[息率]\s*(\d+(?:\.\d+)?)\s*%[^。]*?不變|維持[^。]*?分別為年[息率]\s*(\d+(?:\.\d+)?)\s*%", s)
        if keep and ("維持" in s or "不予調整" in s or "不變" in s):
            lv = next(g for g in keep.groups() if g)
            return "維持", f"台灣央行利率不變，重貼現率 {float(lv):g}%"
    return "", ""


def fetch_cbc_events(full=False):
    """回傳 [(日期, 結果, 說明, 網址)]，舊到新。平常只看列表第 1 頁，新的才去讀新聞稿；CSV 不存在或 full 時翻完全部。"""
    f = DATA_DIR / "events_cbc.csv"
    old = {}
    if f.exists() and not full:
        with f.open(encoding="utf-8") as fh:
            old = {r["url"]: (r["date"], r["kind"], r["text"]) for r in csv.DictReader(fh)}
    seen, items = set(), []
    for p in range(1, 60 if (full or not old) else 2):
        page = get(CBC_LIST.format(p), timeout=60).decode("utf-8", "replace")
        got = re.findall(r'<time>(\d{4}-\d{2}-\d{2})</time><a href="(/tw/cp-357-[^"]+)" title="([^"]+)"', page)
        new = [g for g in got if g[1] not in seen]
        if not new:   # 翻過最後一頁，網站會一直回最後一頁
            break
        for d, href, title in new:
            seen.add(href)
            if "決議" in title and "議事錄" not in title and "不動產" not in title and "審慎措施" not in title:
                items.append((d, CBC_BASE + href))
        time.sleep(0.3)
    out = dict(old)
    for d, url in items:
        if url in out:
            continue
        kind, text = cbc_decision(url)
        out[url] = (d, kind, text)
        time.sleep(0.3)
    rows = sorted((d, k, t, u) for u, (d, k, t) in out.items())
    if sum(1 for r in rows if r[1]) < 20 and (full or not old):
        raise ValueError(f"央行決議只解析出 {sum(1 for r in rows if r[1])} 筆，格式可能改了")
    return rows


def save_cbc_events(rows):
    with (DATA_DIR / "events_cbc.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["date", "kind", "market", "text", "url"])
        for d, k, t, u in rows:
            w.writerow([d, k, "台股", t, u])


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
        where = where if where == "GitHub Actions" else "本機"   # 不記實際地點
        w.writerow([dt.datetime.now(TPE).strftime("%Y-%m-%d %H:%M:%S"), where, code, source, status, last_date, rows, note])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--where", default="本機", help="在哪裡跑：本機（預設）或 GitHub Actions")
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
            elif kind in TPEX_FULL:   # 櫃買中心：沒有備案來源，失敗就記 FAIL
                rows, source = SOURCES[kind](param, months_since(TPEX_FULL[kind]) if full else TWSE_MONTHS_DAILY)
                if not rows and kind == "tpex_bond10y" and not full:
                    print(f"     {name:<18} 沒有新的日子")
                    continue
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

    if not args.only or "events_fed" in args.only:   # 政策事件：聯準會升降息
        try:
            rows = fetch_fed_events()
            save_fed_events(rows)
            linked = sum(1 for r in rows if r[3] != FED_TABLE)
            log(args.where, "events_fed", "federalreserve.gov", "OK", rows[-1][0], len(rows), f"{linked} 筆連到聲明稿")
            print(f"OK   {'聯準會升降息':<18} 最新 {rows[-1][0]}  {rows[-1][2]}  （共 {len(rows)} 次，{linked} 次連到聲明稿）")
            ok += 1
        except Exception as e:
            log(args.where, "events_fed", "federalreserve.gov", "FAIL", "", 0, repr(e)[:200])
            print(f"FAIL {'聯準會升降息':<18} {e!r}"[:200])

    if not args.only or "events_cbc" in args.only:   # 政策事件：台灣央行理監事會
        try:
            rows = fetch_cbc_events(full=args.full)
            save_cbc_events(rows)
            chg = [r for r in rows if r[1] in ("升息", "降息")]
            log(args.where, "events_cbc", "cbc.gov.tw", "OK", rows[-1][0], len(rows),
                f"升降息 {len(chg)} 次、維持 {sum(1 for r in rows if r[1] == '維持')} 次")
            print(f"OK   {'台灣央行理監事會':<18} 最新 {rows[-1][0]}  {rows[-1][2] or '（判斷不出結果）'}  （共 {len(rows)} 則，升降息 {len(chg)} 次）")
            ok += 1
        except Exception as e:
            log(args.where, "events_cbc", "cbc.gov.tw", "FAIL", "", 0, repr(e)[:200])
            print(f"FAIL {'台灣央行理監事會':<18} {e!r}"[:200])

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
