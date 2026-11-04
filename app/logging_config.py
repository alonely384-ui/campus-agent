"""日志配置：让程序输出"带时间戳和级别"的结构化日志，而不是一堆 print。

为什么不用 print？
    print 没有时间、没有级别、没法关掉、没法写进文件。
    上线之后出问题，全靠日志回溯。

为什么还要支持写文件？
    桌面版（desktop/main.py）是 console=False 打包的，没有控制台，
    stdout 直接丢弃——服务端一个 logger.exception 在双击运行的程序里
    等于没写。设 LOG_FILE 环境变量后额外挂一个轮转文件 handler，
    只有打包场景才设，服务端与测试的行为保持原样。

为什么每次启动都要写一条分隔标记？
    桌面版的日志文件是跨启动追加的：用户今天开一次、明天开一次，
    全都进同一个 app.log。没有分界线时，翻日志看到「检索异常」根本
    判断不了是这次的问题还是上周那次留下的。启动时打一条带时间和
    进程号的横幅，等于给每次运行编了号。
"""

import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import settings

# 单文件 5MB，留 3 份历史：长期开着也不会把磁盘吃满
_LOG_MAX_BYTES = 5 * 1024 * 1024
_LOG_BACKUP_COUNT = 3

# 启动横幅：一眼能扫到的分隔线，比一行普通日志好找
_BANNER = "=" * 60


def _build_handlers() -> tuple[logging.Handler, ...]:
    """装配要挂在 root 上的 handler：第一个永远是控制台，第二个可选是文件。

    返回 (控制台 handler[, 文件 handler])，同时日志文件路径若要写横幅，
    由调用方从文件 handler 的 baseFilename 取（避免再返回第三个值）。
    把这段单独抽出来是为了能测：pytest 会占满 root logger 的 handler
    列表，在测试里断言 ``logging.getLogger().handlers`` 断言的是
    pytest 自己的东西，永远看不到我们建的 FileHandler。
    """
    stream = logging.StreamHandler(sys.stdout)  # 输出到控制台

    # 没设 LOG_FILE 就不加，服务端/测试行为与之前完全一致
    log_file = os.getenv("LOG_FILE", "").strip()
    if not log_file:
        return (stream,)

    path = Path(log_file)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path,
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError as exc:
        # 日志目录建不出来不该让服务起不来，退化成只写控制台
        print(f"日志文件不可用，仅输出到控制台：{exc}", file=sys.stderr)
        return (stream,)

    return (stream, file_handler)


def setup_logging() -> None:
    """在程序启动时调用一次即可。"""
    handlers = _build_handlers()

    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=list(handlers),
    )

    # 把第三方库的日志调安静一点，不然满屏都是它们的信息
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("chromadb").setLevel(logging.WARNING)

    # 只有真挂上文件 handler 才写横幅；返回第二个元素就是那个 handler
    if len(handlers) > 1:
        _write_startup_banner(Path(handlers[1].baseFilename))


def _write_startup_banner(path: Path) -> None:
    """在日志文件里打一条启动分隔线，标出"这是第几次运行"。

    直接写文件而不是走 logger：这里想让它紧贴在该文件已有内容的末尾，
    且不带「INFO | app.startup |」这种前缀噪声——它就是一条分隔线，
    不是一条事件。进程号用来区分 PyInstaller 那几层子进程是否各写了一份。
    """
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"{_BANNER}", f"启动 {stamp}（进程号 {os.getpid()}）", f"{_BANNER}"]
    try:
        # 前置换行：上一次运行若没来得及收尾（崩溃/强杀），
        # 最后一行多半没有结尾换行符，不补一个会和横幅粘成一行
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n" + "\n".join(lines) + "\n")
    except OSError as exc:
        # 横幅写不进去不是故障，不能连累服务启动
        print(f"启动分隔标记写入失败：{exc}", file=sys.stderr)


def get_logger(name: str) -> logging.Logger:
    """各模块用法： logger = get_logger(__name__)"""
    return logging.getLogger(name)
