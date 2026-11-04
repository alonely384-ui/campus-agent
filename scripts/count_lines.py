"""统计 README「工程规模」表格用到的行数，口径固定、可复现。

对外公布的数字刨除了三个本地笔记页（web/guide.html、project-tour.html、
study-plan.html）——它们被 .gitignore 排除，不进仓库，读者 clone 后看不到，
把它算进「代码规模」等于虚报。

用法：
    .venv\\Scripts\\python.exe scripts\\count_lines.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 三个本地学习页面：含大量源码摘录，只在本地保留，不计入对外公布的规模
_LOCAL_NOTES = {"guide.html", "project-tour.html", "study-plan.html"}


def count(paths: list[Path]) -> tuple[int, int]:
    total = 0
    files = 0
    for p in paths:
        if not p.is_file():
            continue
        total += len(p.read_text(encoding="utf-8", errors="ignore").splitlines())
        files += 1
    return total, files


def _py(rel: str) -> list[Path]:
    return [p for p in (ROOT / rel).rglob("*.py") if p.is_file()]


def _web_html() -> list[Path]:
    return [p for p in (ROOT / "web").glob("*.html") if p.name not in _LOCAL_NOTES]


def _web_assets() -> list[Path]:
    return [p for pat in ("*.js", "*.css") for p in (ROOT / "web").glob(pat)]


def main() -> None:
    rows = [
        ("app/ (py)", _py("app")),
        ("web/ (html)", _web_html()),
        ("web/ (js+css)", _web_assets()),
        ("tests/ (py)", _py("tests")),
        ("scripts/ (py)", _py("scripts")),
    ]
    grand = 0
    for label, paths in rows:
        n, files = count(paths)
        grand += n
        print(f"{label:<16} {n:>6} 行 / {files:>3} 文件")
    print(f"{'合计':<16} {grand:>6} 行")


if __name__ == "__main__":
    main()