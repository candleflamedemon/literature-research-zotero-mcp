"""Literature MCP 的工具注册、字段规范化和异常处理测试。"""

import asyncio

import httpx

import server
from server import (
    add_item_to_collection,
    add_note,
    add_paper_by_doi,
    add_paper_by_metadata,
    add_tags,
    authorize_zotero_write,
    check_institution_access,
    create_collection,
    diagnose_access_environment,
    explain_network_routing,
    extract_references,
    get_zotero_item,
    inspect_excel_schema,
    list_collection_items,
    list_collections,
    list_excel_sheets,
    lookup_doi,
    mcp,
    ping,
    search_crossref,
    search_openalex,
    search_zotero,
    preview_references,
    remove_item_from_collection,
    zotero_status,
)


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


def _response(status_code: int, payload: dict | None = None, **headers: str) -> httpx.Response:
    request = httpx.Request("GET", "https://example.test/metadata")
    return httpx.Response(status_code, json=payload or {}, headers=headers, request=request)


def test_tools_registered_and_ping_works() -> None:
    tools = asyncio.run(mcp.list_tools())
    assert [tool.name for tool in tools] == [
        "elsevier_api_status",
        "check_elsevier_entitlement",
        "ping",
        "lookup_doi",
        "search_crossref",
        "search_openalex",
        "zotero_status",
        "search_zotero",
        "list_collections",
        "list_collection_items",
        "get_zotero_item",
        "authorize_zotero_write",
        "create_collection",
        "add_item_to_collection",
        "remove_item_from_collection",
        "add_paper_by_doi",
        "add_paper_by_metadata",
        "add_tags",
        "add_note",
        "list_excel_sheets",
        "inspect_excel_schema",
        "preview_references",
        "extract_references",
        "find_oa_fulltext",
        "classify_fulltext_access",
        "fetch_oa_pdf",
        "fetch_institutional_pdf",
        "queue_browser_access",
        "attach_pdf_to_zotero",
        "acquisition_report",
        "diagnose_access_environment",
        "check_institution_access",
        "explain_network_routing",
    ]
    result = asyncio.run(mcp.call_tool("ping", {}))
    assert result.is_error is False
    assert result.content[0].text == "Literature MCP 工作正常"
    assert ping() == "Literature MCP 工作正常"


def test_lookup_doi_normalises_crossref_record(monkeypatch) -> None:
    payload = {
        "message": {
            "title": ["<jats:title>Example &amp; Paper</jats:title>"],
            "author": [{"given": "Ada", "family": "Lovelace"}],
            "published-online": {"date-parts": [[2024, 2, 1]]},
            "container-title": ["Example Journal"],
            "DOI": "10.1234/example",
            "URL": "https://doi.org/10.1234/example",
            "abstract": "<jats:p>An abstract.</jats:p>",
            "is-referenced-by-count": 7,
        }
    }
    monkeypatch.setattr(server.httpx, "get", lambda *args, **kwargs: _response(200, payload))

    result = lookup_doi("https://doi.org/10.1234/example")
    record = result["results"][0]

    assert result["ok"] is True
    assert CORE_FIELDS <= record.keys()
    assert record["title"] == "Example & Paper"
    assert record["authors"] == ["Ada Lovelace"]
    assert record["year"] == 2024
    assert record["abstract"] == "An abstract."

    mcp_result = asyncio.run(
        mcp.call_tool("lookup_doi", {"doi": "10.1234/example"})
    )
    assert mcp_result.is_error is False


def test_missing_fields_do_not_break_searches(monkeypatch) -> None:
    responses = iter(
        [
            _response(200, {"message": {"items": [{}]}}),
            _response(200, {"results": [{}]}),
        ]
    )
    monkeypatch.setattr(server.httpx, "get", lambda *args, **kwargs: next(responses))

    crossref_record = search_crossref("missing fields")["results"][0]
    openalex_record = search_openalex("missing fields")["results"][0]

    assert CORE_FIELDS <= crossref_record.keys()
    assert crossref_record["authors"] == []
    assert CORE_FIELDS <= openalex_record.keys()
    assert openalex_record["topics"] == []


def test_openalex_abstract_oa_topics_and_relations(monkeypatch) -> None:
    payload = {
        "results": [
            {
                "title": "OpenAlex Example",
                "publication_year": 2023,
                "doi": "https://doi.org/10.1234/openalex",
                "authorships": [{"author": {"display_name": "Grace Hopper"}}],
                "primary_location": {
                    "landing_page_url": "https://doi.org/10.1234/openalex",
                    "source": {"display_name": "Open Journal"},
                },
                "abstract_inverted_index": {"Hello": [0], "world": [1]},
                "cited_by_count": 11,
                "open_access": {"is_oa": True, "oa_status": "gold"},
                "topics": [{"id": "https://openalex.org/T1", "display_name": "AI", "score": 0.9}],
                "referenced_works": ["https://openalex.org/W1"],
                "related_works": ["https://openalex.org/W2"],
            }
        ]
    }
    monkeypatch.setattr(server.httpx, "get", lambda *args, **kwargs: _response(200, payload))

    record = search_openalex("artificial intelligence")["results"][0]

    assert record["abstract"] == "Hello world"
    assert record["open_access"] == {"is_oa": True, "status": "gold"}
    assert record["topics"][0]["name"] == "AI"
    assert record["referenced_works"] == ["https://openalex.org/W1"]
    assert record["related_works"] == ["https://openalex.org/W2"]


def test_404_is_structured(monkeypatch) -> None:
    monkeypatch.setattr(server.httpx, "get", lambda *args, **kwargs: _response(404))
    result = lookup_doi("10.1234/not-found")
    assert result["ok"] is False
    assert result["error"]["code"] == "not_found"
    assert result["error"]["status_code"] == 404


def test_429_is_structured(monkeypatch) -> None:
    monkeypatch.setattr(
        server.httpx,
        "get",
        lambda *args, **kwargs: _response(429, **{"Retry-After": "12"}),
    )
    result = search_openalex("rate limit")
    assert result["ok"] is False
    assert result["error"]["code"] == "rate_limited"
    assert result["error"]["retry_after_seconds"] == 12


def test_timeout_is_structured(monkeypatch) -> None:
    def raise_timeout(*args, **kwargs):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(server.httpx, "get", raise_timeout)
    result = search_crossref("timeout")
    assert result["ok"] is False
    assert result["error"]["code"] == "timeout"


def test_year_validation_does_not_call_network(monkeypatch) -> None:
    def fail_if_called(*args, **kwargs):
        raise AssertionError("network should not be called")

    monkeypatch.setattr(server.httpx, "get", fail_if_called)
    assert search_crossref("paper", year=999)["error"]["code"] == "invalid_input"
    assert search_openalex("paper", from_year=2025, to_year=2024)["error"]["code"] == "invalid_input"


class _FakeZoteroClient:
    def __init__(self, response=None, error=None, capture=None, **kwargs):
        self.response = response
        self.error = error
        self.capture = capture if capture is not None else {}
        self.capture["client_options"] = kwargs

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def get(self, path, params=None):
        self.capture["method"] = "GET"
        self.capture["path"] = path
        self.capture["params"] = params
        if self.error:
            raise self.error
        return self.response


def test_zotero_status_uses_only_local_get_and_ignores_proxies(monkeypatch) -> None:
    capture = {}
    response = _response(200)
    monkeypatch.setattr(
        server.httpx,
        "Client",
        lambda **kwargs: _FakeZoteroClient(response=response, capture=capture, **kwargs),
    )

    result = zotero_status()

    assert result["ok"] is True
    assert result["running"] is True
    assert result["local_api_enabled"] is True
    assert str(capture["client_options"]["base_url"]) == "http://localhost:23119/api/"
    assert capture["client_options"]["trust_env"] is False
    assert capture["client_options"]["follow_redirects"] is False
    assert capture["method"] == "GET"


def test_zotero_not_running_is_clear(monkeypatch) -> None:
    request = httpx.Request("GET", "http://localhost:23119/api/")
    error = httpx.ConnectError("connection refused", request=request)
    monkeypatch.setattr(
        server.httpx,
        "Client",
        lambda **kwargs: _FakeZoteroClient(error=error, **kwargs),
    )

    result = zotero_status()

    assert result["ok"] is False
    assert result["running"] is False
    assert result["error"]["code"] == "zotero_not_running"


def test_zotero_local_api_disabled_is_clear(monkeypatch) -> None:
    monkeypatch.setattr(
        server.httpx,
        "Client",
        lambda **kwargs: _FakeZoteroClient(response=_response(403), **kwargs),
    )

    result = zotero_status()

    assert result["ok"] is False
    assert result["running"] is True
    assert result["local_api_enabled"] is False
    assert result["error"]["code"] == "local_api_disabled"


def test_list_collections_returns_minimal_fields(monkeypatch) -> None:
    payload = [
        {
            "key": "ABCDEFGH",
            "data": {"name": "Research", "parentCollection": False},
            "meta": {"numItems": 3, "numCollections": 1},
        }
    ]
    monkeypatch.setattr(server, "_zotero_json", lambda *args, **kwargs: (payload, None))

    result = list_collections(limit=2)

    assert result["count"] == 1
    assert result["results"][0] == {
        "collection_key": "ABCDEFGH",
        "name": "Research",
        "parent_collection": None,
        "num_items": 3,
        "num_subcollections": 1,
    }


def test_list_collection_items_returns_minimal_bibliographic_fields(monkeypatch) -> None:
    payload = [
        {
            "key": "ZXCVBNM1",
            "data": {
                "itemType": "journalArticle",
                "title": "Collection Article",
                "date": "2024",
                "DOI": "10.1234/collection",
                "collections": ["ABCDEFGH"],
                "path": "C:/private/file.pdf",
                "note": "private note body",
            },
        }
    ]
    captured = {}

    def fake_json(path, params=None):
        captured.update(path=path, params=params)
        return payload, None

    monkeypatch.setattr(server, "_zotero_json", fake_json)

    result = list_collection_items("abcdefgh", limit=20)

    assert result["collection_key"] == "ABCDEFGH"
    assert result["count"] == 1
    assert result["results"][0]["item_key"] == "ZXCVBNM1"
    assert "path" not in result["results"][0]
    assert "note" not in result["results"][0]
    assert captured["path"].endswith("/collections/ABCDEFGH/items/top")


def test_search_zotero_returns_titles_without_private_file_fields(monkeypatch) -> None:
    payload = [
        {
            "key": "ZXCVBNM1",
            "data": {
                "itemType": "journalArticle",
                "title": "Local Article",
                "date": "2024-05",
                "publicationTitle": "Local Journal",
                "DOI": "10.1234/local",
                "url": "https://doi.org/10.1234/local",
                "creators": [
                    {"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"}
                ],
                "path": "C:/private/file.pdf",
                "filename": "file.pdf",
                "note": "private note body",
            },
        }
    ]
    monkeypatch.setattr(server, "_zotero_json", lambda *args, **kwargs: (payload, None))

    result = search_zotero("Local", limit=2)
    item = result["results"][0]

    assert item["title"] == "Local Article"
    assert item["authors"] == ["Ada Lovelace"]
    assert item["year"] == 2024
    assert "path" not in item
    assert "filename" not in item
    assert "note" not in item


def test_get_zotero_item_excludes_attachment_path_and_note_body(monkeypatch) -> None:
    payload = {
        "key": "ZXCVBNM1",
        "data": {
            "itemType": "journalArticle",
            "title": "Detailed Local Article",
            "abstractNote": "Published abstract",
            "tags": [{"tag": "reviewed"}],
            "collections": ["ABCDEFGH"],
            "path": "C:/private/file.pdf",
            "filename": "file.pdf",
            "note": "private note body",
        },
    }
    monkeypatch.setattr(server, "_zotero_json", lambda *args, **kwargs: (payload, None))

    result = get_zotero_item("zxcvbnm1")
    item = result["result"]

    assert item["item_key"] == "ZXCVBNM1"
    assert item["abstract"] == "Published abstract"
    assert item["tags"] == ["reviewed"]
    assert "path" not in item
    assert "filename" not in item
    assert "note" not in item


def test_zotero_tools_are_declared_read_only() -> None:
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    for name in (
        "zotero_status",
        "search_zotero",
        "list_collections",
        "list_collection_items",
        "get_zotero_item",
    ):
        assert tools[name].annotations.read_only_hint is True
        assert tools[name].annotations.destructive_hint is False
        assert tools[name].annotations.open_world_hint is False


def test_zotero_write_tools_are_non_destructive_and_local() -> None:
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    for name in (
        "authorize_zotero_write",
        "create_collection",
        "add_item_to_collection",
        "add_paper_by_doi",
        "add_paper_by_metadata",
        "add_tags",
        "add_note",
    ):
        assert tools[name].annotations.read_only_hint is False
        assert tools[name].annotations.destructive_hint is False
        assert tools[name].annotations.open_world_hint is False


def test_collection_removal_is_declared_destructive_and_idempotent() -> None:
    tool = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}[
        "remove_item_from_collection"
    ]
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.destructive_hint is True
    assert tool.annotations.idempotent_hint is True
    assert tool.annotations.open_world_hint is False


def test_remove_item_from_collection_delegates(monkeypatch) -> None:
    monkeypatch.setattr(
        server.zotero_write,
        "remove_item_from_collection",
        lambda item_key, collection_key: {
            "ok": True,
            "item_key": item_key,
            "collection_key": collection_key,
            "collection_removed": True,
        },
    )

    result = remove_item_from_collection("ITEM1234", "COLL1234")

    assert result["collection_removed"] is True


def test_excel_tools_are_read_only_and_local() -> None:
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    for name in (
        "list_excel_sheets",
        "inspect_excel_schema",
        "preview_references",
        "extract_references",
    ):
        assert tools[name].annotations.read_only_hint is True
        assert tools[name].annotations.destructive_hint is False
        assert tools[name].annotations.open_world_hint is False


def test_add_paper_by_doi_stops_at_zotero_duplicate(monkeypatch) -> None:
    monkeypatch.setattr(
        server.zotero_write,
        "find_paper_by_doi",
        lambda doi: ([{"item_key": "ABCDEFGH", "title": "Existing", "DOI": doi}], None),
    )
    monkeypatch.setattr(
        server,
        "_request_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Crossref must not be called after a Zotero duplicate")
        ),
    )

    result = add_paper_by_doi("10.1234/existing")

    assert result["created"] is False
    assert result["duplicate_found"] is True


def test_add_paper_by_doi_links_unique_duplicate_to_collection(monkeypatch) -> None:
    monkeypatch.setattr(
        server.zotero_write,
        "find_paper_by_doi",
        lambda doi: ([{"item_key": "ABCDEFGH", "title": "Existing", "DOI": doi}], None),
    )
    monkeypatch.setattr(
        server.zotero_write,
        "add_item_to_collection",
        lambda item_key, collection_key: {
            "ok": True,
            "collection_key": collection_key,
            "collection_added": True,
            "collection_already_present": False,
        },
    )
    monkeypatch.setattr(
        server,
        "_request_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Crossref must not be called after a Zotero duplicate")
        ),
    )

    result = add_paper_by_doi("10.1234/existing", "COLL1234")

    assert result["item_key"] == "ABCDEFGH"
    assert result["collection_added"] is True


def test_add_paper_by_metadata_delegates_confirmed_fields(monkeypatch) -> None:
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "created": True, "item_key": "ABCDEFGH"}

    monkeypatch.setattr(server.zotero_write, "create_paper_from_metadata", fake_create)

    result = add_paper_by_metadata(
        title="Confirmed",
        authors=["Ada Lovelace"],
        year=1997,
        publication_title="Journal",
        url="https://example.org/article",
        collection_key="COLL1234",
    )

    assert result["created"] is True
    assert captured["title"] == "Confirmed"
    assert captured["collection_key"] == "COLL1234"


def test_network_diagnosis_redacts_and_classifies_tun(monkeypatch) -> None:
    monkeypatch.setattr(
        server,
        "proxy_environment_summary",
        lambda: {
            "variables_present": ["HTTP_PROXY"],
            "values_redacted": True,
            "institutional_client_trust_env": False,
            "institutional_client_inherits_environment_proxy": False,
        },
    )
    monkeypatch.setattr(
        server,
        "detect_vpn_tun_interfaces",
        lambda: {
            "detection": "POSSIBLE",
            "active_candidate_detected": True,
            "installed_candidate_detected": True,
            "active_candidate_count": 1,
            "inactive_candidate_count": 0,
        },
    )
    monkeypatch.setattr(
        server,
        "zotero_status",
        lambda: {"running": True, "local_api_enabled": True},
    )

    result = diagnose_access_environment()

    assert result["institutional_http_client"]["inherits_environment_proxy"] is False
    assert result["environment_proxy"]["values_redacted"] is True
    assert result["risk"]["category"] == "POSSIBLE_ACTIVE_TUN"
    assert result["privacy"]["public_ip_queried"] is False


def test_routing_explanation_keeps_codex_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(
        server,
        "diagnose_access_environment",
        lambda: {
            "vpn_tun": {
                "detection": "NONE_DETECTED",
                "active_candidate_detected": False,
                "installed_candidate_detected": False,
            },
            "risk": {"user_action": "人工检查分流。"},
        },
    )

    result = explain_network_routing()

    assert result["codex_network"]["status"] == "UNCHANGED"
    assert result["institutional_pdf_worker_network"]["trust_env"] is False
    assert result["isolation"]["environment_variable_proxy"] == "ISOLATED"
    assert result["isolation"]["system_tun_or_transparent_routing"] == "NOT_GUARANTEED"


def test_network_tools_are_read_only_with_correct_world_scope() -> None:
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    assert tools["diagnose_access_environment"].annotations.read_only_hint is True
    assert tools["diagnose_access_environment"].annotations.open_world_hint is False
    assert tools["explain_network_routing"].annotations.open_world_hint is False
    assert tools["check_institution_access"].annotations.read_only_hint is True
    assert tools["check_institution_access"].annotations.open_world_hint is True
