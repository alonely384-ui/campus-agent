# -*- mode: python ; coding: utf-8 -*-
"""桌面版打包配置：exe 只装代码和依赖，数据走外部 data.zip。

分开的理由：chroma 312MB + papers 131MB + 重排模型 23MB，全塞进单文件 exe
会让产物接近 1GB，而且每次启动都要把这些内容解压到临时目录复制一遍。
数据单独一个 zip，只在首次运行时解压到 %LOCALAPPDATA%\\ResearchAssistant，
之后直接复用——换数据包也不用重新打包 exe。

打包：python -m PyInstaller desktop/desktop.spec --noconfirm
产物：dist/科研小助手.exe
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821  SPECPATH 是 PyInstaller 注入的

hiddenimports = [
    # uvicorn 的 worker / loop / 协议实现都是按字符串动态导入的，
    # 静态分析看不到，漏一个就启动即崩
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    # app 包下的模块在 main.py 里是 import 进来的，但个别是运行时动态导入
    *collect_submodules("app"),
    # chromadb 用 chromadb.config.Settings 里的字符串去找实现类，再 import_module
    # 进来——静态分析完全看不见。漏了 telemetry.product.posthog 的表现是：
    # PersistentClient 构造时 ModuleNotFoundError，被 agent 的 except 吞成
    # 「检索异常，本轮不带参考资料」——服务照跑，但 RAG 整个哑掉，最难查的那种坏。
    *collect_submodules("chromadb"),
    # pywebview 的 Windows 后端
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "clr",
    "pythonnet",
]

datas = [
    # 前端页面是运行时按目录读的，必须原样带进去
    (str(ROOT / "web"), "web"),
    # app 读 pyproject.toml 取版本号，缺了会走 except 分支返回 unknown
    (str(ROOT / "pyproject.toml"), "."),
    # 数据包的中转目录：build 阶段由 scripts/build_desktop.py 先塞到这里
    (str(ROOT / "desktop" / "_bundle"), "_bundle"),
    # 依赖里带数据文件的包（certifi 证书、onnxruntime 等）
    *collect_data_files("certifi"),
    # pymupdf.layout 在 import pymupdf 时无条件 activate()，会去读 resources/onnx
    # 下的 ONNX 布局模型与 YAML 配置。我们只用 fitz.open/Matrix/Rect/Page 这些基础
    # 能力，从不调用 layout 分析，但激活发生在模块导入阶段，绕不过去——不收集就会
    # FileNotFoundError: ...\pymupdf\layout\resources/onnx/layout_rf2.4.1+imf1.yaml
    # 代价是 19 个文件约 49MB。
    *collect_data_files("pymupdf"),
    *collect_data_files("pymupdf4llm"),
]

excludes = [
    # 没用到但体积大的，明确排掉，避免 PyInstaller 顺着 import 拉进来
    "tkinter",
    "matplotlib.tests",
    "numpy.f2py",
    "pytest",
    "IPython",
]

a = Analysis(  # noqa: F821
    [str(ROOT / "desktop" / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="科研小助手",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX 压缩过的 exe 常被杀软误报，评委电脑上尤其麻烦
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # 双击不弹黑窗
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)