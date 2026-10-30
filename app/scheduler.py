"""1.4.2 的「定期刷新」：进程内定时器，不引入新依赖。

为什么不用 APScheduler/celery：赛题只要求"定期刷新 arXiv 列表"，多一个依赖就多一处
安装失败的可能。这里就是一个后台线程 + 每 30 秒看一次表，够用且便宜。

开关（环境变量）：
- ``DISCOVER_SCHEDULE``：``daily``（默认）/ ``weekly`` / ``off``
- ``DISCOVER_HOUR``：每天几点跑，默认 8 点（本地时间）

安全阀：pytest 运行时绝不启动（否则测试会挂后台线程去联网）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from datetime import datetime, timedelta

from app.logging_config import get_logger

logger = get_logger(__name__)

SCHEDULE_ENV = "DISCOVER_SCHEDULE"
HOUR_ENV = "DISCOVER_HOUR"
DEFAULT_HOUR = 8
VALID = {"daily", "weekly", "off"}

_lock = threading.Lock()
_state: dict[str, object] = {
    "thread": None,
    "stop": False,
    "mode": "off",
    "next_run": "",
    "last_run": "",
    "last_error": "",
}


def _env_setting(name: str, default: str) -> str:
    """按 环境变量 → settings 字段 → 默认值 的顺序取一个字符串配置。

    顺序必须和 pydantic-settings 一致（环境变量 > .env > 默认值），
    否则会出现"进程环境里明明是 off，定时器却按 .env 的 weekly 跑"。

    为什么不能只读 os.environ：pydantic-settings 读 .env 时只填 Settings 对象，
    不会把键塞进 os.environ，所以 .env 里写的 DISCOVER_SCHEDULE=weekly
    在重启后 os.getenv 是拿不到的，定时器会静默退回 daily。
    两个来源都要看，且顺序要对。
    """
    env = os.getenv(name)
    if env is not None and env != "":
        return env

    from app.config import settings

    field = name.lower()
    if field in type(settings).model_fields:
        value = getattr(settings, field, None)
        if value is not None and str(value) != "":
            return str(value)
    return default


def schedule_mode() -> str:
    """读周期配置；非法值按 daily 处理。"""
    mode = _env_setting(SCHEDULE_ENV, "daily").strip().lower()
    return mode if mode in VALID else "daily"


def _hour() -> int:
    try:
        return max(0, min(23, int(_env_setting(HOUR_ENV, str(DEFAULT_HOUR)))))
    except ValueError:
        return DEFAULT_HOUR


def next_run_time(mode: str, now: datetime | None = None) -> datetime:
    """算下一次该跑的时间：daily=最近的一个 HH:00，weekly=下一个周一 HH:00。"""
    now = now or datetime.now()
    candidate = now.replace(hour=_hour(), minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    if mode == "weekly":
        # 周一=0；把候选日推到下一个周一，若已越过则顺延一周
        delta = (0 - candidate.weekday()) % 7
        candidate += timedelta(days=delta)
        if candidate <= now:
            candidate += timedelta(days=7)
    return candidate


def _run_once(mode: str) -> None:
    """跑一次刷新；异常只记录，不能让线程死掉。"""
    from app.recommend import build_recommendations

    try:
        payload = asyncio.run(build_recommendations(period=mode if mode != "weekly" else "weekly"))
        items = len(payload.get("items") or [])
        _state["last_run"] = datetime.now().isoformat(timespec="seconds")
        _state["last_error"] = ""
        logger.info("定时推荐刷新完成：%s 条（周期 %s）", items, mode)
    except Exception as exc:  # 网络坏、模型挂都算，下一轮再试
        _state["last_error"] = f"{type(exc).__name__}: {exc}"
        logger.warning("定时推荐刷新失败：%s", exc)


def _loop(mode: str) -> None:
    while not _state["stop"]:
        target = datetime.fromisoformat(str(_state["next_run"]))
        wait = (target - datetime.now()).total_seconds()
        if wait <= 0:
            _run_once(mode)
            _state["next_run"] = next_run_time(mode).isoformat(timespec="seconds")
            continue
        time.sleep(min(30.0, max(1.0, wait)))


def start(mode: str | None = None) -> dict[str, object]:
    """启动定时器（重复调用无副作用）；返回状态，含下次运行时间。

    mode 与当前运行的模式**不同**时会先停掉旧线程再按新模式起，
    否则设置页把 daily 改成 weekly 却因为"线程还活着"直接返回，
    界面显示成功、实际仍按 daily 跑。
    """
    mode = mode or schedule_mode()
    if mode == "off":
        # 关掉时要把已经在跑的线程也停掉，否则 status() 里 running 还是 True，
        # 用户会以为"关了还在跑"（测试里也会因此互相污染）。
        _state["stop"] = True
        _state.update({"mode": "off", "next_run": ""})
        info = status()
        info["running"] = False
        return info
    with _lock:
        thread = _state.get("thread")
        if isinstance(thread, threading.Thread) and thread.is_alive():
            if _state.get("mode") == mode:
                return status()
            # 模式变了：先让旧线程退出，再重建
            _state["stop"] = True
        _state["stop"] = False
        _state["mode"] = mode
        _state["next_run"] = next_run_time(mode).isoformat(timespec="seconds")
        worker = threading.Thread(target=_loop, args=(mode,), name="discover-scheduler", daemon=True)
        _state["thread"] = worker
        worker.start()
    logger.info("定时推荐刷新已启动：%s，下次 %s", mode, _state["next_run"])
    return status()


def stop() -> dict[str, object]:
    """停掉定时器（测试与关服时用）。"""
    _state["stop"] = True
    _state["mode"] = "off"
    _state["next_run"] = ""
    return status()


def status() -> dict[str, object]:
    thread = _state.get("thread")
    mode = _state.get("mode") or "off"
    # 关掉之后旧线程要等当前这轮 sleep 结束才真正退出（最长 30 秒），
    # 这段时间 is_alive() 还是 True，界面上会短暂显示「已关但仍运行」。
    # 只要模式是 off，对用户来说就是没在跑，直接报 False。
    alive = mode != "off" and isinstance(thread, threading.Thread) and thread.is_alive()
    return {
        "mode": mode,
        "running": alive,
        "next_run": _state.get("next_run") or "",
        "last_run": _state.get("last_run") or "",
        "last_error": _state.get("last_error") or "",
        "hour": _hour(),
    }


def autostart() -> dict[str, object] | None:
    """被 app 导入时调用：pytest 或显式关闭时什么都不做。"""
    if (
        "pytest" in sys.modules  # 测试进程里导入 app.main 也别起线程
        or os.environ.get("PYTEST_CURRENT_TEST")
        or schedule_mode() == "off"
    ):
        return None
    return start()
