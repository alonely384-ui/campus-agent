"""把桌面版打包成 exe + 数据包。一条命令跑完。

做的事：
1. 把 data/ 里运行时真正需要的部分打成一个 zip，放进 desktop/_bundle/，
   让 spec 把它带进 exe（PyInstaller 只能从我的构建目录里取文件）；
2. 调 PyInstaller 出 exe；
3. 把 exe 和数据包一起收进 dist/，并顺手打印体积。

打包完成后评委拿到的就是 dist/ 下一个 exe。数据包由 exe 首次运行时
解压到 %LOCALAPPDATA%\\ResearchAssistant，不再需要人工解压。

用法：python scripts/build_desktop.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESKTOP = ROOT / "desktop"
BUNDLE = DESKTOP / "_bundle"
DIST = ROOT / "dist"

# 只打运行时真正要读的东西。eval/ 和 uploads/ 是本地评测产物与用户上传，
# 跟演示无关，打进去纯粹是浪费体积。
INCLUDE = [
    "chroma",
    "models",
    "papers",
    "meta/structures.json",
    "meta/citation-graph.json",
    "catalog.json",
]


def _iter_files() -> list[tuple[Path, str]]:
    """列出要进数据包的文件，返回 (绝对路径, zip 内相对路径)。"""
    items: list[tuple[Path, str]] = []
    data = ROOT / "data"
    for rel in INCLUDE:
        src = data / rel
        if not src.exists():
            print(f"  跳过（不存在）：data/{rel}")
            continue
        if src.is_dir():
            for f in src.rglob("*"):
                if f.is_file():
                    items.append((f, f"data/{f.relative_to(data).as_posix()}"))
        else:
            items.append((src, f"data/{src.relative_to(data).as_posix()}"))
    return items


def build_data_pack() -> Path:
    print("== 1/3 打数据包 ==")
    BUNDLE.mkdir(parents=True, exist_ok=True)
    target = BUNDLE / "data.zip"
    if target.exists():
        target.unlink()

    items = _iter_files()
    total = sum(f.stat().st_size for f, _ in items)
    print(f"  待打包 {len(items)} 个文件，原始 {total / 1024 / 1024:.1f} MB")

    t0 = time.time()
    # 用 STORED：这些内容是 PDF、SQLite 页和 ONNX 权重，本来就已经压缩过，
    # 再用 deflate 几乎压不动，却要多花十几倍时间；而且 STORED 解压更快。
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for i, (src, arc) in enumerate(items, 1):
            zf.write(src, arc)
            if i % 50 == 0:
                print(f"    {i}/{len(items)}")
    # spec 里写的是 _bundle/data.zip，但如果直接放在 _bundle 下会被
    # PyInstaller 当成目录整体拷进 _bundle/_bundle，这里保持一层目录即可
    size = target.stat().st_size
    print(f"  完成：{target.name} {size / 1024 / 1024:.1f} MB，耗时 {time.time() - t0:.1f}s")
    return target


def build_exe() -> Path:
    print("\n== 2/3 打包 exe（这一步最慢，几分钟）==")
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(DESKTOP / "desktop.spec"),
        "--noconfirm",
        "--distpath",
        str(DIST),
        "--workpath",
        str(ROOT / "build" / "desktop"),
    ]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(cmd, cwd=str(ROOT), env=env)
    if proc.returncode != 0:
        raise SystemExit(f"PyInstaller 失败，退出码 {proc.returncode}")
    exe = DIST / "科研小助手.exe"
    if not exe.exists():
        # 有时代码里有非 ASCII 名字会被换掉，兜底找一个 exe
        cands = [p for p in DIST.glob("*.exe")]
        if not cands:
            raise SystemExit("没找到产物 exe")
        exe = cands[0]
    return exe


def report(exe: Path, pack: Path) -> None:
    print("\n== 3/3 产物 ==")
    print(f"  {exe.name:<20} {exe.stat().st_size / 1024 / 1024:8.1f} MB")
    print(f"  {pack.name:<20} {pack.stat().st_size / 1024 / 1024:8.1f} MB  (已内嵌在 exe 里)")
    print(f"\n  产物路径：{exe}")
    print("  双击即可运行；首次启动会把数据解压到 %LOCALAPPDATA%\\ResearchAssistant")


def main() -> int:
    pack = build_data_pack()
    exe = build_exe()
    report(exe, pack)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())