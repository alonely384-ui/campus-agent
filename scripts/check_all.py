"""演示前 30 秒体检：服务 / 页面 / 索引 / 模型 / Git。用法：python scripts/check_all.py"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
OK, BAD = "PASS", "FAIL"
rows: list[tuple[str, str, str]] = []


def add(name: str, ok: bool, detail: str) -> None:
    rows.append((name, OK if ok else BAD, detail))


def http(url: str, timeout: float = 8.0) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read()


def main() -> int:
    os.chdir(BASE)  # 相对路径（data/chroma）按项目根解析

    # 1. 服务
    try:
        code, body = http("http://127.0.0.1:8000/health")
        add("/health", code == 200, body.decode("utf-8", "replace")[:80])
    except Exception as e:  # noqa: BLE001
        add("/health", False, f"{type(e).__name__}: {e}")

    # 2. 页面
    for path, key in (("/ui/", b"guide"), ("/ui/project-tour.html", b"project-tour")):
        try:
            code, body = http("http://127.0.0.1:8000" + path, 20)
            add(path, code == 200, f"{code} {len(body) // 1024}KB")
        except Exception as e:  # noqa: BLE001
            add(path, False, f"{type(e).__name__}: {e}")

    # 3. 索引
    try:
        sys.path.insert(0, str(BASE))
        import chromadb

        from app.config import settings

        client = chromadb.PersistentClient(path=settings.chroma_dir)
        docs = client.get_collection("docs").count()
        dense = client.get_collection("docs_dense").count()
        add("索引 docs", docs > 0, f"docs={docs} dense={dense} 一致={docs == dense}")
    except Exception as e:  # noqa: BLE001
        add("索引 docs", False, f"{type(e).__name__}: {e}")

    # 4. 模型文件
    for rel, min_kb in (
        ("data/models/ms-marco-MiniLM-L-6-v2/model.onnx", 15_000),
        ("data/models/ms-marco-MiniLM-L-6-v2/tokenizer.json", 500),
    ):
        p = BASE / rel
        add(rel, p.exists() and p.stat().st_size > min_kb * 1024, f"{p.stat().st_size // 1024}KB" if p.exists() else "缺失")

    # 5. Git
    git = shutil.which("git") or r"C:\Program Files\Git\cmd\git.exe"
    try:
        out = subprocess.run([git, "-C", str(BASE), "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True, timeout=20)
        add("git 工作区（已跟踪）", out.returncode == 0 and not out.stdout.strip(), "干净" if not out.stdout.strip() else f"{len(out.stdout.strip().splitlines())} 个改动")
    except Exception as e:  # noqa: BLE001
        add("git 工作区", False, f"{type(e).__name__}: {e}")

    # 6. 依赖
    try:
        add("python venv", ".venv" in sys.executable, sys.executable)
    except Exception as e:  # noqa: BLE001
        add("python venv", False, str(e))

    width = max(len(r[0]) for r in rows) + 2
    print()
    for name, state, detail in rows:
        print(f"{name:<{width}} {state:<5} {detail}")
    bad = sum(1 for r in rows if r[1] == BAD)
    print()
    print(f"{'全部通过' if not bad else f'{bad} 项失败'}（{len(rows)} 项检查）")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())

