"""app/online.py 的纯函数与缓存测试。

1.4 的联网路径（arXiv 搜索要 20 秒、Semantic Scholar 会 429）不适合放进默认回归，
这里只测不联网的部分：编号/仓库地址提取、引用计数、缓存读写与搜索结果解析。
"""

import json
from datetime import timedelta

import pytest

from app import online as on


def test_arxiv_id_of_variants():
    assert on.arxiv_id_of("1706.03762v7.pdf") == "1706.03762"
    assert on.arxiv_id_of("https://arxiv.org/abs/2401.00001") == "2401.00001"
    assert on.arxiv_id_of("no id here") == ""


def test_find_github_skips_placeholders():
    assert on.find_github("code: https://github.com/openai/gpt-3.") == "https://github.com/openai/gpt-3"
    assert on.find_github("see https://github.com/xxx/repo for details") == ""
    assert on.find_github("see https://github.com/yourname/your-repo") == ""
    assert on.find_github("nothing to see") == ""


def test_citation_counts_handles_lists_and_ranges():
    counts = on.citation_counts("a [12] b [3, 5] c [7–9] d [12]")
    assert counts[12] == 2
    assert counts[3] == 1
    assert counts[5] == 1
    assert [counts[n] for n in (7, 8, 9)] == [1, 1, 1]


def test_citation_counts_ignores_bad_numbers():
    # 区间太大（10-100）、0、超过 500 的编号都不算
    assert on.citation_counts("[10-100] [0] [501] [abc]") == {}


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(on, "CACHE_DIR", tmp_path)
    return tmp_path


def test_cache_roundtrip_and_ttl(cache_dir):
    on.write_cache("demo key/1", {"items": [1, 2]})
    assert on.read_cache("demo key/1", timedelta(days=1)) == {"items": [1, 2]}
    assert on.cache_time("demo key/1")

    path = on._cache_path("demo key/1")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["saved_at"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert on.read_cache("demo key/1", timedelta(days=1)) is None
    assert on.read_cache_any_age("demo key/1") == {"items": [1, 2]}
    assert on.read_cache_any_age("missing key") is None


def test_parse_search_extracts_results():
    html = (
        '<li class="arxiv-result"><p class="list-title is-inline-block">'
        '<a href="https://arxiv.org/abs/1607.06450">arXiv:1607.06450</a></p>'
        '<p class="title is-5 mathjax">Layer Normalization</p>'
        '<p class="is-size-7"><span class="has-text-black-bis has-text-weight-semibold">Submitted</span>'
        " 25 Jul 2016</p></li>"
        '<li class="arxiv-result"><a href="https://arxiv.org/abs/2401.00001v2">arXiv:2401.00001v2</a>'
        '<p class="title is-5 mathjax">Another <b>Paper</b></p></li>'
        '<li class="arxiv-result"><p>没有结果的块</p></li>'
    )
    rows = on._parse_search(html)
    assert [row["arxiv_id"] for row in rows] == ["1607.06450", "2401.00001"]
    assert rows[0]["title"] == "Layer Normalization"
    assert rows[0]["submitted"] == "25 Jul 2016"
    assert rows[1]["title"] == "Another Paper"
    assert rows[1]["url"] == "https://arxiv.org/abs/2401.00001"
