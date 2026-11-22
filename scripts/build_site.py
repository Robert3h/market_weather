"""讀 data/*.csv，產生一頁靜態儀表板 website/index.html。

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
OUT = ROOT / "website" / "index.html"
SERIES_DIR = ROOT / "website" / "series"   # 每個指標的完整歷史，點開時才載入

SPARK_DAYS = 63      # 小走勢圖：約 3 個月的交易日
CALENDAR_DAYS = 30   # 天氣日曆格數
HISTORY_DAYS = 400   # 晴雨只算最近這麼多個台股交易日（日曆、連續天數夠用）
TPE = dt.timezone(dt.timedelta(hours=8))   # 一律用台灣時間（GitHub Actions 的機器是 UTC）
DIGITS = {"taiex": 2, "spx": 2, "2330": 1, "vix": 2, "us10y": 3, "usdtwd": 3, "2330_pe": 2, "gold": 1, "btc": 0}   # 走勢圖數字的小數位

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
    ("gold", "黃金", "紐約期貨 · 美元／盎司", "其他資產", "price"),   # 2026-11-22 加：只看走勢，和其他資產比較
    ("btc", "比特幣", "BTC · 美元 · 週末也交易", "其他資產", "price"),
]
SOURCES_TEXT = "證交所（台股）、Yahoo Finance（美股、VIX、美債、匯率、黃金、比特幣）、國發會（景氣燈號，政府資料開放平台）"

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
    """完整歷史寫成 website/series/<代號>.js。
    用 .js（不是 .json），因為直接雙擊開本機 file:/// 時瀏覽器不准讀 JSON，但可以載入 script。
    日期存成『和前一筆差幾天』，數字只留需要的小數位，檔案小很多。"""
    days = [(dt.date.fromisoformat(d) - EPOCH).days for d, _ in series]
    deltas = [days[0]] + [b - a for a, b in zip(days, days[1:])]
    dg = DIGITS.get(code, 2)
    closes = [round(v, dg + 1) for _, v in series]
    body = json.dumps({"d": deltas, "c": closes}, separators=(",", ":"))
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
    return f"""<details>
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
  <p class="note">上下排：每張圖各自刻度，手指移動時每張圖標出同一天，虛線是期間起點。疊在一起：每條線都從期間起點算 0%，看誰強誰弱。台股、美股休市日不同，以最晚收盤的那個市場對齊日期。{"景氣燈號是國發會每月公布的分數（約每月 27 日公布上個月），一整個月畫成同一個高度；上下排時看分數增減，疊在一起時和其他指標一樣算變化 %，只供參考。" if has_ndc else ""}</p>
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
  const W = Math.max(plot.clientWidth, 280), pl = 4, pr = 58, pt = 8, pb = 22, GAP = 18;
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
  const labels = [];
  if (stack) {
    ser.forEach((s, k) => {
      const top = pt + k * (PH + GAP);
      let lo = Infinity, hi = -Infinity;
      for (let i = s.i0; i <= s.last; i++) { const v = s.d.c[i]; if (v < lo) lo = v; if (v > hi) hi = v; }
      if (lo === hi) { lo -= 1; hi += 1; }
      const pad = (hi - lo) * 0.06; lo -= pad; hi += pad;
      const Yv = v => top + (1 - (v - lo) / (hi - lo)) * PH;
      const ticks = niceTicks(lo, hi, 3), step = ticks.length > 1 ? ticks[1] - ticks[0] : 1;
      for (const t of ticks) {
        el("line", {x1: pl, x2: W - pr, y1: Yv(t), y2: Yv(t), stroke: "#262930"}, svg);
        el("text", {x: W - pr + 6, y: Yv(t) + 4}, svg).textContent = tickText(t, step, s.m.suf);
      }
      el("line", {x1: pl, x2: W - pr, y1: Yv(s.base), y2: Yv(s.base), stroke: "#5a5d64", "stroke-dasharray": "3 4"}, svg);
      s.Y = i => Yv(s.d.c[i]);
      line(s, s.Y);
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


def chart_block(code, kind):
    buttons = "".join(
        f'<button type="button" data-r="{r}"{" aria-pressed=true" if r == "1Y" else " aria-pressed=false"}>{label}</button>'
        for r, label in RANGES)
    suf = ' data-suf="%"' if code == "us10y" else ""
    legend = ""
    if kind == "price":   # 均線圖例：季線、年線（顏色和 JS 的 MA_LINES 一致）
        legend = "".join(f'<span><i style="border-color:{col}"></i>{label}</span>' for _, col, label in MA_LINES)
        legend = f'<p class="legend" data-for="price">{legend}</p>'
    modes, pe_attr = "", ""
    pe_code = PE_FOR.get(code)
    if pe_code and (DATA_DIR / f"{pe_code}.csv").exists():   # 有本益比資料才出現「股價｜本益比」切換
        pe_attr = f' data-pe="{pe_code}"'
        modes = ('<div class="modes" role="group" aria-label="圖表類型">'
                 '<button type="button" data-m="price" aria-pressed="true">股價</button>'
                 '<button type="button" data-m="pe" aria-pressed="false">本益比</button></div>\n  ')
        legend += (f'<p class="legend" data-for="pe" hidden><span><i style="border-color:{PE_MEAN}"></i>期間平均</span>'
                   f'<span><i style="border-color:{PE_BAND}"></i>±1 標準差</span><span>（證交所本益比）</span></p>')
    return f"""<div class="chart" data-code="{code}" data-kind="{kind}" data-dg="{DIGITS.get(code, 2)}"{suf}{pe_attr}>
  {modes}<div class="ranges" role="group" aria-label="選擇期間">{buttons}</div>
  <p class="readout" aria-live="polite">載入中…</p>
  <div class="plot"></div>{legend}
</div>"""


MA_LINES = [(60, "#e0a95a", "季線 60 日"), (240, "#7fa7d9", "年線 240 日")]   # 移動平均線：天數、顏色、名稱（只是參考座標）
PE_FOR = {"2330": "2330_pe"}             # 哪個指標有本益比：指標代號 → 本益比資料代號（data/<代號>.csv）
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
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}

function render(box) {
  const pe = box._mode === "pe";                 // 「本益比」模式：畫本益比＋期間平均 ±1 標準差
  const data = pe ? box._pe : box._data; if (!data) return;
  const kind = pe ? "pe" : box.dataset.kind, dg = pe ? 2 : +box.dataset.dg, suf = pe ? "" : (box.dataset.suf || "");
  const r = box._range, days = data.days, c = data.c, n = days.length;
  const i0 = startIndex(days, r), last = n - 1;
  let stat = null;
  if (pe) {                                      // 平均和標準差用所選期間的每一天算（選 3 年＝3 年平均）
    let s = 0, s2 = 0, k = last - i0 + 1;
    for (let i = i0; i <= last; i++) { s += c[i]; s2 += c[i] * c[i]; }
    const mean = s / k, sd = Math.sqrt(Math.max(s2 / k - mean * mean, 0));
    stat = {mean, sd, z: v => sd ? (v - mean) / sd : 0};
  }
  const zText = v => { const z = stat.z(v); return (z >= 0 ? "+" : "−") + Math.abs(z).toFixed(1) + "σ"; };
  const plot = box.querySelector(".plot"), readout = box.querySelector(".readout");
  const W = Math.max(plot.clientWidth, 280), H = 220, pl = 4, pr = 58, pt = 10, pb = 24;
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
  const vline = el("line", {y1: pt, y2: H - pb, stroke: "#8f8d87", visibility: "hidden"}, svg);
  const dot = el("circle", {r: 4, fill: color, stroke: "#14161a", "stroke-width": 2, visibility: "hidden"}, svg);
  plot.replaceChildren(svg);

  const summary = pe
    ? `<b>${RANGE_LABEL[r]}</b>　本益比 ${c[last].toFixed(2)} 倍　<span class="nw"><span style="color:#8f8d87">平均</span> ${stat.mean.toFixed(1)}</span>　<span class="nw"><span style="color:#8f8d87">位置</span> ${zText(c[last])}</span>`
    : `<b>${RANGE_LABEL[r]}</b>　<span style="color:${ch.color}">${ch.text}</span>　<span style="color:#8f8d87">${iso(days[i0])} → ${iso(days[last])}</span>`;
  readout.innerHTML = summary;

  const show = ev => {
    const rect = svg.getBoundingClientRect();
    const x = (ev.clientX - rect.left) / rect.width * W;
    const i = Math.max(i0, Math.min(last, Math.round(i0 + (x - pl) / (W - pl - pr) * span)));
    vline.setAttribute("x1", X(i)); vline.setAttribute("x2", X(i)); vline.setAttribute("visibility", "visible");
    dot.setAttribute("cx", X(i)); dot.setAttribute("cy", Y(c[i])); dot.setAttribute("visibility", "visible");
    if (pe) {
      readout.innerHTML = `<b>${iso(days[i])}</b>　本益比 ${c[i].toFixed(2)} 倍　<span class="nw"><span style="color:#8f8d87">較期間平均</span> ${zText(c[i])}</span>`;
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
            if PE_FOR.get(code) and load(PE_FOR[code]):   # 本益比也寫一份，點「本益比」時才載入
                write_series(PE_FOR[code], load(PE_FOR[code]))
        groups_html.append(f'<section class="group"><h2>{group}</h2>{"".join(rows_html)}</section>')
    ndc = load_ndc()
    groups_html.insert(0, f'<section class="group"><h2>比較</h2>{compare_block(bool(ndc))}</section>')
    if ndc:   # 國發會景氣燈號（每月）
        write_ndc_series(ndc)
        groups_html.append(f'<section class="group"><h2>景氣（每月）</h2>{ndc_row(ndc)}</section>')

    missing_html = f'<p class="warn">抓不到：{"、".join(missing)}</p>' if missing else ""
    ma_js = json.dumps([{"n": n, "color": col} for n, col, _ in MA_LINES])
    cmp_meta = {code: {"name": name, "kind": kind, "dg": DIGITS.get(code, 2), "suf": "%" if code == "us10y" else ""}
                for code, name, _, _, kind in ROWS if series_map[code]}
    if ndc:
        cmp_meta["ndc"] = {"name": "景氣燈號", "kind": "score", "dg": 0, "suf": " 分"}
    cmp_meta = json.dumps(cmp_meta, ensure_ascii=False)
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
.legend{{margin:6px 0 0;font-size:12px;color:#8f8d87;display:flex;gap:14px;flex-wrap:wrap}}
.legend[hidden]{{display:none}}
.legend i.sw{{width:10px;height:10px;border:0;border-radius:2px;vertical-align:-1px}}
.chg .sub{{display:block;font-size:12px;color:#8f8d87}}
.ndc{{margin:8px 6px 16px}}
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
        <p style="margin:6px 0 0"><a href="History/index.html">歷史版本</a>（每次改版前存一份當時的畫面）</p>
        <p style="margin:6px 0 0">只整理公開行情，不是投資建議。</p>
      </div>
    </main>
  </div>
</div>
<noscript><p class="warn" style="text-align:center">走勢圖需要開啟 JavaScript。</p></noscript>
<script>
const UP="{UP}", DN="{DN}", NEUTRAL="#c9c7c0";
const MA_LINES = {ma_js};
const PE_MEAN = "{PE_MEAN}", PE_BAND = "{PE_BAND}";
const NDC_COLORS = {json.dumps([c for _, c, _ in NDC_LIGHTS])}, NDC_NAMES = {json.dumps([n for n, _, _ in NDC_LIGHTS], ensure_ascii=False)};
const NDC_LEAD = "{NDC_LEAD}", NDC_COIN = "{NDC_COIN}";
const CMP_META = {cmp_meta}, CMP_COLORS = {json.dumps(CMP_COLORS)}, CMP_DEFAULT = {json.dumps(CMP_DEFAULT)};
{chart_js}
{NDC_JS}
{CMP_JS}
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
