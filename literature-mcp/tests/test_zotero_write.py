"""Zotero 10+ Local API authorization and safe-write tests."""

import httpx

import zotero_write


SERVER_ID = "server_test_123"


def _response(
    method: str,
    path: str,
    status: int = 200,
    payload=None,
    **headers: str,
) -> httpx.Response:
    request = httpx.Request(method, f"http://localhost:23119/api/{path}")
    if payload is None:
        return httpx.Response(status, headers=headers, request=request)
    return httpx.Response(status, json=payload, headers=headers, request=request)


def setup_function() -> None:
    zotero_write.clear_cached_authorization()


def test_authorization_uses_get_then_post_and_never_returns_key(monkeypatch) -> None:
    calls = []
    secret = "k" * 32

    def fake_request(method, path="", **kwargs):
        calls.append((method, path, kwargs))
        if method == "GET" and path == "":
            return _response("GET", "", **{"Zotero-Server-ID": SERVER_ID}), None
        assert method == "POST" and path == "local/authorize"
        assert kwargs["headers"]["Zotero-Server-ID"] == SERVER_ID
        return _response("POST", path, payload={"key": secret, "remember": False}), None

    monkeypatch.setattr(zotero_write, "_request", fake_request)

    result = zotero_write.authorize_write()

    assert [(method, path) for method, path, _ in calls] == [
        ("GET", ""),
        ("POST", "local/authorize"),
    ]
    assert result["authorized"] is True
    assert secret not in repr(result)
    assert result["key_exposed"] is False


def test_collection_write_has_both_auth_headers_and_write_token(monkeypatch) -> None:
    secret = "z" * 32
    write_calls = []

    def fake_request(method, path="", **kwargs):
        if method == "GET" and path == f"{zotero_write.LIBRARY_PREFIX}/collections":
            return _response("GET", path, payload=[]), None
        if method == "GET" and path == "":
            return _response("GET", "", **{"Zotero-Server-ID": SERVER_ID}), None
        if path == "local/authorize":
            return _response("POST", path, payload={"key": secret, "remember": False}), None
        write_calls.append((method, path, kwargs))
        return _response(
            "POST",
            path,
            payload={"successful": {"0": {"key": "ABCDEFGH"}}},
        ), None

    monkeypatch.setattr(zotero_write, "_request", fake_request)

    result = zotero_write.create_collection("Research")

    assert result["created"] is True
    assert len(write_calls) == 1
    method, path, kwargs = write_calls[0]
    assert method == "POST"
    assert path.endswith("/collections")
    assert kwargs["headers"]["Zotero-Server-ID"] == SERVER_ID
    assert kwargs["headers"]["Zotero-API-Key"] == secret
    assert len(kwargs["headers"]["Zotero-Write-Token"]) == 32
    assert secret not in repr(result)


def test_duplicate_collection_does_not_authorize_or_write(monkeypatch) -> None:
    calls = []

    def fake_request(method, path="", **kwargs):
        calls.append((method, path))
        payload = [
            {
                "key": "ABCDEFGH",
                "data": {"name": "Literature_MCP_Test", "parentCollection": False},
            }
        ]
        return _response("GET", path, payload=payload), None

    monkeypatch.setattr(zotero_write, "_request", fake_request)

    result = zotero_write.create_collection(" Literature_MCP_Test ")

    assert result["created"] is False
    assert result["duplicate_found"] is True
    assert calls == [("GET", f"{zotero_write.LIBRARY_PREFIX}/collections")]


def test_401_causes_one_fresh_authorization(monkeypatch) -> None:
    secrets = iter(["a" * 32, "b" * 32])
    writes = []

    def fake_request(method, path="", **kwargs):
        if method == "GET" and path == "":
            return _response("GET", "", **{"Zotero-Server-ID": SERVER_ID}), None
        if path == "local/authorize":
            return _response("POST", path, payload={"key": next(secrets), "remember": True}), None
        writes.append(kwargs["headers"]["Zotero-API-Key"])
        if len(writes) == 1:
            return _response("POST", path, status=401), None
        return _response("POST", path, payload={"successful": {"0": "ABCDEFGH"}}), None

    monkeypatch.setattr(zotero_write, "_request", fake_request)

    response, error = zotero_write._authorized_write(
        "POST", f"{zotero_write.LIBRARY_PREFIX}/collections", [{"name": "Test"}]
    )

    assert error is None
    assert response.status_code == 200
    assert writes == ["a" * 32, "b" * 32]


def test_add_item_to_collection_preserves_existing_membership(monkeypatch) -> None:
    responses = iter(
        [
            ({"data": {"key": "COLL1234"}}, None),
            (
                {
                    "data": {
                        "key": "ITEM1234",
                        "version": 9,
                        "itemType": "journalArticle",
                        "collections": ["OLDCLL01"],
                    }
                },
                None,
            ),
        ]
    )
    monkeypatch.setattr(zotero_write, "_read_json", lambda *args, **kwargs: next(responses))
    captured = {}

    def fake_write(method, path, body, **kwargs):
        captured.update(method=method, path=path, body=body, kwargs=kwargs)
        return _response("PATCH", path, status=204), None

    monkeypatch.setattr(zotero_write, "_authorized_write", fake_write)

    result = zotero_write.add_item_to_collection("ITEM1234", "COLL1234")

    assert result["collection_added"] is True
    assert captured["method"] == "PATCH"
    assert captured["body"] == {"collections": ["OLDCLL01", "COLL1234"]}
    assert captured["kwargs"]["version"] == 9


def test_add_item_to_collection_is_idempotent(monkeypatch) -> None:
    responses = iter(
        [
            ({"data": {"key": "COLL1234"}}, None),
            (
                {
                    "data": {
                        "key": "ITEM1234",
                        "version": 9,
                        "itemType": "journalArticle",
                        "collections": ["COLL1234"],
                    }
                },
                None,
            ),
        ]
    )
    monkeypatch.setattr(zotero_write, "_read_json", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(
        zotero_write,
        "_authorized_write",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("existing membership must not write")),
    )

    result = zotero_write.add_item_to_collection("ITEM1234", "COLL1234")

    assert result["updated"] is False
    assert result["collection_already_present"] is True


def test_remove_item_from_collection_preserves_other_memberships(monkeypatch) -> None:
    responses = iter(
        [
            ({"data": {"key": "COLL1234"}}, None),
            (
                {
                    "data": {
                        "key": "ITEM1234",
                        "version": 12,
                        "itemType": "journalArticle",
                        "collections": ["OLDCLL01", "COLL1234", "OTHER002"],
                    }
                },
                None,
            ),
        ]
    )
    monkeypatch.setattr(zotero_write, "_read_json", lambda *args, **kwargs: next(responses))
    captured = {}

    def fake_write(method, path, body, **kwargs):
        captured.update(method=method, path=path, body=body, kwargs=kwargs)
        return _response("PATCH", path, status=204), None

    monkeypatch.setattr(zotero_write, "_authorized_write", fake_write)

    result = zotero_write.remove_item_from_collection("ITEM1234", "COLL1234")

    assert result["collection_removed"] is True
    assert captured["method"] == "PATCH"
    assert captured["body"] == {"collections": ["OLDCLL01", "OTHER002"]}
    assert captured["kwargs"]["version"] == 12


def test_remove_item_from_collection_is_idempotent(monkeypatch) -> None:
    responses = iter(
        [
            ({"data": {"key": "COLL1234"}}, None),
            (
                {
                    "data": {
                        "key": "ITEM1234",
                        "version": 12,
                        "itemType": "journalArticle",
                        "collections": ["OTHER002"],
                    }
                },
                None,
            ),
        ]
    )
    monkeypatch.setattr(zotero_write, "_read_json", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(
        zotero_write,
        "_authorized_write",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("absent membership must not write")),
    )

    result = zotero_write.remove_item_from_collection("ITEM1234", "COLL1234")

    assert result["updated"] is False
    assert result["collection_already_absent"] is True


def test_create_paper_from_metadata_uses_confirmed_fields(monkeypatch) -> None:
    monkeypatch.setattr(zotero_write, "find_paper_by_title", lambda title: ([], None))
    monkeypatch.setattr(zotero_write, "_validate_collection_parent", lambda key: (key, None))
    monkeypatch.setattr(
        zotero_write,
        "_template",
        lambda item_type: (
            {
                "itemType": item_type,
                "title": "",
                "date": "",
                "publicationTitle": "",
                "url": "",
                "creators": [],
                "collections": [],
                "tags": [],
                "relations": {},
            },
            None,
        ),
    )
    captured = {}

    def fake_write(method, path, body, **kwargs):
        captured.update(method=method, path=path, body=body)
        return _response("POST", path, payload={"successful": {"0": {"key": "ITEM1234"}}}), None

    monkeypatch.setattr(zotero_write, "_authorized_write", fake_write)

    result = zotero_write.create_paper_from_metadata(
        title="Confirmed title",
        authors=["Ada Lovelace"],
        year=1997,
        publication_title="Confirmed Journal",
        url="https://example.org/article",
        collection_key="COLL1234",
    )

    assert result["created"] is True
    payload = captured["body"][0]
    assert payload["title"] == "Confirmed title"
    assert payload["creators"] == [{"creatorType": "author", "name": "Ada Lovelace"}]
    assert payload["collections"] == ["COLL1234"]


def test_add_tags_deduplicates_and_preserves_existing(monkeypatch) -> None:
    item = {
        "data": {
            "key": "ABCDEFGH",
            "version": 9,
            "itemType": "journalArticle",
            "tags": [{"tag": "existing", "type": 1}],
        }
    }
    captured = {}
    monkeypatch.setattr(zotero_write, "_read_json", lambda *args, **kwargs: (item, None))

    def fake_write(method, path, body, **kwargs):
        captured.update(method=method, path=path, body=body, kwargs=kwargs)
        return _response("PATCH", path, status=204), None

    monkeypatch.setattr(zotero_write, "_authorized_write", fake_write)

    result = zotero_write.add_tags("ABCDEFGH", ["Existing", "new", "new"])

    assert result["added_tags"] == ["new"]
    assert captured["method"] == "PATCH"
    assert captured["kwargs"]["version"] == 9
    assert captured["body"]["tags"] == [
        {"tag": "existing", "type": 1},
        {"tag": "new"},
    ]


def test_attachment_writes_are_refused(monkeypatch) -> None:
    monkeypatch.setattr(
        zotero_write,
        "_read_json",
        lambda *args, **kwargs: ({"data": {"itemType": "attachment", "version": 1}}, None),
    )

    result = zotero_write.add_tags("ABCDEFGH", ["never-written"])

    assert result["ok"] is False
    assert result["error"]["code"] == "attachment_modification_forbidden"


def test_duplicate_note_does_not_write(monkeypatch) -> None:
    responses = iter(
        [
            ({"data": {"itemType": "journalArticle"}}, None),
            ([{"key": "NOTE1234", "data": {"itemType": "note", "note": "<p>Same note</p>"}}], None),
        ]
    )
    monkeypatch.setattr(zotero_write, "_read_json", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(
        zotero_write,
        "_authorized_write",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("duplicate note must not write")),
    )

    result = zotero_write.add_note("ABCDEFGH", "Same note")

    assert result["created"] is False
    assert result["duplicate_found"] is True
