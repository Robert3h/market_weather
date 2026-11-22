"""把「可以公開的部分」做成 public 分支的一個新 commit（只做 commit，不推）。

為什麼需要這支：本機 master 分支有完整歷史，裡面有規則檔、需求文件、進度紀錄等個人文件，
不能放上 GitHub。GitHub 上只放 public 分支，內容只有 PUBLIC_PATHS 列出來的東西。

用法（只用 Python 內建功能）：
    python -I scripts/publish_public.py -m "更新說明"
    → 列出這次會公開的變動，確認沒問題後，等 Robert 說「推」：
    git -c http.sslBackend=schannel push origin

第一次在某台電腦執行時，會自動設定好 origin 和「只准推 public」的保險（pre-push hook）。
"""
import argparse
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
REMOTE_URL = "https://github.com/Robert3h/market_weather.git"
PUBLIC_PATHS = ["README.md", ".gitignore", ".github", "scripts", "data", "website"]   # 只有這些會公開
# 這些由 GitHub Actions 每天更新，以 GitHub 上的為準（2026-11-22 前網頁資料夾叫 docs）。
# 不含 website/History/：歷史版本只在本機新增，整個 website/ 同步會把剛存的版本目錄蓋回舊的（2026-11-22 發生過）。
SYNC_PATHS = ["data", "website/index.html", "website/series"]
SSL = ["-c", "http.sslBackend=schannel"] if os.name == "nt" else []

HOOK = """#!/bin/sh
# market_weather 保險：只准把 public 分支推到 GitHub（master 有個人文件）
while read local_ref local_sha remote_ref remote_sha; do
  if [ "$local_ref" != "refs/heads/public" ]; then
    echo "擋下：只能推 public 分支，$local_ref 含個人文件。請先跑 scripts/publish_public.py" >&2
    exit 1
  fi
done
exit 0
"""


def git(*args, env=None, check=True):
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env=env)
    if check and r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失敗：\n{r.stderr}")
    return r.stdout.strip()


def setup():
    """設定 origin、預設只推 public→main、安裝 pre-push 保險。重複執行沒關係。"""
    if git("remote", check=False).split().count("origin") == 0:
        git("remote", "add", "origin", REMOTE_URL)
    git("config", "remote.origin.push", "refs/heads/public:refs/heads/main")
    hook = ROOT / ".git" / "hooks" / "pre-push"
    if not hook.exists() or hook.read_text(encoding="utf-8") != HOOK:
        hook.write_text(HOOK, encoding="utf-8", newline="\n")


def sync_from_github():
    """data/、網頁和 series/ 每天由 GitHub Actions 更新，推之前先拿回本機 master，免得本機舊資料把它蓋掉。
    本機自己跑 fetch_quotes 產生、還沒 commit 的資料會被 GitHub 版取代（GitHub 版才是每天累積的正本）。
    本機新增、GitHub 上還沒有的檔案（例如新的 website/History/ 版本）不受影響。"""
    paths = [p for p in SYNC_PATHS if git("ls-tree", "origin/main", "--", p)]   # GitHub 上還沒有的資料夾跳過
    if not paths:
        return
    git("checkout", "origin/main", "--", *paths)
    git("add", "--renormalize", "--", *paths)   # 重新加入，讓換行符號照本機設定統一
    if git("diff", "--cached", "--name-only", "--", *paths):
        git("commit", "-m", "同步 GitHub 上的每日資料")
        print(f"已把 GitHub 上最新的 {'、'.join(p + '/' for p in paths)} 同步回本機 master（已 commit）。\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-m", "--message", default=f"更新網站與資料 {dt.date.today()}")
    args = ap.parse_args()
    setup()

    dirty = git("status", "--porcelain", "--", *PUBLIC_PATHS)
    if dirty:
        print("⚠ 下面這些公開檔案還沒 commit 到 master，這次不會包含：\n" + dirty + "\n")

    git(*SSL, "fetch", "origin", "main")
    parent = git("rev-parse", "origin/main")
    sync_from_github()

    # 用一個暫時的 index，只放 PUBLIC_PATHS 的檔案（內容取自 master 最新 commit）
    env = dict(os.environ, GIT_INDEX_FILE=str(ROOT / ".git" / "public-index"))
    git("read-tree", "--empty", env=env)
    listing = git("ls-tree", "-r", "HEAD", "--", *PUBLIC_PATHS)
    bad = [line for line in listing.splitlines() if "PRIVATE_" in line.upper()]
    if bad:
        raise SystemExit("擋下：公開清單裡出現 PRIVATE_ 檔案：\n" + "\n".join(bad))
    # 用 bytes 傳：Windows 的文字模式會把換行改成 \r\n，git 會把每個檔案都忽略掉
    subprocess.run(["git", "update-index", "--index-info"], cwd=ROOT, input=(listing + "\n").encode("utf-8"),
                   env=env, check=True)
    tree = git("write-tree", env=env)
    (ROOT / ".git" / "public-index").unlink(missing_ok=True)

    if tree == git("rev-parse", f"{parent}^{{tree}}"):
        print("和 GitHub 上一模一樣，不用推。")
        git("update-ref", "refs/heads/public", parent)
        return

    files = git("ls-tree", "-r", "--name-only", tree).splitlines()
    if not files:
        raise SystemExit("擋下：公開內容是空的，沒有產生 commit。")
    commit = git("commit-tree", tree, "-p", parent, "-m", args.message)
    git("update-ref", "refs/heads/public", commit)
    print("已產生 public commit（還沒推）。這次會公開的變動：\n")
    print(git("diff", "--stat=100", parent, commit))
    print(f"\nGitHub 上公開的檔案共 {len(files)} 個，最上層：{sorted({f.split('/')[0] for f in files})}")
    print("確認後推送：git -c http.sslBackend=schannel push origin")


if __name__ == "__main__":
    main()
