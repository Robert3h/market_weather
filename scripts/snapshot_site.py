"""把網站存一份歷史版本到 website/History/<日期>_<代號>/（index.html＋當時的 series/ 資料），
並更新版本目錄 website/History/index.html。打開舊版看到的畫面和數字都是當時的樣子。

只用 Python 內建功能。用法：
    網站「改版」推上線前跑一次（只是每天資料更新不用）：
        python -I scripts/snapshot_site.py --name compare --title "比較：勾選指標放在一起看"
    從 git 補舊版本（2026-11-22 前網頁資料夾叫 docs）：
        python -I scripts/snapshot_site.py --git 82619c7 --path docs --date 2026-11-22 --name compare --title "…"
"""
import argparse
import datetime as dt
import html
import json
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "website"
HIST = SITE / "History"
MANIFEST = HIST / "versions.json"   # 版本清單：[{folder, date, title, note}]，依建立順序
TPE = dt.timezone(dt.timedelta(hours=8))

BANNER = ('<div style="background:#2a2410;color:#e0a95a;font:13px/1.6 \'Noto Sans TC\',\'Microsoft JhengHei\',sans-serif;'
          'padding:8px 12px;text-align:center">歷史版本：{date}「{title}」，畫面和數字都是當時的樣子 · '
          '<a href="../../index.html" style="color:#ecebe6">回到最新版</a> · '
          '<a href="../index.html" style="color:#ecebe6">所有版本</a></div>')


def git_bytes(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True).stdout


def read_current():
    files = {p.name: p.read_bytes() for p in sorted((SITE / "series").glob("*.js"))}
    return (SITE / "index.html").read_text(encoding="utf-8"), files


def read_git(commit, path):
    page = git_bytes("show", f"{commit}:{path}/index.html").decode("utf-8")
    names = git_bytes("ls-tree", "--name-only", f"{commit}:{path}/series").decode("utf-8").split() \
        if git_bytes("ls-tree", commit, f"{path}/series") else []
    return page, {n: git_bytes("show", f"{commit}:{path}/series/{n}") for n in names}


def load_manifest():
    return json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else []


def write_index(versions):
    rows = ""
    for v in reversed(versions):   # 新的在上面
        note = f"<p>{html.escape(v['note'])}</p>" if v.get("note") else ""
        rows += (f'<li><a href="{v["folder"]}/index.html"><b>{html.escape(v["title"])}</b>'
                 f'<span>{v["date"]}</span></a>{note}</li>')
    (HIST / "index.html").write_text(f"""<!doctype html>
<html lang="zh-Hant-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>歷史版本 · market_weather</title>
<style>
body{{margin:0;background:#14161a;color:#ecebe6;font-family:"Noto Sans TC","Microsoft JhengHei","PingFang TC",system-ui,sans-serif}}
.wrap{{max-width:720px;margin:0 auto;padding:1.5rem 1rem 3rem}}
h1{{font-size:20px;margin:0 0 4px}}
.sub{{margin:0 0 20px;font-size:13px;color:#8f8d87;line-height:1.6}}
.sub a,.back{{color:#a7a59e}}
ol{{list-style:none;margin:0;padding:0}}
li{{border-top:1px solid #262930;padding:4px 0}}
li a{{display:flex;justify-content:space-between;align-items:center;gap:12px;min-height:48px;padding:4px 8px;border-radius:4px;color:#ecebe6;text-decoration:none}}
li a:hover{{background:#1c1f24}}
li a:focus-visible{{outline:2px solid #d97757;outline-offset:-2px}}
li a b{{font-weight:600;font-size:15px}}
li a span{{font-size:13px;color:#8f8d87;white-space:nowrap;font-variant-numeric:tabular-nums}}
li p{{margin:0 8px 8px;font-size:13px;color:#a7a59e;line-height:1.6}}
</style>
</head>
<body>
<div class="wrap">
  <h1>歷史版本</h1>
  <p class="sub">每次改版前存一份，畫面和數字都是當時的樣子（新的在上面）。<a href="../index.html">回到最新版</a></p>
  <ol>{rows}</ol>
</div>
</body>
</html>
""", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="英文小寫代號，例：compare")
    ap.add_argument("--title", required=True, help="這一版的中文說明（顯示在目錄）")
    ap.add_argument("--note", default="", help="補充說明（可省略）")
    ap.add_argument("--date", default=dt.datetime.now(TPE).date().isoformat())
    ap.add_argument("--git", help="從這個 commit 取網頁（補舊版本用）")
    ap.add_argument("--path", default="website", help="--git 時，那個 commit 裡網頁資料夾的名稱")
    args = ap.parse_args()

    folder = f"{args.date.replace('-', '')}_{args.name}"
    dest = HIST / folder
    if dest.exists():
        raise SystemExit(f"已經有 {dest.relative_to(ROOT)}，換一個 --name")
    page, series = read_git(args.git, args.path) if args.git else read_current()
    banner = BANNER.format(date=args.date, title=html.escape(args.title))
    def add_banner(text):
        # <body> 可能帶屬性（2026-11-23 起首頁是 <body id="top">），找第一個 <body…> 標籤後面插入
        m = re.search(r"<body[^>]*>", text)
        if not m:
            raise SystemExit("網頁裡找不到 <body>，無法加上「歷史版本」標示")
        return text[:m.end()] + "\n" + banner + text[m.end():]

    page = add_banner(page)
    (dest / "series").mkdir(parents=True)
    (dest / "index.html").write_text(page, encoding="utf-8")
    extra = SITE / "backtest.html"   # 2026-11-23 起多一頁「回測」：存目前版本時一起存，舊版裡的連結才點得到
    if not args.git and extra.exists():
        (dest / "backtest.html").write_text(add_banner(extra.read_text(encoding="utf-8")), encoding="utf-8")
    for name, data in series.items():
        (dest / "series" / name).write_bytes(data)
    versions = [v for v in load_manifest() if v["folder"] != folder]
    versions.append({"folder": folder, "date": args.date, "title": args.title, "note": args.note})
    MANIFEST.write_text(json.dumps(versions, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    write_index(versions)
    size = sum(p.stat().st_size for p in dest.rglob("*") if p.is_file()) / 1024
    print(f"已存 {dest.relative_to(ROOT)}（{len(series)} 個資料檔，{size:,.0f} KB）；目錄共 {len(versions)} 個版本")


if __name__ == "__main__":
    main()
