"""把 pdf.js（pdfjs-dist）下载到 web/vendor/pdfjs/，供阅读页离线使用。

为什么必须本地 vendor：
    阅读页（web/reader.html）要用 pdf.js 渲染 PDF。如果引 CDN，评审现场一断网
    页面就白屏；这两个文件一起提交进仓库，克隆下来即可离线运行。

用法：
    python scripts/fetch_pdfjs.py           # 缺什么下什么（已存在就跳过）
    python scripts/fetch_pdfjs.py --force   # 强制重下

本机坑提醒：环境里的 HTTP_PROXY 指向 127.0.0.1:65532，梯子没开时下载会失败；
脚本会自动去掉代理重试一次，仍然失败会把真实错误打出来，不吞异常。
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "3.11.174"
BASE_DIR = Path(__file__).resolve().parent.parent
DEST = BASE_DIR / "web" / "vendor" / "pdfjs"
FILES = ("pdf.min.js", "pdf.worker.min.js")
# legacy 构建：兼容老浏览器，且是 UMD（挂在 window.pdfjsLib），不用改 <script type=module>
MIRRORS = (
    f"https://cdn.jsdelivr.net/npm/pdfjs-dist@{VERSION}/legacy/build",
    f"https://unpkg.com/pdfjs-dist@{VERSION}/legacy/build",
    f"https://registry.npmmirror.com/pdfjs-dist/{VERSION}/files/legacy/build",
)


def _download(url: str, dest: Path, opener=None, timeout: int = 60) -> None:
    """下载到临时文件再原子替换：中途失败不会留下半个 js 文件让页面报语法错。"""
    req = urllib.request.Request(url, headers={"User-Agent": "campus-agent/fetch-pdfjs"})
    open_fn = opener.open if opener else urllib.request.urlopen
    with open_fn(req, timeout=timeout) as resp:  # noqa: S310 - 固定 HTTPS 白名单
        data = resp.read()
    if len(data) < 50_000:
        raise RuntimeError(f"下载内容太小（{len(data)} 字节），可能被拦截：{url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(dest)


def fetch(force: bool = False) -> int:
    """下载缺的文件，返回本次新下载的个数。"""
    got = 0
    for name in FILES:
        dest = DEST / name
        if dest.exists() and not force:
            print(f"[skip] {dest.relative_to(BASE_DIR)} 已存在（{dest.stat().st_size:,} 字节）")
            continue

        last_err: Exception | None = None
        for base in MIRRORS:
            url = f"{base}/{name}"
            try:
                _download(url, dest)
                print(f"[ok]   {name} <- {url} （{dest.stat().st_size:,} 字节）")
                got += 1
                last_err = None
                break
            except Exception as exc:  # noqa: BLE001 —— 多镜像逐个试，最后统一报错
                last_err = exc
                print(f"[miss] {url}: {exc}")

        if last_err is not None:
            # 代理是本地最大概率的坑（梯子没开），去掉代理再试一轮
            direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for base in MIRRORS:
                url = f"{base}/{name}"
                try:
                    _download(url, dest, opener=direct)
                    print(f"[ok]   {name} <- {url} （直连，{dest.stat().st_size:,} 字节）")
                    got += 1
                    last_err = None
                    break
                except Exception as exc:  # noqa: BLE001
                    last_err = exc

        if last_err is not None:
            print(f"[fail] {name} 下载失败：{last_err}", file=sys.stderr)
            return -1
    return got


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 pdf.js 到 web/vendor/pdfjs/")
    parser.add_argument("--force", action="store_true", help="已存在也重新下载")
    args = parser.parse_args()
    got = fetch(force=args.force)
    if got < 0:
        return 1
    print(f"完成：本次下载 {got} 个文件，目录 {DEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
