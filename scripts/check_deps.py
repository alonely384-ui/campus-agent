"""依赖自检：扫描 app/ 下真实的第三方 import，与 requirements.txt 比对。

用途：改了 import 忘了改 requirements.txt 时，这里会报出来。
退出码 1 表示不一致，可直接挂进 CI。

用法：
    python scripts/check_deps.py
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# import 名 → PyPI 发行包名（只在两者不同时才需要映射）
IMPORT_TO_DIST = {
    "fitz": "pymupdf",
    "pymupdf": "pymupdf",
    "rapidocr_onnxruntime": "rapidocr-onnxruntime",
    "pydantic_settings": "pydantic-settings",
    "starlette": "starlette",
    "nx": "networkx",
    "np": "numpy",
    "pd": "pandas",
    "plt": "matplotlib",
    "cv2": "opencv-python",
    "yaml": "pyyaml",
    "PIL": "pillow",
    "bs4": "beautifulsoup4",
    "sklearn": "scikit-learn",
}

# 这些 import 名由其它包顺带提供，不必单列（FastAPI 的 UploadFile 需要 python-multipart，
# 但那个包没有合法的 import 名可检测，只能在 requirements.txt 里手工保留）
PROVIDED_BY_OTHERS = {"starlette"}


def scan_imports() -> dict[str, set[str]]:
    """返回 {顶层模块名: {出现的文件名}}。"""
    stdlib = set(sys.stdlib_module_names)
    local = {p.stem for p in (BASE_DIR / "app").rglob("*.py")} | {"app"}

    found: dict[str, set[str]] = {}
    for path in sorted((BASE_DIR / "app").rglob("*.py")):
        text = path.read_text(encoding="utf-8-sig")
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:  # 语法错了就让调用方知道，别静默跳过
            raise SystemExit(f"无法解析 {path}：{exc}") from exc

        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]

            for name in names:
                if name in stdlib or name in local:
                    continue
                found.setdefault(name, set()).add(path.name)
    return found


def read_requirements(path: Path) -> dict[str, str]:
    """返回 {归一化包名: 原始行}。"""
    declared: dict[str, str] = {}
    if not path.exists():
        raise SystemExit(f"找不到 {path}")

    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.split("#")[0].strip()
        if not raw:
            continue
        name = re.split(r"[<>=!~\[ ]", raw)[0].strip()
        if name:
            declared[_norm(name)] = raw
    return declared


def _norm(name: str) -> str:
    return name.lower().replace("-", "_").replace(".", "_")


def main() -> int:
    imports = scan_imports()
    declared = read_requirements(BASE_DIR / "requirements.txt")
    declared_norm = set(declared)

    missing: list[tuple[str, str]] = []
    for mod in sorted(imports):
        if mod in PROVIDED_BY_OTHERS:
            continue
        dist = IMPORT_TO_DIST.get(mod, mod)
        if _norm(dist) not in declared_norm and _norm(mod) not in declared_norm:
            missing.append((mod, dist))

    print(f"扫描 app/ 得到第三方模块 {len(imports)} 个，requirements.txt 声明 {len(declared)} 个")
    print()

    if missing:
        print("缺少声明（代码 import 了但清单里没有）：")
        for mod, dist in missing:
            where = ", ".join(sorted(imports[mod]))
            print(f"  ✗ {mod:<24} → {dist:<24} ({where})")
        return 1

    print("✓ requirements.txt 覆盖了 app/ 的全部第三方 import")

    # 反向提示：声明了但代码里没 import 的，只提醒不报错
    unused = []
    for norm_name, raw in sorted(declared.items()):
        # 反查：有没有哪个 import 名映射到它
        hit = any(
            norm_name in {_norm(IMPORT_TO_DIST.get(m, m)), _norm(m)} for m in imports
        )
        if not hit and norm_name not in {
            "pytest", "pytest_asyncio", "ruff", "python_multipart",
        }:
            unused.append(raw)
    if unused:
        print()
        print("提示：以下声明在 app/ 里没找到对应 import（开发工具与 python-multipart 已豁免）：")
        for raw in unused:
            print(f"  ? {raw}")
    return 0


if __name__ == "__main__":
    sys.exit(main())