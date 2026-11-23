"""回測（第 6 輪，2026-11-23）：長期持有 vs「年線進出」、vs「景氣燈號藍燈買紅燈賣」。只用 Python 內建功能。

build_site.py 會呼叫這裡，產生網站的「回測」頁；工具/backtest_ndc.py 也用這裡的函式寫 Markdown 報告。
兩種回測共同的限制：只用收盤價（不含股息）、賣出後的現金沒算利息、沒算手續費和稅。
"""
import datetime as dt

MA_DAYS = 240                     # 年線：240 個交易日的簡單移動平均
MA_MARKETS = ["taiex", "spx", "qqq"]   # 只比整體市場，不放個股
TW_FEE_BUY, TW_FEE_SELL = 0.001425, 0.001425 + 0.003   # 台股：手續費上限 0.1425%（買賣各一次）＋證交稅 0.3%（賣出）


def years_between(d1, d2):
    return (dt.date.fromisoformat(d2) - dt.date.fromisoformat(d1)).days / 365.25


def max_drawdown(values):
    peak, worst = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        worst = min(worst, v / peak - 1)
    return worst * 100


def summary(curve, start, end):
    yrs = years_between(start, end)
    mult = curve[-1] / curve[0]
    return {"mult": mult, "cagr": (mult ** (1 / yrs) - 1) * 100 if yrs > 0 else 0.0, "mdd": max_drawdown(curve)}


# ---------- 年線進出 ----------
def ma_backtest(series, n=MA_DAYS, fee_buy=0.0, fee_sell=0.0):
    """收盤在年線上方就持有、跌破就賣出。第 t 天收盤判斷，第 t+1 天收盤才動作（不偷看）。
    從年線第一次算得出來那天開始，長期持有也從同一天算，兩邊公平。"""
    closes = [v for _, v in series]
    if len(closes) < n + 2:
        return None
    sma, s = [None] * len(closes), 0.0
    for i, v in enumerate(closes):
        s += v
        if i >= n:
            s -= closes[i - n]
        if i >= n - 1:
            sma[i] = s / n
    i0 = n - 1
    equity, cash, units, inside, switches = [], 1.0, 0.0, 0, 0
    want = closes[i0] > sma[i0]           # 第一天的判斷，隔天才動作
    for i in range(i0 + 1, len(closes)):
        v = closes[i]
        if want and units == 0:
            units, cash = cash * (1 - fee_buy) / v, 0.0
            switches += 1
        elif not want and units > 0:
            cash, units = units * v * (1 - fee_sell), 0.0
            switches += 1
        equity.append(cash + units * v)
        inside += units > 0
        want = v > sma[i]
    start, end = series[i0 + 1][0], series[-1][0]
    bh = [v / closes[i0 + 1] for v in closes[i0 + 1:]]
    return {"start": start, "end": end, "years": years_between(start, end),
            "bh": summary(bh, start, end), "st": summary(equity, start, end),
            "switches": switches, "in_mkt": inside / len(equity) * 100}


# ---------- 景氣燈號：藍燈買、紅燈賣 ----------
def add_months(ym, k):
    y, m = map(int, ym.split("-"))
    t = y * 12 + m - 1 + k
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


def ndc_backtest(px, lights):
    """px：[(日期, 收盤)]；lights：[(月份, 燈號, 分數)]。
    藍燈全部買進、紅燈全部賣出，一開始是現金。M 月燈號約在 M+1 月 27 日公布，
    所以用 M+2 月第一個交易日收盤價進出（比實際能動作晚幾天，算法保守）。"""
    start = px[0][0]
    signals = []
    for ym, light, score in lights:
        act = add_months(ym, 2) + "-01"
        if act < start:                   # 指數資料開始之前就該動作的燈號不算
            continue
        i = next((k for k, (d, _) in enumerate(px) if d >= act), None)
        if i is not None:
            signals.append((i, ym, light, score))
    trades, holding, buy = [], False, None
    for i, ym, light, score in signals:
        if not holding and light == "藍":
            holding, buy = True, (i, ym)
        elif holding and light == "紅":
            trades.append((buy, (i, ym)))
            holding = False
    if holding:
        trades.append((buy, None))
    buys = {b[0] for b, _ in trades}
    sells = {s[0] for _, s in trades if s}
    equity, cash, units, inside = [], 1.0, 0.0, 0
    for i, (_, v) in enumerate(px):
        if i in buys and units == 0:
            units, cash = cash / v, 0.0
        if i in sells and units > 0:
            cash, units = units * v, 0.0
        equity.append(cash + units * v)
        inside += units > 0
    end = px[-1][0]
    rows = []
    for (bi, bym), s in trades:
        si, sym = s if s else (None, None)
        sv = px[si][1] if si is not None else px[-1][1]
        rows.append({"buy_month": bym, "buy_date": px[bi][0], "buy": px[bi][1],
                     "sell_month": sym, "sell_date": px[si][0] if si is not None else None, "sell": sv,
                     "ret": (sv / px[bi][1] - 1) * 100})
    return {"start": start, "end": end, "years": years_between(start, end),
            "bh": summary([v for _, v in px], start, end), "st": summary(equity, start, end),
            "in_mkt": inside / len(px) * 100, "trades": rows, "last_light": lights[-1] if lights else None}
