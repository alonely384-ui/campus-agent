"""日志配置的启动分隔标记。

桌面版的 app.log 是跨启动追加的，每次启动写一条横幅才分得清哪段是
哪次运行。这里测横幅本身与它的边界（追加、粘行、写不进去）。

为什么绕开 root logger 断言？
    pytest 默认往 root 挂 4 个 handler（_LiveLoggingNullHandler、
    指向 \\\\.\\nul 的 _FileHandler、两个 LogCaptureHandler）。而
    ``logging.basicConfig`` 见到 root 已有 handler 就整个跳过，
    所以测试里 ``handlers=`` 参数根本不生效——断言 ``root.handlers``
    等于在断言 pytest 内部状态，会永远拿不到我们要的那个 FileHandler。
    因此 handler 装配用一个**独立 logger** 来验，横幅内容直接调函数。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from app import logging_config


def _read(path: Path) -> str:
    # RotatingFileHandler 在 Windows 上会一直持有句柄，读之前要自己开流
    with path.open("r", encoding="utf-8") as fh:
        return fh.read()


class TestBanner:
    """横幅的写入内容。直接调 _write_startup_banner，不经过 basicConfig。"""

    def test_has_banner_time_and_pid(self, tmp_path: Path) -> None:
        log_file = tmp_path / "app.log"

        logging_config._write_startup_banner(log_file)

        text = _read(log_file)
        assert logging_config._BANNER in text
        assert "启动 " in text
        assert f"（进程号 {os.getpid()}）" in text

    def test_repeats_once_per_startup(self, tmp_path: Path) -> None:
        """连续两次启动在同一文件里留下两条横幅——这正是可区分的前提。"""
        log_file = tmp_path / "app.log"

        logging_config._write_startup_banner(log_file)
        logging_config._write_startup_banner(log_file)

        assert _read(log_file).count("（进程号 ") == 2

    def test_appends_without_truncating(self, tmp_path: Path) -> None:
        """二次启动不能把上一轮的日志冲掉。"""
        log_file = tmp_path / "app.log"
        log_file.write_text(
            "2026-11-04 15:31:58 | INFO | app.llm | 上一轮的日志\n", encoding="utf-8"
        )

        logging_config._write_startup_banner(log_file)

        text = _read(log_file)
        assert "上一轮的日志" in text
        assert logging_config._BANNER in text

    def test_does_not_glue_to_unterminated_last_line(self, tmp_path: Path) -> None:
        """上一次运行被强杀，末行没有换行符时，横幅不能和它粘成一行。"""
        log_file = tmp_path / "app.log"
        log_file.write_text("2026-11-04 16:00:00 | ERROR | 检索异常", encoding="utf-8")

        logging_config._write_startup_banner(log_file)

        lines = _read(log_file).splitlines()
        assert "2026-11-04 16:00:00 | ERROR | 检索异常" in lines, f"末行被粘住了：{lines}"
        assert logging_config._BANNER in lines

    def test_failure_does_not_raise(self, tmp_path: Path) -> None:
        """横幅写不进去只是没标记，绝不能连累服务启动。"""
        # 用一个目录当日志文件路径：打开即 IsADirectoryError
        bogus = tmp_path / "a_dir"
        bogus.mkdir()

        logging_config._write_startup_banner(bogus)  # 不该抛

    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        """父目录不存在时 setup_logging 会建；手动调则要求已存在，这里只验不炸。"""
        deep = tmp_path / "a" / "b"
        deep.mkdir(parents=True)

        logging_config._write_startup_banner(deep / "app.log")

        assert (deep / "app.log").exists()


class TestHandlerWiring:
    """handler 装配。直接测 _build_handlers，避开 pytest 对 root 的占用。"""

    def test_no_log_file_keeps_single_stream(self, monkeypatch) -> None:
        """不设 LOG_FILE 时只返回控制台 handler，没有文件 handler。"""
        monkeypatch.delenv("LOG_FILE", raising=False)

        handlers = logging_config._build_handlers()

        assert len(handlers) == 1, f"实际 {handlers}"
        assert not isinstance(handlers[0], logging.FileHandler)

    def test_log_file_set_yields_one_file_handler(self, tmp_path, monkeypatch) -> None:
        """设了 LOG_FILE 就多一个文件 handler，且指向那个文件。"""
        log_file = tmp_path / "app.log"
        monkeypatch.setenv("LOG_FILE", str(log_file))

        handlers = logging_config._build_handlers()

        assert len(handlers) == 2, f"实际 {handlers}"
        file_handlers = [h for h in handlers if isinstance(h, logging.FileHandler)]
        assert len(file_handlers) == 1
        assert Path(file_handlers[0].baseFilename) == log_file

    def test_setup_logging_writes_banner(self, tmp_path, monkeypatch) -> None:
        """走完整路径（setup_logging 而不是直接调内部函数）也要有横幅。

        不断言 root.handlers（pytest 已占满），只看文件产物。
        """
        log_file = tmp_path / "app.log"
        monkeypatch.setenv("LOG_FILE", str(log_file))

        logging_config.setup_logging()

        assert "（进程号 " in _read(log_file)