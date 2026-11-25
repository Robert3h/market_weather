"""讀舊版 Excel（.xls，OLE 複合檔＋BIFF8）的儲存格。只用 Python 內建功能（2026-11-24）。

用途：櫃買中心的「公債殖利率曲線」每天一個 .xls（data/tw10y.csv 的來源）。
只處理數字和文字儲存格（SST、LABELSST、NUMBER、RK、MULRK、LABEL），公式、格式、圖表都略過。
    from xls_reader import read_sheets
    sheets = read_sheets(raw_bytes)   # [{(列, 欄): 值, ...}, ...]，列、欄從 0 開始
"""
import struct

END = (0xFFFFFFFE, 0xFFFFFFFF)


def _ole_stream(data, names=("Workbook", "Book")):
    """從 OLE 複合檔取出 Workbook 資料流。"""
    if data[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise ValueError("不是 .xls（OLE）檔")
    ss = 1 << struct.unpack_from("<H", data, 30)[0]
    ms = 1 << struct.unpack_from("<H", data, 32)[0]
    n_fat, dir_start = struct.unpack_from("<I", data, 44)[0], struct.unpack_from("<I", data, 48)[0]
    mini_cut, minifat_start = struct.unpack_from("<I", data, 56)[0], struct.unpack_from("<I", data, 60)[0]
    difat = list(struct.unpack_from("<109I", data, 76))
    s = struct.unpack_from("<I", data, 68)[0]
    while s not in END:
        vals = struct.unpack_from(f"<{ss // 4}I", data, 512 + s * ss)
        difat += vals[:-1]
        s = vals[-1]
    fat = []
    for sec in difat[:n_fat]:
        fat += struct.unpack_from(f"<{ss // 4}I", data, 512 + sec * ss)

    def chain(st, table):
        out = []
        while st not in END and len(out) < len(table) + 1:
            out.append(st)
            st = table[st]
        return out

    def big(st):
        return b"".join(data[512 + c * ss:512 + (c + 1) * ss] for c in chain(st, fat))

    d = big(dir_start)
    entries = []
    for i in range(len(d) // 128):
        e = d[i * 128:(i + 1) * 128]
        nl = struct.unpack_from("<H", e, 64)[0]
        st, size = struct.unpack_from("<IQ", e, 116)
        entries.append((e[:max(nl - 2, 0)].decode("utf-16-le", "replace"), st, size & 0xFFFFFFFF))
    mini = big(entries[0][1])
    minifat = []
    for c in chain(minifat_start, fat):
        minifat += struct.unpack_from(f"<{ss // 4}I", data, 512 + c * ss)
    for name, st, size in entries:
        if name in names:
            if size < mini_cut:
                return b"".join(mini[c * ms:(c + 1) * ms] for c in chain(st, minifat))[:size]
            return big(st)[:size]
    raise ValueError(".xls 裡找不到 Workbook 資料流")


def _rk(v):
    if v & 2:   # 整數
        x = v >> 2
        n = float(x - (1 << 30) if x & (1 << 29) else x)
    else:       # 浮點數的高 30 位
        n = struct.unpack("<d", struct.pack("<Q", (v & 0xFFFFFFFC) << 32))[0]
    return n / 100 if v & 1 else n


def _sst(buf):
    """共用字串表。跨 CONTINUE 的字串可能有一兩個字亂碼，不影響數字。"""
    _, uniq = struct.unpack_from("<II", buf, 0)
    out, i = [], 8
    for _ in range(uniq):
        if i + 3 > len(buf):
            break
        cch, flags = struct.unpack_from("<HB", buf, i)
        i += 3
        runs = struct.unpack_from("<H", buf, i)[0] if flags & 8 else 0
        i += 2 if flags & 8 else 0
        ext = struct.unpack_from("<I", buf, i)[0] if flags & 4 else 0
        i += 4 if flags & 4 else 0
        if flags & 1:
            out.append(buf[i:i + cch * 2].decode("utf-16-le", "replace"))
            i += cch * 2
        else:
            out.append(buf[i:i + cch].decode("latin-1"))
            i += cch
        i += runs * 4 + ext
    return out


def read_sheets(raw):
    wb = _ole_stream(raw)
    recs, i = [], 0
    while i + 4 <= len(wb):
        rid, ln = struct.unpack_from("<HH", wb, i)
        recs.append((rid, wb[i + 4:i + 4 + ln]))
        i += 4 + ln
    sst = []
    for k, (rid, body) in enumerate(recs):
        if rid == 0x00FC:   # SST，後面可能接 CONTINUE
            parts, j = [body], k + 1
            while j < len(recs) and recs[j][0] == 0x003C:
                parts.append(recs[j][1])
                j += 1
            sst = _sst(b"".join(parts))
            break
    sheets, cur = [], None
    for rid, b in recs:
        if rid == 0x0809:   # BOF：新的工作表（第一個是活頁簿本身，沒有儲存格，最後會被濾掉）
            cur = {}
            sheets.append(cur)
        elif cur is None:
            continue
        elif rid == 0x00FD:   # LABELSST
            r, c, _, idx = struct.unpack_from("<HHHI", b)
            cur[(r, c)] = sst[idx] if idx < len(sst) else ""
        elif rid == 0x0203:   # NUMBER
            r, c, _ = struct.unpack_from("<HHH", b)
            cur[(r, c)] = struct.unpack_from("<d", b, 6)[0]
        elif rid == 0x027E:   # RK
            r, c, _, v = struct.unpack_from("<HHHI", b)
            cur[(r, c)] = _rk(v)
        elif rid == 0x00BD:   # MULRK
            r, c0 = struct.unpack_from("<HH", b)
            for t in range((len(b) - 6) // 6):
                cur[(r, c0 + t)] = _rk(struct.unpack_from("<HI", b, 4 + t * 6)[1])
        elif rid == 0x0204:   # LABEL（舊格式）
            r, c, _, ln = struct.unpack_from("<HHHH", b)
            cur[(r, c)] = b[8:8 + ln].decode("cp950", "replace")
    return [s for s in sheets if s]
