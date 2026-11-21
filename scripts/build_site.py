"""讀 data/*.csv，產生一頁靜態儀表板 docs/index.html。

只用 Python 內建功能。執行方式：
    python -I scripts/build_site.py

晴雨規則是【草案】，集中在 judge_day()，要改規則只改那裡。
"""
import bisect
import csv
import datetime as dt
import html
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUT = ROOT / "docs" / "index.html"
SERIES_DIR = ROOT / "docs" / "series"   # 每個指標的完整歷史，點開時才載入

SPARK_DAYS = 63      # 小走勢圖：約 3 個月的交易日
CALENDAR_DAYS = 30   # 天氣日曆格數
HISTORY_DAYS = 400   # 晴雨只算最近這麼多個台股交易日（日曆、連續天數夠用）
TPE = dt.timezone(dt.timedelta(hours=8))   # 一律用台灣時間（GitHub Actions 的機器是 UTC）
DIGITS = {"taiex": 2, "spx": 2, "2330": 1, "vix": 2, "us10y": 3, "usdtwd": 3}   # 走勢圖數字的小數位

UP, DN, FLAT = "#ef6a5f", "#3fb27f", "#a7a59e"   # 紅漲綠跌（台股習慣），風向組用灰色
WEATHER = {
    "晴": {"color": "#d97757", "cell": "#d97757"},
    "陰": {"color": "#a7a59e", "cell": "#3a3d44"},
    "雨": {"color": "#6b9bf0", "cell": "#3d6fd1"},
}

# (代號, 名稱, 副標, 組別, 顯示方式)  顯示方式：price＝漲跌用 %，point＝漲跌用點數、不上紅綠
ROWS = [
    ("taiex", "加權指數", "TAIEX", "台股", "price"),
    ("2330", "台積電", "2330", "台股", "price"),
    ("spx", "標普 500", "S&P 500", "美股", "price"),
    ("qqq", "那斯達克 100", "QQQ", "美股", "price"),
    ("smh", "費城半導體", "SMH", "美股", "price"),
    ("tsm", "台積電 ADR", "TSM", "美股", "price"),
    ("vix", "VIX 恐慌指數", "20 以下算平靜", "風向", "point"),
    ("us10y", "美債 10 年殖利率", "US10Y", "風向", "point"),
    ("usdtwd", "美元兌台幣", "USD/TWD", "風向", "point"),
]
SOURCES_TEXT = "證交所（台股）、Yahoo Finance（美股、VIX、美債、匯率）"


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
    if code in ("taiex", "spx"):
        return f"{v:,.2f}"
    if code == "2330":
        return f"{v:,.0f}" if v == int(v) else f"{v:,.1f}"
    if code == "us10y":
        return f"{v:.2f}%"
    if code == "vix":
        return f"{v:.2f}"
    if code == "usdtwd":
        return f"{v:.3f}"
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
    digits = 3 if code in ("us10y", "usdtwd") else 2
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


def write_series(code, series):
    """完整歷史寫成 docs/series/<代號>.js。
    用 .js（不是 .json），因為直接雙擊開本機 file:/// 時瀏覽器不准讀 JSON，但可以載入 script。
    日期存成『和前一筆差幾天』，數字只留需要的小數位，檔案小很多。"""
    days = [(dt.date.fromisoformat(d) - EPOCH).days for d, _ in series]
    deltas = [days[0]] + [b - a for a, b in zip(days, days[1:])]
    dg = DIGITS.get(code, 2)
    closes = [round(v, dg + 1) for _, v in series]
    body = json.dumps({"d": deltas, "c": closes}, separators=(",", ":"))
    SERIES_DIR.mkdir(parents=True, exist_ok=True)
    (SERIES_DIR / f"{code}.js").write_text(f"MW_SERIES[{json.dumps(code)}]={body};\n", encoding="utf-8")


def chart_block(code, kind):
    buttons = "".join(
        f'<button type="button" data-r="{r}"{" aria-pressed=true" if r == "1Y" else " aria-pressed=false"}>{label}</button>'
        for r, label in RANGES)
    suf = ' data-suf="%"' if code == "us10y" else ""
    return f"""<div class="chart" data-code="{code}" data-kind="{kind}" data-dg="{DIGITS.get(code, 2)}"{suf}>
  <div class="ranges" role="group" aria-label="選擇期間">{buttons}</div>
  <p class="readout" aria-live="polite">載入中…</p>
  <div class="plot"></div>
</div>"""


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
        if len(s) < 61 or kind != "price":
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


# ---------- 互動走勢圖（瀏覽器端，純 JavaScript，不用套件） ----------
CHART_JS = r"""
window.MW_SERIES = window.MW_SERIES || {};
const DAY = 86400000, SVGNS = "http://www.w3.org/2000/svg";
const RANGE_LABEL = {"5D":"5天","1M":"1個月","3M":"3個月","6M":"6個月","YTD":"今年以來","1Y":"1年","3Y":"3年","5Y":"5年","MAX":"最長"};
const loading = {};

// 點開才載入那個指標的完整歷史（docs/series/<代號>.js）
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
function decode(raw) {            // 日期是「和前一筆差幾天」，還原成 1970 起的天數
  if (!raw.days) { let t = 0; raw.days = raw.d.map(x => (t += x)); }
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
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}

function render(box) {
  const data = box._data; if (!data) return;
  const kind = box.dataset.kind, dg = +box.dataset.dg, suf = box.dataset.suf || "";
  const r = box._range, days = data.days, c = data.c, n = days.length;
  const i0 = startIndex(days, r), last = n - 1;
  const plot = box.querySelector(".plot"), readout = box.querySelector(".readout");
  const W = Math.max(plot.clientWidth, 280), H = 220, pl = 4, pr = 58, pt = 10, pb = 24;
  const pts = sample(i0, n, c, Math.min(800, W * 2));
  let lo = Infinity, hi = -Infinity;
  for (const i of pts) { if (c[i] < lo) lo = c[i]; if (c[i] > hi) hi = c[i]; }
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
  el("line", {x1: pl, x2: W - pr, y1: Y(c[i0]), y2: Y(c[i0]), stroke: "#5a5d64", "stroke-dasharray": "3 4"}, svg);
  el("polyline", {points: pts.map(i => X(i).toFixed(1) + "," + Y(c[i]).toFixed(1)).join(" "),
    fill: "none", stroke: color, "stroke-width": 1.6, "stroke-linejoin": "round"}, svg);
  el("text", {x: pl, y: H - 6}, svg).textContent = iso(days[i0]);
  el("text", {x: W - pr, y: H - 6, "text-anchor": "end"}, svg).textContent = iso(days[last]);
  const vline = el("line", {y1: pt, y2: H - pb, stroke: "#8f8d87", visibility: "hidden"}, svg);
  const dot = el("circle", {r: 4, fill: color, stroke: "#14161a", "stroke-width": 2, visibility: "hidden"}, svg);
  plot.replaceChildren(svg);

  const summary = `<b>${RANGE_LABEL[r]}</b>　<span style="color:${ch.color}">${ch.text}</span>　<span style="color:#8f8d87">${iso(days[i0])} → ${iso(days[last])}</span>`;
  readout.innerHTML = summary;

  const show = ev => {
    const rect = svg.getBoundingClientRect();
    const x = (ev.clientX - rect.left) / rect.width * W;
    const i = Math.max(i0, Math.min(last, Math.round(i0 + (x - pl) / (W - pl - pr) * span)));
    vline.setAttribute("x1", X(i)); vline.setAttribute("x2", X(i)); vline.setAttribute("visibility", "visible");
    dot.setAttribute("cx", X(i)); dot.setAttribute("cy", Y(c[i])); dot.setAttribute("visibility", "visible");
    const ci = change(c[i0], c[i], kind, dg);
    readout.innerHTML = `<b>${iso(days[i])}</b>　${fmt(c[i], dg, suf)}　<span style="color:#8f8d87">較起點</span> <span style="color:${ci.color}">${ci.text}</span>`;
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
});
let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => document.querySelectorAll("details[open] .chart").forEach(render), 150);
});
"""


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
    newest = {"台股": tw_last, "美股": us_last, "風向": us_last}
    for group in ("台股", "美股", "風向"):
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
            rows_html.append(f"""<details>
  <summary class="row">
    <span class="name">{name}<small>{sub}</small>{stale}</span>
    <span class="value">{fmt_value(code, s[-1][1])}</span>
    <span class="chg" style="color:{color}">{chg}</span>
    <svg class="spark" viewBox="0 0 84 28" preserveAspectRatio="none" aria-hidden="true"><polyline points="{spark}" stroke="{color}" stroke-width="1.4" fill="none" vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>
  </summary>
  {chart_block(code, kind)}
</details>""")
            write_series(code, s)
        groups_html.append(f'<section class="group"><h2>{group}</h2>{"".join(rows_html)}</section>')

    missing_html = f'<p class="warn">抓不到：{"、".join(missing)}</p>' if missing else ""
    chart_js = CHART_JS.replace("__BUILD__", dt.datetime.now(TPE).strftime("%Y%m%d%H%M"))   # 讓瀏覽器拿到最新的資料檔

    page = f"""<!doctype html>
<html lang="zh-Hant-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{w} · market_weather {md(today['date'])}</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#14161a;color:#ecebe6;font-family:"Noto Sans TC","Microsoft JhengHei","PingFang TC",system-ui,sans-serif;-webkit-text-size-adjust:100%}}
.wrap{{max-width:1180px;margin:0 auto;padding:1.5rem 1rem 3rem;display:flex;flex-direction:column;gap:24px}}
.top{{display:flex;justify-content:space-between;align-items:center;font-size:13px;color:#a7a59e}}
.top b{{font-weight:600;letter-spacing:.04em}}
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
.ranges button{{font:inherit;font-size:13px;min-height:32px;min-width:44px;padding:0 8px;border-radius:16px;border:1px solid #3a3d44;background:transparent;color:#c9c7c0;cursor:pointer}}
.ranges button:hover{{border-color:#8f8d87;color:#ecebe6}}
.ranges button:focus-visible{{outline:2px solid #d97757;outline-offset:1px}}
.ranges button[aria-pressed=true]{{background:#ecebe6;border-color:#ecebe6;color:#14161a;font-weight:600}}
.readout{{margin:10px 0 4px;font-size:14px;min-height:1.6em;font-variant-numeric:tabular-nums;color:#c9c7c0}}
.readout b{{font-weight:600}}
.plot svg{{display:block;width:100%;height:220px;touch-action:pan-y;cursor:crosshair}}
.plot text{{font-size:11px;fill:#8f8d87;font-variant-numeric:tabular-nums}}
.foot{{font-size:12px;color:#8f8d87;line-height:1.7}}
.foot details{{border:0}}
.foot summary{{display:inline-block;min-height:24px;color:#a7a59e;text-decoration:underline;text-underline-offset:3px}}
.foot summary:hover{{color:#ecebe6}}
.foot summary:focus-visible{{outline:2px solid #d97757}}
.warn{{color:#e0a95a;font-size:13px;margin:0}}
@media (max-width:480px){{
  .row{{grid-template-columns:minmax(0,1.4fr) minmax(0,1fr) minmax(0,.9fr);min-height:56px}}
  .spark{{display:none}}
  .ranges{{display:grid;grid-template-columns:repeat(9,minmax(0,1fr));gap:3px}}
  .ranges button{{min-width:0;padding:0;font-size:12px;min-height:34px}}
  .hero .word{{font-size:64px}}
  .hero .icon{{width:64px;height:64px}}
}}
</style>
</head>
<body>
<div class="wrap">
  <div class="top"><b>market_weather</b><span>更新於 {built}</span></div>
  {missing_html}
  <div class="cols">
    <aside>
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
      <div class="foot">
        <p style="margin:0">點任一行展開走勢，可切換 5 天到最長；手指或滑鼠移到圖上看當天數字。小走勢圖是近 3 個月。資料來源：{SOURCES_TEXT}。</p>
        <details>
          <summary>晴雨怎麼判斷（草案）</summary>
          <p style="margin:6px 0 0">晴：台股和前一晚標普 500 都收紅，而且 VIX 低於 20。<br>雨：台股或標普 500 有一個跌超過 1%，或 VIX 高於 25。<br>陰：其他情況。</p>
        </details>
        <p style="margin:6px 0 0">只整理公開行情，不是投資建議。</p>
      </div>
    </main>
  </div>
</div>
<noscript><p class="warn" style="text-align:center">走勢圖需要開啟 JavaScript。</p></noscript>
<script>
const UP="{UP}", DN="{DN}", NEUTRAL="#c9c7c0";
{chart_js}
</script>
</body>
</html>
"""
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(page, encoding="utf-8")
    print(f"已產生 {OUT.relative_to(ROOT)}：今天 {w}（台股 {today['date']}、美股 {today['us_date']}），"
          f"值得注意 {len(notes)} 則，日曆 晴{counts['晴']} 陰{counts['陰']} 雨{counts['雨']}")


if __name__ == "__main__":
    build()
