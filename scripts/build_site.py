"""讀 data/*.csv，產生一頁靜態儀表板 website/index.html。

只用 Python 內建功能。執行方式：
    python -I scripts/build_site.py

晴雨規則是【草案】，集中在 judge_day()，要改規則只改那裡。
"""
import bisect
import csv
import re
import datetime as dt
import html
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUT = ROOT / "website" / "index.html"
SERIES_DIR = ROOT / "website" / "series"   # 每個指標的完整歷史，點開時才載入

SPARK_DAYS = 63      # 小走勢圖：約 3 個月的交易日
CALENDAR_DAYS = 30   # 天氣日曆格數
HISTORY_DAYS = 400   # 晴雨只算最近這麼多個台股交易日（日曆、連續天數夠用）
TPE = dt.timezone(dt.timedelta(hours=8))   # 一律用台灣時間（GitHub Actions 的機器是 UTC）
YIELDS = ("us10y", "tw10y")   # 殖利率：顯示成 %，漲跌用「個百分點」（2026-11-24 加台債）
DIGITS = {"taiex": 2, "otc": 2, "spx": 2, "dji": 2, "ixic": 2, "2330": 1, "vix": 2, "us10y": 3, "tw10y": 3, "usdtwd": 3, "2330_pe": 2, "gold": 1, "btc": 0}   # 走勢圖數字的小數位

UP, DN, FLAT = "#ef6a5f", "#3fb27f", "#a7a59e"   # 紅漲綠跌（台股習慣），風向組用灰色
WEATHER = {
    "晴": {"color": "#d97757", "cell": "#d97757"},
    "陰": {"color": "#a7a59e", "cell": "#3a3d44"},
    "雨": {"color": "#6b9bf0", "cell": "#3d6fd1"},
}

# (代號, 名稱, 副標, 組別, 顯示方式)  顯示方式：price＝漲跌用 %，point＝漲跌用點數、不上紅綠
GROUP_IDS = {"台股": "list", "美股": "us", "風向": "wind", "其他資產": "other"}
ROWS = [
    ("taiex", "加權指數", "TAIEX", "台股", "price"),
    ("otc", "櫃買指數", "TPEx · 上櫃股票", "台股", "price"),   # 2026-11-24 加（櫃買中心）
    ("2330", "台積電", "2330", "台股", "price"),
    ("spx", "標普 500", "S&P 500", "美股", "price"),
    ("dji", "道瓊工業", "DJIA", "美股", "price"),                 # 2026-11-24 加
    ("ixic", "那斯達克綜合", "Nasdaq Composite", "美股", "price"),   # 2026-11-24 加（QQQ 是那斯達克 100）
    ("qqq", "那斯達克 100", "QQQ", "美股", "price"),
    ("sox", "費城半導體指數", "SOX", "美股", "price"),   # 2026-11-23 加：觀察名單
    ("smh", "半導體 ETF", "SMH", "美股", "price"),       # 2026-11-23 改名：SMH 追蹤 MVIS 半導體指數，不是費城半導體（SOX）
    ("nvda", "輝達", "NVDA", "美股", "price"),            # 2026-11-23 加：觀察名單
    ("tsm", "台積電 ADR", "TSM", "美股", "price"),
    ("vix", "VIX 恐慌指數", "20 以下算平靜", "風向", "point"),
    ("us10y", "美債 10 年殖利率", "US10Y", "風向", "point"),
    ("tw10y", "台債 10 年殖利率", "指標公債 · 櫃買中心", "風向", "point"),   # 2026-11-24 加
    ("usdtwd", "美元兌台幣", "USD/TWD", "風向", "point"),
    ("gold", "黃金", "紐約期貨 · 美元／盎司", "其他資產", "price"),   # 2026-11-22 加：只看走勢，和其他資產比較
    ("btc", "比特幣", "BTC · 美元 · 週末也交易", "其他資產", "price"),
]
SOURCES_TEXT = "證交所（台股）、櫃買中心（櫃買指數、台債 10 年）、Yahoo Finance（美股、VIX、美債、匯率、黃金、比特幣）、國發會（景氣燈號，政府資料開放平台）"

# 國發會景氣對策信號：燈號 → (顏色, 分數範圍)。顏色從冷到熱，和國發會圖一樣藍→綠→紅
NDC_LIGHTS = [("藍", "#3d6fd1", "9–16"), ("黃藍", "#8fb8e8", "17–22"), ("綠", "#3fb27f", "23–31"),
              ("黃紅", "#f0a03c", "32–37"), ("紅", "#e5484d", "38–45")]
NDC_RANGES = [("1Y", "1年"), ("3Y", "3年"), ("5Y", "5年"), ("10Y", "10年"), ("20Y", "20年"), ("MAX", "最長")]
NDC_LEAD, NDC_COIN = "#ecebe6", "#7fa7d9"   # 領先、同時指標（不含趨勢）線的顏色


# ---------- 讀資料 ----------
def load(code):
    path = DATA_DIR / f"{code}.csv"
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return [(r["date"], float(r["close"])) for r in csv.DictReader(f)]


def pct(series, i):
    """第 i 天相對前一天的漲跌幅（%）"""
    return (series[i][1] / series[i - 1][1] - 1) * 100


def last_before(series, date):
    """series 裡日期 < date 的最後一筆索引（台股某天對應的『前一晚美股』）"""
    i = bisect.bisect_left(series, (date,)) - 1
    return i if i >= 0 else None


# ---------- 晴雨規則【草案】 ----------
def judge_day(tw_chg, spx_chg, vix):
    """晴：台股、標普都收紅且 VIX < 20；雨：任一跌超過 1% 或 VIX > 25；其他是陰。"""
    if tw_chg <= -1 or spx_chg <= -1 or vix > 25:
        return "雨"
    if tw_chg > 0 and spx_chg > 0 and vix < 20:
        return "晴"
    return "陰"


def weather_history(taiex, spx, vix):
    """每個台股交易日的晴雨，搭配該日之前最近一次美股收盤。"""
    out = []
    for i in range(max(1, len(taiex) - HISTORY_DAYS), len(taiex)):
        d = taiex[i][0]
        j = last_before(spx, d)
        k = last_before(vix, d)
        if j is None or j < 1 or k is None:
            continue
        tw_chg, spx_chg, v = pct(taiex, i), pct(spx, j), vix[k][1]
        out.append({"date": d, "us_date": spx[j][0], "tw": tw_chg, "spx": spx_chg, "vix": v,
                    "w": judge_day(tw_chg, spx_chg, v)})
    return out


def reason_text(day):
    def mv(name, c):
        return f"{name}{'漲' if c > 0 else '跌'} {abs(c):.2f}%"
    parts = [mv("台股", day["tw"]), mv("標普 500 ", day["spx"])]
    v = day["vix"]
    vtxt = f"VIX {v:.1f}，" + ("高於 25" if v > 25 else "低於 20" if v < 20 else "在 20～25 之間")
    return "、".join(parts) + "；" + vtxt + "。"


# ---------- 格式 ----------
def fmt_value(code, v):
    if code in ("taiex", "otc", "spx", "dji", "ixic"):
        return f"{v:,.2f}"
    if code == "2330":
        return f"{v:,.0f}" if v == int(v) else f"{v:,.1f}"
    if code in YIELDS:
        return f"{v:.2f}%"
    if code == "vix":
        return f"{v:.2f}"
    if code == "usdtwd":
        return f"{v:.3f}"
    if code == "gold":
        return f"{v:,.1f}"
    if code == "btc":
        return f"{v:,.0f}"
    return f"{v:,.2f}"


def fmt_change(code, kind, series):
    if len(series) < 2:
        return "—", FLAT
    a, b = series[-2][1], series[-1][1]
    if kind == "price":
        c = (b / a - 1) * 100
        arrow = "▲" if c > 0 else "▼" if c < 0 else "－"
        return f"{arrow} {abs(c):.2f}%", UP if c > 0 else DN if c < 0 else FLAT
    c = b - a
    arrow = "▲" if c > 0 else "▼" if c < 0 else "－"
    digits = 3 if code in (*YIELDS, "usdtwd") else 2
    return f"{arrow} {abs(c):.{digits}f}", FLAT


def md(d):
    """'2026-11-20' → '11/20'"""
    return f"{int(d[5:7])}/{int(d[8:10])}"


# ---------- 圖 ----------
def spark_points(values, w=84, h=28, pad=2):
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    n = len(values)
    return " ".join(
        f"{pad + (w - 2 * pad) * i / max(n - 1, 1):.1f},{pad + (h - 2 * pad) * (1 - (v - lo) / span):.1f}"
        for i, v in enumerate(values))


EPOCH = dt.date(1970, 1, 1)


def first_estimate(code):
    """本益比資料裡第一筆「估計值」的日期（source 欄有「估計」），沒有就 None。"""
    path = DATA_DIR / f"{code}.csv"
    if not path.exists():
        return None
    with path.open(encoding="utf-8", newline="") as f:
        return next((r["date"] for r in csv.DictReader(f) if "估計" in r["source"]), None)


def write_series(code, series, extra=None):
    """完整歷史寫成 website/series/<代號>.js。
    用 .js（不是 .json），因為直接雙擊開本機 file:/// 時瀏覽器不准讀 JSON，但可以載入 script。
    日期存成『和前一筆差幾天』，數字只留需要的小數位，檔案小很多。"""
    days = [(dt.date.fromisoformat(d) - EPOCH).days for d, _ in series]
    deltas = [days[0]] + [b - a for a, b in zip(days, days[1:])]
    dg = DIGITS.get(code, 2)
    closes = [round(v, dg + 1) for _, v in series]
    body = json.dumps({"d": deltas, "c": closes, **(extra or {})}, separators=(",", ":"))
    SERIES_DIR.mkdir(parents=True, exist_ok=True)
    (SERIES_DIR / f"{code}.js").write_text(f"MW_SERIES[{json.dumps(code)}]={body};\n", encoding="utf-8")


def load_ndc():
    """data/ndc_signal.csv → [(月份, 分數或 None, 燈號或 '', 領先, 同時)]"""
    path = DATA_DIR / "ndc_signal.csv"
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return [(r["month"], int(r["score"]) if r["score"] else None, r["light"],
                 float(r["leading"]) if r["leading"] else None, float(r["coincident"]) if r["coincident"] else None)
                for r in csv.DictReader(f)]


def write_ndc_series(rows):
    """website/series/ndc.js：起始月份＋每月的分數、燈號（0＝藍…4＝紅）、領先、同時。"""
    idx = {name: i for i, (name, _, _) in enumerate(NDC_LIGHTS)}
    body = json.dumps({"start": rows[0][0], "s": [r[1] for r in rows], "l": [idx.get(r[2]) for r in rows],
                       "ld": [r[3] for r in rows], "co": [r[4] for r in rows]}, separators=(",", ":"))
    SERIES_DIR.mkdir(parents=True, exist_ok=True)
    (SERIES_DIR / "ndc.js").write_text(f'MW_SERIES["ndc"]={body};\n', encoding="utf-8")


def ndc_row(rows):
    """「景氣（每月）」那一行：最新燈號、分數、和上個月比，展開是每月燈號長條圖。"""
    scored = [r for r in rows if r[1] is not None]
    if not scored:
        return ""
    ym, score, light = scored[-1][:3]
    color = dict((n, c) for n, c, _ in NDC_LIGHTS)[light]
    diff = score - scored[-2][1] if len(scored) > 1 else 0
    diff_txt = f"較上月 {'+' if diff > 0 else '−' if diff < 0 else '±'}{abs(diff)}"
    streak = 0
    for r in reversed(scored):
        if r[2] != light:
            break
        streak += 1
    spark = spark_points([r[1] for r in scored[-36:]])
    buttons = "".join(f'<button type="button" data-r="{r}" aria-pressed="{str(r == "5Y").lower()}">{lab}</button>'
                      for r, lab in NDC_RANGES)
    legend = "".join(f'<span><i class="sw" style="background:{c}"></i>{n} {rg}</span>' for n, c, rg in NDC_LIGHTS)
    return f"""<details id="i-ndc">
  <summary class="row">
    <span class="name">景氣燈號<small>國發會 · {int(ym[:4])} 年 {int(ym[5:])} 月</small></span>
    <span class="value">{score} 分</span>
    <span class="chg"><span style="color:{color}">● {light}燈</span><small class="sub">{diff_txt}</small></span>
    <svg class="spark" viewBox="0 0 84 28" preserveAspectRatio="none" aria-hidden="true"><polyline points="{spark}" stroke="{color}" stroke-width="1.4" fill="none" vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>
  </summary>
  <div class="ndc" data-streak="{streak}">
  <div class="modes" role="group" aria-label="圖表類型"><button type="button" data-m="signal" aria-pressed="true">燈號</button><button type="button" data-m="lead" aria-pressed="false">領先指標</button></div>
  <div class="ranges r6" role="group" aria-label="選擇期間">{buttons}</div>
  <p class="readout" aria-live="polite">載入中…</p>
  <div class="plot"></div>
  <p class="legend" data-for="signal">{legend}</p>
  <p class="legend" data-for="lead" hidden><span><i style="border-color:{NDC_LEAD};border-top-style:solid"></i>領先指標</span><span><i style="border-color:{NDC_COIN};border-top-style:solid"></i>同時指標</span><span>（不含趨勢；100＝長期趨勢）</span></p>
  <p class="note">每月一筆，約每月 27 日公布上個月，所以比股市慢 1～2 個月。9 個構成項目裡有一項是股價指數。領先指標通常比燈號早幾個月轉彎。</p>
  </div>
</details>"""


NDC_JS = r"""
// 景氣燈號：每月一根長條（顏色＝燈號），另一個模式畫領先、同時指標
function ndcRender(box) {
  const data = box._data; if (!data) return;
  const lead = box._mode === "lead", n = data.s.length, last = n - 1;
  const [y0, m0] = data.start.split("-").map(Number);
  const ym = i => { const t = y0 * 12 + m0 - 1 + i; return Math.floor(t / 12) + "-" + String(t % 12 + 1).padStart(2, "0"); };
  const first = data.s.findIndex(v => v != null);
  const months = {"1Y":12,"3Y":36,"5Y":60,"10Y":120,"20Y":240}[box._range];
  const i0 = Math.max(lead ? 0 : first, months ? n - months : 0);
  const plot = box.querySelector(".plot"), readout = box.querySelector(".readout");
  const W = Math.max(plot.clientWidth, 280), H = 220, pl = 4, pr = 58, pt = 10, pb = 24;
  const cnt = n - i0, slot = (W - pl - pr) / cnt;
  const X = i => pl + (i - i0 + 0.5) * slot;
  let lo, hi;
  if (lead) {
    lo = Infinity; hi = -Infinity;
    for (let i = i0; i < n; i++) for (const v of [data.ld[i], data.co[i]]) if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    lo = Math.min(lo, 100); hi = Math.max(hi, 100); const pad = (hi - lo) * 0.05 || 1; lo -= pad; hi += pad;
  } else { lo = 8; hi = 45; }   // 最低分是 9，底線放 8，9 分的月份才看得到長條
  const Y = v => pt + (1 - (v - lo) / (hi - lo)) * (H - pt - pb);
  const svg = el("svg", {viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `景氣${lead ? "領先指標" : "燈號"}：${ym(i0)} 到 ${ym(last)}`});
  if (lead) {
    const ticks = niceTicks(lo, hi, 4);
    for (const t of ticks) {
      el("line", {x1: pl, x2: W - pr, y1: Y(t), y2: Y(t), stroke: t === 100 ? "#5a5d64" : "#262930", "stroke-dasharray": t === 100 ? "3 4" : ""}, svg);
      el("text", {x: W - pr + 6, y: Y(t) + 4}, svg).textContent = t;
    }
    for (const [key, col] of [["co", NDC_COIN], ["ld", NDC_LEAD]]) {
      const p = []; for (let i = i0; i < n; i++) if (data[key][i] != null) p.push(X(i).toFixed(1) + "," + Y(data[key][i]).toFixed(1));
      el("polyline", {points: p.join(" "), fill: "none", stroke: col, "stroke-width": 1.6, "stroke-linejoin": "round"}, svg);
    }
  } else {
    for (const [v, t] of [[17, "17"], [23, "23"], [32, "32"], [38, "38"]]) {   // 燈號分界：黃藍、綠、黃紅、紅的起點
      el("line", {x1: pl, x2: W - pr, y1: Y(v - 0.5), y2: Y(v - 0.5), stroke: "#262930"}, svg);
      el("text", {x: W - pr + 6, y: Y(v - 0.5) + 4}, svg).textContent = t;
    }
    const bw = Math.max(slot * 0.78, 0.8);
    for (let i = i0; i < n; i++) if (data.s[i] != null)
      el("rect", {x: (X(i) - bw / 2).toFixed(2), y: Y(data.s[i]).toFixed(1), width: bw.toFixed(2), height: (Y(8) - Y(data.s[i])).toFixed(1), fill: NDC_COLORS[data.l[i]]}, svg);
  }
  el("text", {x: pl, y: H - 6}, svg).textContent = ym(i0);
  el("text", {x: W - pr, y: H - 6, "text-anchor": "end"}, svg).textContent = ym(last);
  const vline = el("line", {y1: pt, y2: H - pb, stroke: "#8f8d87", visibility: "hidden"}, svg);
  plot.replaceChildren(svg);

  const lastScored = data.s.map((v, i) => v != null ? i : -1).filter(i => i >= 0).pop();
  const dot = (k) => `<span style="color:${NDC_COLORS[k]}">●</span>`;
  let summary;
  if (lead) {
    const li = data.ld.map((v, i) => v != null ? i : -1).filter(i => i >= 0).pop();
    const d6 = li >= 6 && data.ld[li - 6] != null ? data.ld[li] - data.ld[li - 6] : null;
    summary = `<b>${ym(li)}</b>　領先 ${data.ld[li].toFixed(2)}　同時 ${data.co[li] != null ? data.co[li].toFixed(2) : "—"}` +
      (d6 != null ? `　<span class="nw"><span style="color:#8f8d87">領先較半年前</span> ${d6 >= 0 ? "+" : "−"}${Math.abs(d6).toFixed(2)}</span>` : "");
  } else {
    const c = [0, 0, 0, 0, 0]; for (let i = i0; i < n; i++) if (data.l[i] != null) c[data.l[i]]++;
    const order = [4, 3, 2, 1, 0];
    summary = `<b>${ym(i0)} 起</b>　` + order.filter(k => c[k]).map(k => `<span class="nw">${dot(k)} ${NDC_NAMES[k]} ${c[k]}</span>`).join("　") +
      `　<span style="color:#8f8d87">（個月）</span>　<span class="nw"><span style="color:#8f8d87">最新</span> ${NDC_NAMES[data.l[lastScored]]}燈連續 ${box.dataset.streak} 個月</span>`;
  }
  readout.innerHTML = summary;
  const show = ev => {
    const rect = svg.getBoundingClientRect();
    const x = (ev.clientX - rect.left) / rect.width * W;
    const i = Math.max(i0, Math.min(last, Math.floor(i0 + (x - pl) / slot)));
    vline.setAttribute("x1", X(i)); vline.setAttribute("x2", X(i)); vline.setAttribute("visibility", "visible");
    readout.innerHTML = lead
      ? `<b>${ym(i)}</b>　領先 ${data.ld[i] != null ? data.ld[i].toFixed(2) : "—"}　同時 ${data.co[i] != null ? data.co[i].toFixed(2) : "—"}`
      : data.s[i] != null ? `<b>${ym(i)}</b>　${dot(data.l[i])} ${NDC_NAMES[data.l[i]]}燈　${data.s[i]} 分` : `<b>${ym(i)}</b>　還沒公布`;
  };
  const hide = () => { vline.setAttribute("visibility", "hidden"); readout.innerHTML = summary; };
  svg.addEventListener("pointermove", show);
  svg.addEventListener("pointerdown", show);
  svg.addEventListener("pointerleave", hide);
  svg.addEventListener("pointercancel", hide);
}
document.querySelectorAll(".ndc").forEach(box => {
  box._range = "5Y"; box._mode = "signal";
  const det = box.closest("details");
  det.addEventListener("toggle", () => {
    if (!det.open) return;
    if (box._data) return ndcRender(box);
    loadSeries("ndc").then(d => { box._data = d; ndcRender(box); })
      .catch(() => { box.querySelector(".readout").textContent = "景氣資料載入失敗。"; });
  });
  box.querySelectorAll(".ranges button").forEach(b => b.addEventListener("click", () => {
    box._range = b.dataset.r;
    box.querySelectorAll(".ranges button").forEach(x => x.setAttribute("aria-pressed", x === b));
    ndcRender(box);
  }));
  box.querySelectorAll(".modes button").forEach(b => b.addEventListener("click", () => {
    box._mode = b.dataset.m;
    box.querySelectorAll(".modes button").forEach(x => x.setAttribute("aria-pressed", x === b));
    box.querySelectorAll(".legend").forEach(l => { l.hidden = l.dataset.for !== box._mode; });
    ndcRender(box);
  }));
});
window.addEventListener("resize", () => setTimeout(() => document.querySelectorAll("details[open] .ndc").forEach(ndcRender), 160));
"""


CMP_DEFAULT = ["taiex", "2330", "spx"]   # 「比較」第一次打開時預設勾選的指標
CMP_COLORS = ["#ecebe6", "#7fa7d9", "#e0a95a", "#5fc4b8", "#d58fb5"]   # 依勾選順序配色（避開紅綠，紅綠留給漲跌）


def compare_block(has_ndc):
    """「比較」：勾選最多 5 個指標放在一起看。上下排＝各自刻度、同一條時間軸；疊在一起＝同一起點的漲跌 %。
    has_ndc：有國發會景氣燈號資料時，多一個「景氣燈號」可以勾（每月資料，畫成階梯線）。"""
    items = [(code, name) for code, name, *_ in ROWS] + ([("ndc", "景氣燈號（每月）")] if has_ndc else [])
    chips = "".join(f'<button type="button" data-c="{code}" aria-pressed="false">{name}</button>'
                    for code, name in items)
    buttons = "".join(
        f'<button type="button" data-r="{r}" aria-pressed="{str(r == "1Y").lower()}">{label}</button>'
        for r, label in RANGES)
    # 勾選按鈕直接露在外面（2026-11-22 改：原本收在「把指標放在一起看」裡面，太不明顯）。
    # 按任一個勾選按鈕或「看比較圖」才展開圖，一進網頁不會多一大張圖。
    return f"""<div class="cmp">
  <p class="hint">勾選最多 5 個指標，放在一起看走勢</p>
  <div class="chips" role="group" aria-label="勾選要比較的指標">{chips}</div>
  <button type="button" class="open" aria-expanded="false" aria-controls="cmp-body">看比較圖<span aria-hidden="true">▾</span></button>
  <div class="body" id="cmp-body" hidden>
  <div class="modes" role="group" aria-label="排法"><button type="button" data-m="stack" aria-pressed="true">上下排</button><button type="button" data-m="overlay" aria-pressed="false">疊在一起</button></div>
  <div class="ranges" role="group" aria-label="選擇期間">{buttons}</div>
  <p class="readout" aria-live="polite">載入中…</p>
  <div class="plot"></div>
  <p class="note">上下排：每張圖各自刻度，手指移動時每張圖標出同一天，虛線是期間起點。疊在一起：每條線都從期間起點算 0%，看誰強誰弱。台股、美股休市日不同，以最晚收盤的那個市場對齊日期。{"景氣燈號是國發會每月公布的分數（約每月 27 日公布上個月），一整個月畫成同一個高度；上下排時看分數增減（背景顏色是燈號的分數區間），疊在一起時和其他指標一樣算變化 %，只供參考。線下方的色帶是每個月的燈號。" if has_ndc else ""}</p>
  </div>
</div>"""


CMP_JS = r"""
// 比較：勾選幾個指標放在一起看
const CMP_MAX = 5;
function lastLE(days, t) { let lo = 0, hi = days.length - 1, ans = -1; while (lo <= hi) { const m = (lo + hi) >> 1; if (days[m] <= t) { ans = m; lo = m + 1; } else hi = m - 1; } return ans; }
function ndcMonthly(raw) {   // 景氣燈號（每月）→ 比較用的格式：每個月放在該月 1 日，end＝最後一個月的下個月 1 日
  const [y, m] = raw.start.split("-").map(Number), days = [], c = [], l = [];
  raw.s.forEach((v, i) => { if (v != null) { days.push(Date.UTC(y, m - 1 + i, 1) / DAY); c.push(v); l.push(raw.l[i]); } });
  const e = new Date(days[days.length - 1] * DAY);
  return {days, c, l, monthly: true, end: Date.UTC(e.getUTCFullYear(), e.getUTCMonth() + 1, 1) / DAY};
}
function cmpRender(box) {
  const plot = box.querySelector(".plot"), readout = box.querySelector(".readout");
  const ser = box._sel.filter(c => box._d[c]).map(code => ({code, m: CMP_META[code], d: box._d[code], color: CMP_COLORS[box._sel.indexOf(code)]}));
  if (!ser.length) { plot.replaceChildren(); readout.textContent = "勾選上面的指標（最多 5 個）。"; return; }
  const stack = box._mode !== "overlay", r = box._range;
  const lastDay = s => s.d.days[s.d.days.length - 1];
  const ref = ser.reduce((a, b) => lastDay(b) > lastDay(a) ? b : a);   // 用最晚收盤的那個指標對齊日期
  const T0 = r === "MAX" ? Math.max(...ser.map(s => s.d.days[0])) : ref.d.days[startIndex(ref.d.days, r)];   // 最長＝大家都有資料的那天起
  const T1 = lastDay(ref);
  for (const s of ser) {
    s.i0 = Math.max(0, lastLE(s.d.days, T0)); s.last = s.d.days.length - 1; s.base = s.d.c[s.i0];
    s.pct = i => (s.d.c[i] / s.base - 1) * 100;
  }
  const W = Math.max(plot.clientWidth, 280), pl = 4, pr = 58, pt = 8, pb = ser.some(s => s.d.l) ? 30 : 22, GAP = 18;   // 有景氣燈號色帶時，底下多留一點給日期
  const PH = stack ? (ser.length > 3 ? 84 : 104) : 250;
  const H = pt + (stack ? ser.length * PH + (ser.length - 1) * GAP : PH) + pb;
  const X = day => pl + (day - T0) / Math.max(T1 - T0, 1) * (W - pl - pr);
  const svg = el("svg", {viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "比較：" + ser.map(s => s.m.name).join("、")});
  svg.style.height = H + "px";
  const tickText = (t, step, suf) => { const d = Math.max(0, Math.min(3, -Math.floor(Math.log10(step) + 1e-9))); return t.toLocaleString("en-US", {minimumFractionDigits: d, maximumFractionDigits: d}) + suf; };
  const Xc = day => Math.max(pl, X(day));   // 期間起點前的那一筆（例如上個月的燈號）畫在最左邊
  const line = (s, Y) => {
    let pts;
    if (s.d.monthly) {   // 每月資料畫成階梯：一整個月同一個高度
      pts = [];
      for (let i = s.i0; i <= s.last; i++) {
        const y = Y(i).toFixed(1), x1 = Math.min(X(i < s.last ? s.d.days[i + 1] : s.d.end), W - pr);
        pts.push(Xc(s.d.days[i]).toFixed(1) + "," + y, x1.toFixed(1) + "," + y);
      }
    } else pts = sample(s.i0, s.last + 1, s.d.c, Math.min(800, W * 2)).map(i => Xc(s.d.days[i]).toFixed(1) + "," + Y(i).toFixed(1));
    el("polyline", {points: pts.join(" "), fill: "none", stroke: s.color, "stroke-width": 1.6, "stroke-linejoin": "round"}, svg);
  };
  const strip = (s, y) => {   // 景氣燈號色帶（2026-11-23）：每個月一格，顏色＝當月燈號
    for (let i = s.i0; i <= s.last; i++) {
      const x0 = Xc(s.d.days[i]), x1 = Math.min(X(i < s.last ? s.d.days[i + 1] : s.d.end), W - pr);
      if (x1 > x0) el("rect", {x: x0.toFixed(1), y: y.toFixed(1), width: (x1 - x0 + 0.3).toFixed(2), height: 5, fill: NDC_COLORS[s.d.l[i]]}, svg);
    }
  };
  const labels = [];
  if (stack) {
    ser.forEach((s, k) => {
      const top = pt + k * (PH + GAP);
      let lo = Infinity, hi = -Infinity;
      for (let i = s.i0; i <= s.last; i++) { const v = s.d.c[i]; if (v < lo) lo = v; if (v > hi) hi = v; }
      if (lo === hi) { lo -= 1; hi += 1; }
      const pad = (hi - lo) * 0.06; lo -= pad; hi += pad;
      const Yv = v => top + (1 - (v - lo) / (hi - lo)) * PH;
      if (s.d.l) for (const [a, b, k] of [[8.5, 16.5, 0], [16.5, 22.5, 1], [22.5, 31.5, 2], [31.5, 37.5, 3], [37.5, 45.5, 4]]) {
        const y0 = Yv(Math.min(b, hi)), y1 = Yv(Math.max(a, lo));   // 燈號分數區間：9–16 藍、17–22 黃藍、23–31 綠、32–37 黃紅、38–45 紅
        if (y1 > y0) el("rect", {x: pl, y: y0.toFixed(1), width: W - pl - pr, height: (y1 - y0).toFixed(1), fill: NDC_COLORS[k], opacity: 0.14}, svg);
      }
      const ticks = niceTicks(lo, hi, 3), step = ticks.length > 1 ? ticks[1] - ticks[0] : 1;
      for (const t of ticks) {
        el("line", {x1: pl, x2: W - pr, y1: Yv(t), y2: Yv(t), stroke: "#262930"}, svg);
        el("text", {x: W - pr + 6, y: Yv(t) + 4}, svg).textContent = tickText(t, step, s.m.suf);
      }
      el("line", {x1: pl, x2: W - pr, y1: Yv(s.base), y2: Yv(s.base), stroke: "#5a5d64", "stroke-dasharray": "3 4"}, svg);
      s.Y = i => Yv(s.d.c[i]);
      line(s, s.Y);
      if (s.d.l) strip(s, top + PH + 3);
      const ch = change(s.base, s.d.c[s.last], s.m.kind, s.m.dg);
      if (s.m.kind === "score") ch.text += " 分";
      labels.push([top + 13, s, ch]);
    });
  } else {
    let lo = 0, hi = 0;
    for (const s of ser) for (let i = s.i0; i <= s.last; i++) { const v = s.pct(i); if (v < lo) lo = v; if (v > hi) hi = v; }
    if (lo === hi) { lo -= 1; hi += 1; }
    const Yp = v => pt + (1 - (v - lo) / (hi - lo)) * PH;
    const ticks = niceTicks(lo, hi, 4), step = ticks.length > 1 ? ticks[1] - ticks[0] : 1;
    for (const t of ticks) {
      el("line", {x1: pl, x2: W - pr, y1: Yp(t), y2: Yp(t), stroke: t === 0 ? "#5a5d64" : "#262930", "stroke-dasharray": t === 0 ? "3 4" : ""}, svg);
      el("text", {x: W - pr + 6, y: Yp(t) + 4}, svg).textContent = (t > 0 ? "+" : "") + tickText(t, step, "%");
    }
    for (const s of ser) { s.Y = i => Yp(s.pct(i)); line(s, s.Y); }
    for (const s of ser) if (s.d.l) strip(s, pt + PH + 3);
  }
  for (const [y, s, ch] of labels) {   // 每張小圖左上角：名稱＋期間漲跌（最後畫，才不會被線蓋住）
    const t = el("text", {x: pl + 2, y, style: "paint-order:stroke;stroke:#14161a;stroke-width:4px;stroke-linejoin:round;font-size:12px"}, svg);
    const a = el("tspan", {style: `fill:${s.color}`}, t); a.textContent = s.m.name + "  ";
    const b = el("tspan", {style: `fill:${ch.color}`}, t); b.textContent = ch.text;
  }
  el("text", {x: pl, y: H - 6}, svg).textContent = iso(T0);
  el("text", {x: W - pr, y: H - 6, "text-anchor": "end"}, svg).textContent = iso(T1);
  const vline = el("line", {y1: pt, y2: H - pb, stroke: "#8f8d87", visibility: "hidden"}, svg);
  const dots = ser.map(s => el("circle", {r: 3.5, fill: s.color, stroke: "#14161a", "stroke-width": 2, visibility: "hidden"}, svg));
  plot.replaceChildren(svg);

  const dot = s => `<span style="color:${s.color}">●</span>`;
  const pctText = v => `${v > 0 ? "▲" : v < 0 ? "▼" : "－"} ${Math.abs(v).toFixed(2)}%`;
  const chgHtml = (s, i) => {   // 漲跌：一般是 %；景氣燈號在上下排時看分數增減
    if (stack && s.m.kind === "score") { const d = s.d.c[i] - s.base; return `<span style="color:${NEUTRAL}">${d > 0 ? "▲" : d < 0 ? "▼" : "－"} ${Math.abs(d)} 分</span>`; }
    const v = s.pct(i);
    return `<span style="color:${v >= 0 ? UP : DN}">${pctText(v)}</span>`;
  };
  const summary = `<b>${RANGE_LABEL[r]}</b>　` + ser.map(s => `<span class="nw">${dot(s)} ${s.m.name} ${chgHtml(s, s.last)}</span>`).join("　") +
    `　<span class="nw" style="color:#8f8d87">${iso(T0)} 起</span>`;
  readout.innerHTML = summary;
  const show = ev => {
    const rect = svg.getBoundingClientRect();
    const x = (ev.clientX - rect.left) / rect.width * W;
    const want = T0 + (x - pl) / (W - pl - pr) * (T1 - T0);
    const day = ref.d.days[Math.max(ref.i0, lastLE(ref.d.days, want))];
    vline.setAttribute("x1", X(day)); vline.setAttribute("x2", X(day)); vline.setAttribute("visibility", "visible");
    readout.innerHTML = `<b>${iso(day)}</b>　` + ser.map((s, k) => {
      const i = Math.max(s.i0, lastLE(s.d.days, day));
      dots[k].setAttribute("cx", s.d.monthly ? X(day) : Xc(s.d.days[i])); dots[k].setAttribute("cy", s.Y(i)); dots[k].setAttribute("visibility", "visible");
      const extra = s.d.monthly ? ` ${iso(s.d.days[i]).slice(0, 7)}` : "";   // 每月資料標出是哪個月的
      const light = s.d.l ? ` ${NDC_NAMES[s.d.l[i]]}燈` : "";
      return `<span class="nw">${dot(s)} ${s.m.name}${extra} ${fmt(s.d.c[i], s.m.dg, s.m.suf)}${light} ${chgHtml(s, i)}</span>`;
    }).join("　");
  };
  const hide = () => { vline.setAttribute("visibility", "hidden"); dots.forEach(d => d.setAttribute("visibility", "hidden")); readout.innerHTML = summary; };
  svg.addEventListener("pointermove", show);
  svg.addEventListener("pointerdown", show);
  svg.addEventListener("pointerleave", hide);
  svg.addEventListener("pointercancel", hide);
}
document.querySelectorAll(".cmp").forEach(box => {
  box._d = {}; box._range = "1Y"; box._mode = "stack";
  try { box._sel = JSON.parse(localStorage.getItem("mw_compare")); } catch (e) {}   // 記住上次勾了哪些（只存在這台裝置的瀏覽器）
  if (!Array.isArray(box._sel)) box._sel = CMP_DEFAULT.slice();
  box._sel = box._sel.filter(c => CMP_META[c]).slice(0, CMP_MAX);
  const chips = box.querySelectorAll(".chips button"), readout = box.querySelector(".readout");
  const sync = () => chips.forEach(b => {
    const k = box._sel.indexOf(b.dataset.c);
    b.setAttribute("aria-pressed", k >= 0);
    b.style.setProperty("--c", k >= 0 ? CMP_COLORS[k] : "transparent");
    b.disabled = k < 0 && box._sel.length >= CMP_MAX;
  });
  const load = () => Promise.all(box._sel.map(c => box._d[c] || loadSeries(c).then(d => { box._d[c] = c === "ndc" ? ndcMonthly(d) : d; })))
    .then(() => cmpRender(box)).catch(() => { readout.textContent = "走勢資料載入失敗。"; });
  sync();
  const body = box.querySelector(".body"), open = box.querySelector(".open");
  const setOpen = on => {   // 展開／收起比較圖
    body.hidden = !on;
    open.setAttribute("aria-expanded", on);
    open.firstChild.textContent = on ? "收起比較圖" : "看比較圖";
    open.lastChild.textContent = on ? "▴" : "▾";
    if (on) load();
  };
  open.addEventListener("click", () => setOpen(body.hidden));
  chips.forEach(b => b.addEventListener("click", () => {
    const c = b.dataset.c, k = box._sel.indexOf(c);
    if (k >= 0) box._sel.splice(k, 1); else if (box._sel.length < CMP_MAX) box._sel.push(c);
    try { localStorage.setItem("mw_compare", JSON.stringify(box._sel)); } catch (e) {}
    sync();
    if (body.hidden) setOpen(true); else load();   // 按勾選按鈕就自動展開圖
  }));
  box.querySelectorAll(".ranges button").forEach(b => b.addEventListener("click", () => {
    box._range = b.dataset.r;
    box.querySelectorAll(".ranges button").forEach(x => x.setAttribute("aria-pressed", x === b));
    cmpRender(box);
  }));
  box.querySelectorAll(".modes button").forEach(b => b.addEventListener("click", () => {
    box._mode = b.dataset.m;
    box.querySelectorAll(".modes button").forEach(x => x.setAttribute("aria-pressed", x === b));
    cmpRender(box);
  }));
});
window.addEventListener("resize", () => setTimeout(() => document.querySelectorAll(".cmp").forEach(box => { if (!box.querySelector(".body").hidden) cmpRender(box); }), 170));
"""


# ---------- 政策事件（2026-11-23，點子清單第四節第 6 點：做法 ①＋②） ----------
# ① data/events_fed.csv：聯準會升降息，fetch_quotes.py 從聯準會利率表自動抓，每則都連到當天的聲明稿。
# ①' data/events_cbc.csv：台灣央行理監事會升降息（2026-11-24），fetch_quotes.py 從央行新聞稿自動判斷，連到新聞稿；市場＝台股。
# ② data/events_manual.csv：Robert 自己挑的事件（日期,市場,說明,連結）。Claude 不自己編事件和日期。
#    可能用 Excel 存檔：UTF-8 和 Excel 預設的 Big5（cp950）都讀；日期 2025-04-02、2025/4/2 都可以；市場空白＝全部。
# 走勢圖底部畫小三角形；手指或滑鼠靠近時，圖下方一行寫說明和原文連結（離開圖也留著，觸控才點得到連結）。
EVENT_MARKETS = ("全部", "台股", "美股")


def _read_manual(f):
    for enc in ("utf-8-sig", "cp950"):
        try:
            return list(csv.DictReader(f.read_text(encoding=enc).splitlines()))
        except UnicodeDecodeError:
            continue
    raise SystemExit(f"{f.name} 讀不出來：請用 Excel「另存新檔 → CSV UTF-8（逗號分隔）」")


def load_events():
    out = []
    f = DATA_DIR / "events_fed.csv"
    if f.exists():
        with f.open(encoding="utf-8") as fh:
            out += [(r["date"], r["market"], r["text"], r["url"]) for r in csv.DictReader(fh)]
    f = DATA_DIR / "events_cbc.csv"   # 台灣央行：只標升降息（維持不變的不畫，和聯準會一致）
    if f.exists():
        with f.open(encoding="utf-8") as fh:
            out += [(r["date"], r["market"], r["text"], r["url"]) for r in csv.DictReader(fh)
                    if r["kind"] in ("升息", "降息")]
    f = DATA_DIR / "events_manual.csv"
    if f.exists():
        for n, r in enumerate(_read_manual(f), 2):
            raw = (r.get("日期") or "").strip()
            if not raw:
                continue
            try:
                y, m, d = re.split(r"[-/.]", raw)
                date = dt.date(int(y), int(m), int(d)).isoformat()
            except ValueError:
                print(f"【事件】events_manual.csv 第 {n} 行的日期「{raw}」看不懂，先略過（要寫成 2025-04-02）")
                continue
            mk = (r.get("市場") or "").strip() or "全部"
            if mk not in EVENT_MARKETS:
                print(f"【事件】events_manual.csv 第 {n} 行的市場「{mk}」要是 全部／台股／美股，先當成全部")
                mk = "全部"
            out.append((date, mk, (r.get("說明") or "").strip(), (r.get("連結") or "").strip()))
    return sorted(out)


def chart_block(code, kind):
    buttons = "".join(
        f'<button type="button" data-r="{r}"{" aria-pressed=true" if r == "1Y" else " aria-pressed=false"}>{label}</button>'
        for r, label in RANGES)
    suf = ' data-suf="%"' if code in YIELDS else ""
    legend = ""
    if kind == "price":   # 均線圖例：季線、年線（顏色和 JS 的 MA_LINES 一致）
        legend = "".join(f'<span><i style="border-color:{col}"></i>{label}</span>' for _, col, label in MA_LINES)
        legend = f'<p class="legend" data-for="price">{legend}</p>'
    modes, pe_attr = "", ""
    pe_code = PE_FOR.get(code)
    if pe_code and (DATA_DIR / f"{pe_code}.csv").exists():   # 有本益比資料才出現「股價｜本益比」切換
        pe_attr = f' data-pe="{pe_code}"'
        modes = ('<div class="modes" role="group" aria-label="圖表類型">'
                 f'<button type="button" data-m="price" aria-pressed="true">{"股價" if code == "2330" else "指數"}</button>'
                 '<button type="button" data-m="pe" aria-pressed="false">本益比</button></div>\n  ')
        legend += (f'<p class="legend" data-for="pe" hidden><span><i style="border-color:{PE_MEAN}"></i>期間平均</span>'
                   f'<span><i style="border-color:{PE_BAND}"></i>±1 標準差</span><span>（{PE_NOTE.get(pe_code, "")}）</span></p>')
    mk = "台股" if code in ("taiex", "otc", "2330", "usdtwd", "tw10y") else "美股"   # 事件的「市場」：台股事件只標在台股、台幣
    return f"""<div class="chart" data-code="{code}" data-kind="{kind}" data-dg="{DIGITS.get(code, 2)}" data-mk="{mk}"{suf}{pe_attr}>
  {modes}<div class="ranges" role="group" aria-label="選擇期間">{buttons}</div>
  <p class="readout" aria-live="polite">載入中…</p>
  <div class="plot"></div>
  <p class="evline" aria-live="polite"></p>{legend}
</div>"""


MA_LINES = [(60, "#e0a95a", "季線 60 日"), (240, "#7fa7d9", "年線 240 日")]   # 移動平均線：天數、顏色、名稱（只是參考座標）
PE_FOR = {"2330": "2330_pe", "spx": "spx_pe"}   # 哪個指標有本益比：指標代號 → 本益比資料代號（data/<代號>.csv）
PE_NOTE = {"2330_pe": "證交所本益比，每天",
           "spx_pe": "multpl.com，每月、過去 12 個月盈餘；最近幾個月是用預估盈餘算的估計值，之後會修正"}
PE_MEAN, PE_BAND = "#ecebe6", "#8f8d87"  # 本益比圖：期間平均線、±1 標準差線的顏色
RANGES = [("5D", "5天"), ("1M", "1月"), ("3M", "3月"), ("6M", "6月"), ("YTD", "今年"),
          ("1Y", "1年"), ("3Y", "3年"), ("5Y", "5年"), ("MAX", "最長")]


SUN = """<svg class="icon" viewBox="0 0 72 72" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" aria-hidden="true"><circle cx="36" cy="36" r="14"/><line x1="36" y1="6" x2="36" y2="14"/><line x1="36" y1="58" x2="36" y2="66"/><line x1="6" y1="36" x2="14" y2="36"/><line x1="58" y1="36" x2="66" y2="36"/><line x1="15" y1="15" x2="20.5" y2="20.5"/><line x1="51.5" y1="51.5" x2="57" y2="57"/><line x1="15" y1="57" x2="20.5" y2="51.5"/><line x1="51.5" y1="20.5" x2="57" y2="15"/></svg>"""
CLOUD = """<svg class="icon" viewBox="0 0 72 72" fill="none" stroke="currentColor" stroke-width="3" stroke-linejoin="round" aria-hidden="true"><path d="M20 52h34a12 12 0 0 0 0-24 16 16 0 0 0-30-4 12 12 0 0 0-4 28z"/></svg>"""
RAIN = """<svg class="icon" viewBox="0 0 72 72" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 42h34a11 11 0 0 0 0-22 15 15 0 0 0-28-4 11 11 0 0 0-6 26z"/><line x1="26" y1="50" x2="22" y2="60"/><line x1="38" y1="50" x2="34" y2="60"/><line x1="50" y1="50" x2="46" y2="60"/></svg>"""
ICONS = {"晴": SUN, "陰": CLOUD, "雨": RAIN}


# ---------- 今天值得注意（固定規則） ----------
def sp(name):
    """名稱結尾是英數字時補一個空格：『標普 500 跌』而不是『標普 500跌』"""
    return name + " " if name[-1].isascii() else name


def notable(series_map):
    """固定規則挑異常。創 60 日紀錄的單獨一行；其他大漲大跌（≥2%）合併成一行，避免大跌日一次列七八條。"""
    notes, big_up, big_dn = [], [], []
    for code, name, _, group, kind in ROWS:
        s = series_map[code]
        if len(s) < 61 or kind != "price" or group == "其他資產":   # 黃金、比特幣只看走勢，不列進「值得注意」（比特幣常常一天動 2% 以上）
            continue
        c = pct(s, len(s) - 1)
        if abs(c) < 2:
            continue
        past = [pct(s, i) for i in range(len(s) - 60, len(s) - 1)]
        if c < 0 and c <= min(past):
            notes.append(f"{sp(name)}跌 {abs(c):.2f}%，是近 60 個交易日最大單日跌幅（{md(s[-1][0])}）。")
        elif c > 0 and c >= max(past):
            notes.append(f"{sp(name)}漲 {abs(c):.2f}%，是近 60 個交易日最大單日漲幅（{md(s[-1][0])}）。")
        else:
            (big_up if c > 0 else big_dn).append(f"{name} {abs(c):.1f}%")
    if big_dn:
        notes.append("跌超過 2%：" + "、".join(big_dn) + "。")
    if big_up:
        notes.append("漲超過 2%：" + "、".join(big_up) + "。")
    vix = series_map["vix"]
    if len(vix) >= 2:
        v, dv = vix[-1][1], vix[-1][1] - vix[-2][1]
        if v > 25:
            notes.append(f"VIX {v:.1f}，高於 25（市場明顯緊張）。")
        elif dv >= 3:
            notes.append(f"VIX 一天升 {dv:.1f} 點到 {v:.1f}。")
    t = series_map["us10y"]
    if len(t) >= 2 and abs(t[-1][1] - t[-2][1]) >= 0.1:
        notes.append(f"美債 10 年殖利率一天{'升' if t[-1][1] > t[-2][1] else '降'} {abs(t[-1][1] - t[-2][1]):.2f} 個百分點，到 {t[-1][1]:.2f}%。")
    fx = series_map["usdtwd"]
    if len(fx) >= 2 and abs(pct(fx, len(fx) - 1)) >= 0.5:
        c = pct(fx, len(fx) - 1)
        notes.append(f"美元兌台幣一天{'升' if c > 0 else '貶'} {abs(c):.2f}%（台幣{'貶值' if c > 0 else '升值'}）。")
    return notes


# ---------- 展開細節：距最高點、52 週高低、過去大跌（第 2 輪，2026-11-23） ----------
BEAR = -20.0       # 「大跌」門檻：從前高跌超過 20%（業界慣用的熊市定義）
FACT_ROWS = 8      # 過去大跌最多列幾次（最近的在上面）


def span_text(d1, d2):
    """兩個日期相隔多久：未滿 2 年寫月，2 年以上寫年。"""
    days = (dt.date.fromisoformat(d2) - dt.date.fromisoformat(d1)).days
    months = days / 30.44
    if months < 1:
        return f"{days} 天"
    return f"{months:.0f} 個月" if months < 24 else f"{months / 12:.1f} 年"


def drawdowns(s):
    """把整段歷史切成一波一波的「從前高下跌 → 回到前高」。
    回傳 (目前的前高索引, [(高點索引, 谷底索引, 回到前高的索引或 None), ...])。只用收盤價。"""
    eps, peak, trough = [], 0, 0
    for i, (_, v) in enumerate(s):
        if v >= s[peak][1]:
            if trough != peak:                       # 有跌下去過、現在回到前高：這一波結束
                eps.append((peak, trough, i))
            peak = trough = i
        elif v < s[trough][1]:
            trough = i
    if trough != peak:                               # 還沒回到前高：這一波還在進行
        eps.append((peak, trough, None))
    return peak, eps


def deepest_inside(s, t_, r_):
    """谷底之後、回到前高之前，又從某個高點跌了多深（例：加權 2000 年那次，中間還有 2007-10 → 2008-11）。
    這種下跌沒有回到「前高」，drawdowns() 不會另外算一次，表格上看不到，所以另外寫一行。"""
    sub = s[t_:(r_ if r_ is not None else len(s))]
    best, hi = None, 0
    for i, (_, v) in enumerate(sub):
        if v > sub[hi][1]:
            hi = i
        d = v / sub[hi][1] - 1
        if best is None or d < best[0]:
            best = (d, hi, i)
    if best and best[0] * 100 <= BEAR:
        d, h, lo = best
        return sub[h], sub[lo], d * 100
    return None


def facts_block(code, kind, s):
    """展開後、走勢圖下面的固定數字。價格類：距最高點、52 週高低、過去大跌；點數類只有 52 週高低。"""
    if len(s) < 2:
        return ""
    last_d, last_v = s[-1]
    cut = (dt.date.fromisoformat(last_d) - dt.timedelta(days=365)).isoformat()
    win = [x for x in s if x[0] > cut]
    hi_d, hi_v = max(win, key=lambda x: x[1])
    lo_d, lo_v = min(win, key=lambda x: x[1])
    pos = (last_v - lo_v) / (hi_v - lo_v) * 100 if hi_v > lo_v else 50
    f = lambda v: fmt_value(code, v)
    parts = [f"""<div class="fact">
    <h3>52 週高低</h3>
    <div class="range" role="img" aria-label="52 週最低 {f(lo_v)}，最高 {f(hi_v)}，現在 {f(last_v)}，在區間的 {pos:.0f}% 位置">
      <span class="bar"><i style="left:{pos:.1f}%"></i></span>
      <span class="lo">{f(lo_v)}<small>{lo_d}</small></span><span class="hi">{f(hi_v)}<small>{hi_d}</small></span>
    </div>
  </div>"""]
    if kind != "price":
        return f'<div class="facts">{"".join(parts)}</div>'

    peak, eps = drawdowns(s)
    ath_d, ath_v = s[peak]
    dd = (last_v / ath_v - 1) * 100
    if dd > -0.05:
        now = f"現在就在歷史最高點附近（{ath_d}，{f(ath_v)}）。"
    else:
        now = f'距歷史最高點 <b style="color:{DN}">−{abs(dd):.1f}%</b>（最高點 {ath_d}，{f(ath_v)}）。'
    ongoing = eps[-1] if eps and eps[-1][2] is None else None
    if ongoing:
        p_, t_, _ = ongoing
        depth = (s[t_][1] / s[p_][1] - 1) * 100
        if t_ == len(s) - 1:                         # 谷底就是今天：跌幅和上一句一樣，不重複寫
            now += f"從高點下跌到現在 {span_text(s[p_][0], last_d)}，還沒回到前高。"
        elif depth <= -5:
            now += f"這一波最深跌到 −{abs(depth):.1f}%（{s[t_][0]}），從高點算起已 {span_text(s[p_][0], last_d)}，還沒回到前高。"
    parts.insert(0, f'<div class="fact"><h3>距最高點</h3><p>{now}</p></div>')

    big = [e for e in eps if (s[e[1]][1] / s[e[0]][1] - 1) * 100 <= BEAR]
    since = s[0][0][:4]
    if big:
        done = [e for e in big if e[2] is not None]
        rec = sorted((dt.date.fromisoformat(s[r][0]) - dt.date.fromisoformat(s[t][0])).days / 30.44 for _, t, r in done)
        med = ""
        if rec:
            m = rec[len(rec) // 2] if len(rec) % 2 else (rec[len(rec) // 2 - 1] + rec[len(rec) // 2]) / 2
            med = f"已回到前高的 {len(done)} 次，從谷底回到前高的時間中位數約 {m:.0f} 個月。"
        rows = []
        ep = lambda d: (dt.date.fromisoformat(d) - EPOCH).days
        for p_, t_, r_ in reversed(big[-FACT_ROWS:]):
            depth = (s[t_][1] / s[p_][1] - 1) * 100
            back = span_text(s[t_][0], s[r_][0]) if r_ is not None else "還沒回到"
            cls = ' class="now"' if r_ is None else ""
            end = s[r_][0] if r_ is not None else last_d
            # 2026-11-23：點日期，走勢圖切到「最長」，從高點到回到前高塗一段底色
            under = span_text(s[p_][0], end)
            label = f"{'水面下' if r_ is not None else '從高點到現在'} {under}"
            btn = (f'<button type="button" class="ddbtn" aria-pressed="false" data-a="{ep(s[p_][0])}" data-b="{ep(end)}" '
                   f'data-label="{label}">{s[p_][0][:7]} → {s[t_][0][:7]}</button>')
            rows.append(f'<tr{cls}><td>{btn}</td><td class="num" style="color:{DN}">−{abs(depth):.1f}%</td>'
                        f'<td class="num">{span_text(s[p_][0], s[t_][0])}</td><td class="num">{back}</td></tr>')
            inner = deepest_inside(s, t_, r_) if r_ is None or (dt.date.fromisoformat(end) - dt.date.fromisoformat(s[t_][0])).days > 3 * 365 else None
            if inner:
                (hd, hv), (ld, lv), d = inner
                rows.append(f'<tr class="inside"><td colspan="4">這段期間裡，還從 {hd}（{fmt_value(code, hv)}）跌到 {ld}（{fmt_value(code, lv)}），'
                            f'<span style="color:{DN}">−{abs(d):.1f}%</span>；因為沒有回到前高，不另外算一次。</td></tr>')
        more = f"（只列最近 {FACT_ROWS} 次）" if len(big) > FACT_ROWS else ""
        parts.append(f"""<div class="fact">
    <h3>過去跌超過 {abs(BEAR):.0f}% 的幾次</h3>
    <p>{since} 年以來共 {len(big)} 次{more}。{med}「回到前高」從谷底起算。</p>
    <div class="tbl"><table>
      <thead><tr><th scope="col">高點 → 谷底</th><th scope="col">最大跌幅</th><th scope="col">跌了多久</th><th scope="col">回到前高</th></tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table></div>
  </div>""")
    else:
        parts.append(f'<div class="fact"><h3>過去跌超過 {abs(BEAR):.0f}% 的幾次</h3><p>{since} 年以來沒有從前高跌超過 {abs(BEAR):.0f}% 過。</p></div>')
    parts.append(f'<p class="note">用每天收盤價計算，{since} 年起；是價格、不含股息或配息，和實際持有的報酬會有差距。只是歷史紀錄，不代表之後會怎麼走。</p>')
    return f'<div class="facts">{"".join(parts)}</div>'


# ---------- 互動走勢圖（瀏覽器端，純 JavaScript，不用套件） ----------
CHART_JS = r"""
window.MW_SERIES = window.MW_SERIES || {};
const DAY = 86400000, SVGNS = "http://www.w3.org/2000/svg";
const RANGE_LABEL = {"5D":"5天","1M":"1個月","3M":"3個月","6M":"6個月","YTD":"今年以來","1Y":"1年","3Y":"3年","5Y":"5年","MAX":"最長"};
const loading = {};

// 點開才載入那個指標的完整歷史（website/series/<代號>.js）
function loadSeries(code) {
  if (MW_SERIES[code]) return Promise.resolve(decode(MW_SERIES[code]));
  if (!loading[code]) loading[code] = new Promise((ok, fail) => {
    const s = document.createElement("script");
    s.src = "series/" + code + ".js?v=__BUILD__";
    s.onload = () => MW_SERIES[code] ? ok(decode(MW_SERIES[code])) : fail();
    s.onerror = fail;
    document.head.appendChild(s);
  });
  return loading[code];
}
function decode(raw) {            // 日期是「和前一筆差幾天」，還原成 1970 起的天數（景氣燈號是每月資料，沒有 d，不用還原）
  if (!raw.days && raw.d) { let t = 0; raw.days = raw.d.map(x => (t += x)); }
  return raw;
}
const iso = day => new Date(day * DAY).toISOString().slice(0, 10);

// 區間起點：最後一個「日期 <= 目標日」的交易日（和 Google 財經一樣拿那天收盤當基準）
function startIndex(days, r) {
  const n = days.length;
  if (r === "MAX") return 0;
  if (r === "5D") return Math.max(0, n - 6);
  const L = new Date(days[n - 1] * DAY);
  const y = L.getUTCFullYear(), m = L.getUTCMonth(), d = L.getUTCDate();
  let target;
  if (r === "YTD") target = Date.UTC(y, 0, 1) / DAY - 1;      // 去年最後一天
  else {
    const mm = m - {"1M":1,"3M":3,"6M":6,"1Y":12,"3Y":36,"5Y":60}[r];
    const dim = new Date(Date.UTC(y, mm + 1, 0)).getUTCDate();
    target = Date.UTC(y, mm, Math.min(d, dim)) / DAY;
  }
  let lo = 0, hi = n - 1, ans = 0;
  while (lo <= hi) { const mid = (lo + hi) >> 1; if (days[mid] <= target) { ans = mid; lo = mid + 1; } else hi = mid - 1; }
  return ans;
}

// 點太多（例如最長 1 萬多天）時，每一小段只留最高和最低，畫起來一樣但快很多
function sample(i0, n, c, maxPts) {
  const len = n - i0;
  if (len <= maxPts) return Array.from({length: len}, (_, k) => i0 + k);
  const buckets = Math.floor(maxPts / 2), size = len / buckets, out = [i0];
  for (let b = 0; b < buckets; b++) {
    const s = i0 + Math.floor(b * size), e = Math.min(n, i0 + Math.floor((b + 1) * size));
    let mi = s, ma = s;
    for (let i = s; i < e; i++) { if (c[i] < c[mi]) mi = i; if (c[i] > c[ma]) ma = i; }
    if (mi === ma) out.push(mi); else out.push(Math.min(mi, ma), Math.max(mi, ma));
  }
  out.push(n - 1);
  return out;
}

function sma(c, n) {              // 簡單移動平均：前 n 天收盤的平均，不足 n 天是 null
  const out = new Array(c.length).fill(null); let sum = 0;
  for (let i = 0; i < c.length; i++) { sum += c[i]; if (i >= n) sum -= c[i - n]; if (i >= n - 1) out[i] = sum / n; }
  return out;
}
function niceTicks(lo, hi, count) {
  const span = hi - lo || Math.abs(hi) || 1, raw = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw))), e = raw / mag;
  const step = (e < 1.5 ? 1 : e < 3 ? 2 : e < 7 ? 5 : 10) * mag, out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(10));
  return out;
}

function fmt(v, dg, suf) {
  const min = dg === 1 ? 0 : dg;
  return v.toLocaleString("en-US", {minimumFractionDigits: min, maximumFractionDigits: dg}) + (suf || "");
}
function change(a, b, kind, dg) {
  const diff = b - a, arrow = diff > 0 ? "▲" : diff < 0 ? "▼" : "－";
  if (kind === "price") {
    const p = (b / a - 1) * 100;
    return {text: arrow + " " + Math.abs(p).toFixed(2) + "%", color: p >= 0 ? UP : DN};
  }
  return {text: arrow + " " + Math.abs(diff).toFixed(dg), color: NEUTRAL};
}
function el(tag, attrs, parent) {
  const e = document.createElementNS(SVGNS, tag);
  for (const k in attrs) {
    const v = attrs[k];
    if ((k === "fill" || k === "stroke") && typeof v === "string" && v.startsWith("var(")) e.style.setProperty(k, v);   // 主題色是 CSS 變數
    else e.setAttribute(k, v);
  }
  if (parent) parent.appendChild(e);
  return e;
}

function render(box) {
  const pe = box._mode === "pe";                 // 「本益比」模式：畫本益比＋期間平均 ±1 標準差
  const data = pe ? box._pe : box._data; if (!data) return;
  const kind = pe ? "pe" : box.dataset.kind, dg = pe ? 2 : +box.dataset.dg, suf = pe ? "" : (box.dataset.suf || "");
  const r = box._range, days = data.days, c = data.c, n = days.length;
  const i0 = pe && data.monthly && (r === "5D" || r === "1M") ? Math.max(0, n - 2) : startIndex(days, r), last = n - 1;   // 每月資料：期間太短時至少畫最近兩個月
  const estTag = i => pe && data.est != null && days[i] >= data.est ? '<span style="color:#e0a95a">（估計）</span>' : "";
  let stat = null;
  if (pe) {                                      // 平均和標準差用所選期間的每一天算（選 3 年＝3 年平均）
    let s = 0, s2 = 0, k = last - i0 + 1;
    for (let i = i0; i <= last; i++) { s += c[i]; s2 += c[i] * c[i]; }
    const mean = s / k, sd = Math.sqrt(Math.max(s2 / k - mean * mean, 0));
    stat = {mean, sd, z: v => sd ? (v - mean) / sd : 0};
  }
  const zText = v => { const z = stat.z(v); return (z >= 0 ? "+" : "−") + Math.abs(z).toFixed(1) + "σ"; };
  const plot = box.querySelector(".plot"), readout = box.querySelector(".readout");
  const W = Math.max(plot.clientWidth, 280), H = 230, pl = 4, pr = 58, pt = 10, pb = 34;   // 圖和日期之間留一條放事件三角形
  const pts = sample(i0, n, c, Math.min(800, W * 2));
  // 移動平均線（只有價格類）：季線 60 日、年線 240 日，用完整歷史算，所以期間短也畫得出來
  const mas = kind === "price" ? MA_LINES.map(m => ({...m, v: data["ma" + m.n] || (data["ma" + m.n] = sma(c, m.n))})) : [];
  let lo = Infinity, hi = -Infinity;
  for (const i of pts) {
    if (c[i] < lo) lo = c[i]; if (c[i] > hi) hi = c[i];
    for (const m of mas) { const v = m.v[i]; if (v != null) { if (v < lo) lo = v; if (v > hi) hi = v; } }
  }
  if (stat) { lo = Math.min(lo, stat.mean - stat.sd); hi = Math.max(hi, stat.mean + stat.sd); }
  if (lo === hi) { lo -= 1; hi += 1; }
  const span = Math.max(last - i0, 1);
  const X = i => pl + (i - i0) / span * (W - pl - pr);
  const Y = v => pt + (1 - (v - lo) / (hi - lo)) * (H - pt - pb);
  const ch = change(c[i0], c[last], kind, dg);
  const color = kind === "price" ? ch.color : NEUTRAL;

  const svg = el("svg", {viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": `${RANGE_LABEL[r]}走勢：${iso(days[i0])} 到 ${iso(days[last])}，${ch.text}`});
  const ticks = niceTicks(lo, hi, 4);
  const step = ticks.length > 1 ? ticks[1] - ticks[0] : 1;
  const tdg = Math.max(0, Math.min(3, -Math.floor(Math.log10(step) + 1e-9)));   // 同一軸的刻度用同樣小數位
  for (const t of ticks) {
    el("line", {x1: pl, x2: W - pr, y1: Y(t), y2: Y(t), stroke: "#262930"}, svg);
    el("text", {x: W - pr + 6, y: Y(t) + 4}, svg).textContent =
      t.toLocaleString("en-US", {minimumFractionDigits: tdg, maximumFractionDigits: tdg}) + suf;
  }
  const peLabels = [];
  if (stat) {                                    // 平均線、±1 標準差線，左邊標數字
    for (const [v, col, label] of [[stat.mean, PE_MEAN, "平均"], [stat.mean + stat.sd, PE_BAND, "+1σ"], [stat.mean - stat.sd, PE_BAND, "−1σ"]]) {
      el("line", {x1: pl, x2: W - pr, y1: Y(v), y2: Y(v), stroke: col, "stroke-dasharray": "5 4", "stroke-width": 1.1}, svg);
      peLabels.push([Y(v) - 4, col, `${label} ${v.toFixed(1)}`]);   // 標籤等曲線畫完再放，才不會被蓋住
    }
  } else {
    el("line", {x1: pl, x2: W - pr, y1: Y(c[i0]), y2: Y(c[i0]), stroke: "#5a5d64", "stroke-dasharray": "3 4"}, svg);
  }
  for (const m of mas) {
    const p = pts.filter(i => m.v[i] != null).map(i => X(i).toFixed(1) + "," + Y(m.v[i]).toFixed(1));
    if (p.length > 1) el("polyline", {points: p.join(" "), fill: "none", stroke: m.color, "stroke-width": 1.2,
      "stroke-dasharray": "5 4", "stroke-linejoin": "round"}, svg);
  }
  el("polyline", {points: pts.map(i => X(i).toFixed(1) + "," + Y(c[i]).toFixed(1)).join(" "),
    fill: "none", stroke: color, "stroke-width": 1.6, "stroke-linejoin": "round"}, svg);
  for (const [y, col, t] of peLabels)
    el("text", {x: pl + 2, y, style: `fill:${col};paint-order:stroke;stroke:#14161a;stroke-width:4px;stroke-linejoin:round`}, svg).textContent = t;
  el("text", {x: pl, y: H - 6}, svg).textContent = iso(days[i0]);
  el("text", {x: W - pr, y: H - 6, "text-anchor": "end"}, svg).textContent = iso(days[last]);
  // 「過去跌超過 20%」表格點了哪一次：從高點到回到前高塗一段底色（只在指數／股價模式）
  if (box._band && !pe) {
    const seek = v => { let a = i0, b = last; while (a < b) { const m = (a + b) >> 1; if (days[m] < v) a = m + 1; else b = m; } return a; };
    const a = seek(box._band.a), b = seek(box._band.b);
    if (b > a) {
      el("rect", {x: X(a), y: pt, width: Math.max(X(b) - X(a), 1), height: H - pt - pb, fill: "#d97757", "fill-opacity": 0.13}, svg);
      for (const k of [a, b]) el("line", {x1: X(k), x2: X(k), y1: pt, y2: H - pb, stroke: "#d97757", "stroke-dasharray": "2 3"}, svg);
      const lx = X(a) + 6, room = W - pr - lx;
      el("text", {x: room > 90 ? lx : X(b) - 6, y: pt + 14, "text-anchor": room > 90 ? "start" : "end",
        style: "fill:#d97757;font-weight:600;paint-order:stroke;stroke:#14161a;stroke-width:4px;stroke-linejoin:round"}, svg).textContent = box._band.label;
    }
  }
  // 政策事件：期間內、這個市場的事件，在圖底部畫小三角形（非交易日的事件，標在下一個交易日）
  const evs = EVENTS.filter(e => e[0] >= days[i0] && e[0] <= days[last] && (e[1] === "全部" || e[1] === box.dataset.mk))
    .map(e => { let a = i0, b = last; while (a < b) { const m = (a + b) >> 1; if (days[m] < e[0]) a = m + 1; else b = m; } return {i: a, x: X(a), e}; });
  const evMarks = evs.map(v => el("path", {d: `M${(v.x - 4.5).toFixed(1)},${H - pb + 11} l4.5,-8 l4.5,8 z`, fill: EV_COLOR}, svg));
  const vline = el("line", {y1: pt, y2: H - pb, stroke: "#8f8d87", visibility: "hidden"}, svg);
  const dot = el("circle", {r: 4, fill: color, stroke: "#14161a", "stroke-width": 2, visibility: "hidden"}, svg);
  plot.replaceChildren(svg);
  const evline = box.querySelector(".evline");
  evline.textContent = evs.length ? `▲ 這段期間有 ${evs.length} 個政策事件` : "";
  evline.classList.remove("on");
  const showEvent = v => {   // 用 textContent 組，事件文字不會被當成 HTML
    evMarks.forEach((m, k) => m.style.fill = evs[k] === v ? "#d97757" : EV_COLOR);
    evline.replaceChildren();
    const b = document.createElement("b"); b.textContent = iso(v.e[0]) + "　"; evline.append(b, v.e[2]);
    if (v.e[3]) {
      const a = document.createElement("a"); a.href = v.e[3]; a.target = "_blank"; a.rel = "noopener"; a.textContent = "原文 ↗";
      evline.append("　", a);
    }
    evline.classList.add("on");
  };

  const summary = pe
    ? `<b>${RANGE_LABEL[r]}</b>　本益比 ${c[last].toFixed(2)} 倍${estTag(last)}　<span class="nw"><span style="color:#8f8d87">平均</span> ${stat.mean.toFixed(1)}</span>　<span class="nw"><span style="color:#8f8d87">位置</span> ${zText(c[last])}</span>`
    : `<b>${RANGE_LABEL[r]}</b>　<span style="color:${ch.color}">${ch.text}</span>　<span style="color:#8f8d87">${iso(days[i0])} → ${iso(days[last])}</span>`;
  readout.innerHTML = summary;

  const show = ev => {
    const rect = svg.getBoundingClientRect();
    const x = (ev.clientX - rect.left) / rect.width * W;
    let i = Math.max(i0, Math.min(last, Math.round(i0 + (x - pl) / (W - pl - pr) * span)));
    const tol = 10 * W / rect.width;   // 靠近三角形 10px 內就算指到事件（期間長時一天不到 1px，對不準）
    let near = null;
    for (const v of evs) if (Math.abs(v.x - x) <= tol && (!near || Math.abs(v.x - x) < Math.abs(near.x - x))) near = v;
    if (near) { i = near.i; showEvent(near); }
    vline.setAttribute("x1", X(i)); vline.setAttribute("x2", X(i)); vline.setAttribute("visibility", "visible");
    dot.setAttribute("cx", X(i)); dot.setAttribute("cy", Y(c[i])); dot.setAttribute("visibility", "visible");
    if (pe) {
      readout.innerHTML = `<b>${iso(days[i])}</b>　本益比 ${c[i].toFixed(2)} 倍${estTag(i)}　<span class="nw"><span style="color:#8f8d87">較期間平均</span> ${zText(c[i])}</span>`;
      return;
    }
    const ci = change(c[i0], c[i], kind, dg);
    let extra = "";
    const y = mas.find(m => m.n === 240);
    if (y && y.v[i] != null) {
      const d = (c[i] / y.v[i] - 1) * 100;
      extra = `　<span class="nw"><span style="color:#8f8d87">距年線</span> ${d >= 0 ? "+" : "−"}${Math.abs(d).toFixed(1)}%</span>`;
    }
    readout.innerHTML = `<b>${iso(days[i])}</b>　${fmt(c[i], dg, suf)}　<span style="color:#8f8d87">較起點</span> <span style="color:${ci.color}">${ci.text}</span>${extra}`;
  };
  const hide = () => { vline.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); readout.innerHTML = summary; };
  svg.addEventListener("pointermove", show);
  svg.addEventListener("pointerdown", show);
  svg.addEventListener("pointerleave", hide);
  svg.addEventListener("pointercancel", hide);
}

document.querySelectorAll(".chart").forEach(box => {
  box._range = "1Y";
  const det = box.closest("details");
  det.addEventListener("toggle", () => {
    if (!det.open || box._data) { if (det.open) render(box); return; }
    loadSeries(box.dataset.code).then(d => { box._data = d; render(box); })
      .catch(() => { box.querySelector(".readout").textContent = "走勢資料載入失敗。"; });
  });
  box.querySelectorAll(".ranges button").forEach(b => b.addEventListener("click", () => {
    box._range = b.dataset.r;
    box.querySelectorAll(".ranges button").forEach(x => x.setAttribute("aria-pressed", x === b));
    render(box);
  }));
  box.querySelectorAll(".modes button").forEach(b => b.addEventListener("click", () => {   // 股價｜本益比
    box._mode = b.dataset.m;
    box.querySelectorAll(".modes button").forEach(x => x.setAttribute("aria-pressed", x === b));
    box.querySelectorAll(".legend").forEach(l => { l.hidden = l.dataset.for !== box._mode; });
    if (box._mode === "pe" && !box._pe) {
      box.querySelector(".readout").textContent = "載入中…";
      loadSeries(box.dataset.pe).then(d => { box._pe = d; render(box); })
        .catch(() => { box.querySelector(".readout").textContent = "本益比資料載入失敗。"; });
    } else render(box);
  }));
});
document.querySelectorAll(".ddbtn").forEach(btn => btn.addEventListener("click", () => {
  const det = btn.closest("details"), box = det.querySelector(".chart"), on = btn.getAttribute("aria-pressed") !== "true";
  det.querySelectorAll(".ddbtn").forEach(b => b.setAttribute("aria-pressed", "false"));
  box._band = on ? {a: +btn.dataset.a, b: +btn.dataset.b, label: btn.dataset.label} : null;
  btn.setAttribute("aria-pressed", String(on));
  if (box._mode === "pe") box.querySelector('.modes button[data-m="price"]').click();   // 本益比模式不畫，先切回指數
  if (on) box.querySelector('.ranges button[data-r="MAX"]').click(); else render(box);
  if (on) box.scrollIntoView({block: "nearest"});
}));
let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => document.querySelectorAll("details[open] .chart").forEach(render), 150);
});
"""


# ---------- 回測頁（第 6 輪，2026-11-23）：website/backtest.html ----------
sys.path.insert(0, str(Path(__file__).resolve().parent))   # python -I 不會把程式所在資料夾放進搜尋路徑
import backtest as bt   # noqa: E402

BT_NAMES = {"taiex": "加權指數", "spx": "標普 500", "qqq": "那斯達克 100（QQQ）"}

BT_CSS = """*{box-sizing:border-box}
html{scroll-padding-top:64px}
@media (max-width:480px){html{scroll-padding-top:92px}}
body{margin:0;background:#14161a;color:#ecebe6;font-family:"Noto Sans TC","Microsoft JhengHei","PingFang TC",system-ui,sans-serif;-webkit-text-size-adjust:100%}
.site-header{position:sticky;top:0;z-index:20;background:#14161a;border-bottom:1px solid #262930}
.hwrap{max-width:1180px;margin:0 auto;padding:8px 1rem;display:flex;align-items:center;gap:4px 16px}
.brand{display:inline-flex;align-items:center;gap:8px;min-height:40px;color:#ecebe6;text-decoration:none;font-size:15px;font-weight:600;letter-spacing:.04em}
.brand .icon{width:22px;height:22px;flex:none}
.brand:hover{color:#d97757}
.brand:focus-visible,.site-header nav a:focus-visible,a:focus-visible{outline:2px solid #d97757;outline-offset:2px;border-radius:4px}
.site-header nav{margin-left:auto;display:flex;gap:2px}
.site-header nav a{display:inline-flex;align-items:center;min-height:40px;padding:0 10px;color:#a7a59e;text-decoration:none;font-size:14px;white-space:nowrap}
.site-header nav a:hover{color:#d97757;text-decoration:underline;text-underline-offset:4px}
.site-header nav a[aria-current=page]{color:#d97757;font-weight:600}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.doc{max-width:760px;margin:0 auto;padding:1.5rem 1rem 3rem;display:flex;flex-direction:column;gap:28px}
h1{margin:0 0 10px;font-size:28px;line-height:1.3}
h2{margin:0 0 8px;font-size:18px}
p{margin:0 0 10px;font-size:15px;line-height:1.8;color:#c9c7c0}
.lead{font-size:16px;color:#ecebe6}
.muted{font-size:13px;color:#8f8d87}
a{color:#d97757}a:hover{color:#eb9474}
.tbl{overflow-x:auto;margin:8px 0 10px}
table{border-collapse:collapse;width:100%;font-size:14px;font-variant-numeric:tabular-nums;white-space:nowrap}
th{font-weight:600;color:#8f8d87;text-align:right;padding:7px 8px;border-bottom:1px solid #262930}
td{padding:8px;border-bottom:1px solid #1f2227;text-align:right;color:#c9c7c0}
th:first-child,td:first-child{text-align:left;padding-left:0}
td.win{color:#ecebe6;font-weight:600}
ul{margin:0;padding-left:1.2em;color:#c9c7c0;font-size:14px;line-height:1.8}
.site-footer{border-top:1px solid #262930}
.site-footer .foot{max-width:760px;margin:0 auto;padding:20px 1rem 40px}
.site-footer p{margin:0 0 6px;font-size:12px;color:#8f8d87;line-height:1.7}
.site-footer a{color:#a7a59e}
@media (max-width:480px){.hwrap{padding:4px 12px 0;gap:0;flex-wrap:wrap}.brand{font-size:14px;gap:6px;min-height:36px}
.site-header nav{margin-left:0;width:100%;justify-content:space-between}.site-header nav a{padding:0 4px}
table{font-size:12px}th,td{padding:6px 4px}h1{font-size:24px}}
"""


def _x(m):
    return f"{m:.1f} 倍" if m < 100 else f"{m:.0f} 倍"


def backtest_page(series_map, ndc, w, built, nav_links, has_ndc):
    """回測頁：作者的長期原則＋年線進出、藍燈紅燈兩個回測。數字每次建站用最新資料重算。"""
    ma_rows, as_of = [], ""
    for code in bt.MA_MARKETS:
        s = series_map.get(code) or []
        r = bt.ma_backtest(s)
        if not r:
            continue
        as_of = max(as_of, r["end"])
        bh, st = r["bh"], r["st"]
        win_bh = bh["mult"] >= st["mult"]
        fee = ""
        if code == "taiex":   # 台股的交易成本有官方費率，另外算一次扣成本後
            rf = bt.ma_backtest(s, fee_buy=bt.TW_FEE_BUY, fee_sell=bt.TW_FEE_SELL)
            fee = (f'<tr><td class="muted">　扣台股交易成本後</td><td></td><td></td>'
                   f'<td>{_x(rf["st"]["mult"])}（{rf["st"]["cagr"]:.1f}%）</td><td></td><td></td></tr>')
        ma_rows.append(
            f'<tr><td>{BT_NAMES[code]}<br><span class="muted">{r["start"][:4]}–{r["end"][:4]}</span></td>'
            f'<td class="{"win" if win_bh else ""}">{_x(bh["mult"])}（{bh["cagr"]:.1f}%）</td><td>{bh["mdd"]:.0f}%</td>'
            f'<td class="{"" if win_bh else "win"}">{_x(st["mult"])}（{st["cagr"]:.1f}%）</td><td>{st["mdd"]:.0f}%</td>'
            f'<td>{r["switches"]}</td></tr>{fee}')
    ndc_html = ""
    if has_ndc and series_map.get("taiex"):
        lights = [(m, l, sc) for m, sc, l, *_ in ndc if sc is not None]
        r = bt.ndc_backtest(series_map["taiex"], lights)
        trades = "".join(
            f'<tr><td>{t["buy_month"]} 藍</td><td>{t["buy_date"]}</td><td>{t["buy"]:,.0f}</td>'
            f'<td>{(t["sell_month"] + " 紅") if t["sell_month"] else "—"}</td><td>{t["sell_date"] or "還抱著"}</td>'
            f'<td>{t["sell"]:,.0f}</td><td>{t["ret"]:+.1f}%</td></tr>' for t in r["trades"])
        ll = r["last_light"]
        bh, st = r["bh"], r["st"]
        win_bh = bh["mult"] >= st["mult"]
        done = sum(1 for t in r["trades"] if t["sell_month"])
        ndc_html = f"""<section>
  <h2>景氣燈號：藍燈買、紅燈賣</h2>
  <p>國發會景氣燈號出現藍燈（9–16 分）就全部買進加權指數，出現紅燈（38–45 分）就全部賣出，再等下一次藍燈；一開始是現金。燈號約在下個月 27 日才公布，所以 M 月的燈號用 M+2 月第一個交易日的收盤價進出。</p>
  <div class="tbl"><table>
    <thead><tr><th scope="col">{r["start"][:4]}–{r["end"][:4]}</th><th scope="col">倍數（年化）</th><th scope="col">最大跌幅</th><th scope="col">在市場裡</th></tr></thead>
    <tbody>
      <tr><td>長期持有</td><td class="{"win" if win_bh else ""}">{_x(bh["mult"])}（{bh["cagr"]:.1f}%）</td><td>{bh["mdd"]:.0f}%</td><td>100%</td></tr>
      <tr><td>藍燈買、紅燈賣</td><td class="{"" if win_bh else "win"}">{_x(st["mult"])}（{st["cagr"]:.1f}%）</td><td>{st["mdd"]:.0f}%</td><td>{r["in_mkt"]:.0f}%</td></tr>
    </tbody>
  </table></div>
  <p>{r["years"]:.0f} 年裡只有 {done} 次完整的藍燈買、紅燈賣。燈號反映的是已經發生的景氣，比股市慢：2000 年和 2008 年兩次大跌，這個做法都是抱著股票跌下去，最大跌幅沒有比較小。</p>
  <div class="tbl"><table>
    <thead><tr><th scope="col">買進燈號</th><th scope="col">買進日</th><th scope="col">點數</th><th scope="col">賣出燈號</th><th scope="col">賣出日</th><th scope="col">點數</th><th scope="col">漲跌</th></tr></thead>
    <tbody>{trades}</tbody>
  </table></div>
  <p class="muted">燈號資料到 {ll[0]}（{ll[1]}燈 {ll[2]} 分）。</p>
</section>"""

    body = f"""<div class="doc">
<section>
  <h1>長期來看</h1>
  <p class="lead">這個小工具的出發點：長期持有台灣、美國這樣長期向上的整體市場，加上不被斷頭的資金管理（不借錢、不加槓桿、持股比例放在自己睡得著的程度）。</p>
  <p>網站上的均線、本益比、景氣燈號，是用來看清現在的環境、穩住心情的儀表板，不是進出訊號。下面兩個回測，是為什麼這樣想。這裡沒有分析任何單一個股；這是作者自己的做法，不是給你的投資建議。</p>
</section>
<section>
  <h2>年線進出 vs 長期持有</h2>
  <p>年線進出：收盤在年線（240 個交易日平均）上方就持有，跌破就全部賣出，站回去再買；當天收盤判斷、隔天收盤才動作。長期持有從同一天買進，之後不動。</p>
  <div class="tbl"><table>
    <thead><tr><th scope="col">市場</th><th scope="col">長期持有<br>倍數（年化）</th><th scope="col">最大<br>跌幅</th><th scope="col">年線進出<br>倍數（年化）</th><th scope="col">最大<br>跌幅</th><th scope="col">進出<br>次數</th></tr></thead>
    <tbody>{"".join(ma_rows)}</tbody>
  </table></div>
  <p>看得出來的事：</p>
  <ul>
    <li>年線進出最大的好處是跌得比較淺，大盤的最大跌幅少了三分之一以上。</li>
    <li>代價是幾十年要進出一兩百次以上，每一次都要照規則執行，而且常常剛賣就漲回來。</li>
    <li>報酬：歷史最長的標普 500 是長期持有大幅領先。加權指數不算成本時差不多，扣掉台股的手續費和證交稅後，長期持有明顯領先。那斯達克 100 的資料從 2000 年網路泡沫最高點附近開始，長期持有一開始就跌了八成多，這個起點對長期持有特別不利。</li>
    <li>這裡都沒算股息。長期持有一直在市場裡，領到的股息比較多（台股一年約 3～4%），實際差距比表上更偏向長期持有。</li>
  </ul>
</section>
{ndc_html}
<section>
  <h2>方法和限制</h2>
  <ul>
    <li>只用每天收盤價（價格指數，不含股息），賣出後的現金沒算利息。</li>
    <li>交易成本只對加權指數另外算（手續費 0.1425%、證交稅 0.3%）；美股沒算。</li>
    <li>年化報酬＝期末倍數換算成每年平均。最大跌幅＝資產從前高跌到谷底最深的一次。</li>
    <li>這些是過去的紀錄，不代表之後會一樣。資料截至 {as_of}，每次網站更新都用最新資料重算。</li>
  </ul>
</section>
</div>"""
    nav = "".join(f'<a href="{h}"{" aria-current=page" if cur else ""}>{t}</a>' for h, t, cur in nav_links)
    page = f"""<!doctype html>
<html lang="zh-Hant-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>長期來看（回測）· market_weather</title>
<style>{BT_CSS}.brand .icon{{color:{WEATHER[w]['color']}}}</style>
</head>
<body>
<header class="site-header">
  <div class="hwrap">
    <a class="brand" href="index.html">{ICONS[w]}<span class="sr">今天{w}，回首頁，</span>market_weather</a>
    <nav aria-label="網站導覽">{nav}</nav>
  </div>
</header>
<main>
{body}
</main>
<footer class="site-footer">
  <div class="foot">
    <p>網頁更新於 {built}（台灣時間）。只整理公開行情，用來觀察市場，不是投資建議。</p>
    <p><a href="index.html">回到今天的天氣</a> · <a href="History/index.html">歷史版本</a></p>
    <p><a href="https://robert3h.github.io/lsa711/">Robert Huang 黃星翰</a> 的小工具 · © 2026</p>
  </div>
</footer>
</body>
</html>
"""
    (OUT.parent / "backtest.html").write_text(themed(page), encoding="utf-8", newline="\n")
    return as_of


# ---------- 手冊頁（第 7 輪，2026-11-23）：website/guide.html ----------
# 圖由 工具/publish_guide.py 放進 website/guide/<比例>/01.jpg…（投影片原檔在 說明手冊/，產生器 工具/make_guide_v2.py）。
# 兩個比例都放：依螢幕推薦（手機直拿先 4:5，電腦或橫放先 16:9），「建議」那顆、正在看的那顆用橘底；右邊露出下一張；可全螢幕。
GUIDE_CSS = """.doc.wide{max-width:1040px}
.pick{display:flex;gap:10px;flex-wrap:wrap;margin:4px 0 16px}
.pick button{font:inherit;font-size:15px;min-height:44px;padding:0 16px;border-radius:10px;border:1px solid #3a3d44;background:transparent;color:#c9c7c0;cursor:pointer;display:inline-flex;align-items:center;gap:8px}
.pick button:hover{border-color:#d97757;color:#ecebe6}
.pick button:focus-visible,.ctl button:focus-visible,.strip:focus-visible{outline:2px solid #d97757;outline-offset:2px}
.pick button[aria-pressed=true]{background:#d97757;border-color:#d97757;color:#14161a;font-weight:600}
.pick .rec{font-size:12px;line-height:1.4;padding:0 6px;border-radius:4px;border:1px solid currentColor}
.deck[hidden]{display:none}
.strip{display:flex;gap:12px;overflow-x:auto;scroll-snap-type:x mandatory;scrollbar-width:none;padding-bottom:2px}
.strip::-webkit-scrollbar{display:none}
.strip img{flex:0 0 88%;width:88%;height:auto;scroll-snap-align:start;border-radius:10px;border:1px solid #262930;display:block;background:#1c1f24}
.deck[data-r="4x5"] .strip img{flex-basis:min(88%,440px);width:min(88%,440px)}
.ctl{display:flex;align-items:center;gap:8px;margin-top:10px}
.ctl button{font:inherit;font-size:15px;min-width:44px;min-height:44px;padding:0 12px;border-radius:8px;border:1px solid #3a3d44;background:transparent;color:#ecebe6;cursor:pointer}
.ctl button:hover{border-color:#d97757;color:#d97757}
.ctl .pos{font-variant-numeric:tabular-nums;color:#a7a59e;font-size:14px;margin-right:auto}
.deck:fullscreen{background:#000;display:flex;flex-direction:column;justify-content:center;padding:12px}
.deck:fullscreen .strip img,.deck[data-r="4x5"]:fullscreen .strip img{flex-basis:100%;width:100%;max-height:calc(100vh - 84px);object-fit:contain;border:0;background:#000}
"""

GUIDE_JS = r"""
(function(){
  const picks=[...document.querySelectorAll('.pick button')], decks=[...document.querySelectorAll('.deck')];
  const phone=matchMedia('(orientation: portrait) and (max-width: 820px)').matches;   // 手機直拿：先看 4:5
  const rec=phone?'4x5':'16x9';
  picks.forEach(b=>{ if(b.dataset.r===rec){ b.insertAdjacentHTML('beforeend','<small class="rec">建議</small>'); b.parentNode.prepend(b); } });
  const show=r=>{ picks.forEach(b=>b.setAttribute('aria-pressed',b.dataset.r===r)); decks.forEach(d=>d.hidden=d.dataset.r!==r); };
  picks.forEach(b=>b.addEventListener('click',()=>show(b.dataset.r)));
  show(rec);
  decks.forEach(d=>{
    const strip=d.querySelector('.strip'), imgs=[...strip.children], pos=d.querySelector('.pos');
    const cur=()=>{ let best=0,dist=1e9; imgs.forEach((im,i)=>{ const x=Math.abs(im.offsetLeft-strip.scrollLeft-strip.offsetLeft); if(x<dist){dist=x;best=i;} }); return best; };
    const go=i=>{ i=Math.max(0,Math.min(imgs.length-1,i)); strip.scrollTo({left:imgs[i].offsetLeft-strip.offsetLeft,behavior:'smooth'}); };
    const upd=()=>{ pos.textContent=(cur()+1)+' / '+imgs.length; };
    strip.addEventListener('scroll',()=>{ clearTimeout(strip._t); strip._t=setTimeout(upd,60); });
    d.querySelector('.prev').addEventListener('click',()=>go(cur()-1));
    d.querySelector('.next').addEventListener('click',()=>go(cur()+1));
    strip.addEventListener('keydown',e=>{ if(e.key==='ArrowRight'){go(cur()+1);e.preventDefault();} if(e.key==='ArrowLeft'){go(cur()-1);e.preventDefault();} });
    const fs=d.querySelector('.fs');
    if(!d.requestFullscreen){ fs.hidden=true; }
    // 16:9 全螢幕時要求手機轉成橫向（Screen Orientation API：Android Chrome 支援；iPhone Safari 不支援，失敗就照原樣）
    const lockLand=()=>{ if(d.dataset.r==='16x9' && screen.orientation && screen.orientation.lock) screen.orientation.lock('landscape').catch(()=>{}); };
    const unlock=()=>{ if(screen.orientation && screen.orientation.unlock) try{ screen.orientation.unlock(); }catch(e){} };
    fs.addEventListener('click',()=>{ const i=cur(); (document.fullscreenElement?document.exitFullscreen():d.requestFullscreen().then(lockLand)).then(()=>setTimeout(()=>go(i),200)); });
    // 兩組投影片都會收到 fullscreenchange：只處理自己的。之前 4:5 那組在 16:9 進全螢幕時誤以為「離開了」而解除橫向鎖定，
    // 手機轉一下又轉回來（2026-11-23 用 Android 手機實測發現）。所以記住「自己有沒有在全螢幕」，自己離開時才解除。
    document.addEventListener('fullscreenchange',()=>{
      const on=document.fullscreenElement===d;
      if(on){ d._fs=true; fs.textContent='離開全螢幕'; strip.focus(); }
      else if(d._fs){ d._fs=false; fs.textContent='全螢幕'; unlock(); }
    });
    upd();
  });
})();
"""


DECK_LABEL = {"16x9": ("電腦版 16:9", "頁"), "4x5": ("手機版 4:5", "張")}


def deck_sets(folder):
    """website/<folder>/<比例>/*.jpg（工具/publish_guide.py 放的）。"""
    base = OUT.parent / folder
    return {r: sorted((base / r).glob("*.jpg")) for r in DECK_LABEL}


def deck_pair(folder, name, alts=None, eager=2):
    """兩顆切換按鈕＋兩組投影片的 HTML。alts＝{比例: 每張的替代文字}，張數要和圖一樣，不一樣就停下來（換了投影片忘了改文字）。"""
    sets = deck_sets(folder)
    for r, v in sets.items():
        if v and alts and len(alts[r]) != len(v):
            raise SystemExit(f"{folder}/{r} 有 {len(v)} 張，替代文字有 {len(alts[r])} 句，要一樣（改 scripts/logbook_data.py 的 DECK_ALTS）")
    picks = "".join(f'<button type="button" data-r="{r}" aria-pressed="false">{DECK_LABEL[r][0]} · {len(v)} {DECK_LABEL[r][1]}</button>'
                    for r, v in sets.items() if v)
    alt = lambda r, i: alts[r][i - 1] if alts else f"{name} {DECK_LABEL[r][0]} 第 {i} {DECK_LABEL[r][1]}"
    decks = "".join(
        f'<div class="deck" data-r="{r}"><div class="strip" tabindex="0" aria-label="{name} {DECK_LABEL[r][0]}，左右滑動">'
        + "".join(f'<img src="{folder}/{r}/{f.name}" alt="{html.escape(alt(r, i))}" loading="{"eager" if i <= eager else "lazy"}">'
                  for i, f in enumerate(v, 1))
        + '</div><div class="ctl"><button type="button" class="prev" aria-label="上一張">‹</button>'
          f'<span class="pos" aria-live="polite">1 / {len(v)}</span>'
          '<button type="button" class="next" aria-label="下一張">›</button><button type="button" class="fs">全螢幕</button></div></div>'
        for r, v in sets.items() if v)
    return f'<div class="pick" role="group" aria-label="選擇版本">{picks}</div>\n  {decks}'


def guide_page(w, built, nav_links):
    """手冊頁：16:9、4:5 兩組使用教學。website/guide/ 沒有圖就不產生，回傳 False。"""
    if not any(deck_sets("guide").values()):
        return False
    body = f"""<div class="doc wide">
<section>
  <h1>使用教學</h1>
  <p>雙宇和子琦帶你看懂這個網站：今天的天氣、指標、走勢圖、比較和回測頁怎麼看。</p>
  {deck_pair("guide", "使用教學")}
  <p class="muted">想知道這個小工具是怎麼一步步改出來的？看<a href="logbook.html">維修日誌</a>。</p>
</section>
</div>"""
    page = doc_shell("使用教學 · market_weather", body, w, built, nav_links, GUIDE_CSS, GUIDE_JS)
    (OUT.parent / "guide.html").write_text(themed(page), encoding="utf-8", newline="\n")
    return True


def doc_shell(title, body, w, built, nav_links, extra_css="", extra_js=""):
    """回測頁、手冊頁共用的外框：釘選頁首＋頁尾，樣式和首頁一致。"""
    nav = "".join(f'<a href="{h}"{" aria-current=page" if cur else ""}>{t}</a>' for h, t, cur in nav_links)
    js = f"<script>{extra_js}</script>" if extra_js else ""
    return f"""<!doctype html>
<html lang="zh-Hant-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{BT_CSS}{extra_css}.brand .icon{{color:{WEATHER[w]['color']}}}</style>
</head>
<body>
<header class="site-header">
  <div class="hwrap">
    <a class="brand" href="index.html">{ICONS[w]}<span class="sr">今天{w}，回首頁，</span>market_weather</a>
    <nav aria-label="網站導覽">{nav}</nav>
  </div>
</header>
<main>
{body}
</main>
<footer class="site-footer">
  <div class="foot">
    <p>網頁更新於 {built}（台灣時間）。只整理公開行情，用來觀察市場，不是投資建議。</p>
    <p><a href="index.html">回到今天的天氣</a> · <a href="guide.html">使用教學</a> · <a href="logbook.html">維修日誌</a> · <a href="History/index.html">歷史版本</a></p>
    <p><a href="https://robert3h.github.io/lsa711/">Robert Huang 黃星翰</a> 的小工具 · © 2026</p>
  </div>
</footer>
{js}
</body>
</html>
"""


# ---------- 維修日誌頁（第 8 輪，2026-11-23）：website/logbook.html ----------
# 照 LSA 網站 /logbook/ 的做法：三個設計初衷，每張改裝卡分「讀者遇到什麼／○ 選擇這樣做／✕ 選擇不做」，加上 Robert 當天說的話。
# 卡片文字只寫在 scripts/logbook_data.py（投影片產生器 工具/make_logbook_v*.py 也讀它）。
# 卡片用 <details> 收折：平常只露編號、日期、標題，一眼掃完全部改裝；沒有 JS 也能開合，網址帶 #mx-… 時自動打開那一張。
import logbook_data as LBD   # noqa: E402

LB_CSS = """.log{font-family:Consolas,"Cascadia Mono",monospace;font-size:12px;letter-spacing:.08em;color:#8f8d87}
.lb-head h1{margin-top:6px}
.lb-stack{list-style:none;padding:0;margin:16px 0 0;display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.lb-stack a{display:flex;flex-direction:column;gap:4px;padding:14px 16px;border:1px solid #3a3d44;border-radius:12px;text-decoration:none;color:#ecebe6;height:100%}
.lb-stack a:hover{border-color:#d97757}
.lb-stack b{font-size:17px}
.lb-stack span:last-child{font-size:13px;line-height:1.6;color:#a7a59e}
.quick{margin:14px 0 0;font-size:14px}
.quick a{display:inline-flex;align-items:center;min-height:44px}
section h2 .log{margin-right:6px}
.sub{font-size:14px;color:#a7a59e;margin:0 0 4px}
.mx-list{list-style:none;padding:0;margin:10px 0 0;display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px;align-items:start}
.mx-card{border:1px solid #2c2f36;border-radius:12px;background:#181a1f}
.mx-card:hover{border-color:#d97757}
.mx-card:target{border-color:#d97757;box-shadow:0 0 0 1px #d97757}
.mx-card summary{list-style:none;cursor:pointer;padding:12px 44px 12px 16px;position:relative;min-height:44px;border-radius:12px}
.mx-card summary::-webkit-details-marker{display:none}
.mx-card summary::after{content:"+";position:absolute;right:16px;top:10px;font-size:22px;line-height:1.2;color:#a7a59e}
.mx-card[open] summary::after{content:"−"}
.mx-card summary:focus-visible{outline:2px solid #d97757;outline-offset:2px}
.mx-card h3{margin:4px 0 0;font-size:16px;line-height:1.45;font-weight:600}
.mx-body{padding:0 16px 16px}
.mx-dl{margin:0;display:flex;flex-direction:column;gap:10px}
.mx-dl dt{font-size:12px;color:#8f8d87;margin-bottom:2px}
.mx-dl dd{margin:0;font-size:14px;line-height:1.75;color:#c9c7c0}
.mx-mine dt{color:#d97757}
.mx-quote{margin:12px 0 0;padding:10px 12px;border-left:3px solid #d97757;background:#1f2126;border-radius:0 8px 8px 0}
.mx-quote p{margin:0;font-size:14px;color:#ecebe6}
.mx-quote footer{margin-top:2px}
.mx-go{display:inline-flex;align-items:center;min-height:44px;margin-top:4px;font-size:14px}
.lb-ground{list-style:none;padding:0;margin:10px 0 0;display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px 20px}
.lb-ground li{border-top:1px solid #2c2f36;padding-top:10px}
.lb-ground h3{font-size:15px;margin:4px 0 4px}
.lb-ground p{font-size:13px;line-height:1.7;margin:0}
@media (max-width:640px){.lb-stack{grid-template-columns:1fr}}
"""


def _mx_card(c):
    k, mx, date, title, reader, mine, no, quote, href, go = c
    E = html.escape
    q = (f'<blockquote class="mx-quote"><p>「{E(quote)}」</p><footer class="log">改裝當天 Robert 說的話</footer></blockquote>'
         if quote else "")
    link = f'<a class="mx-go" href="{href}">{E(go)} →</a>' if href else ""
    return f"""<li><details class="mx-card" id="{mx.lower()}">
  <summary><span class="log">{mx} · {date}</span><h3>{E(title)}</h3></summary>
  <div class="mx-body"><dl class="mx-dl">
    <div><dt>{E(LBD.LABELS['reader'])}</dt><dd>{E(reader)}</dd></div>
    <div class="mx-mine"><dt>{E(LBD.LABELS['mine'])}</dt><dd>{E(mine)}</dd></div>
    <div><dt>{E(LBD.LABELS['no'])}</dt><dd>{E(no)}</dd></div>
  </dl>{q}{link}</div>
</details></li>"""


def logbook_page(w, built, nav_links):
    E = html.escape
    sets = deck_sets("logbook")
    has_deck = any(sets.values())
    stack = "".join(f'<li><a href="#{k}"><span class="log">初衷 {n}</span><b>{E(name)}</b><span>{E(short)}</span></a></li>'
                    for k, n, name, short, _ in LBD.PRINCIPLES)
    quick = ""
    if has_deck:
        counts = "、".join(f"{DECK_LABEL[r][0]} · {len(v)} {DECK_LABEL[r][1]}" for r, v in sets.items() if v)
        quick = f'<p class="quick">字太多了嗎？<a href="#deck">看機長和座艙長的投影片版（{counts}）↓</a></p>'
    sections = "".join(f"""
<section aria-labelledby="{k}">
  <h2 id="{k}"><span class="log">初衷 {n}</span>{E(name)}</h2>
  <p class="sub">{E(short)}：{E(longer)}</p>
  <ul class="mx-list">{"".join(_mx_card(c) for c in LBD.CARDS if c[0] == k)}</ul>
</section>""" for k, n, name, short, longer in LBD.PRINCIPLES)
    ground = "".join(f'<li><p class="log">{mx}</p><h3>{E(t)}</h3><p>{E(d)}</p></li>' for mx, t, d in LBD.GROUND)
    deck = (f"""
<section aria-labelledby="deck">
  <h2 id="deck">投影片版</h2>
  <p class="sub">同一份維修日誌做成簡報：子琦廣播帶路，雙宇引用 Robert 改裝當天說的話。</p>
  {deck_pair("logbook", "維修日誌", {"16x9": LBD.DECK_ALTS, "4x5": LBD.DECK_ALTS45}, eager=0)}
</section>""" if has_deck else "")
    first = min(c[2] for c in LBD.CARDS)
    body = f"""<div class="doc wide">
<header class="lb-head">
  <p class="log">SW711 TECH LOG // 維修日誌</p>
  <h1>這個小工具是怎麼改出來的</h1>
  <p class="lead">從 {first} 開工到現在，進廠改裝過 {len(LBD.CARDS)} 次。每一次，都在回答同樣三件事。</p>
  <ol class="lb-stack">{stack}</ol>
  {quick}
</header>
{sections}
<section aria-labelledby="ground">
  <h2 id="ground"><span class="log">GROUND CREW</span>看不見的地勤</h2>
  <p class="sub">讀者看不到這些，但每天準時更新，都靠它們。</p>
  <ul class="lb-ground">{ground}</ul>
</section>
{deck}
</div>"""
    page = doc_shell("維修日誌 · market_weather", body, w, built, nav_links, GUIDE_CSS + LB_CSS, GUIDE_JS + OPEN_HASH_JS)
    (OUT.parent / "logbook.html").write_text(themed(page), encoding="utf-8", newline="\n")


# 網址帶 #某個 <details> 的 id 時打開它（首頁 #i-<代號>、維修日誌 #mx-…）：「去看看」一點就落在那個功能上，不用自己找、自己點開
OPEN_HASH_JS = r"""
(function(){
  const open=()=>{ const id=decodeURIComponent(location.hash.slice(1)), t=id&&document.getElementById(id);
    if(t&&t.tagName==='DETAILS'&&!t.open){ t.open=true; requestAnimationFrame(()=>t.scrollIntoView()); } };
  open(); addEventListener('hashchange',open);
})();
"""


# ---------- 深色（預設）／淺色主題（2026-11-23）----------
# 配色照 網站配色圖卡/網站配色圖卡_20261020.md（LSA 網站同一套）：淺色底白、主要文字 #1B1C1E、橘 #B4532E。
# 做法：程式裡照舊寫深色的色碼；寫出每一頁之前，themed() 把這些色碼換成 CSS 變數，深淺兩組值放在 :root。
#   切換時顏色直接跟著變，走勢圖不用重畫。SVG 的 fill／stroke 屬性不吃變數，所以畫圖的 el() 和靜態小走勢圖改用 style。
#   選擇存在瀏覽器的 localStorage「theme」：和 LSA 網站同一個網域、同一個名稱，在 LSA 選了淺色，這裡也是淺色。
# 投影片是圖片，維持深色。景氣燈號五個燈的顏色兩種主題都一樣（它們是燈號本身的顏色）。
PALETTE = {   # 深色色碼: (變數名稱, 淺色色碼)
    "#14161a": ("bg", "#ffffff"),
    "#181a1f": ("surface", "#f6f3f1"),
    "#1c1f24": ("surface2", "#f6f3f1"),
    "#1f2126": ("surface3", "#f0ece9"),
    "#1f2227": ("rule0", "#eceef0"),
    "#262930": ("rule", "#e3e5e8"),
    "#2c2f36": ("rule2", "#dfe1e5"),
    "#3a3d44": ("rule3", "#c4c7cc"),
    "#5a5d64": ("faint", "#9aa0a6"),
    "#8f8d87": ("muted2", "#686c71"),
    "#a7a59e": ("muted", "#5f6368"),
    "#c9c7c0": ("ink2", "#3c4043"),
    "#ecebe6": ("ink", "#1b1c1e"),
    "#d97757": ("accent", "#b4532e"),
    "#eb9474": ("accent-strong", "#8f3f20"),
    "#ef6a5f": ("up", "#c5302a"),        # 紅漲：白底上要深一點才看得清楚
    "#3fb27f": ("down", "#18794e"),      # 綠跌
    "#6b9bf0": ("rain", "#2a62c9"),
    "#e0a95a": ("ma60", "#a96d12"),
    "#7fa7d9": ("ma240", "#3b6aa8"),
    "#5fc4b8": ("teal", "#12806f"),
    "#d58fb5": ("pink", "#a8487c"),
}
THEME_CSS = (":root{color-scheme:dark;" + "".join(f"--c-{n}:{d};" for d, (n, _) in PALETTE.items()) + "}"
             ':root[data-theme="light"]{color-scheme:light;' + "".join(f"--c-{n}:{l};" for _, (n, l) in PALETTE.items()) + "}"
             ".theme-toggle{font:inherit;font-size:13px;color:var(--c-muted);background:transparent;border:1px solid var(--c-rule3);"
             "border-radius:6px;padding:0 10px;min-height:32px;margin-left:8px;cursor:pointer;white-space:nowrap;flex:none}"
             ".theme-toggle:hover{color:var(--c-accent);border-color:var(--c-accent)}"
             ".theme-toggle:focus-visible{outline:2px solid var(--c-accent);outline-offset:2px}"
             "@media (max-width:480px){.theme-toggle{order:2;margin-left:auto;min-height:30px}.site-header nav{order:3}}")
THEME_HEAD = ("<script>/* 畫面出現前套用上次選的主題，避免閃一下 */try{if(localStorage.getItem('theme')==='light')"
              "document.documentElement.setAttribute('data-theme','light');}catch(e){}</script>")
THEME_JS = """<script>
(function(){
  const root=document.documentElement, b=document.querySelector('.theme-toggle'); if(!b) return;
  const paint=()=>{ const dark=root.getAttribute('data-theme')!=='light'; b.textContent=dark?'切換淺色':'切換深色'; b.setAttribute('aria-pressed',String(!dark)); };
  b.addEventListener('click',()=>{ const next=root.getAttribute('data-theme')==='light'?'dark':'light'; root.setAttribute('data-theme',next);
    try{ localStorage.setItem('theme',next); }catch(e){} paint(); });
  paint();
})();
</script>"""


def themed(page):
    for dark, (name, _) in PALETTE.items():
        page = re.sub(dark, f"var(--c-{name})", page, flags=re.I)
    page = re.sub(r'\b(fill|stroke)="(var\(--c-[a-z0-9-]+\))"', r'style="\1:\2"', page)   # 靜態 SVG（小走勢圖）
    page = page.replace("</head>", f"<style>{THEME_CSS}</style>\n{THEME_HEAD}\n</head>", 1)
    page = re.sub(r'(<nav aria-label="(?:網站導覽|跳到這一頁的區塊)">.*?</nav>)', r'\1\n    <button type="button" class="theme-toggle" aria-pressed="false">切換淺色</button>', page, count=1, flags=re.S)
    return page.replace("</body>", THEME_JS + "\n</body>", 1)


# ---------- 組頁面 ----------
def build():
    series_map = {code: load(code) for code, *_ in ROWS}
    missing = [name for code, name, *_ in ROWS if not series_map[code]]
    hist = weather_history(series_map["taiex"], series_map["spx"], series_map["vix"])
    if not hist:
        raise SystemExit("台股、標普或 VIX 沒有資料，無法判斷晴雨。請先跑 fetch_quotes.py")

    # 標題用「台股、美股各自最新的收盤」：週六早上美股週五收盤已出來，不能還停在週四。
    # 日曆（每個台股交易日配前一晚美股）照舊，所以日曆最後一格可能和標題不同。
    tx, sx, vx = series_map["taiex"], series_map["spx"], series_map["vix"]
    today = {"date": tx[-1][0], "us_date": sx[-1][0], "tw": pct(tx, len(tx) - 1),
             "spx": pct(sx, len(sx) - 1), "vix": vx[-1][1]}
    today["w"] = w = judge_day(today["tw"], today["spx"], today["vix"])
    streak = 0
    for d in reversed(hist):
        if d["w"] != w:
            break
        streak += 1
    streak = max(streak, 1)
    streak_txt = f"連續第 {streak} 個{w}天。" if streak > 1 else ""

    tw_last = series_map["taiex"][-1][0]
    us_last = series_map["spx"][-1][0]
    built = dt.datetime.now(TPE).strftime("%Y-%m-%d %H:%M")

    # 天氣日曆
    cal = hist[-CALENDAR_DAYS:]
    counts = {k: sum(1 for d in cal if d["w"] == k) for k in "晴陰雨"}
    cells = "".join(
        f'<span class="cell{" today" if i == len(cal) - 1 else ""}" style="background:{WEATHER[d["w"]]["cell"]}" '
        f'title="{md(d["date"])} {d["w"]}" role="img" aria-label="{md(d["date"])} {d["w"]}"></span>'
        for i, d in enumerate(cal))

    # 值得注意
    notes = notable(series_map)
    notes_html = ""
    if notes:
        notes_html = '<section class="block"><h2>今天值得注意</h2>' + "".join(f"<p>{html.escape(n)}</p>" for n in notes) + "</section>"

    # 指標列表
    groups_html = []
    newest = {"台股": tw_last, "美股": us_last, "風向": us_last, "其他資產": us_last}
    for group in ("台股", "美股", "風向", "其他資產"):
        rows_html = []
        for code, name, sub, g, kind in ROWS:
            if g != group:
                continue
            s = series_map[code]
            if not s:
                rows_html.append(f'<div class="row nodata"><span class="name">{name}<small>{sub}</small></span><span class="stale">抓不到資料</span></div>')
                continue
            chg, color = fmt_change(code, kind, s)
            spark = spark_points([v for _, v in s[-SPARK_DAYS:]])
            stale = ""
            if s[-1][0] < newest[group]:
                stale = f'<small class="stale">資料停在 {md(s[-1][0])}</small>'
            rows_html.append(f"""<details id="i-{code}">
  <summary class="row">
    <span class="name">{name}<small>{sub}</small>{stale}</span>
    <span class="value">{fmt_value(code, s[-1][1])}</span>
    <span class="chg" style="color:{color}">{chg}</span>
    <svg class="spark" viewBox="0 0 84 28" preserveAspectRatio="none" aria-hidden="true"><polyline points="{spark}" stroke="{color}" stroke-width="1.4" fill="none" vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>
  </summary>
  {chart_block(code, kind)}
  {facts_block(code, kind, s)}
</details>""")
            write_series(code, s)
            if PE_FOR.get(code) and load(PE_FOR[code]):   # 本益比也寫一份，點「本益比」時才載入
                pe_code = PE_FOR[code]
                extra = {"monthly": 1} if pe_code == "spx_pe" else {}   # 每月資料
                est = first_estimate(pe_code)
                if est:   # 記下從哪天起是估計值，圖上才能標「估計」
                    extra["est"] = (dt.date.fromisoformat(est) - EPOCH).days
                write_series(pe_code, load(pe_code), extra)
        anchor = f' id="{GROUP_IDS[group]}"'   # 台股＝#list（頁首「指標」跳到第一組）；其他組給維修日誌的「去看看」用
        groups_html.append(f'<section class="group"{anchor}><h2>{group}</h2>{"".join(rows_html)}</section>')
    ndc = load_ndc()
    groups_html.insert(0, f'<section class="group" id="compare"><h2>比較</h2>{compare_block(bool(ndc))}</section>')
    if ndc:   # 國發會景氣燈號（每月）
        write_ndc_series(ndc)
        groups_html.append(f'<section class="group" id="ndc"><h2>景氣（每月）</h2>{ndc_row(ndc)}</section>')

    nav = [("today", "今天"), ("compare", "比較"), ("list", "指標")] + ([("ndc", "景氣")] if ndc else [])
    pages = [("backtest.html", "回測")] + ([("guide.html", "手冊")] if any((OUT.parent / "guide").glob("*/*.jpg")) else [])
    nav_html = "".join(f'<a href="#{a}">{t}</a>' for a, t in nav) + "".join(f'<a href="{h}">{t}</a>' for h, t in pages)
    links = lambda here: [(f"index.html#{a}", t, False) for a, t in nav] + [(h, t, h == here) for h, t in pages]
    backtest_page(series_map, ndc, w, built, links("backtest.html"), bool(ndc))
    guide_page(w, built, links("guide.html"))
    logbook_page(w, built, links("logbook.html"))
    missing_html = f'<p class="warn">抓不到：{"、".join(missing)}</p>' if missing else ""
    ma_js = json.dumps([{"n": n, "color": col} for n, col, _ in MA_LINES])
    cmp_meta = {code: {"name": name, "kind": kind, "dg": DIGITS.get(code, 2), "suf": "%" if code in YIELDS else ""}
                for code, name, _, _, kind in ROWS if series_map[code]}
    if ndc:
        cmp_meta["ndc"] = {"name": "景氣燈號", "kind": "score", "dg": 0, "suf": " 分"}
    cmp_meta = json.dumps(cmp_meta, ensure_ascii=False)
    events = load_events()
    events_js = json.dumps([[(dt.date.fromisoformat(d) - EPOCH).days, mk, t, u] for d, mk, t, u in events], ensure_ascii=False)
    chart_js = CHART_JS.replace("__BUILD__", dt.datetime.now(TPE).strftime("%Y%m%d%H%M"))   # 讓瀏覽器拿到最新的資料檔

    page = f"""<!doctype html>
<html lang="zh-Hant-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{w} · market_weather {md(today['date'])}</title>
<style>
*{{box-sizing:border-box}}
html{{scroll-padding-top:64px}}
@media (max-width:480px){{html{{scroll-padding-top:92px}}}}
body{{margin:0;background:#14161a;color:#ecebe6;font-family:"Noto Sans TC","Microsoft JhengHei","PingFang TC",system-ui,sans-serif;-webkit-text-size-adjust:100%}}
.wrap{{max-width:1180px;margin:0 auto;padding:1.5rem 1rem 3rem;display:flex;flex-direction:column;gap:24px}}
.site-header{{position:sticky;top:0;z-index:20;background:#14161a;border-bottom:1px solid #262930}}
.hwrap{{max-width:1180px;margin:0 auto;padding:8px 1rem;display:flex;align-items:center;gap:4px 16px}}
.brand{{display:inline-flex;align-items:center;gap:8px;min-height:40px;color:#ecebe6;text-decoration:none;font-size:15px;font-weight:600;letter-spacing:.04em}}
.brand .icon{{width:22px;height:22px;flex:none;color:{WEATHER[w]['color']}}}
.brand:hover{{color:#d97757}}
.brand:focus-visible,.site-header nav a:focus-visible{{outline:2px solid #d97757;outline-offset:2px;border-radius:4px}}
.site-header nav{{margin-left:auto;display:flex;gap:2px}}
.site-header nav a{{display:inline-flex;align-items:center;min-height:40px;padding:0 10px;color:#a7a59e;text-decoration:none;font-size:14px;white-space:nowrap}}
.site-header nav a:hover{{color:#d97757;text-decoration:underline;text-underline-offset:4px}}
.sr{{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}}
.site-footer{{border-top:1px solid #262930}}
.site-footer .foot{{max-width:1180px;margin:0 auto;padding:20px 1rem 40px;display:flex;flex-direction:column;gap:6px}}
.site-footer p{{margin:0}}
.cols{{display:flex;flex-wrap:wrap;gap:40px;align-items:flex-start}}
aside{{flex:1 1 300px;max-width:400px;display:flex;flex-direction:column;gap:22px;min-width:0}}
main{{flex:999 1 520px;min-width:0;display:flex;flex-direction:column;gap:22px}}
.hero{{display:flex;align-items:center;gap:16px;color:{WEATHER[w]['color']}}}
.hero .icon{{width:76px;height:76px;flex:none}}
.hero .word{{font-size:76px;font-weight:700;line-height:1}}
.why{{margin:0;font-size:16px;line-height:1.6;color:#c9c7c0}}
.dates{{margin:4px 0 0;font-size:13px;color:#8f8d87}}
.block{{border-top:1px solid #262930;padding-top:14px;display:flex;flex-direction:column;gap:6px}}
.block p{{margin:0;font-size:15px;line-height:1.6}}
h2{{margin:0;font-size:13px;font-weight:600;color:#a7a59e;letter-spacing:.06em}}
.calhead{{display:flex;justify-content:space-between;align-items:baseline;font-size:12px;color:#8f8d87}}
.cal{{display:grid;grid-template-columns:repeat(10,minmax(0,1fr));gap:4px;margin-top:4px}}
.cell{{aspect-ratio:1;border-radius:4px}}
.cell.today{{outline:2px solid #ecebe6;outline-offset:1px}}
.group h2{{margin-bottom:6px}}
details{{border-top:1px solid #262930}}
summary{{list-style:none;cursor:pointer}}
summary::-webkit-details-marker{{display:none}}
.row{{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(0,1.1fr) minmax(0,.9fr) minmax(0,1.2fr);align-items:center;gap:12px;min-height:52px;padding:4px 6px;border-radius:4px}}
summary.row:hover{{background:#1c1f24}}
summary.row:focus-visible{{outline:2px solid #d97757;outline-offset:-2px}}
details[open] summary.row{{background:#1c1f24}}
.name{{display:flex;flex-direction:column;font-size:15px}}
.name small{{font-size:12px;color:#8f8d87}}
.name small.stale,.stale{{color:#e0a95a}}
.value{{font-size:17px;font-weight:600;text-align:right;font-variant-numeric:tabular-nums}}
.chg{{font-size:14px;text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
.spark{{width:100%;height:30px}}
.chart{{margin:8px 6px 16px}}
.ranges{{display:flex;flex-wrap:wrap;gap:4px}}
.modes{{display:inline-flex;margin-bottom:8px;border:1px solid #3a3d44;border-radius:8px;overflow:hidden}}
.modes button{{font:inherit;font-size:13px;min-height:34px;padding:0 16px;border:0;background:transparent;color:#c9c7c0;cursor:pointer}}
.modes button+button{{border-left:1px solid #3a3d44}}
.modes button:hover{{color:#ecebe6;background:#1c1f24}}
.modes button:focus-visible{{outline:2px solid #d97757;outline-offset:-2px}}
.modes button[aria-pressed=true]{{background:#d97757;color:#14161a;font-weight:600}}
.ranges button{{font:inherit;font-size:13px;min-height:32px;min-width:44px;padding:0 8px;border-radius:16px;border:1px solid #3a3d44;background:transparent;color:#c9c7c0;cursor:pointer}}
.ranges button:hover{{border-color:#8f8d87;color:#ecebe6}}
.ranges button:focus-visible{{outline:2px solid #d97757;outline-offset:1px}}
.ranges button[aria-pressed=true]{{background:#ecebe6;border-color:#ecebe6;color:#14161a;font-weight:600}}
.readout{{margin:10px 0 4px;font-size:14px;min-height:1.6em;font-variant-numeric:tabular-nums;color:#c9c7c0}}
.readout b{{font-weight:600}}
.nw{{white-space:nowrap}}
.evline{{margin:4px 0 0;font-size:13px;line-height:1.6;min-height:1.6em;color:#8f8d87}}
.evline.on{{color:#ecebe6}}
.evline a{{color:#d97757;display:inline-flex;align-items:center;min-height:32px}}
.legend{{margin:6px 0 0;font-size:12px;color:#8f8d87;display:flex;gap:14px;flex-wrap:wrap}}
.legend[hidden]{{display:none}}
.legend i.sw{{width:10px;height:10px;border:0;border-radius:2px;vertical-align:-1px}}
.chg .sub{{display:block;font-size:12px;color:#8f8d87}}
.ndc{{margin:8px 6px 16px}}
.ddbtn{{font:inherit;font-size:inherit;background:none;border:0;padding:4px 0;min-height:32px;color:#d97757;cursor:pointer;text-decoration:underline dotted;text-underline-offset:4px;white-space:nowrap}}
.ddbtn:hover{{color:#eb9474}}
.ddbtn[aria-pressed=true]{{font-weight:700;text-decoration-style:solid}}
.ddbtn:focus-visible{{outline:2px solid #d97757;outline-offset:2px}}
tr.inside td{{white-space:normal;font-size:12px;line-height:1.6;color:#8f8d87;padding-top:0}}
.facts{{margin:0 6px 20px;display:flex;flex-direction:column;gap:16px}}
.fact h3{{margin:0 0 6px;font-size:13px;font-weight:600;color:#a7a59e}}
.fact p{{margin:0;font-size:14px;line-height:1.7;color:#c9c7c0}}
.fact b{{font-weight:600}}
.range{{display:grid;grid-template-columns:1fr 1fr;row-gap:6px;font-size:14px;font-variant-numeric:tabular-nums}}
.range .bar{{grid-column:1/3;position:relative;height:6px;border-radius:3px;background:#2c2f36;margin:6px 0}}
.range .bar i{{position:absolute;top:-5px;width:4px;height:16px;margin-left:-2px;border-radius:2px;background:#ecebe6}}
.range small{{display:block;font-size:12px;color:#8f8d87}}
.range .hi{{text-align:right}}
.tbl{{overflow-x:auto;margin-top:8px}}
.tbl table{{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums;white-space:nowrap}}
.tbl th{{font-weight:600;color:#8f8d87;text-align:right;padding:6px 8px;border-bottom:1px solid #262930}}
.tbl th:first-child,.tbl td:first-child{{text-align:left;padding-left:0}}
.tbl td{{padding:7px 8px;border-bottom:1px solid #1f2227;color:#c9c7c0}}
.tbl td.num{{text-align:right}}
.tbl tr.now td{{color:#ecebe6;font-weight:600}}
.cmp{{border-top:1px solid #262930;padding:10px 6px 8px}}
.cmp .hint{{margin:0 0 8px;font-size:13px;color:#a7a59e}}
.cmp .open{{font:inherit;font-size:14px;min-height:40px;padding:0 16px;border-radius:8px;border:1px solid #d97757;background:transparent;color:#ecebe6;cursor:pointer}}
.cmp .open span{{margin-left:6px;color:#d97757}}
.cmp .open:hover{{background:#1c1f24}}
.cmp .open:focus-visible{{outline:2px solid #d97757;outline-offset:1px}}
.cmp .open[aria-expanded=true]{{border-color:#3a3d44;color:#a7a59e;margin-bottom:10px}}
.cmp .body[hidden]{{display:none}}
.row.one{{grid-template-columns:1fr}}
.chips{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}}
.chips button{{font:inherit;font-size:13px;min-height:34px;padding:0 12px;border-radius:17px;border:1px solid #3a3d44;background:transparent;color:#a7a59e;cursor:pointer}}
.chips button::before{{content:"";display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:var(--c,transparent);box-shadow:inset 0 0 0 1px #5a5d64;vertical-align:1px}}
.chips button:hover{{border-color:#8f8d87;color:#ecebe6}}
.chips button:focus-visible{{outline:2px solid #d97757;outline-offset:1px}}
.chips button[aria-pressed=true]{{color:#ecebe6;border-color:#8f8d87;background:#1c1f24}}
.chips button[aria-pressed=true]::before{{box-shadow:none}}
.chips button:disabled{{opacity:.4;cursor:not-allowed}}
.note{{margin:8px 0 0;font-size:12px;line-height:1.6;color:#8f8d87}}
.legend i{{display:inline-block;width:18px;border-top:2px dashed;vertical-align:middle;margin-right:5px}}
.plot svg{{display:block;width:100%;height:220px;touch-action:pan-y;cursor:crosshair}}
.plot text{{font-size:11px;fill:#8f8d87;font-variant-numeric:tabular-nums}}
.foot{{font-size:12px;color:#8f8d87;line-height:1.7}}
.foot details{{border:0}}
.foot summary{{display:inline-block;min-height:24px;color:#a7a59e;text-decoration:underline;text-underline-offset:3px}}
.foot summary:hover{{color:#ecebe6}}
.foot summary:focus-visible{{outline:2px solid #d97757}}
.foot a{{display:inline-block;min-height:24px;color:#a7a59e;text-underline-offset:3px}}
.foot a:hover{{color:#ecebe6}}
.foot a:focus-visible{{outline:2px solid #d97757}}
.warn{{color:#e0a95a;font-size:13px;margin:0}}
@media (max-width:480px){{
  .row{{grid-template-columns:minmax(0,1.4fr) minmax(0,1fr) minmax(0,.9fr);min-height:56px}}
  .spark{{display:none}}
  .ranges{{display:grid;grid-template-columns:repeat(9,minmax(0,1fr));gap:3px}}
  .ranges.r6{{grid-template-columns:repeat(6,minmax(0,1fr))}}
  .ranges button{{min-width:0;padding:0;font-size:12px;min-height:34px}}
  .hero .word{{font-size:64px}}
  .hero .icon{{width:64px;height:64px}}
  .hwrap{{padding:4px 12px 0;gap:0;flex-wrap:wrap}}
  .tbl table{{font-size:12px}}
  .tbl th,.tbl td{{padding:6px 4px}}
  .brand{{font-size:14px;gap:6px;min-height:36px}}
  .site-header nav{{margin-left:0;width:100%;justify-content:space-between}}
  .site-header nav a{{padding:0 4px}}
}}
</style>
</head>
<body id="top">
<header class="site-header">
  <div class="hwrap">
    <a class="brand" href="#top">{ICONS[w]}<span class="sr">今天{w}，</span>market_weather</a>
    <nav aria-label="跳到這一頁的區塊">{nav_html}</nav>
  </div>
</header>
<div class="wrap">
  {missing_html}
  <div class="cols">
    <aside id="today">
      <div>
        <div class="hero">{ICONS[w]}<span class="word">{w}</span></div>
      </div>
      <div>
        <p class="why">{html.escape(reason_text(today))}{streak_txt}</p>
        <p class="dates">台股 {md(today['date'])} 收盤 · 美股 {md(today['us_date'])} 收盤</p>
      </div>
      {notes_html}
      <section class="block">
        <div class="calhead"><h2>最近 {len(cal)} 個交易日</h2><span>晴 {counts['晴']} · 陰 {counts['陰']} · 雨 {counts['雨']}</span></div>
        <div class="cal">{cells}</div>
      </section>
    </aside>
    <main>
      {"".join(groups_html)}
    </main>
  </div>
</div>
<footer class="site-footer">
  <div class="foot">
    <p>點任一行展開走勢，可切換 5 天到最長；手指或滑鼠移到圖上看當天數字。小走勢圖是近 3 個月。資料來源：{SOURCES_TEXT}。</p>
    <details>
      <summary>晴雨怎麼判斷（草案）</summary>
      <p style="margin:6px 0 0">晴：台股和前一晚標普 500 都收紅，而且 VIX 低於 20。<br>雨：台股或標普 500 有一個跌超過 1%，或 VIX 高於 25。<br>陰：其他情況。</p>
    </details>
    <p><a href="guide.html">使用教學</a>（雙宇和子琦帶你看懂這個網站）</p>
    <p><a href="backtest.html">長期來看：回測</a>（年線進出、景氣燈號 vs 長期持有）</p>
    <p><a href="logbook.html">維修日誌</a>（這個小工具每一次改了什麼、為什麼這樣改）</p>
    <p><a href="History/index.html">歷史版本</a>（每次改版前存一份當時的畫面）</p>
    <p>網頁更新於 {built}（台灣時間）。只整理公開行情，用來觀察市場，不是投資建議。</p>
    <p><a href="https://robert3h.github.io/lsa711/">Robert Huang 黃星翰</a> 的小工具 · © 2026</p>
  </div>
</footer>
<noscript><p class="warn" style="text-align:center">走勢圖需要開啟 JavaScript。</p></noscript>
<script>
const UP="{UP}", DN="{DN}", NEUTRAL="#c9c7c0";
const MA_LINES = {ma_js};
const PE_MEAN = "{PE_MEAN}", PE_BAND = "{PE_BAND}";
const NDC_COLORS = {json.dumps([c for _, c, _ in NDC_LIGHTS])}, NDC_NAMES = {json.dumps([n for n, _, _ in NDC_LIGHTS], ensure_ascii=False)};
const NDC_LEAD = "{NDC_LEAD}", NDC_COIN = "{NDC_COIN}";
const EVENTS = {events_js}, EV_COLOR = "#8f8d87";
const CMP_META = {cmp_meta}, CMP_COLORS = {json.dumps(CMP_COLORS)}, CMP_DEFAULT = {json.dumps(CMP_DEFAULT)};
{chart_js}
{NDC_JS}
{CMP_JS}
{OPEN_HASH_JS}
</script>
</body>
</html>
"""
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(themed(page), encoding="utf-8")
    print(f"已產生 {OUT.relative_to(ROOT)}：今天 {w}（台股 {today['date']}、美股 {today['us_date']}），"
          f"值得注意 {len(notes)} 則，日曆 晴{counts['晴']} 陰{counts['陰']} 雨{counts['雨']}")


if __name__ == "__main__":
    build()
