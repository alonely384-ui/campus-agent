"""重排（Cross-Encoder）的单元测试。

原则：测试不依赖 23MB 的模型文件，也不依赖真实 Chroma 库。
- 排序逻辑：把 `score_pairs` 换成一个假函数，验证"按分数降序 + 打上 rerank 标记"；
- 不可用回退：把模型目录指到空目录，验证抛出 RerankUnavailable（调用方据此回退）；
- 证据卡片：通道里带 rerank 时，score_type 必须是 rerank 而不是 rrf。
"""

import pytest

from app.agent import Evidence
from app.rag import rerank
from app.rag.pipeline import Chunk


def test_model_dir_follows_data_dir():
    """模型目录必须落在 DATA_DIR 下，而不是写死成代码所在目录的 data/。

    这是桌面版踩过的坑：打包成 exe 后 BASE_DIR 是 PyInstaller 的临时解压
    目录（_MEIxxxx，每次启动重建且只读），模型实际解压在用户目录
    （DATA_DIR 指向那里），于是重排永远加载失败、静默回退融合排序，
    界面上完全看不出异常。

    直接比较常量而不是改环境变量重载模块：DATA_DIR 在 app.papers 导入时
    就定死了，只 reload 本模块不会重新读取它。未设 DATA_DIR 时
    DATA_DIR 恰好等于 BASE_DIR/data，所以这里只断言"跟着 DATA_DIR"，
    不断言"不等于 BASE_DIR 下的路径"——那样在没有覆盖时会误报。
    """
    from app.papers import DATA_DIR

    assert rerank._MODEL_DIR == DATA_DIR / "models" / "ms-marco-MiniLM-L-6-v2"


def test_rerank_orders_by_cross_encoder_score(monkeypatch):
    items = [
        (Chunk(text="无关段落", source="a.pdf"), 0.03, ["dense"]),
        (Chunk(text="答案段落", source="a.pdf"), 0.02, ["bm25"]),
    ]
    # 关键：分数与融合分相反 —— 重排必须信模型分，不能信原来的 RRF 顺序
    monkeypatch.setattr(rerank, "score_pairs", lambda q, ps: [0.1, 0.9])

    out = rerank.rerank("问题", items, top_k=2)

    assert [chunk.text for chunk, _s, _ch in out] == ["答案段落", "无关段落"]
    assert out[0][2][-1] == "rerank"
    assert out[0][1] == pytest.approx(0.9)


def test_rerank_empty_input_returns_empty():
    assert rerank.rerank("问题", [], top_k=4) == []


def test_missing_model_raises_rerank_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(rerank, "_MODEL_DIR", tmp_path / "not-there")
    monkeypatch.setattr(rerank, "_session", None)
    monkeypatch.setattr(rerank, "_load_failed", False)

    with pytest.raises(rerank.RerankUnavailable):
        rerank.score_pairs("问题", ["段落"])


def test_evidence_marks_rerank_score_type():
    reranked = Evidence(
        rank=1, source="a.pdf", page=7, score=-4.07, channels=["dense", "rerank"], text="t"
    )
    plain = Evidence(
        rank=1, source="a.pdf", page=7, score=0.05, channels=["dense", "bm25"], text="t"
    )

    assert reranked.to_dict()["score_type"] == "rerank"
    assert plain.to_dict()["score_type"] == "rrf"
