"""显式启用后调用真实官方元数据 API；不访问出版商或 PDF。"""

import os

import pytest

from server import lookup_doi, search_crossref, search_openalex


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_METADATA_TESTS") != "1",
        reason="set RUN_LIVE_METADATA_TESTS=1 to call official metadata APIs",
    ),
]

CORE_FIELDS = {
    "title",
    "authors",
    "year",
    "journal/source",
    "DOI",
    "URL",
    "abstract",
    "cited_by_count",
    "open_access",
}


def test_real_doi_lookup() -> None:
    result = lookup_doi("10.1038/s41586-021-03819-2")
    assert result["ok"] is True, result
    assert result["results"][0]["DOI"].lower() == "10.1038/s41586-021-03819-2"
    assert CORE_FIELDS <= result["results"][0].keys()


def test_real_crossref_keyword_search() -> None:
    result = search_crossref("crop residue cover remote sensing", limit=3)
    assert result["ok"] is True, result
    assert result["results"], result
    assert CORE_FIELDS <= result["results"][0].keys()


def test_real_openalex_keyword_and_year_search() -> None:
    result = search_openalex(
        "crop residue cover remote sensing", limit=3, from_year=2010, to_year=2026
    )
    assert result["ok"] is True, result
    assert result["results"], result
    assert CORE_FIELDS <= result["results"][0].keys()
    assert "topics" in result["results"][0]
    assert "related_works" in result["results"][0]
