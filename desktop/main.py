"""桌面版入口：把网页界面装进一个本地窗口。

跑起来做四件事：
1. 定位数据目录——首次运行时把随包的 data.zip 解压到用户目录（之后复用）；
2. 挑一个空闲端口，在后台子进程里起 uvicorn；
3. 等 /health 真的能通，再开 pywebview 窗口指向它；
4. 窗口关掉就把后台服务一起收掉——不然进程退不干净，下次启动端口还占着。

为什么不直接 `uvicorn.run` 在子线程：uvicorn 装了自己的信号处理器，
非主线程里注册信号会抛 ValueError；而且它一旦把事件循环跑起来就很难打断。
放子进程里，父进程只要 terminate() 就行，退出路径简单得多。

数据为什么不全塞进 exe：chroma 索引 312MB + 论文 PDF 131MB + 重排模型 23MB，
加起来 470MB。塞进单文件 exe，每次启动都要先把它们解压到临时目录复制一遍。
分成「exe（代码+依赖）+ data.zip（语料）」后，数据只解压一次，之后直接复用。
"""

from __future__ import annotations

import multiprocessing as mp
import os
import socket
import sys
import time
import zipfile
from pathlib import Path

APP_NAME = "ResearchAssistant"
DEFAULT_PORT = 8756
READY_TIMEOUT = 120.0  # 首次要建 Chroma 连接并读索引，给足两分钟


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _bundle_dir() -> Path:
    """打包后是解压出来的临时目录，源码运行时是项目根目录。"""
    if _is_frozen() and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def _user_data_dir() -> Path:
    """数据落在用户目录，而不是 exe 旁边——Program Files 下没有写权限。"""
    base = os.getenv("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / APP_NAME


def _ensure_data() -> Path:
    """保证数据目录可用，返回它的路径。

    三种情况：
    - 源码运行：直接用仓库里的 data/（开发时改数据立即生效，不该被复制走）；
    - 打包运行 + 已有解压结果：直接用，不重复解压；
    - 打包运行 + 首次：解压 >>> 落一个版本标记。
    版本标记记录 zip 的大小和时间，换了新数据包能自动重新解压。
    """
    if not _is_frozen():
        return Path(__file__).resolve().parent.parent / "data"

    target = _user_data_dir()
    data_dir = target / "data"
    stamp = target / ".data_version"

    # 数据包由 scripts/build_desktop.py 放在 desktop/_bundle/ 下，
    # spec 把整个目录映射成 exe 内的 _bundle/，所以这里是 _bundle/data.zip
    bundle_zip = _bundle_dir() / "_bundle" / "data.zip"
    if not bundle_zip.exists():
        # 没打数据包（比如只做了最小验证构建）时退化成空目录，
        # 让服务自己在日志里报「没有索引」，而不是崩在解压这步
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir

    stat = bundle_zip.stat()
    want = f"{stat.st_size}-{int(stat.st_mtime)}"
    if stamp.exists() and stamp.read_text(encoding="utf-8").strip() == want and data_dir.exists():
        return data_dir

    data_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(bundle_zip) as zf:
        zf.extractall(data_dir.parent)
    stamp.write_text(want, encoding="utf-8")
    return data_dir


def _free_port(preferred: int = DEFAULT_PORT) -> int:
    """优先用固定端口，被占了就让系统给一个。

    端口固定住截图和文档里好写；被占也不能崩，换个能用的继续。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _ensure_env(data_dir: Path) -> Path:
    """保证 .env 存在，返回它的路径。

    密钥绝不能打进 exe——exe 是随处分发的二进制，任何人 strings 一下就拿到了。
    所以首次运行时生成一份**只含默认厂商地址、API Key 留空**的模板，
    让用户在设置页（或记事本）里自己填。写哪个文件靠 CAMPUS_ENV_FILE 告诉 app 层，
    不用改 app/config.py 一行代码。

    已经存在就原样返回，不覆盖——用户填过的 key 不能被下次启动冲掉。
    """
    env_path = data_dir / ".env"
    if env_path.exists():
        return env_path

    template = (
        "# 科研小助手桌面版配置\n"
        "# 填好 LLM_API_KEY 后重启程序（或到「设置」页里保存）。\n"
        "# 换厂商只需改下面两行——可选值见「设置」页的厂商预设。\n"
        "LLM_API_KEY=\n"
        "LLM_BASE_URL=https://api.deepseek.com\n"
        "LLM_MODEL=deepseek-chat\n"
        "APP_ENV=dev\n"
        "LOG_LEVEL=INFO\n"
    )
    env_path.write_text(template, encoding="utf-8")
    return env_path


def _serve(port: int, root: str, data_dir: str) -> None:
    """子进程入口：起 uvicorn，永不 return（靠父进程 terminate）。"""
    os.chdir(root)
    for p in (root, str(Path(root).parent)):
        if p not in sys.path:
            sys.path.insert(0, p)
    # 让 app 层读到解压后的数据目录，而不是 exe 内部那份只读的
    os.environ.setdefault("DATA_DIR", data_dir)
    os.environ.setdefault("CHROMA_DIR", str(Path(data_dir) / "chroma"))
    # app/config.py 的 _env_path() 优先看 CAMPUS_ENV_FILE，这样 .env 落在
    # 用户可写的目录里，既不用改业务代码，也不会去写 exe 内部那份只读文件
    os.environ.setdefault("CAMPUS_ENV_FILE", str(Path(data_dir) / ".env"))
    # exe 是 console=False 打包的，stdout 没有任何地方接收。出问题时
    # logger.exception 的堆栈就彻底丢了，所以桌面版强制落一份日志文件
    os.environ.setdefault("LOG_FILE", str(Path(data_dir).parent / "app.log"))
    import uvicorn  # 延迟导入，子进程里才付这份代价

    uvicorn.run("app.main:app", host="127.0.0.1", port=port, log_level="warning")


def _wait_ready(port: int, timeout: float = READY_TIMEOUT) -> bool:
    """轮询 /health 直到通。"""
    import httpx

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            # trust_env=False：本机代理会把这个回环请求也吞掉
            resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2.0, trust_env=False)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def main() -> int:
    import webview

    # mp 在 Windows 上是 spawn：不 freeze_support 的话，子进程会把本文件当 __main__
    # 重新执行一遍，直接死循环开窗口
    mp.freeze_support()

    root = str(_bundle_dir())
    data_dir = str(_ensure_data())
    _ensure_env(Path(data_dir))
    port = _free_port()

    ctx = mp.get_context("spawn")
    proc = ctx.Process(target=_serve, args=(port, root, data_dir), daemon=True)
    proc.start()

    try:
        if not _wait_ready(port):
            webview.create_window(
                "科研小助手",
                html=(
                    "<html><body style='font:15px sans-serif;padding:32px;line-height:1.8'>"
                    "<h2>本地服务没能起来</h2>"
                    "<p>常见原因：项目根目录的 <code>.env</code> 里没配 "
                    "<code>LLM_API_KEY</code>，或数据包没有解压成功。</p>"
                    f"<p style='color:#666'>数据目录：{data_dir}</p>"
                    "</body></html>"
                ),
                width=620,
                height=300,
            )
            webview.start()
            return 1

        webview.create_window(
            "科研小助手",
            f"http://127.0.0.1:{port}/ui/",
            width=1440,
            height=900,
            min_size=(960, 640),
        )
        webview.start()
    finally:
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)
        if proc.is_alive():
            proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())