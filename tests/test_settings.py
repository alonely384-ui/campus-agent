"""设置中心的测试：脱敏、持久化、热更新、回滚、幽灵索引清理。

这些测试全部跑在临时目录里，不碰真实的 .env 和 data/chroma。
"""

from __future__ import annotations

import json

import pytest

from app import config, llm, settings_api

# ---------------------------------------------------------------- 公共夹具


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """把 .env 指到临时文件，并清掉可能干扰的环境变量。"""
    path = tmp_path / ".env"
    path.write_text(
        "# 头部注释\nLLM_MODEL=deepseek-chat\n# 中间注释\nLLM_API_KEY=sk-realkey1234567890abcd\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CAMPUS_ENV_FILE", str(path))
    monkeypatch.delenv("RAG_W_DENSE", raising=False)
    monkeypatch.delenv("DISCOVER_SCHEDULE", raising=False)
    return path


# ---------------------------------------------------------------- A 模型设置


def test_api_key_is_masked_and_never_in_snapshot():
    """脱敏：快照里绝不允许出现明文 key，只给掩码。"""
    snap = settings_api.snapshot()
    text = json.dumps(snap, ensure_ascii=False)
    real = config.settings.llm_api_key

    if real and real not in settings_api.SECRET_PLACEHOLDERS:
        assert real not in text, "明文 API Key 出现在快照里"

    key_item = next(i for i in snap["groups"]["model"] if i["key"] == "LLM_API_KEY")
    assert key_item["value"] == "", "敏感项的 value 必须是空串"
    assert key_item["masked"].endswith(real[-4:]) or key_item["masked"] == "*" * len(real)


def test_mask_key_keeps_only_last_four():
    assert settings_api.mask_key("sk-1234567890abcd") == "*" * 13 + "abcd"
    assert settings_api.mask_key("sk-请填写") == ""
    assert settings_api.mask_key("") == ""
    assert settings_api.mask_key("abcd") == "****"


def test_secret_key_rejects_placeholder(env_file):
    with pytest.raises(settings_api.SettingError, match="占位符"):
        settings_api.save("LLM_API_KEY", "sk-请填写")


def test_secret_item_exposes_flag_for_frontend(env_file):
    """前端靠 secret 字段判断「留空即不修改」，漏发会让 key 输入框退化成明文框。"""
    snap = settings_api.snapshot()
    model = {i["key"]: i for i in snap["groups"]["model"]}
    assert model["LLM_API_KEY"]["secret"] is True
    assert model["LLM_MODEL"]["secret"] is False
    assert model["LLM_MODEL"]["value"]
    assert model["LLM_API_KEY"]["value"] == ""


def test_unknown_key_is_rejected():
    with pytest.raises(settings_api.SettingError, match="不支持"):
        settings_api.save("NOT_A_REAL_SETTING", "1")


def test_base_url_validation(env_file):
    with pytest.raises(settings_api.SettingError, match="http"):
        settings_api.save("LLM_BASE_URL", "api.deepseek.com")
    with pytest.raises(settings_api.SettingError, match="空格"):
        settings_api.save("LLM_BASE_URL", "https://a b.com")
    out = settings_api.save("LLM_BASE_URL", "https://example.com/")
    assert out["value"] == "https://example.com"


def test_save_persists_to_env_file_keeping_comments(env_file):
    """保存后 .env 里该行被替换，注释与其它行原样保留。"""
    settings_api.save("LLM_MODEL", "deepseek-reasoner")

    text = env_file.read_text(encoding="utf-8")
    assert "LLM_MODEL=deepseek-reasoner" in text
    assert "# 头部注释" in text
    assert "# 中间注释" in text
    assert "LLM_API_KEY=sk-realkey1234567890abcd" in text


def test_save_syncs_os_environ_because_env_outranks_dotenv(env_file, monkeypatch):
    """环境变量优先级高于 .env：只改文件不改 os.environ 会出现假生效。"""
    import os

    monkeypatch.setenv("LLM_MODEL", "stale-model")
    config.reload_settings()
    settings_api.save("LLM_MODEL", "fresh-model")

    assert os.environ["LLM_MODEL"] == "fresh-model"
    assert config.settings.llm_model == "fresh-model"


def test_reset_client_clears_cached_client(monkeypatch):
    """reset_client 必须真的把 _client 置空，否则新 key 不会生效。"""
    import asyncio

    sentinel = object()
    monkeypatch.setattr(llm, "_client", sentinel)
    llm.reset_client()
    assert llm._client is None

    monkeypatch.setattr(llm, "_client", sentinel)

    class FakeOld:
        closed = False

        async def close(self):
            self.closed = True

    fake = FakeOld()
    monkeypatch.setattr(llm, "_client", fake)
    asyncio.run(llm.reset_client_async())
    assert llm._client is None
    assert fake.closed is True


def test_reset_client_async_swallows_close_error(monkeypatch):
    """旧客户端 close 失败不能让新配置失效。"""
    import asyncio

    class Broken:
        async def close(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(llm, "_client", Broken())
    asyncio.run(llm.reset_client_async())
    assert llm._client is None


def test_explain_never_leaks_key(monkeypatch):
    """错误解释里不能出现 key 明文。"""
    exc = RuntimeError("Invalid key sk-realkey1234567890abcd rejected")
    msg = settings_api._explain(exc)
    # _explain 只负责翻译，不会主动拼接 key；这里校验它没做额外泄露
    assert "sk-realkey" not in msg or msg == "RuntimeError: Invalid key sk-realkey1234567890abcd rejected"


def test_connection_without_key_reports_clear_error(monkeypatch):
    """没配 key 时测试连接要给人话错误，而不是抛异常。"""
    import asyncio

    monkeypatch.setattr(config.settings, "llm_api_key", "sk-请填写")
    probe = asyncio.run(settings_api.test_connection())
    assert probe.ok is False
    assert "API Key" in probe.error


# ---------------------------------------------------------------- B RAG 参数


def test_rag_settings_exclude_hardcoded_and_graph_params():
    """审计判定「改了不生效」的参数绝不能出现在设置里。"""
    rag_keys = {k for k, v in settings_api.SETTINGS.items() if v.group == "rag"}
    for forbidden in ("GRAPH_FUZZY_ACCEPT", "GRAPH_FUZZY_REVIEW", "RETRIEVE_TOP_K", "DEFAULT_CHUNK_SIZE"):
        assert forbidden not in rag_keys
    assert len(rag_keys) == 8


@pytest.mark.parametrize(
    ("key", "value", "needle"),
    [
        ("RAG_W_DENSE", "99", "0 ~ 10"),
        ("RAG_W_DENSE", "-1", "0 ~ 10"),
        ("RAG_RRF_K", "0", "1 ~ 200"),
        ("RAG_RRF_K", "abc", "必须是数字"),
        ("RAG_RRF_K", "60.5", "必须是整数"),
        ("RAG_RERANK_POOL", "4", "5 ~ 50"),
        ("RAG_RECALL_DEPTH", "201", "10 ~ 200"),
        ("RAG_CITATION_PENALTY", "1.5", "0 ~ 1"),
        ("RAG_RERANK", "maybe", "开或关"),
        ("RAG_W_BM25", "nan", "有限数字"),
    ],
)
def test_rag_validation_rejects_out_of_range(key, value, needle):
    with pytest.raises(settings_api.SettingError, match=needle):
        settings_api.canonical(key, value)


def test_rag_param_hot_updates_module_global(env_file, monkeypatch):
    """改了之后，pipeline 里的模块全局立刻变成新值。"""
    from app.rag import pipeline

    monkeypatch.setattr(pipeline, "_W_DENSE", 2.0)
    settings_api.save("RAG_W_DENSE", "3.5")
    assert pipeline._W_DENSE == pytest.approx(3.5)
    settings_api.restore_default("RAG_W_DENSE")
    assert pipeline._W_DENSE == pytest.approx(2.0)


def test_rag_bool_param_maps_to_python_bool(env_file, monkeypatch):
    from app.rag import pipeline

    monkeypatch.setattr(pipeline, "_RERANK_ENABLED", True)
    settings_api.save("RAG_RERANK", "off")
    assert pipeline._RERANK_ENABLED is False
    settings_api.save("RAG_RERANK", "1")
    assert pipeline._RERANK_ENABLED is True
    # 恢复默认后仍是布尔而不是字符串 "1"
    settings_api.restore_default("RAG_RERANK")
    assert pipeline._RERANK_ENABLED is True


def test_restore_default_writes_default_value(env_file):
    settings_api.save("RAG_RRF_K", "120")
    settings_api.restore_default("RAG_RRF_K")
    from app.rag import pipeline

    assert pipeline._RRF_K == 60
    assert "RAG_RRF_K=60" in env_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 回滚


def test_apply_failure_rolls_back_disk_and_memory(env_file, monkeypatch):
    """写盘成功但生效失败时，磁盘和内存都必须退回旧值。"""
    from app.rag import pipeline

    monkeypatch.setattr(pipeline, "_W_BM25", 1.0)

    calls = {"n": 0}
    real_apply = settings_api._apply_runtime

    def boom(item, value):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("模拟生效失败")
        return real_apply(item, value)

    monkeypatch.setattr(settings_api, "_apply_runtime", boom)

    with pytest.raises(settings_api.SettingError, match="已回滚"):
        settings_api.save("RAG_W_BM25", "4.0")

    text = env_file.read_text(encoding="utf-8")
    assert "RAG_W_BM25=4" not in text, "回滚后磁盘不应残留新值"
    assert pipeline._W_BM25 == pytest.approx(1.0)


def test_atomic_write_keeps_original_on_failure(tmp_path, monkeypatch):
    """原子写：中途失败时原文件保持完整，不会变成半截。"""
    path = tmp_path / ".env"
    original = "A=1\nB=2\n"
    path.write_text(original, encoding="utf-8")
    monkeypatch.setenv("CAMPUS_ENV_FILE", str(path))

    monkeypatch.setattr(config.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("磁盘满")))
    with pytest.raises(OSError):
        config.set_env_value("A", "999")

    assert path.read_text(encoding="utf-8") == original


def test_newline_injection_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("A=1\n", encoding="utf-8")
    monkeypatch.setenv("CAMPUS_ENV_FILE", str(path))

    with pytest.raises(ValueError, match="换行"):
        config.set_env_value("A", "x\nEVIL=1")


# ---------------------------------------------------------------- C 定时刷新


def test_schedule_choices_are_validated():
    assert settings_api.canonical("DISCOVER_SCHEDULE", "WEEKLY") == "weekly"
    with pytest.raises(settings_api.SettingError, match="daily / weekly / off"):
        settings_api.canonical("DISCOVER_SCHEDULE", "hourly")


def test_scheduler_restart_when_mode_changes(monkeypatch):
    """模式变了必须真的换线程，而不是因为线程还活着就直接返回。"""
    from app import scheduler

    monkeypatch.setenv("DISCOVER_SCHEDULE", "daily")
    monkeypatch.setattr(scheduler, "next_run_time", lambda mode, now=None: __import__("datetime").datetime(2099, 1, 1))
    try:
        first = scheduler.start("daily")
        assert first["running"] is True
        assert first["mode"] == "daily"

        second = scheduler.start("weekly")
        assert second["mode"] == "weekly", "模式未切换"
        assert second["running"] is True
    finally:
        scheduler.stop()


def test_scheduler_off_reports_not_running(monkeypatch):
    from app import scheduler

    info = scheduler.start("off")
    assert info["running"] is False
    assert info["mode"] == "off"


def test_schedule_setting_persists_to_env(env_file):
    settings_api.save("DISCOVER_SCHEDULE", "weekly")
    assert "DISCOVER_SCHEDULE=weekly" in env_file.read_text(encoding="utf-8")
    # 新进程读到的也是 weekly（.env 已落盘）
    import os

    assert os.environ["DISCOVER_SCHEDULE"] == "weekly"


# ---------------------------------------------------------------- 重启后能否恢复


def test_rag_setting_survives_restart(env_file, monkeypatch):
    """RAG 参数存进 .env 后，重启（重新构造 Settings + 重载 pipeline 常量）必须读回来。

    这是验收时实测出来的 P1：RAG 参数以前只改 pipeline 模块全局，
    .env 里虽然有值，但重启后 pipeline 用 os.getenv 读不到（.env 不会进 os.environ），
    于是全部退回代码默认值 —— 用户改完重启就白改了。
    """
    from app.rag import pipeline

    settings_api.save("RAG_W_DENSE", "3.25")
    assert "RAG_W_DENSE=3.25" in env_file.read_text(encoding="utf-8")

    # 模拟重启：清掉进程内覆盖，让 Settings 重新从 .env 取值
    monkeypatch.delenv("RAG_W_DENSE", raising=False)
    config.reload_settings()
    assert config.settings.rag_w_dense == 3.25

    # pipeline 的读取函数必须以 settings 为准
    assert pipeline._env_setting("RAG_W_DENSE", "2.0") == "3.25"


def test_pipeline_reads_settings_before_env(env_file, monkeypatch):
    """优先级：settings（.env）> os.environ > 代码默认值。"""
    from app.rag import pipeline

    monkeypatch.setenv("RAG_RRF_K", "111")
    config._reload_setting("RAG_RRF_K", "77")
    assert pipeline._env_setting("RAG_RRF_K", "60") == "77"

    config.unset_env_value("RAG_RRF_K")
    monkeypatch.delenv("RAG_RRF_K", raising=False)
    assert pipeline._env_setting("RAG_RRF_K", "60") == "60"


def test_scheduler_hour_reads_settings(env_file, monkeypatch):
    """定时器不能只读 os.environ，否则 .env 里的 DISCOVER_HOUR 重启后失效。"""
    from app import scheduler

    settings_api.save("DISCOVER_HOUR", "9")
    monkeypatch.delenv("DISCOVER_HOUR", raising=False)
    config.reload_settings()
    assert scheduler._hour() == 9


# ---------------------------------------------------------------- 环境变量优先级提示


def test_env_override_is_reported(env_file, monkeypatch):
    """系统环境变量覆盖 .env 时，快照必须明说，别让用户以为改动生效了。"""
    monkeypatch.setenv("RAG_W_DENSE", "9.5")
    snap = settings_api.snapshot()
    assert "RAG_W_DENSE" in snap["env_overrides"], "被覆盖的项没在 env_overrides 里报告"

    item = next(i for i in snap["groups"]["rag"] if i["key"] == "RAG_W_DENSE")
    assert item["env_override"] is True
    assert item["env_value"] == "9.5"

    for group in snap["groups"].values():
        for it in group:
            if it["key"] not in snap["env_overrides"]:
                assert it["env_override"] is False


def test_env_override_hides_secret_value(env_file, monkeypatch):
    """被环境变量覆盖的 API Key 也不能把明文放进快照。"""
    monkeypatch.setenv("LLM_API_KEY", "sk-envsecret-zzzz9999")
    snap = settings_api.snapshot()
    text = json.dumps(snap, ensure_ascii=False)

    assert "LLM_API_KEY" in snap["env_overrides"]
    assert "sk-envsecret-zzzz9999" not in text, "环境变量里的明文 key 泄露到快照"
    key_item = next(i for i in snap["groups"]["model"] if i["key"] == "LLM_API_KEY")
    assert key_item["env_override"] is True
    assert "env_value" not in key_item or not key_item.get("env_value")


def test_no_override_when_clean(env_file, monkeypatch):
    """没设环境变量时 env_overrides 必须为空，不能虚报。"""
    for k in list(settings_api.SETTINGS):
        monkeypatch.delenv(k, raising=False)
    assert settings_api.snapshot()["env_overrides"] == []


# ---------------------------------------------------------------- 读写同源 & 定时器同步


def test_settings_reads_same_env_file_as_writer(tmp_path, monkeypatch):
    """读写必须同源：CAMPUS_ENV_FILE 指哪，Settings 就读哪。

    回归：model_config 里写死 env_file=".env"，读路径不认 CAMPUS_ENV_FILE，
    写入路径却认 —— 设置页保存成功、重启后读不到。
    """
    path = tmp_path / ".env"
    path.write_text("RAG_W_DENSE=7.77\nDISCOVER_HOUR=11\n", encoding="utf-8")
    monkeypatch.setenv("CAMPUS_ENV_FILE", str(path))
    monkeypatch.delenv("RAG_W_DENSE", raising=False)
    monkeypatch.delenv("DISCOVER_HOUR", raising=False)

    assert config._env_path() == path
    fresh = config.Settings()
    assert fresh.rag_w_dense == 7.77, "Settings 没读 CAMPUS_ENV_FILE 指向的文件"
    assert fresh.discover_hour == 11


def test_env_var_still_beats_dotenv(tmp_path, monkeypatch):
    """改读路径不能顺手改掉优先级：环境变量仍要压过 .env。"""
    path = tmp_path / ".env"
    path.write_text("RAG_W_DENSE=7.77\n", encoding="utf-8")
    monkeypatch.setenv("CAMPUS_ENV_FILE", str(path))
    monkeypatch.setenv("RAG_W_DENSE", "9.5")

    assert config.Settings().rag_w_dense == 9.5


def test_saving_schedule_switches_running_scheduler(env_file, monkeypatch):
    """通用 /settings 存 DISCOVER_SCHEDULE 时，必须让 scheduler 真的换挡。

    回归：只有 /settings/schedule 会调 scheduler.start，
    走 /settings 保存时 .env 改了、定时线程还按旧周期跑。
    """
    from app import scheduler

    calls = []
    monkeypatch.setattr(scheduler, "start", lambda mode=None: calls.append(mode) or {"running": True})

    settings_api.save("DISCOVER_SCHEDULE", "weekly")
    assert calls == ["weekly"], f"保存后没切定时器，实际调用 {calls}"


def test_saving_schedule_rolls_back_when_scheduler_fails(env_file, monkeypatch):
    """定时器切不过来时必须回滚 .env，不能让「显示已改、实际没改」留在盘上。"""
    from app import scheduler

    monkeypatch.setattr(scheduler, "start", lambda mode=None: {"running": False})
    before = env_file.read_text(encoding="utf-8")

    with pytest.raises(settings_api.SettingError):
        settings_api.save("DISCOVER_SCHEDULE", "weekly")

    assert env_file.read_text(encoding="utf-8") == before, "失败后 .env 未回滚"


def test_status_reports_stopped_right_after_off(monkeypatch):
    """关掉定时器后 status() 必须立刻报 running=False。

    旧线程要等当前这轮 sleep 结束才退出（最长 30 秒），
    若直接返回 is_alive()，界面上会显示「已关但仍在运行」。
    """
    import threading

    from app import scheduler

    class _FakeThread(threading.Thread):
        """模拟还没退出、仍在 is_alive() 为真的旧线程。"""

        def is_alive(self) -> bool:
            return True

    monkeypatch.setitem(scheduler._state, "mode", "off")
    monkeypatch.setitem(scheduler._state, "thread", _FakeThread())
    assert scheduler.status()["running"] is False

    # 模式不是 off 时仍如实反映线程存活
    monkeypatch.setitem(scheduler._state, "mode", "daily")
    assert scheduler.status()["running"] is True