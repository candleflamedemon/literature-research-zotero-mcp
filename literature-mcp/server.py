"""ZoteroMCP文献管理服务：文献元数据查询与受控 Zotero 写入 MCP Server。"""

from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import quote

import httpx
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

import excel_catalog
import elsevier_api
import zotero_write
from fulltext_resolver import resolver as fulltext

from network_access import (
    INSTITUTIONAL_TRUST_ENV,
    check_institution_access_target,
    detect_vpn_tun_interfaces,
    proxy_environment_summary,
)


CROSSREF_WORKS_URL = "https://api.crossref.org/works"
OPENALEX_WORKS_URL = "https://api.openalex.org/works"
ZOTERO_LOCAL_API_URL = "http://localhost:23119/api/"
ZOTERO_LIBRARY_PREFIX = "users/0"
REQUEST_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
ZOTERO_TIMEOUT = httpx.Timeout(8.0, connect=2.0)
MAX_RESULTS = 20
MAX_ZOTERO_RESULTS = 50
USER_AGENT = "LiteratureMCP/0.9 (scholarly metadata client)"
ZOTERO_READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
INSTITUTIONAL_READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)
ZOTERO_WRITE_AUTH = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
ZOTERO_WRITE_CREATE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)
ZOTERO_WRITE_MEMBERSHIP_REMOVE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=False,
)
EXCEL_READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

mcp = MCPServer("ZoteroMCP文献管理服务")


@mcp.tool(annotations=ZOTERO_READ_ONLY)
def elsevier_api_status() -> dict[str, Any]:
    """仅本地检查 Elsevier 配置和协议确认状态；不联网、不输出密钥。"""
    return elsevier_api.diagnostics.status()


@mcp.tool(annotations=INSTITUTIONAL_READ_ONLY)
def check_elsevier_entitlement(doi: str) -> dict[str, Any]:
    """本地用户确认 API/MCP 用途获准后，只读检查单个 DOI 的 ENTITLED 权益；不请求全文。"""
    return elsevier_api.diagnostics.check(doi)


def _error(
    source: str,
    code: str,
    message: str,
    *,
    status_code: int | None = None,
    retry_after_seconds: int | None = None,
) -> dict[str, Any]:
    """Create a stable, non-throwing error envelope for MCP clients."""

    details: dict[str, Any] = {"code": code, "message": message}
    if status_code is not None:
        details["status_code"] = status_code
    if retry_after_seconds is not None:
        details["retry_after_seconds"] = retry_after_seconds
    return {"ok": False, "source": source, "results": [], "error": details}


def _request_json(
    source: str, url: str, params: dict[str, Any] | None = None
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Fetch JSON from a fixed metadata API with explicit timeout handling."""

    try:
        response = httpx.get(
            url,
            params=params,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
        )
    except httpx.TimeoutException:
        return None, _error(source, "timeout", "元数据服务请求超时，请稍后重试。")
    except httpx.RequestError:
        return None, _error(source, "network_error", "无法连接元数据服务，请检查网络后重试。")

    if response.status_code == 404:
        return None, _error(
            source, "not_found", "未找到匹配的文献记录。", status_code=404
        )
    if response.status_code == 429:
        retry_after: int | None = None
        raw_retry_after = response.headers.get("Retry-After")
        if raw_retry_after and raw_retry_after.isdigit():
            retry_after = int(raw_retry_after)
        return None, _error(
            source,
            "rate_limited",
            "元数据服务请求过于频繁，请稍后重试。",
            status_code=429,
            retry_after_seconds=retry_after,
        )
    if response.status_code >= 500:
        return None, _error(
            source,
            "upstream_error",
            "元数据服务暂时不可用，请稍后重试。",
            status_code=response.status_code,
        )
    if response.status_code >= 400:
        return None, _error(
            source,
            "bad_request",
            "元数据服务拒绝了该查询，请检查查询条件。",
            status_code=response.status_code,
        )

    try:
        payload = response.json()
    except ValueError:
        return None, _error(source, "invalid_response", "元数据服务返回了无效 JSON。")

    if not isinstance(payload, dict):
        return None, _error(source, "invalid_response", "元数据服务返回格式异常。")
    return payload, None


def _zotero_error(
    code: str,
    message: str,
    *,
    running: bool | None,
    local_api_enabled: bool | None,
    status_code: int | None = None,
) -> dict[str, Any]:
    details: dict[str, Any] = {"code": code, "message": message}
    if status_code is not None:
        details["status_code"] = status_code
    return {
        "ok": False,
        "source": "Zotero Local API",
        "running": running,
        "local_api_enabled": local_api_enabled,
        "error": details,
    }


def _zotero_get(
    path: str = "", params: dict[str, Any] | None = None
) -> tuple[httpx.Response | None, dict[str, Any] | None]:
    """Send only a local GET; environment proxies and redirects are disabled."""

    try:
        with httpx.Client(
            base_url=ZOTERO_LOCAL_API_URL,
            headers={
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
                "Zotero-API-Version": "3",
            },
            timeout=ZOTERO_TIMEOUT,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            response = client.get(path.lstrip("/"), params=params)
    except httpx.ConnectError:
        return None, _zotero_error(
            "zotero_not_running",
            "无法连接 Zotero。请先启动 Zotero 桌面客户端。",
            running=False,
            local_api_enabled=None,
        )
    except httpx.TimeoutException:
        return None, _zotero_error(
            "zotero_unresponsive",
            "Zotero Local API 响应超时，请确认 Zotero 正常运行。",
            running=None,
            local_api_enabled=None,
        )
    except httpx.RequestError:
        return None, _zotero_error(
            "local_connection_error",
            "连接 Zotero Local API 时发生本机网络错误。",
            running=None,
            local_api_enabled=None,
        )

    if response.status_code == 403:
        return None, _zotero_error(
            "local_api_disabled",
            "Zotero 正在运行，但 Local API 未启用。请在 Zotero 设置 → 高级中启用“允许此计算机上的其他应用程序与 Zotero 通信”。",
            running=True,
            local_api_enabled=False,
            status_code=403,
        )
    if response.status_code == 404:
        return None, _zotero_error(
            "not_found",
            "Zotero 中未找到请求的对象。",
            running=True,
            local_api_enabled=True,
            status_code=404,
        )
    if response.status_code >= 500:
        return None, _zotero_error(
            "local_api_error",
            "Zotero Local API 暂时无法完成请求。",
            running=True,
            local_api_enabled=True,
            status_code=response.status_code,
        )
    if response.status_code >= 400:
        return None, _zotero_error(
            "invalid_request",
            "Zotero Local API 拒绝了该只读请求。",
            running=True,
            local_api_enabled=True,
            status_code=response.status_code,
        )
    return response, None


def _zotero_json(
    path: str, params: dict[str, Any] | None = None
) -> tuple[Any, dict[str, Any] | None]:
    response, error = _zotero_get(path, params)
    if error:
        return None, error
    try:
        return response.json(), None
    except ValueError:
        return None, _zotero_error(
            "invalid_response",
            "Zotero Local API 返回了无效 JSON。",
            running=True,
            local_api_enabled=True,
        )


def _clean_text(value: Any) -> str | None:
    """Return plain text from optional API text, including Crossref JATS markup."""

    if not isinstance(value, str) or not value.strip():
        return None
    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip() or None


def _first_text(value: Any) -> str | None:
    if isinstance(value, list):
        for item in value:
            cleaned = _clean_text(item)
            if cleaned:
                return cleaned
        return None
    return _clean_text(value)


def _normalise_doi(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    doi = value.strip()
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", doi, flags=re.I)
    return doi or None


def _crossref_year(item: dict[str, Any]) -> int | None:
    for key in ("published-print", "published-online", "published", "issued"):
        date_parts = item.get(key)
        if not isinstance(date_parts, dict):
            continue
        parts = date_parts.get("date-parts")
        if not isinstance(parts, list) or not parts or not isinstance(parts[0], list):
            continue
        if parts[0] and isinstance(parts[0][0], int):
            return parts[0][0]
    return None


def _crossref_authors(item: dict[str, Any]) -> list[str]:
    authors: list[str] = []
    raw_authors = item.get("author")
    if not isinstance(raw_authors, list):
        return authors
    for author in raw_authors:
        if not isinstance(author, dict):
            continue
        name = " ".join(
            part.strip()
            for part in (author.get("given"), author.get("family"))
            if isinstance(part, str) and part.strip()
        )
        if not name:
            name = _clean_text(author.get("name")) or ""
        if name:
            authors.append(name)
    return authors


def _normalise_crossref(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        item = {}
    doi = _normalise_doi(item.get("DOI"))
    return {
        "title": _first_text(item.get("title")),
        "authors": _crossref_authors(item),
        "year": _crossref_year(item),
        "journal/source": _first_text(item.get("container-title"))
        or _clean_text(item.get("publisher")),
        "DOI": doi,
        "URL": _clean_text(item.get("URL"))
        or (f"https://doi.org/{doi}" if doi else None),
        "abstract": _clean_text(item.get("abstract")),
        "cited_by_count": item.get("is-referenced-by-count")
        if isinstance(item.get("is-referenced-by-count"), int)
        else None,
        "open_access": None,
    }


def _openalex_abstract(inverted_index: Any) -> str | None:
    if not isinstance(inverted_index, dict):
        return None
    positioned_words: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        if not isinstance(word, str) or not isinstance(positions, list):
            continue
        for position in positions:
            if isinstance(position, int):
                positioned_words.append((position, word))
    if not positioned_words:
        return None
    positioned_words.sort(key=lambda pair: pair[0])
    return " ".join(word for _, word in positioned_words)


def _openalex_authors(item: dict[str, Any]) -> list[str]:
    authors: list[str] = []
    authorships = item.get("authorships")
    if not isinstance(authorships, list):
        return authors
    for authorship in authorships:
        if not isinstance(authorship, dict):
            continue
        author = authorship.get("author")
        if isinstance(author, dict):
            name = _clean_text(author.get("display_name"))
            if name:
                authors.append(name)
    return authors


def _openalex_source(item: dict[str, Any]) -> str | None:
    location = item.get("primary_location")
    if isinstance(location, dict):
        source = location.get("source")
        if isinstance(source, dict):
            return _clean_text(source.get("display_name"))
    return None


def _openalex_url(item: dict[str, Any], doi: str | None) -> str | None:
    location = item.get("primary_location")
    if isinstance(location, dict):
        landing_page = _clean_text(location.get("landing_page_url"))
        if landing_page:
            return landing_page
    if doi:
        return f"https://doi.org/{doi}"
    return _clean_text(item.get("id"))


def _openalex_topics(item: dict[str, Any]) -> list[dict[str, Any]]:
    topics: list[dict[str, Any]] = []
    raw_topics = item.get("topics")
    if not isinstance(raw_topics, list):
        return topics
    for topic in raw_topics:
        if not isinstance(topic, dict):
            continue
        topics.append(
            {
                "id": _clean_text(topic.get("id")),
                "name": _clean_text(topic.get("display_name")),
                "score": topic.get("score")
                if isinstance(topic.get("score"), (int, float))
                else None,
            }
        )
    return topics


def _string_list(value: Any, limit: int = 20) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)][:limit]


def _normalise_openalex(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        item = {}
    ids = item.get("ids") if isinstance(item.get("ids"), dict) else {}
    doi = _normalise_doi(item.get("doi") or ids.get("doi"))
    oa = item.get("open_access") if isinstance(item.get("open_access"), dict) else {}
    year = item.get("publication_year")
    return {
        "title": _clean_text(item.get("title") or item.get("display_name")),
        "authors": _openalex_authors(item),
        "year": year if isinstance(year, int) else None,
        "journal/source": _openalex_source(item),
        "DOI": doi,
        "URL": _openalex_url(item, doi),
        "abstract": _openalex_abstract(item.get("abstract_inverted_index")),
        "cited_by_count": item.get("cited_by_count")
        if isinstance(item.get("cited_by_count"), int)
        else None,
        "open_access": {
            "is_oa": oa.get("is_oa") if isinstance(oa.get("is_oa"), bool) else None,
            "status": _clean_text(oa.get("oa_status")),
        },
        "topics": _openalex_topics(item),
        "referenced_works": _string_list(item.get("referenced_works")),
        "related_works": _string_list(item.get("related_works")),
    }


def _zotero_data(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        return {}
    data = entry.get("data")
    return data if isinstance(data, dict) else entry


def _zotero_creators(data: dict[str, Any]) -> list[dict[str, str | None]]:
    creators: list[dict[str, str | None]] = []
    raw_creators = data.get("creators")
    if not isinstance(raw_creators, list):
        return creators
    for creator in raw_creators:
        if not isinstance(creator, dict):
            continue
        name = _clean_text(creator.get("name"))
        if not name:
            name = " ".join(
                part.strip()
                for part in (creator.get("firstName"), creator.get("lastName"))
                if isinstance(part, str) and part.strip()
            ) or None
        if name:
            creators.append(
                {
                    "name": name,
                    "creator_type": _clean_text(creator.get("creatorType")),
                }
            )
    return creators


def _zotero_year(value: Any) -> int | None:
    text = _clean_text(value)
    if not text:
        return None
    match = re.search(r"\b(?:1|2)\d{3}\b", text)
    return int(match.group()) if match else None


def _zotero_source(data: dict[str, Any]) -> str | None:
    for field in (
        "publicationTitle",
        "bookTitle",
        "proceedingsTitle",
        "websiteTitle",
        "blogTitle",
        "university",
        "publisher",
    ):
        value = _clean_text(data.get(field))
        if value:
            return value
    return None


def _zotero_tags(data: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    raw_tags = data.get("tags")
    if not isinstance(raw_tags, list):
        return tags
    for entry in raw_tags:
        if isinstance(entry, dict):
            tag = _clean_text(entry.get("tag"))
            if tag:
                tags.append(tag)
    return tags


def _normalise_zotero_item(entry: Any, *, detailed: bool) -> dict[str, Any]:
    data = _zotero_data(entry)
    creators = _zotero_creators(data)
    authors = [
        creator["name"]
        for creator in creators
        if creator.get("creator_type") in {"author", "bookAuthor"}
        and creator.get("name")
    ]
    if not authors:
        authors = [creator["name"] for creator in creators if creator.get("name")]
    key = None
    if isinstance(entry, dict):
        key = _clean_text(entry.get("key"))
    key = key or _clean_text(data.get("key"))
    result: dict[str, Any] = {
        "item_key": key,
        "item_type": _clean_text(data.get("itemType")),
        "title": _clean_text(data.get("title")),
        "authors": authors,
        "creators": creators,
        "year": _zotero_year(data.get("date")),
        "date": _clean_text(data.get("date")),
        "journal/source": _zotero_source(data),
        "DOI": _normalise_doi(data.get("DOI")),
        "URL": _clean_text(data.get("url")),
    }
    if detailed:
        result.update(
            {
                "abstract": _clean_text(data.get("abstractNote")),
                "ISBN": _clean_text(data.get("ISBN")),
                "ISSN": _clean_text(data.get("ISSN")),
                "volume": _clean_text(data.get("volume")),
                "issue": _clean_text(data.get("issue")),
                "pages": _clean_text(data.get("pages")),
                "language": _clean_text(data.get("language")),
                "tags": _zotero_tags(data),
                "collections": _string_list(data.get("collections"), limit=100),
                "parent_item": _clean_text(data.get("parentItem")),
            }
        )
    return result


def _normalise_zotero_collection(entry: Any) -> dict[str, Any]:
    data = _zotero_data(entry)
    meta = entry.get("meta") if isinstance(entry, dict) else None
    meta = meta if isinstance(meta, dict) else {}
    key = _clean_text(entry.get("key")) if isinstance(entry, dict) else None
    return {
        "collection_key": key or _clean_text(data.get("key")),
        "name": _clean_text(data.get("name")),
        "parent_collection": _clean_text(data.get("parentCollection")),
        "num_items": meta.get("numItems")
        if isinstance(meta.get("numItems"), int)
        else None,
        "num_subcollections": meta.get("numCollections")
        if isinstance(meta.get("numCollections"), int)
        else None,
    }


def _bounded_zotero_limit(limit: int) -> int:
    return min(max(limit, 1), MAX_ZOTERO_RESULTS)


def _valid_zotero_key(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    key = value.strip().upper()
    return key if re.fullmatch(r"[A-Z0-9]{8}", key) else None


def _bounded_limit(limit: int) -> int:
    return min(max(limit, 1), MAX_RESULTS)


def _valid_year(year: int | None) -> bool:
    return year is None or 1000 <= year <= 3000


@mcp.tool()
def ping() -> str:
    """检查 Literature MCP 服务是否可以正常响应。"""

    return "Literature MCP 工作正常"


@mcp.tool()
def lookup_doi(doi: str) -> dict[str, Any]:
    """通过 Crossref 只读查询 DOI，并返回标准化书目信息。"""

    normalised_doi = _normalise_doi(doi)
    if not normalised_doi:
        return _error("Crossref", "invalid_input", "DOI 不能为空。")
    payload, error = _request_json(
        "Crossref", f"{CROSSREF_WORKS_URL}/{quote(normalised_doi, safe='')}"
    )
    if error:
        return error
    message = payload.get("message") if payload else None
    if not isinstance(message, dict):
        return _error("Crossref", "invalid_response", "Crossref 返回格式异常。")
    return {"ok": True, "source": "Crossref", "results": [_normalise_crossref(message)]}


@mcp.tool()
def search_crossref(
    query: str, limit: int = 5, year: int | None = None
) -> dict[str, Any]:
    """通过 Crossref 按标题或书目信息只读搜索，可选限定出版年份。"""

    if not isinstance(query, str) or not query.strip():
        return _error("Crossref", "invalid_input", "检索词不能为空。")
    if not _valid_year(year):
        return _error("Crossref", "invalid_input", "年份必须在 1000 到 3000 之间。")
    params: dict[str, Any] = {
        "query.bibliographic": query.strip(),
        "rows": _bounded_limit(limit),
    }
    if year is not None:
        params["filter"] = f"from-pub-date:{year}-01-01,until-pub-date:{year}-12-31"
    payload, error = _request_json("Crossref", CROSSREF_WORKS_URL, params)
    if error:
        return error
    message = payload.get("message") if payload else None
    items = message.get("items") if isinstance(message, dict) else None
    if not isinstance(items, list):
        return _error("Crossref", "invalid_response", "Crossref 返回格式异常。")
    return {
        "ok": True,
        "source": "Crossref",
        "count": len(items),
        "results": [_normalise_crossref(item) for item in items],
    }


@mcp.tool()
def search_openalex(
    query: str,
    limit: int = 5,
    from_year: int | None = None,
    to_year: int | None = None,
) -> dict[str, Any]:
    """通过 OpenAlex 只读搜索论文，可限定年份范围并返回主题和文献关系。"""

    if not isinstance(query, str) or not query.strip():
        return _error("OpenAlex", "invalid_input", "检索词不能为空。")
    if not _valid_year(from_year) or not _valid_year(to_year):
        return _error("OpenAlex", "invalid_input", "年份必须在 1000 到 3000 之间。")
    if from_year is not None and to_year is not None and from_year > to_year:
        return _error("OpenAlex", "invalid_input", "起始年份不能晚于结束年份。")

    params: dict[str, Any] = {
        "search": query.strip(),
        "per_page": _bounded_limit(limit),
    }
    filters: list[str] = []
    if from_year is not None:
        filters.append(f"from_publication_date:{from_year}-01-01")
    if to_year is not None:
        filters.append(f"to_publication_date:{to_year}-12-31")
    if filters:
        params["filter"] = ",".join(filters)

    payload, error = _request_json("OpenAlex", OPENALEX_WORKS_URL, params)
    if error:
        return error
    items = payload.get("results") if payload else None
    if not isinstance(items, list):
        return _error("OpenAlex", "invalid_response", "OpenAlex 返回格式异常。")
    return {
        "ok": True,
        "source": "OpenAlex",
        "count": len(items),
        "results": [_normalise_openalex(item) for item in items],
    }


@mcp.tool(annotations=ZOTERO_READ_ONLY)
def zotero_status() -> dict[str, Any]:
    """只读检查本机 Zotero 是否运行以及 Local API 是否启用。"""

    response, error = _zotero_get()
    if error:
        return error
    return {
        "ok": True,
        "source": "Zotero Local API",
        "running": True,
        "local_api_enabled": True,
        "base_url": ZOTERO_LOCAL_API_URL,
        "api_version": response.headers.get("Zotero-API-Version"),
        "schema_version": response.headers.get("Zotero-Schema-Version"),
        "message": "Zotero 正在运行，Local API 已启用。",
    }


@mcp.tool(annotations=ZOTERO_READ_ONLY)
def search_zotero(
    query: str, limit: int = 10, qmode: str = "titleCreatorYear"
) -> dict[str, Any]:
    """只读搜索本机 Zotero；默认匹配标题、作者和年份。"""

    if not isinstance(query, str) or not query.strip():
        return _zotero_error(
            "invalid_input",
            "检索词不能为空。",
            running=None,
            local_api_enabled=None,
        )
    if qmode not in {"titleCreatorYear", "everything"}:
        return _zotero_error(
            "invalid_input",
            "qmode 只能是 titleCreatorYear 或 everything。",
            running=None,
            local_api_enabled=None,
        )
    payload, error = _zotero_json(
        f"{ZOTERO_LIBRARY_PREFIX}/items/top",
        {
            "q": query.strip(),
            "qmode": qmode,
            "limit": _bounded_zotero_limit(limit),
            "sort": "dateModified",
            "direction": "desc",
        },
    )
    if error:
        return error
    if not isinstance(payload, list):
        return _zotero_error(
            "invalid_response",
            "Zotero Local API 返回的文献列表格式异常。",
            running=True,
            local_api_enabled=True,
        )
    return {
        "ok": True,
        "source": "Zotero Local API",
        "count": len(payload),
        "results": [
            _normalise_zotero_item(item, detailed=False) for item in payload
        ],
    }


@mcp.tool(annotations=ZOTERO_READ_ONLY)
def list_collections(
    limit: int = 10, top_level_only: bool = False
) -> dict[str, Any]:
    """只读列出本机 Zotero 的少量 Collections。"""

    suffix = "/top" if top_level_only else ""
    payload, error = _zotero_json(
        f"{ZOTERO_LIBRARY_PREFIX}/collections{suffix}",
        {
            "limit": _bounded_zotero_limit(limit),
            "sort": "title",
            "direction": "asc",
        },
    )
    if error:
        return error
    if not isinstance(payload, list):
        return _zotero_error(
            "invalid_response",
            "Zotero Local API 返回的 Collection 列表格式异常。",
            running=True,
            local_api_enabled=True,
        )
    return {
        "ok": True,
        "source": "Zotero Local API",
        "count": len(payload),
        "results": [_normalise_zotero_collection(item) for item in payload],
    }


@mcp.tool(annotations=ZOTERO_READ_ONLY)
def list_collection_items(collection_key: str, limit: int = 50) -> dict[str, Any]:
    """只读列出指定 Collection 的顶层书目条目，不返回附件路径或笔记正文。"""

    key = _valid_zotero_key(collection_key)
    if not key:
        return _zotero_error(
            "invalid_input",
            "collection_key 必须是 8 位英文字母或数字。",
            running=None,
            local_api_enabled=None,
        )
    payload, error = _zotero_json(
        f"{ZOTERO_LIBRARY_PREFIX}/collections/{key}/items/top",
        {
            "limit": _bounded_zotero_limit(limit),
            "sort": "title",
            "direction": "asc",
        },
    )
    if error:
        return error
    if not isinstance(payload, list):
        return _zotero_error(
            "invalid_response",
            "Zotero Local API 返回的 Collection 条目列表格式异常。",
            running=True,
            local_api_enabled=True,
        )
    return {
        "ok": True,
        "source": "Zotero Local API",
        "collection_key": key,
        "count": len(payload),
        "results": [
            _normalise_zotero_item(item, detailed=False) for item in payload
        ],
    }


@mcp.tool(annotations=ZOTERO_READ_ONLY)
def get_zotero_item(item_key: str) -> dict[str, Any]:
    """按 Zotero item key 只读获取书目字段，不返回文件路径或笔记正文。"""

    key = _valid_zotero_key(item_key)
    if not key:
        return _zotero_error(
            "invalid_input",
            "item_key 必须是 8 位英文字母或数字。",
            running=None,
            local_api_enabled=None,
        )
    payload, error = _zotero_json(f"{ZOTERO_LIBRARY_PREFIX}/items/{key}")
    if error:
        return error
    if not isinstance(payload, dict):
        return _zotero_error(
            "invalid_response",
            "Zotero Local API 返回的文献条目格式异常。",
            running=True,
            local_api_enabled=True,
        )
    return {
        "ok": True,
        "source": "Zotero Local API",
        "result": _normalise_zotero_item(payload, detailed=True),
    }


@mcp.tool(annotations=ZOTERO_WRITE_AUTH)
def authorize_zotero_write() -> dict[str, Any]:
    """请求 Zotero 桌面端确认本机写入授权；授权凭据不包含在工具结果中。"""

    return zotero_write.authorize_write()


@mcp.tool(annotations=ZOTERO_WRITE_CREATE)
def create_collection(
    name: str, parent_collection: str | None = None
) -> dict[str, Any]:
    """查重后创建一个 Zotero Collection；同名同父级时不重复写入。"""

    return zotero_write.create_collection(name, parent_collection)


@mcp.tool(annotations=ZOTERO_WRITE_CREATE)
def add_item_to_collection(item_key: str, collection_key: str) -> dict[str, Any]:
    """保留现有 Collection 归属，把一个顶层书目条目加入指定 Collection。"""

    return zotero_write.add_item_to_collection(item_key, collection_key)


@mcp.tool(annotations=ZOTERO_WRITE_MEMBERSHIP_REMOVE)
def remove_item_from_collection(item_key: str, collection_key: str) -> dict[str, Any]:
    """经用户明确确认后，只移除一个 Collection 归属，不删除 Zotero 条目。"""

    return zotero_write.remove_item_from_collection(item_key, collection_key)


@mcp.tool(annotations=ZOTERO_WRITE_CREATE)
def add_paper_by_doi(
    doi: str, collection_key: str | None = None
) -> dict[str, Any]:
    """按 DOI 查重，使用 Crossref 元数据创建单篇 Zotero 文献，不添加附件。"""

    normalised_doi = _normalise_doi(doi)
    if not normalised_doi or not re.fullmatch(r"10\.\d{4,9}/\S+", normalised_doi):
        return _zotero_error(
            "invalid_input",
            "DOI 格式无效。",
            running=None,
            local_api_enabled=None,
        )
    duplicates, duplicate_error = zotero_write.find_paper_by_doi(normalised_doi)
    if duplicate_error:
        return duplicate_error
    if duplicates:
        result: dict[str, Any] = {
            "ok": True,
            "source": "Zotero Local API",
            "created": False,
            "duplicate_found": True,
            "duplicates": duplicates,
        }
        if collection_key:
            if len(duplicates) != 1 or not duplicates[0].get("item_key"):
                return {
                    "ok": False,
                    "source": "Zotero Local API",
                    "created": False,
                    "duplicate_found": True,
                    "duplicates": duplicates,
                    "error": {
                        "code": "ambiguous_duplicate",
                        "message": "DOI 命中多个 Zotero 条目，未自动加入 Collection。",
                    },
                }
            linked = zotero_write.add_item_to_collection(
                duplicates[0]["item_key"], collection_key
            )
            if not linked.get("ok"):
                return linked
            result.update(
                item_key=duplicates[0]["item_key"],
                collection_key=linked["collection_key"],
                collection_added=linked["collection_added"],
                collection_already_present=linked["collection_already_present"],
            )
        return result
    payload, error = _request_json(
        "Crossref", f"{CROSSREF_WORKS_URL}/{quote(normalised_doi, safe='')}"
    )
    if error:
        return error
    message = payload.get("message") if payload else None
    if not isinstance(message, dict):
        return _error("Crossref", "invalid_response", "Crossref 返回格式异常。")
    return zotero_write.create_paper_from_crossref(
        normalised_doi, message, collection_key
    )


@mcp.tool(annotations=ZOTERO_WRITE_CREATE)
def add_paper_by_metadata(
    title: str,
    authors: list[str] | None = None,
    year: int | None = None,
    publication_title: str | None = None,
    url: str | None = None,
    collection_key: str | None = None,
) -> dict[str, Any]:
    """标题精确查重后，以用户已确认的书目信息创建无 DOI 期刊条目。"""

    return zotero_write.create_paper_from_metadata(
        title=title,
        authors=authors,
        year=year,
        publication_title=publication_title,
        url=url,
        collection_key=collection_key,
    )


@mcp.tool(annotations=ZOTERO_WRITE_CREATE)
def add_tags(item_key: str, tags: list[str]) -> dict[str, Any]:
    """保留原有 tags 并仅添加缺少项；禁止修改附件条目。"""

    return zotero_write.add_tags(item_key, tags)


@mcp.tool(annotations=ZOTERO_WRITE_CREATE)
def add_note(parent_item_key: str, note: str) -> dict[str, Any]:
    """查重后为一个普通 Zotero 条目添加纯文本子 Note，不返回 Note 正文。"""

    return zotero_write.add_note(parent_item_key, note)


@mcp.tool(annotations=EXCEL_READ_ONLY)
def list_excel_sheets(excel_path: str) -> dict[str, Any]:
    """只读列出工作区内 Excel 的 Sheet、有效区域和文献候选评分。"""

    return excel_catalog.list_sheets(excel_path)


@mcp.tool(annotations=EXCEL_READ_ONLY)
def inspect_excel_schema(
    excel_path: str, sheet_name: str | None = None
) -> dict[str, Any]:
    """自动识别 Excel 表头及文献字段；不要求固定列顺序。"""

    return excel_catalog.inspect_schema(excel_path, sheet_name)


@mcp.tool(annotations=EXCEL_READ_ONLY)
def preview_references(
    excel_path: str,
    sheet_name: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """只读预览标准化文献记录和无法识别的行。"""

    return excel_catalog.preview(excel_path, sheet_name, limit)


@mcp.tool(annotations=EXCEL_READ_ONLY)
def extract_references(
    excel_path: str,
    sheet_name: str | None = None,
    max_rows: int = 2_000,
) -> dict[str, Any]:
    """只读提取标准化文献记录，保留 Sheet、Excel 原始行号和逐行错误。"""

    return excel_catalog.extract(excel_path, sheet_name, max_rows)


@mcp.tool(annotations=INSTITUTIONAL_READ_ONLY)
def find_oa_fulltext(doi: str) -> dict[str, Any]:
    """仅查询 Unpaywall/OpenAlex 合法 OA 地址，不获取 PDF。"""
    return fulltext.find_oa(doi)


@mcp.tool(annotations=INSTITUTIONAL_READ_ONLY)
def classify_fulltext_access(doi: str, item_key: str | None = None, publisher_url: str | None = None, institutional_route: str = "IP") -> dict[str, Any]:
    """依次检查 Zotero 附件、OA、机构访问、浏览器登录；不下载 PDF。"""
    return fulltext.classify(doi, item_key, publisher_url, institutional_route)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True))
def fetch_oa_pdf(doi: str, item_key: str | None = None) -> dict[str, Any]:
    """从有 API OA 证据的来源获取单篇 PDF，本地独立保存；不写 Zotero。"""
    return fulltext.fetch_oa(doi, item_key)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True))
def fetch_institutional_pdf(doi: str, publisher_url: str, institutional_route: str = "IP", item_key: str | None = None) -> dict[str, Any]:
    """仅机构检测为 AVAILABLE 才获取 PDF；不继承环境代理、不登录、不写 Zotero。"""
    return fulltext.fetch_institutional(doi, publisher_url, institutional_route, item_key)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False))
def queue_browser_access(doi: str, publisher_url: str | None = None) -> dict[str, Any]:
    """加入 NEEDS_BROWSER_LOGIN 本地队列，交给用户合法登录和 Zotero Connector。"""
    return fulltext.queue(doi, publisher_url)


@mcp.tool(annotations=ZOTERO_WRITE_CREATE)
def attach_pdf_to_zotero(doi: str, parent_item_key: str, confirm: bool = False) -> dict[str, Any]:
    """明确确认和桌面授权后，将已校验的 Resolver PDF 作为新子附件本地上传。"""
    return fulltext.attach(doi, parent_item_key, confirm)


@mcp.tool(annotations=ZOTERO_READ_ONLY)
def acquisition_report(doi: str | None = None) -> dict[str, Any]:
    """返回全文获取记录和停止来源；不输出凭据、Cookie 或公网 IP。"""
    return fulltext.report(doi)


def _routing_risk(tun: dict[str, Any]) -> dict[str, str]:
    if tun.get("active_candidate_detected") is True:
        return {
            "level": "HIGH",
            "category": "POSSIBLE_ACTIVE_TUN",
            "message": "发现可能正在工作的 VPN/TUN 接口，trust_env=False 无法保证出版社流量绕过它。",
            "user_action": "请在代理/VPN 软件中把学校、出版社和 DOI 相关域名配置为直连或正确的机构访问线路；Literature MCP 不会修改路由。",
        }
    if tun.get("installed_candidate_detected") is True:
        return {
            "level": "MEDIUM",
            "category": "TUN_ADAPTER_PRESENT",
            "message": "发现可能的 VPN/TUN 适配器，但未确认其当前接管流量。",
            "user_action": "启用代理/VPN 时，请由你在对应软件中配置学校和出版社域名分流；Literature MCP 不会修改代理或路由。",
        }
    if tun.get("detection") == "UNKNOWN":
        return {
            "level": "UNKNOWN",
            "category": "INTERFACE_DETECTION_UNAVAILABLE",
            "message": "无法可靠读取网络接口，不能判断是否存在 TUN 接管。",
            "user_action": "请人工确认代理软件的 TUN/系统代理模式及学校、出版社域名分流。",
        }
    return {
        "level": "LOW",
        "category": "ENV_PROXY_ISOLATED_NO_TUN_SIGNAL",
        "message": "环境变量代理已隔离，当前未发现明显活动 TUN 信号，但仍不能作绝对保证。",
        "user_action": "若机构访问测试异常，请人工检查透明代理、TUN、系统 VPN 和路由分流。",
    }


@mcp.tool(annotations=ZOTERO_READ_ONLY)
def diagnose_access_environment() -> dict[str, Any]:
    """只读诊断机构访问客户端的代理继承、VPN/TUN、Zotero 和风险类别。"""

    proxy = proxy_environment_summary()
    tun = detect_vpn_tun_interfaces()
    zotero = zotero_status()
    return {
        "ok": True,
        "institutional_http_client": {
            "trust_env": INSTITUTIONAL_TRUST_ENV,
            "inherits_environment_proxy": False,
            "scope": "仅用于机构访问和全文可用性检测",
        },
        "environment_proxy": proxy,
        "vpn_tun": tun,
        "zotero": {
            "running": zotero.get("running"),
            "local_api_enabled": zotero.get("local_api_enabled"),
        },
        "risk": _routing_risk(tun),
        "privacy": {
            "proxy_values_exposed": False,
            "public_ip_queried": False,
            "credentials_or_tokens_read": False,
        },
    }


@mcp.tool(annotations=INSTITUTIONAL_READ_ONLY)
def check_institution_access(
    test_doi: str | None = None, publisher_url: str | None = None
) -> dict[str, Any]:
    """用一个 DOI 或出版社 URL 保守判断当前网络是否可能具备机构访问权限。"""

    return check_institution_access_target(test_doi, publisher_url)


@mcp.tool(annotations=ZOTERO_READ_ONLY)
def explain_network_routing() -> dict[str, Any]:
    """说明 Codex 网络与 Institutional PDF Worker 网络当前的隔离程度。"""

    diagnosis = diagnose_access_environment()
    tun = diagnosis["vpn_tun"]
    active_tun = tun.get("active_candidate_detected") is True
    possible_tun = tun.get("installed_candidate_detected") is True
    if active_tun:
        overall = "AT_RISK"
    elif possible_tun or tun.get("detection") == "UNKNOWN":
        overall = "PARTIAL"
    else:
        overall = "IMPLEMENTED_WITH_TUN_LIMITATION"
    return {
        "ok": True,
        "codex_network": {
            "status": "UNCHANGED",
            "message": "Codex/OpenAI 继续使用用户当前网络和代理设置；Literature MCP 不修改它。",
        },
        "institutional_pdf_worker_network": {
            "status": "ENVIRONMENT_PROXY_ISOLATED",
            "trust_env": INSTITUTIONAL_TRUST_ENV,
            "message": "机构访问 HTTP Client 不继承 HTTP_PROXY、HTTPS_PROXY 或 ALL_PROXY。",
        },
        "isolation": {
            "overall": overall,
            "environment_variable_proxy": "ISOLATED",
            "system_tun_or_transparent_routing": "NOT_GUARANTEED",
        },
        "warning": (
            "trust_env=False 只能绕过环境变量类 HTTP 代理，不能绕过操作系统路由、透明代理或 TUN 接管。"
        ),
        "user_action": diagnosis["risk"]["user_action"],
    }
