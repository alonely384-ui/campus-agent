"""日志配置：让程序输出"带时间戳和级别"的结构化日志，而不是一堆 print。

为什么不用 print？
    print 没有时间、没有级别、没法关掉、没法写进文件。
    上线之后出问题，全靠日志回溯。
"""

import logging
import sys

from app.config import settings


def setup_logging() -> None:
    """在程序启动时调用一次即可。"""
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),  # 输出到控制台
        ],
    )

    # 把第三方库的日志调安静一点，不然满屏都是它们的信息
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("chromadb").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """各模块用法： logger = get_logger(__name__)"""
    return logging.getLogger(name)
