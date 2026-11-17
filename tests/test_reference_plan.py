"""参考文献推荐列表（GET /paper/{f}/references/plan）的单元测试。

这块的关键不是"能不能列出候选"，而是**理由生成失败时不能把整个接口带崩**：
理由只是锦上添花，没配 Key、额度用完、上游超时都必须回落到客观数字。
所以大部分用例都在打这方面的洞。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import main as main_mod
from app.main import app

client = TestClient(app)


@pytest.fixture()
def fake_env(monkeypatch):
    """把挑候选、库内清单、取标题都替换成固定值，不碰网络与真实 PDF。"""
    ranked = [
        {"arxiv_id": "1706.03762", "title": "Attention Is All You Need", "year": 2017,
         "cites": 12, "cited_by": 100000, "abstract": "The dominant sequence transduction models..."},
        {"arxiv_id": "1810.04805", "title": "BERT", "year": 2018,
         "cites": 5, "cited_by": 80000, "abstract": "We introduce a new language representation model."},
    ]
    monkeypatch.setattr(
        main_mod, "_rank_references", lambda filename, min_cites: (list(ranked), {1: 12, 2: 5}, "1706.03762")
    )
    monkeypatch.setattr(
        main_mod, "library_rows",
        lambda: [{"arxiv_id": "1810.04805", "filename": "1810.04805.pdf", "title": "BERT"},
                 {"arxiv_id": "1706.03762", "filename": "1706.03762v7.pdf", "title": "Attention Is All You Need"}],
    )
    monkeypatch.setattr(main_mod, "_REASON_CACHE", {})
    return ranked


def test_plan_lists_candidates_with_reason(fake_env, monkeypatch):
    """正常路径：两条候选都带上理由，已在库的那条要标 in_library。"""

    async def fake_reason(source_title, item):
        return f"理由：{item['title']}"

    monkeypatch.setattr(main_mod, "_reference_reason", fake_reason)
    resp = client.get("/paper/1706.03762v7.pdf/references/plan?limit=5&min_cites=2")
    assert resp.status_code == 200
    data = resp.json()
    assert data["candidates"] == 2
    assert [it["arxiv_id"] for it in data["items"]] == ["1706.03762", "1810.04805"]
    first = data["items"][0]
    assert first["cites"] == 12
    assert first["cited_by"] == 100000
    assert first["in_library"] is True  # 1706.03762 在库里
    assert first["reason"].startswith("理由：")
    assert data["items"][1]["in_library"] is True


def test_plan_reason_falls_back_when_llm_fails(fake_env, monkeypatch):
    """LLM 抛异常时理由回落到客观数字，接口仍然 200。"""

    async def boom(*_a, **_kw):
        raise RuntimeError("还没有配置 API Key")

    monkeypatch.setattr(main_mod, "llm_chat", boom)
    resp = client.get("/paper/1706.03762v7.pdf/references/plan?limit=5&min_cites=2")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items, "降级后仍要有候选"
    for it in items:
        assert "正文引用" in it["reason"]
        assert "失败" not in it["reason"]


def test_plan_reason_fallback_text_contains_counts(fake_env, monkeypatch):
    """回落文案要带上正文引用次数与 S2 被引次数，用户才知道凭什么推荐。"""

    async def boom(*_a, **_kw):
        raise RuntimeError("upstream timeout")

    monkeypatch.setattr(main_mod, "llm_chat", boom)
    resp = client.get("/paper/1706.03762v7.pdf/references/plan?limit=5&min_cites=2")
    first = resp.json()["items"][0]
    assert "正文引用 12 次" in first["reason"]
    assert "被引 100000 次" in first["reason"]
    assert "2017 年" in first["reason"]


def test_plan_reason_is_cached(fake_env, monkeypatch):
    """同一篇论文点第二次不能重复烧模型额度。"""
    calls = {"n": 0}

    async def counting(*_a, **_kw):
        calls["n"] += 1
        return "一句理由"

    monkeypatch.setattr(main_mod, "llm_chat", counting)
    client.get("/paper/1706.03762v7.pdf/references/plan?limit=5&min_cites=2")
    first = calls["n"]
    assert first == 2, "两条候选各调一次"
    client.get("/paper/1706.03762v7.pdf/references/plan?limit=5&min_cites=2")
    assert calls["n"] == first, "第二次应该全部命中缓存"


def test_plan_respects_limit(fake_env, monkeypatch):
    async def fake_reason(source_title, item):
        return "r"

    monkeypatch.setattr(main_mod, "_reference_reason", fake_reason)
    resp = client.get("/paper/1706.03762v7.pdf/references/plan?limit=1&min_cites=2")
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1
    assert resp.json()["candidates"] == 2  # 候选总数不受 limit 影响


def test_plan_rejects_bad_params():
    """limit 越界要被 422 拦住（而不是悄悄跑一遍烧额度）。"""
    assert client.get("/paper/x.pdf/references/plan?limit=0").status_code == 422
    assert client.get("/paper/x.pdf/references/plan?limit=99").status_code == 422
    assert client.get("/paper/x.pdf/references/plan?min_cites=0").status_code == 422


def test_plan_paper_missing_returns_404():
    resp = client.get("/paper/不存在的论文.pdf/references/plan")
    assert resp.status_code == 404


def test_papers_files_stays_plain_by_default(monkeypatch):
    """不给 meta 时仍是纯文件名字符串数组 —— 老调用方不该被这个改动影响。

    2026-11-16 修正：这里以前断言等于磁盘上的物理文件名。上传入口把文件存成
    ``<paper_id>.pdf``，对外却必须用索引里的上传原名（全站都拿 paper.filename
    当论文标识），所以改成断言"等于索引名、且不含哈希名"。
    """
    monkeypatch.setattr(main_mod, "_scan_paper_names", None, raising=False)
    from app.papers import PAPERS_DIR, _load_index

    alias = {f"{r.paper_id}.pdf": r.filename for r in _load_index()}
    expected = sorted({alias.get(p.name, p.name) for p in PAPERS_DIR.glob("*.pdf")})
    resp = client.get("/papers/files")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"files"}
    assert body["files"] == expected
    assert all(isinstance(x, str) for x in body["files"])

    # 对外标识不能漏出 <paper_id>.pdf。注意：大多数论文的 paper_id 就是文件名 stem
    # （1706.03762v1.pdf 的 id 也叫 1706.03762v1），那种情况本来就看不出区别；
    # 真正要防的是上传入口那种"id 是内容哈希、原名才是标题"的记录。
    hashed = {
        r.paper_id: r.filename
        for r in _load_index()
        if r.paper_id != str(r.filename).rsplit(".", 1)[0]
    }
    leaked = [x for x in body["files"] if x[:-4] in hashed]
    assert not leaked, f"文件名接口漏出了内部哈希名：{leaked}"
    # 同一个名字不能出现两次（索引里存在两条记录指向同一 filename 的历史数据）
    assert len(body["files"]) == len(set(body["files"])), "文件名列表有重复项"


def test_papers_files_meta_returns_titles(monkeypatch):
    """meta=true 时每条带标题：发现页的下拉框要靠它显示论文名而不是文件名。

    2026-11-17 追加 ``theme``：论文分析页要按主题分组勾选论文
    （勾主题 = 批量勾该主题下全部论文），分组依据来自这里而不是
    /graph/survey 的 citable——后者会被 filenames 收窄，当不了全库分组。
    """
    monkeypatch.setattr(
        main_mod, "library_rows",
        lambda: [{"filename": "1706.03762v1.pdf", "title": "Attention Is All You Need"}],
    )
    resp = client.get("/papers/files?meta=true")
    assert resp.status_code == 200
    body = resp.json()
    files = body["files"]
    assert files, "论文目录里应该有 PDF"
    assert all(isinstance(x, dict) and set(x) == {"filename", "title", "theme"} for x in files)
    # theme 允许空串（catalog 里没有的论文），但不许是 None：前端按字符串用
    assert all(isinstance(x["theme"], str) for x in files)
    # 顶层给一份主题清单，前端不用自己再拼
    assert isinstance(body["themes"], list)
    assert body["themes"] == sorted({x["theme"] for x in files if x["theme"]})

    by_name = {x["filename"]: x["title"] for x in files}
    if "1706.03762v1.pdf" in by_name:
        assert by_name["1706.03762v1.pdf"] == "Attention Is All You Need"
    # 索引里查不到的（扫描件等）也要有可用文字，不能是空串
    assert all(x["title"].strip() for x in files)


def test_reference_plan_is_not_throttled_like_single_llm_call():
    """推荐列表一次内部连调多次模型，不能沿用 BUCKET_LLM（突发 3）。

    实测现象：连点 4 次「获取推荐」就 429，用户以为是功能坏了。
    """
    from app import security

    bucket = security._RATE_TABLE["/paper/{filename}/references/plan"]
    # 撞 429 的直接原因是突发容量：BUCKET_LLM 只有 3 个令牌，而这里一次请求
    # 内部要连调 N 次模型、首次 20 秒以上，用户一定会反复点。
    assert bucket.burst > security.BUCKET_LLM.burst, "突发次数要比单次模型调用档更宽"
    # 长期速率可以比单次模型调用更低（单次请求内部已经消耗了多次额度），
    # 这里只要求它不为 0，确保脚本刷仍会被挡住。
    assert bucket.rate > 0


def test_rank_references_rejects_non_arxiv(monkeypatch):
    """非 arXiv 来源的论文取不到引用关系，要给出 400 而不是空列表。"""
    monkeypatch.setattr(main_mod, "safe_paper_path", lambda f: "dummy.pdf")
    monkeypatch.setattr(main_mod, "arxiv_id_of", lambda f: "")
    with pytest.raises(HTTPException) as exc:
        main_mod._rank_references("x.pdf", 2)
    assert exc.value.status_code == 400


# ---------------------------------------------------------------- 引用写法识别
# 背景：citation_counts 只认 [12] 数字体，实测 43 篇里只有 14 篇算得出候选（33%）。
# 其余论文用作者-年份体，数字体数不出东西 → 候选被 min_cites 全过滤 → 用户看到
# 「候选 0 篇」。下面这几条锁住四种写法的识别。


@pytest.mark.parametrize(
    ("text", "expect"),
    [
        # 1. 方括号作者年（Layer Normalization 那种，还会跨行）
        ("as shown [Krizhevsky et al., 2012] and [Ioffe and\nSzegedy, 2015]", {("Krizhevsky", "2012"), ("Ioffe", "2015")}),
        # 2. 圆括号作者年（Llama 2 / ViT / CLIP）
        ("following (Scao et al., 2022) and (Vaswani et al., 2017).", {("Scao", "2022"), ("Vaswani", "2017")}),
        # 3. 叙述式（正文里写 Brown et al. (2020)）
        ("results from Brown et al. (2020) show", {("Brown", "2020")}),
        # 4. 前三作者首字母缩写（GPT-3 的 (RWC, 2019)）
        ("data from (RWC, 2019) was used", {("RWC", "2019")}),
    ],
)
def test_author_year_counts_recognises_styles(text, expect):
    from app.online import author_year_counts

    assert set(author_year_counts(text)) == expect


def test_author_year_counts_handles_latex_author_tag():
    """[Bel+15] / [Mni+16]：两位年份要还原成四位数，缩写保留原样。"""
    from app.online import author_year_counts

    got = author_year_counts("see [Mni+15], [Mni+16] and [Bel+15].")
    assert got[("Mni", "2015")] == 1
    assert got[("Mni", "2016")] == 1
    assert got[("Bel", "2015")] == 1


def test_author_year_counts_ignores_formula_variables():
    """别把公式里的 [y | x]、[W, b] 当成引用。"""
    from app.online import author_year_counts

    assert author_year_counts("E[y | x] = f(a+b), θ = [w1, b1]") == {}


def test_surname_matches_is_prefix_based():
    """[Mni+16] 要能对上 Mnih —— 缩写是姓氏前缀，不是首字母。"""
    from app.online import _surname_matches

    assert _surname_matches("Mni", "Mnih")
    assert _surname_matches("Bel", "Bellemare")
    assert not _surname_matches("Mni", "Wang")


def test_rank_references_falls_back_to_author_year(monkeypatch):
    """数字体一个都数不出来时，必须改用作者-年份，不能返回空列表。

    这是用户报「没有符合条件的参考文献（候选 0 篇）」的直接成因。
    """
    import fitz

    from app.papers import PAPERS_DIR

    real_pdf = next(PAPERS_DIR.glob("*.pdf"))
    body = "We follow (Vaswani et al., 2017) and again (Vaswani et al., 2017).\nReferences\n"
    monkeypatch.setattr(main_mod, "safe_paper_path", lambda f: real_pdf)
    monkeypatch.setattr(main_mod, "arxiv_id_of", lambda f: "1706.03762")
    monkeypatch.setattr(main_mod, "references", lambda aid, n: [
        {"arxiv_id": "1706.03762", "title": "Attention", "year": 2017, "authors": ["Ashish Vaswani"]},
        {"arxiv_id": "1512.03385", "title": "ResNet", "year": 2015, "authors": ["Kaiming He"]},
    ])
    monkeypatch.setattr(fitz, "open", lambda *a, **k: _FakeDoc(body))

    ranked, picked, aid = main_mod._rank_references("x.pdf", 2)
    assert aid == "1706.03762"
    assert "author_year" in picked, "应该走作者-年份回退"
    assert [r["title"] for r in ranked] == ["Attention"], "被提及 2 次的应该留下，0 次的应该被过滤"
    assert ranked[0]["cites"] == 2


class _FakeDoc:
    """替代 fitz.open：只需要能当上下文管理器并吐出固定文本。"""

    def __init__(self, text: str) -> None:
        self._text = text

    def __enter__(self) -> _FakeDoc:
        return self

    def __exit__(self, *exc) -> None:
        return None

    def __iter__(self):
        class _Page:
            def __init__(self, text: str) -> None:
                self._text = text

            def get_text(self) -> str:
                return self._text

        return iter([_Page(self._text)])