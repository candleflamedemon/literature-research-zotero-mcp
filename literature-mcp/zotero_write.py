"""Authorized, non-deleting writes to the Zotero 10+ Local API."""

from __future__ import annotations

import html
import logging
import re
import secrets
import threading
import hashlib
from pathlib import Path
from urllib.parse import urlsplit
from contextlib import contextmanager
from typing import Any

import httpx


BASE_URL = "http://localhost:23119/api/"
LIBRARY_PREFIX = "users/0"
USER_AGENT = "LiteratureMCP/0.9 (local Zotero API client)"
READ_TIMEOUT = httpx.Timeout(8.0, connect=2.0)
AUTHORIZE_TIMEOUT = httpx.Timeout(60.0, connect=2.0)
ALLOWED_WRITE_METHODS = frozenset({"POST", "PATCH"})

_AUTH_LOCK = threading.RLock()
_AUTH_SERVER_ID: str | None = None
_AUTH_KEY: str | None = None
_AUTH_REMEMBERED = False
_HTTP_LOG_LOCK = threading.RLock()
_HTTP_LOG_USERS = 0
_HTTP_LOG_PREVIOUS: list[bool] = []


def _error(code: str, message: str, *, status_code: int | None = None) -> dict[str, Any]:
    details: dict[str, Any] = {"code": code, "message": message}
    if status_code is not None:
        details["status_code"] = status_code
    return {"ok": False, "source": "Zotero Local API", "error": details}


def _clear_auth_locked() -> None:
    global _AUTH_SERVER_ID, _AUTH_KEY, _AUTH_REMEMBERED
    _AUTH_SERVER_ID = None
    _AUTH_KEY = None
    _AUTH_REMEMBERED = False


def clear_cached_authorization() -> None:
    """Forget process-memory credentials. Intended for tests and server changes."""

    with _AUTH_LOCK:
        _clear_auth_locked()


def _base_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "Zotero-API-Version": "3",
        "Zotero-Allowed-Request": "true",
    }


@contextmanager
def _suppress_sensitive_http_logs(enabled: bool):
    """Prevent HTTPX/httpcore from emitting authorization or key-bearing traffic."""

    if not enabled:
        yield
        return
    global _HTTP_LOG_USERS, _HTTP_LOG_PREVIOUS
    loggers = [logging.getLogger("httpx"), logging.getLogger("httpcore")]
    with _HTTP_LOG_LOCK:
        if _HTTP_LOG_USERS == 0:
            _HTTP_LOG_PREVIOUS = [logger.disabled for logger in loggers]
        _HTTP_LOG_USERS += 1
        for logger in loggers:
            logger.disabled = True
    try:
        yield
    finally:
        with _HTTP_LOG_LOCK:
            _HTTP_LOG_USERS -= 1
            if _HTTP_LOG_USERS == 0:
                for logger, disabled in zip(loggers, _HTTP_LOG_PREVIOUS, strict=True):
                    logger.disabled = disabled


def _request(
    method: str,
    path: str = "",
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    authorization_dialog: bool = False,
    form_body: Any = None,
    raw_content: bytes | None = None,
) -> tuple[httpx.Response | None, dict[str, Any] | None]:
    request_headers = _base_headers()
    if headers:
        request_headers.update(headers)
    sensitive = authorization_dialog or "Zotero-API-Key" in request_headers
    try:
        with _suppress_sensitive_http_logs(sensitive):
            with httpx.Client(
                base_url=BASE_URL,
                headers=request_headers,
                timeout=AUTHORIZE_TIMEOUT if authorization_dialog else READ_TIMEOUT,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = client.request(
                    method,
                    path.lstrip("/"),
                    params=params,
                    json=json_body,
                    data=form_body,
                    content=raw_content,
                )
    except httpx.ConnectError:
        return None, _error("zotero_not_running", "无法连接 Zotero。请先启动 Zotero 桌面客户端。")
    except httpx.TimeoutException:
        message = "等待 Zotero 授权确认超时。" if authorization_dialog else "Zotero Local API 响应超时。"
        return None, _error("authorization_timeout" if authorization_dialog else "zotero_unresponsive", message)
    except httpx.RequestError:
        return None, _error("local_connection_error", "连接 Zotero Local API 时发生本机网络错误。")
    return response, None


def _server_identity() -> tuple[str | None, dict[str, Any] | None]:
    response, error = _request("GET")
    if error:
        return None, error
    assert response is not None
    if response.status_code == 403:
        return None, _error(
            "local_api_disabled",
            "Zotero 正在运行，但 Local API 未启用。请在 Zotero 设置 → 高级中启用本机应用通信。",
            status_code=403,
        )
    if response.status_code >= 400:
        return None, _error("local_api_error", "无法读取 Zotero Local API 身份。", status_code=response.status_code)
    server_id = response.headers.get("Zotero-Server-ID")
    if not server_id or not re.fullmatch(r"[A-Za-z0-9_-]{4,128}", server_id):
        return None, _error(
            "write_api_unavailable",
            "当前 Zotero 未提供有效的 Zotero-Server-ID；需要支持写入授权的 Zotero 10+。",
        )
    return server_id, None


def _authorization_result(*, already_authorized: bool = False) -> dict[str, Any]:
    return {
        "ok": True,
        "source": "Zotero Local API",
        "authorized": True,
        "already_authorized": already_authorized,
        "credential_storage": "process_memory_only",
        "key_exposed": False,
        "message": "Zotero 写入授权已就绪。",
    }


def _authorize_for_server(server_id: str) -> dict[str, Any]:
    global _AUTH_SERVER_ID, _AUTH_KEY, _AUTH_REMEMBERED

    response, error = _request(
        "POST",
        "local/authorize",
        headers={"Zotero-Server-ID": server_id},
        json_body={"appName": "Literature MCP"},
        authorization_dialog=True,
    )
    if error:
        return error
    assert response is not None
    if response.status_code == 403:
        return _error("authorization_denied", "用户未批准 Zotero 写入授权。", status_code=403)
    if response.status_code == 429:
        result = _error("authorization_rate_limited", "Zotero 授权请求过于频繁，请稍后重试。", status_code=429)
        retry_after = response.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            result["error"]["retry_after_seconds"] = int(retry_after)
        return result
    if response.status_code in {412, 428}:
        return _error("server_identity_rejected", "Zotero 拒绝了当前 Server ID，请重新发起授权。", status_code=response.status_code)
    if response.status_code >= 400:
        return _error("authorization_failed", "Zotero 无法完成写入授权。", status_code=response.status_code)
    try:
        payload = response.json()
    except ValueError:
        return _error("invalid_authorization_response", "Zotero 授权响应不是有效 JSON。")
    key = payload.get("key") if isinstance(payload, dict) else None
    if not isinstance(key, str) or len(key) != 32 or any(char.isspace() for char in key):
        return _error("invalid_authorization_response", "Zotero 授权响应未包含有效的本地写入凭据。")
    _AUTH_SERVER_ID = server_id
    _AUTH_KEY = key
    _AUTH_REMEMBERED = bool(payload.get("remember"))
    return _authorization_result()


def authorize_write() -> dict[str, Any]:
    """Run the official GET identity -> POST authorize flow without exposing the key."""

    with _AUTH_LOCK:
        server_id, error = _server_identity()
        if error:
            return error
        assert server_id is not None
        if _AUTH_SERVER_ID != server_id:
            _clear_auth_locked()
        if _AUTH_KEY:
            return _authorization_result(already_authorized=True)
        return _authorize_for_server(server_id)


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _read_json(path: str, params: dict[str, Any] | None = None, *, server_id: str | None = None) -> tuple[Any, dict[str, Any] | None]:
    options = {"headers": {"Zotero-Server-ID": server_id}} if server_id else {}
    response, error = _request("GET", path, params=params, **options)
    if error:
        return None, error
    assert response is not None
    if response.status_code == 403:
        return None, _error("local_api_disabled", "Zotero Local API 未启用。", status_code=403)
    if response.status_code == 404:
        return None, _error("not_found", "Zotero 中未找到请求的对象。", status_code=404)
    if response.status_code >= 400:
        return None, _error("read_failed", "Zotero 查重请求失败。", status_code=response.status_code)
    payload = _json(response)
    if payload is None:
        return None, _error("invalid_response", "Zotero Local API 返回了无效 JSON。")
    return payload, None


def _consume_single_use_key() -> None:
    global _AUTH_KEY
    if not _AUTH_REMEMBERED:
        _AUTH_KEY = None


def _write_once(
    method: str,
    path: str,
    body: Any,
    server_id: str,
    key: str,
    *,
    version: int | None = None,
    write_token: str | None = None,
) -> tuple[httpx.Response | None, dict[str, Any] | None]:
    if method not in ALLOWED_WRITE_METHODS:
        return None, _error("method_forbidden", "Literature MCP 未开放该写入方法。")
    headers = {
        "Content-Type": "application/json",
        "Zotero-Server-ID": server_id,
        "Zotero-API-Key": key,
    }
    if version is not None:
        headers["If-Unmodified-Since-Version"] = str(version)
    elif write_token:
        headers["Zotero-Write-Token"] = write_token
    return _request(method, path, headers=headers, json_body=body)


def _authorized_write(
    method: str,
    path: str,
    body: Any,
    *,
    version: int | None = None,
    expected_server_id: str | None = None,
) -> tuple[httpx.Response | None, dict[str, Any] | None]:
    """Write with both required headers, reauthorizing once after a 401."""

    write_token = secrets.token_hex(16) if version is None else None
    with _AUTH_LOCK:
        for attempt in range(2):
            server_id, error = _server_identity()
            if error:
                return None, error
            assert server_id is not None
            if expected_server_id and server_id != expected_server_id:
                _clear_auth_locked()
                return None, _error("server_changed", "Zotero 实例已改变，停止写入。")
            if _AUTH_SERVER_ID != server_id:
                _clear_auth_locked()
            if not _AUTH_KEY:
                auth = _authorize_for_server(server_id)
                if not auth.get("ok"):
                    return None, auth
            assert _AUTH_KEY is not None
            response, error = _write_once(
                method,
                path,
                body,
                server_id,
                _AUTH_KEY,
                version=version,
                write_token=write_token,
            )
            if error:
                return None, error
            assert response is not None
            if response.status_code == 401 and attempt == 0:
                _clear_auth_locked()
                continue
            _consume_single_use_key()
            return response, None
    return None, _error("authorization_failed", "Zotero 写入授权失败。")


def _valid_key(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    key = value.strip().upper()
    return key if re.fullmatch(r"[A-Z0-9]{8}", key) else None


def _data(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        return {}
    payload = entry.get("data")
    return payload if isinstance(payload, dict) else entry


def _entry_key(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return None
    data = _data(entry)
    value = entry.get("key") or data.get("key")
    return value if isinstance(value, str) else None


def _created_key(response: httpx.Response) -> str | None:
    payload = _json(response)
    if not isinstance(payload, dict):
        return None
    bucket = payload.get("successful") or payload.get("success")
    if not isinstance(bucket, dict):
        return None
    created = bucket.get("0") or bucket.get(0)
    if isinstance(created, str):
        return created
    return _entry_key(created)


def _write_error(response: httpx.Response) -> dict[str, Any] | None:
    status = response.status_code
    if status in {200, 201, 204}:
        return None
    messages = {
        400: ("invalid_write", "Zotero 拒绝了写入数据。"),
        401: ("authorization_required", "Zotero 写入授权无效，请重新授权。"),
        403: ("write_forbidden", "当前 Zotero 库不允许此写入。"),
        409: ("library_locked", "Zotero 库当前被锁定，请稍后重试。"),
        412: ("version_conflict", "条目已被其他操作修改，请重新执行。"),
        428: ("precondition_required", "Zotero 要求写入前置条件。"),
    }
    code, message = messages.get(status, ("write_failed", "Zotero Local API 写入失败。"))
    return _error(code, message, status_code=status)


def _normalise_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def _validate_collection_parent(parent_collection: str | None) -> tuple[str | None, dict[str, Any] | None]:
    if parent_collection is None:
        return None, None
    key = _valid_key(parent_collection)
    if not key:
        return None, _error("invalid_input", "parent_collection 必须是 8 位 Zotero collection key。")
    _, error = _read_json(f"{LIBRARY_PREFIX}/collections/{key}")
    return (None, error) if error else (key, None)


def create_collection(name: str, parent_collection: str | None = None) -> dict[str, Any]:
    if not isinstance(name, str):
        return _error("invalid_input", "Collection 名称不能为空。")
    clean_name = _normalise_name(name)
    if not clean_name or len(clean_name) > 255 or any(ord(char) < 32 for char in clean_name):
        return _error("invalid_input", "Collection 名称必须为 1–255 个可显示字符。")
    parent_key, error = _validate_collection_parent(parent_collection)
    if error:
        return error

    collections, error = _read_json(f"{LIBRARY_PREFIX}/collections")
    if error:
        return error
    if not isinstance(collections, list):
        return _error("invalid_response", "Zotero Collection 查重响应格式异常。")
    for entry in collections:
        data = _data(entry)
        existing_name = data.get("name")
        existing_parent = data.get("parentCollection") or None
        if (
            isinstance(existing_name, str)
            and _normalise_name(existing_name).casefold() == clean_name.casefold()
            and existing_parent == parent_key
        ):
            return {
                "ok": True,
                "source": "Zotero Local API",
                "created": False,
                "duplicate_found": True,
                "collection_key": _entry_key(entry),
                "name": clean_name,
            }

    response, error = _authorized_write(
        "POST",
        f"{LIBRARY_PREFIX}/collections",
        [{"name": clean_name, "parentCollection": parent_key or False}],
    )
    if error:
        return error
    assert response is not None
    write_error = _write_error(response)
    if write_error:
        return write_error
    key = _created_key(response)
    if not key:
        return _error("invalid_write_response", "Collection 可能已创建，但 Zotero 未返回可识别的 key。")
    return {
        "ok": True,
        "source": "Zotero Local API",
        "created": True,
        "duplicate_found": False,
        "collection_key": key,
        "name": clean_name,
    }


def find_paper_by_doi(doi: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    payload, error = _read_json(
        f"{LIBRARY_PREFIX}/items/top",
        {"q": doi, "qmode": "everything", "itemType": "-attachment"},
    )
    if error:
        return [], error
    if not isinstance(payload, list):
        return [], _error("invalid_response", "Zotero DOI 查重响应格式异常。")
    target = doi.casefold()
    matches: list[dict[str, Any]] = []
    for entry in payload:
        data = _data(entry)
        stored = data.get("DOI")
        if isinstance(stored, str):
            stored = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", stored.strip(), flags=re.I)
        if isinstance(stored, str) and stored.casefold() == target:
            matches.append({"item_key": _entry_key(entry), "title": data.get("title"), "DOI": stored})
    return matches, None


def _normalise_title(value: str) -> str:
    plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(value))).strip()
    return plain.casefold()


def find_paper_by_title(title: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    clean_title = _normalise_name(title)
    payload, error = _read_json(
        f"{LIBRARY_PREFIX}/items/top",
        {"q": clean_title, "qmode": "everything", "itemType": "-attachment"},
    )
    if error:
        return [], error
    if not isinstance(payload, list):
        return [], _error("invalid_response", "Zotero 标题查重响应格式异常。")
    target = _normalise_title(clean_title)
    matches: list[dict[str, Any]] = []
    for entry in payload:
        data = _data(entry)
        stored = data.get("title")
        if (
            data.get("itemType") not in {"attachment", "note"}
            and isinstance(stored, str)
            and _normalise_title(stored) == target
        ):
            matches.append(
                {
                    "item_key": _entry_key(entry),
                    "title": stored,
                    "DOI": data.get("DOI") if isinstance(data.get("DOI"), str) else None,
                    "year": data.get("date") if isinstance(data.get("date"), str) else None,
                }
            )
    return matches, None


def add_item_to_collection(item_key: str, collection_key: str) -> dict[str, Any]:
    key = _valid_key(item_key)
    collection, error = _validate_collection_parent(collection_key)
    if not key or error:
        return error or _error("invalid_input", "item_key 必须是 8 位 Zotero item key。")
    assert collection is not None

    item, error = _read_json(f"{LIBRARY_PREFIX}/items/{key}")
    if error:
        return error
    data = _data(item)
    if data.get("itemType") in {"attachment", "note"} or data.get("parentItem"):
        return _error("child_item_forbidden", "只能把顶层书目条目加入 Collection。")
    version = data.get("version")
    if not isinstance(version, int):
        return _error("invalid_response", "Zotero 条目缺少版本号，未执行写入。")
    existing = [
        value.upper()
        for value in data.get("collections", [])
        if isinstance(value, str) and _valid_key(value)
    ]
    if collection in existing:
        return {
            "ok": True,
            "source": "Zotero Local API",
            "updated": False,
            "collection_added": False,
            "collection_already_present": True,
            "item_key": key,
            "collection_key": collection,
        }

    response, error = _authorized_write(
        "PATCH",
        f"{LIBRARY_PREFIX}/items/{key}",
        {"collections": existing + [collection]},
        version=version,
    )
    if error:
        return error
    assert response is not None
    write_error = _write_error(response)
    if write_error:
        return write_error
    return {
        "ok": True,
        "source": "Zotero Local API",
        "updated": True,
        "collection_added": True,
        "collection_already_present": False,
        "item_key": key,
        "collection_key": collection,
    }


def remove_item_from_collection(item_key: str, collection_key: str) -> dict[str, Any]:
    """Remove one Collection membership without deleting or moving the item elsewhere."""

    key = _valid_key(item_key)
    collection, error = _validate_collection_parent(collection_key)
    if not key or error:
        return error or _error("invalid_input", "item_key 必须是 8 位 Zotero item key。")
    assert collection is not None

    item, error = _read_json(f"{LIBRARY_PREFIX}/items/{key}")
    if error:
        return error
    data = _data(item)
    if data.get("itemType") in {"attachment", "note"} or data.get("parentItem"):
        return _error("child_item_forbidden", "只能移除顶层书目条目的 Collection 归属。")
    version = data.get("version")
    if not isinstance(version, int):
        return _error("invalid_response", "Zotero 条目缺少版本号，未执行写入。")
    existing = [
        value.upper()
        for value in data.get("collections", [])
        if isinstance(value, str) and _valid_key(value)
    ]
    if collection not in existing:
        return {
            "ok": True,
            "source": "Zotero Local API",
            "updated": False,
            "collection_removed": False,
            "collection_already_absent": True,
            "item_key": key,
            "collection_key": collection,
        }

    response, error = _authorized_write(
        "PATCH",
        f"{LIBRARY_PREFIX}/items/{key}",
        {"collections": [value for value in existing if value != collection]},
        version=version,
    )
    if error:
        return error
    assert response is not None
    write_error = _write_error(response)
    if write_error:
        return write_error
    return {
        "ok": True,
        "source": "Zotero Local API",
        "updated": True,
        "collection_removed": True,
        "collection_already_absent": False,
        "item_key": key,
        "collection_key": collection,
    }


def _template(item_type: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    params = {"itemType": item_type}
    if item_type == "attachment":
        params["linkMode"] = "imported_file"
    payload, error = _read_json("items/new", params)
    # Some Local API builds expose /schema but not the Web API /items/new.
    # Derive fields from this Zotero instance, never from its database or a
    # third-party service. Only fall back for the known missing endpoint.
    if error and error.get("error", {}).get("status_code") == 404:
        schema, schema_error = _read_json("schema")
        if schema_error:
            return None, schema_error
        types = schema.get("itemTypes") if isinstance(schema, dict) else None
        definition = next((entry for entry in types if isinstance(entry, dict) and entry.get("itemType") == item_type), None) if isinstance(types, list) else None
        if definition is None:
            return None, _error("invalid_response", "本机 Zotero schema 未提供请求的条目类型，未写入。")
        payload = {"itemType": item_type, "tags": [], "collections": [], "relations": {}}
        for field in definition.get("fields", []):
            if isinstance(field, dict) and isinstance(field.get("field"), str):
                payload[field["field"]] = ""
        if definition.get("creatorTypes"):
            payload["creators"] = []
        if item_type == "note":
            payload["note"] = ""
        return payload, None
    if error:
        return None, error
    if not isinstance(payload, dict):
        return None, _error("invalid_response", "Zotero 条目模板响应格式异常。")
    return payload, None


def _set_if_supported(template: dict[str, Any], field: str, value: Any) -> None:
    if field in template and value not in {None, ""}:
        template[field] = value


def create_paper_from_crossref(
    doi: str,
    crossref: dict[str, Any],
    collection_key: str | None = None,
) -> dict[str, Any]:
    matches, error = find_paper_by_doi(doi)
    if error:
        return error
    if matches:
        result = {
            "ok": True,
            "source": "Zotero Local API",
            "created": False,
            "duplicate_found": True,
            "duplicates": matches,
        }
        if collection_key and len(matches) == 1 and matches[0].get("item_key"):
            linked = add_item_to_collection(matches[0]["item_key"], collection_key)
            if not linked.get("ok"):
                return linked
            result.update(
                item_key=matches[0]["item_key"],
                collection_key=linked["collection_key"],
                collection_added=linked["collection_added"],
                collection_already_present=linked["collection_already_present"],
            )
        return result
    collection, error = _validate_collection_parent(collection_key)
    if error:
        return error
    template, error = _template("journalArticle")
    if error:
        return error
    assert template is not None

    title = crossref.get("title")
    if isinstance(title, list):
        title = next((value for value in title if isinstance(value, str) and value.strip()), None)
    if not isinstance(title, str) or not title.strip():
        return _error("metadata_incomplete", "Crossref 记录缺少标题，未写入 Zotero。")
    template["itemType"] = "journalArticle"
    _set_if_supported(template, "title", re.sub(r"<[^>]+>", " ", html.unescape(title)).strip())
    _set_if_supported(template, "DOI", doi)
    _set_if_supported(template, "url", crossref.get("URL") or f"https://doi.org/{doi}")
    _set_if_supported(template, "publicationTitle", _first_string(crossref.get("container-title")))
    _set_if_supported(template, "volume", crossref.get("volume"))
    _set_if_supported(template, "issue", crossref.get("issue"))
    _set_if_supported(template, "pages", crossref.get("page"))
    _set_if_supported(template, "ISSN", _first_string(crossref.get("ISSN")))
    _set_if_supported(template, "abstractNote", _plain_text(crossref.get("abstract")))
    _set_if_supported(template, "language", crossref.get("language"))
    year = _crossref_year(crossref)
    _set_if_supported(template, "date", str(year) if year else None)

    creators: list[dict[str, str]] = []
    for author in crossref.get("author") if isinstance(crossref.get("author"), list) else []:
        if not isinstance(author, dict):
            continue
        given = author.get("given")
        family = author.get("family")
        if isinstance(family, str) and family.strip():
            creators.append({
                "creatorType": "author",
                "firstName": given.strip() if isinstance(given, str) else "",
                "lastName": family.strip(),
            })
        elif isinstance(author.get("name"), str) and author["name"].strip():
            creators.append({"creatorType": "author", "name": author["name"].strip()})
    template["creators"] = creators
    template["collections"] = [collection] if collection else []
    template["tags"] = []
    template["relations"] = {}

    response, error = _authorized_write("POST", f"{LIBRARY_PREFIX}/items", [template])
    if error:
        return error
    assert response is not None
    write_error = _write_error(response)
    if write_error:
        return write_error
    key = _created_key(response)
    if not key:
        return _error("invalid_write_response", "文献可能已创建，但 Zotero 未返回可识别的 key。")
    return {
        "ok": True,
        "source": "Zotero Local API",
        "created": True,
        "duplicate_found": False,
        "item_key": key,
        "title": _plain_text(title),
        "DOI": doi,
    }


def _optional_text(value: str | None, field: str, *, max_length: int) -> tuple[str | None, dict[str, Any] | None]:
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, _error("invalid_input", f"{field} 必须是字符串。")
    clean = _normalise_name(value)
    if not clean or len(clean) > max_length or any(ord(char) < 32 for char in clean):
        return None, _error("invalid_input", f"{field} 格式无效。")
    return clean, None


def create_paper_from_metadata(
    title: str,
    authors: list[str] | None = None,
    year: int | None = None,
    publication_title: str | None = None,
    url: str | None = None,
    collection_key: str | None = None,
) -> dict[str, Any]:
    clean_title, error = _optional_text(title, "title", max_length=2_048)
    if error:
        return error
    assert clean_title is not None

    cleaned_authors: list[str] = []
    if authors is not None:
        if not isinstance(authors, list) or len(authors) > 100:
            return _error("invalid_input", "authors 必须是最多 100 个姓名组成的列表。")
        for author in authors:
            clean_author, error = _optional_text(author, "author", max_length=512)
            if error:
                return error
            assert clean_author is not None
            cleaned_authors.append(clean_author)
    if year is not None and (not isinstance(year, int) or isinstance(year, bool) or not 1000 <= year <= 2100):
        return _error("invalid_input", "year 必须是 1000–2100 之间的整数。")
    clean_publication, error = _optional_text(publication_title, "publication_title", max_length=1_024)
    if error:
        return error
    clean_url, error = _optional_text(url, "url", max_length=4_096)
    if error:
        return error
    if clean_url:
        parsed = urlsplit(clean_url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            return _error("invalid_input", "url 必须是不含凭据的 HTTP(S) URL。")

    matches, error = find_paper_by_title(clean_title)
    if error:
        return error
    if len(matches) > 1:
        return {
            "ok": False,
            "source": "Zotero Local API",
            "duplicate_found": True,
            "duplicates": matches,
            "error": {"code": "ambiguous_duplicate", "message": "标题命中多个 Zotero 条目，未自动选择。"},
        }
    if matches:
        result: dict[str, Any] = {
            "ok": True,
            "source": "Zotero Local API",
            "created": False,
            "duplicate_found": True,
            "duplicates": matches,
            "item_key": matches[0].get("item_key"),
        }
        if collection_key and matches[0].get("item_key"):
            linked = add_item_to_collection(matches[0]["item_key"], collection_key)
            if not linked.get("ok"):
                return linked
            result.update(
                collection_key=linked["collection_key"],
                collection_added=linked["collection_added"],
                collection_already_present=linked["collection_already_present"],
            )
        return result

    collection, error = _validate_collection_parent(collection_key)
    if error:
        return error
    template, error = _template("journalArticle")
    if error:
        return error
    assert template is not None
    template["itemType"] = "journalArticle"
    _set_if_supported(template, "title", clean_title)
    _set_if_supported(template, "date", str(year) if year else None)
    _set_if_supported(template, "publicationTitle", clean_publication)
    _set_if_supported(template, "url", clean_url)
    template["creators"] = [{"creatorType": "author", "name": author} for author in cleaned_authors]
    template["collections"] = [collection] if collection else []
    template["tags"] = []
    template["relations"] = {}

    response, error = _authorized_write("POST", f"{LIBRARY_PREFIX}/items", [template])
    if error:
        return error
    assert response is not None
    write_error = _write_error(response)
    if write_error:
        return write_error
    key = _created_key(response)
    if not key:
        return _error("invalid_write_response", "文献可能已创建，但 Zotero 未返回可识别的 key。")
    return {
        "ok": True,
        "source": "Zotero Local API",
        "created": True,
        "duplicate_found": False,
        "item_key": key,
        "title": clean_title,
        "DOI": None,
    }


def _first_string(value: Any) -> str | None:
    if isinstance(value, list):
        return next((item.strip() for item in value if isinstance(item, str) and item.strip()), None)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _plain_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(value))).strip() or None


def _crossref_year(item: dict[str, Any]) -> int | None:
    for key in ("published-print", "published-online", "published", "issued"):
        value = item.get(key)
        parts = value.get("date-parts") if isinstance(value, dict) else None
        if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
            year = parts[0][0]
            if isinstance(year, int):
                return year
    return None


def add_tags(item_key: str, tags: list[str]) -> dict[str, Any]:
    key = _valid_key(item_key)
    if not key or not isinstance(tags, list):
        return _error("invalid_input", "item_key 或 tags 格式无效。")
    cleaned: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        if not isinstance(tag, str):
            return _error("invalid_input", "每个 tag 必须是字符串。")
        value = _normalise_name(tag)
        if not value or len(value) > 255 or any(ord(char) < 32 for char in value):
            return _error("invalid_input", "Tag 必须为 1–255 个可显示字符。")
        if value.casefold() not in seen:
            cleaned.append(value)
            seen.add(value.casefold())
    if not cleaned or len(cleaned) > 50:
        return _error("invalid_input", "每次必须提供 1–50 个不同的 tags。")

    item, error = _read_json(f"{LIBRARY_PREFIX}/items/{key}")
    if error:
        return error
    data = _data(item)
    if data.get("itemType") == "attachment":
        return _error("attachment_modification_forbidden", "禁止修改 PDF 或其他附件条目。")
    version = data.get("version")
    if not isinstance(version, int):
        return _error("invalid_response", "Zotero 条目缺少版本号，未执行写入。")
    existing = data.get("tags") if isinstance(data.get("tags"), list) else []
    existing_names = {
        entry.get("tag").casefold()
        for entry in existing
        if isinstance(entry, dict) and isinstance(entry.get("tag"), str)
    }
    additions = [tag for tag in cleaned if tag.casefold() not in existing_names]
    if not additions:
        return {"ok": True, "source": "Zotero Local API", "updated": False, "duplicate_found": True, "item_key": key, "added_tags": []}
    updated_tags = [entry for entry in existing if isinstance(entry, dict)] + [{"tag": tag} for tag in additions]
    response, error = _authorized_write(
        "PATCH",
        f"{LIBRARY_PREFIX}/items/{key}",
        {"tags": updated_tags},
        version=version,
    )
    if error:
        return error
    assert response is not None
    write_error = _write_error(response)
    if write_error:
        return write_error
    return {"ok": True, "source": "Zotero Local API", "updated": True, "duplicate_found": False, "item_key": key, "added_tags": additions}


def _note_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def add_note(parent_item_key: str, note: str) -> dict[str, Any]:
    key = _valid_key(parent_item_key)
    if not key or not isinstance(note, str):
        return _error("invalid_input", "parent_item_key 或 note 格式无效。")
    plain = re.sub(r"\s+", " ", note.strip())
    if not plain or len(plain) > 100_000:
        return _error("invalid_input", "Note 必须为 1–100000 个字符。")
    parent, error = _read_json(f"{LIBRARY_PREFIX}/items/{key}")
    if error:
        return error
    if _data(parent).get("itemType") == "attachment":
        return _error("attachment_modification_forbidden", "禁止针对 PDF 或其他附件执行写入。")
    children, error = _read_json(f"{LIBRARY_PREFIX}/items/{key}/children")
    if error:
        return error
    if not isinstance(children, list):
        return _error("invalid_response", "Zotero Note 查重响应格式异常。")
    target = plain.casefold()
    for child in children:
        data = _data(child)
        if data.get("itemType") == "note" and _note_text(data.get("note")).casefold() == target:
            return {"ok": True, "source": "Zotero Local API", "created": False, "duplicate_found": True, "note_key": _entry_key(child), "parent_item_key": key}

    template, error = _template("note")
    if error:
        return error
    assert template is not None
    template["itemType"] = "note"
    template["parentItem"] = key
    template["note"] = "<p>" + html.escape(note.strip()).replace("\n", "<br>") + "</p>"
    template["tags"] = []
    template["collections"] = []
    template["relations"] = {}
    response, error = _authorized_write("POST", f"{LIBRARY_PREFIX}/items", [template])
    if error:
        return error
    assert response is not None
    write_error = _write_error(response)
    if write_error:
        return write_error
    note_key = _created_key(response)
    if not note_key:
        return _error("invalid_write_response", "Note 可能已创建，但 Zotero 未返回可识别的 key。")
    return {"ok": True, "source": "Zotero Local API", "created": True, "duplicate_found": False, "note_key": note_key, "parent_item_key": key, "character_count": len(note)}


def _authorized_file_post(path: str, *, server_id: str, form: dict | None = None, content: bytes | None = None) -> tuple[httpx.Response | None, dict | None]:
    """Local attachment POSTs with both authorization headers on every stage."""
    if not re.fullmatch(r"(?:users/0/items/[A-Z0-9]{8}/file|local/uploads/[A-Za-z0-9_-]+)", path):
        return None, _error("unsafe_upload_endpoint", "拒绝非本机附件上传端点。")
    with _AUTH_LOCK:
        for attempt in range(2):
            current, error = _server_identity()
            if error:
                return None, error
            if current != server_id:
                _clear_auth_locked()
                return None, _error("server_changed", "Zotero 实例已改变，请重新开始。")
            if _AUTH_SERVER_ID != current:
                _clear_auth_locked()
            if not _AUTH_KEY:
                auth = _authorize_for_server(current)
                if not auth.get("ok"):
                    return None, auth
            headers = {"Zotero-Server-ID": current, "Zotero-API-Key": _AUTH_KEY, "Content-Type": "application/octet-stream" if content is not None else "application/x-www-form-urlencoded"}
            if form is not None:
                headers["If-None-Match"] = "*"
            response, error = _request("POST", path, headers=headers, form_body=form, raw_content=content)
            if error:
                return None, error
            if response.status_code == 401 and attempt == 0:
                _clear_auth_locked()
                continue
            _consume_single_use_key()
            return response, _write_error(response)
    return None, _error("authorization_failed", "本地上传授权失败。")


def attach_new_pdf(doi: str, parent_item_key: str, path: Path, content: bytes, pending: dict | None = None) -> dict:
    """Create new imported_file child and perform official local 3-phase upload.

    Never modifies/deletes existing PDFs. Failed empty children are reported.
    """
    key = _valid_key(parent_item_key)
    if not key:
        return _error("invalid_input", "父条目 key 无效。")
    server_id, error = _server_identity()
    if error:
        return error
    parent, error = _read_json(f"users/0/items/{key}", server_id=server_id)
    if error:
        return error
    data = _data(parent)
    stored = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", str(data.get("DOI", "")).strip(), flags=re.I)
    if data.get("itemType") in ("attachment", "note") or stored.casefold() != doi.casefold():
        return _error("parent_doi_mismatch", "父条目的 DOI 不匹配，未写入附件。")
    children, error = _read_json(f"users/0/items/{key}/children", server_id=server_id)
    if error:
        return error
    if not isinstance(children, list):
        return _error("invalid_response", "附件查重失败。")
    digest = hashlib.md5(content).hexdigest()
    for child in children:
        child_data = _data(child)
        if child_data.get("itemType") == "attachment" and child_data.get("md5") == digest:
            return {"ok": True, "created": False, "duplicate_found": True, "attachment_key": _entry_key(child)}
    attachment_key = None
    if pending:
        if pending.get("server_id") != server_id or pending.get("parent_item_key") != key or pending.get("md5") != digest:
            return _error("pending_upload_mismatch", "待完成附件与当前实例或文件不匹配。")
        attachment_key = _valid_key(pending.get("attachment_key"))
        child = next((c for c in children if _entry_key(c) == attachment_key), None)
        if not child or _data(child).get("parentItem") != key or _data(child).get("md5"):
            return _error("pending_attachment_changed", "待上传附件已改变，不覆盖现有文件。")
    if not attachment_key:
        template, error = _template("attachment")
        if error:
            return error
        if not isinstance(template, dict):
            return _error("invalid_template", "无法获取附件模板。")
        template.update({"itemType": "attachment", "linkMode": "imported_file", "parentItem": key, "title": "PDF", "filename": path.name, "contentType": "application/pdf", "md5": None, "mtime": None})
        response, error = _authorized_write("POST", "users/0/items", [template], expected_server_id=server_id)
        if error:
            return error
        error = _write_error(response)
        if error:
            return error
        if response.headers.get("Zotero-Server-ID", server_id) != server_id:
            return _error("server_changed", "创建附件时 Zotero 实例改变。")
        attachment_key = _created_key(response)
        if not _valid_key(attachment_key):
            return _error("invalid_write_response", "附件可能已创建，但缺少有效 key；请人工检查，勿盲目重试。")
    pending = {"server_id": server_id, "parent_item_key": key, "attachment_key": attachment_key, "md5": digest}

    def failed(error):
        return {**error, "pending_attachment": pending, "message": "保留新建的待上传附件，不删除任何内容；可在本进程内重试。"}

    endpoint = f"users/0/items/{attachment_key}/file"
    response, error = _authorized_file_post(endpoint, server_id=server_id, form={"md5": digest, "filename": path.name, "filesize": str(len(content)), "mtime": str(int(path.stat().st_mtime * 1000))})
    if error:
        return failed(error)
    upload = _json(response)
    if not isinstance(upload, dict):
        return failed(_error("invalid_upload_response", "上传授权格式异常。"))
    if upload.get("exists") == 1:
        return {"ok": True, "created": True, "attachment_key": attachment_key}
    target = urlsplit(str(upload.get("url", "")))
    upload_key = upload.get("uploadKey")
    if target.scheme not in ("", "http") or target.netloc not in ("", "localhost:23119", "127.0.0.1:23119") or target.query or target.fragment or not isinstance(upload_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", upload_key) or target.path != f"/api/local/uploads/{upload_key}" or upload.get("prefix") or upload.get("suffix"):
        return failed(_error("unsafe_upload_url", "拒绝非本机或异常的上传 URL。"))
    response, error = _authorized_file_post(f"local/uploads/{upload_key}", server_id=server_id, content=content)
    if error or response.status_code != 201:
        return failed(error or _error("upload_failed", "PDF 上传未成功。"))
    response, error = _authorized_file_post(endpoint, server_id=server_id, form={"upload": upload_key})
    if error or response.status_code != 204:
        return failed(error or _error("registration_failed", "PDF 注册未完成。"))
    return {"ok": True, "created": True, "duplicate_found": False, "attachment_key": attachment_key, "parent_item_key": key, "uploaded_locally": True}
