"""Opt-in, read-only Elsevier API entitlement diagnostics.

Credentials are accepted only through the local process environment, never as
MCP arguments or output.  This module does not retrieve article full text or
PDF files.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx
from zotero_write import _suppress_sensitive_http_logs

API_ORIGIN = "https://api.elsevier.com"
KEY_ENV = "ELSEVIER_API_KEY"
APPROVAL_ENV = "ELSEVIER_MCP_DIAGNOSTICS_APPROVED"
TIMEOUT = httpx.Timeout(15, connect=5)
MAX_RESPONSE_BYTES = 32 * 1024
MAX_SESSION_REQUESTS = 3
MIN_INTERVAL_SECONDS = 3
AGREEMENT_URL = "https://dev.elsevier.com/api_service_agreement.html"
POLICY_URL = "https://dev.elsevier.com/policy.html"


def _approved() -> bool:
    # Must be set by the user locally after confirming the specific use is allowed.
    # A flag records an attestation, not independent evidence of authorization.
    return os.environ.get(APPROVAL_ENV) == "yes"


def _key() -> str | None:
    value = os.environ.get(KEY_ENV, "")
    # Only guard header safety; this does not claim a key is valid at Elsevier.
    if not value or len(value) > 512 or any(not 33 <= ord(c) <= 126 for c in value):
        return None
    return value


def _doi(value: str) -> str | None:
    if not isinstance(value, str) or len(value) > 512:
        return None
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value.strip(), flags=re.I).lower()
    if not re.fullmatch(r"10\.\d{4,9}/[^\s?#,\\]+", value):
        return None
    if any(part in (".", "..") for part in value.split("/")):
        return None
    return value


def _integer(value: str | None) -> int | None:
    if value and re.fullmatch(r"\d{1,12}", value):
        return int(value)
    return None


def _retry_after(headers: httpx.Headers) -> int:
    waits = []
    raw = headers.get("Retry-After")
    seconds = _integer(raw)
    if seconds is not None:
        waits.append(seconds)
    elif raw:
        try:
            date = parsedate_to_datetime(raw)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            waits.append(max(0, int((date - datetime.now(timezone.utc)).total_seconds()) + 1))
        except (ValueError, TypeError, OverflowError):
            pass
    reset = _integer(headers.get("X-RateLimit-Reset"))
    if reset is not None:
        waits.append(max(0, int(reset - time.time()) + 1))
    # If the server supplies no actionable time, fail closed pending manual review.
    return max(waits) if waits else 3600


def _quota(headers: httpx.Headers) -> dict[str, int]:
    result = {}
    for header, field in (("X-RateLimit-Limit", "limit"), ("X-RateLimit-Remaining", "remaining"), ("X-RateLimit-Reset", "reset_at_unix_seconds")):
        value = _integer(headers.get(header))
        if value is not None:
            result[field] = value
    return result


ERROR_CODES = frozenset({"AUTHENTICATION_ERROR", "AUTHORIZATION_ERROR", "INVALID_API_KEY",
                         "RESOURCE_NOT_FOUND", "QUOTA_EXCEEDED", "INVALID_REQUEST"})


def _safe_error_details(response: httpx.Response) -> dict[str, Any]:
    """Parse only bounded official error JSON/XML; emit fixed labels, never raw text.

    Error text can contain keys, public IPs and institutional IDs. It exists only
    in local memory for classification and must not be logged or returned.
    HTML/PDF, XML entities/DTDs and unknown response schemas are not processed.
    """
    result = {"error_category": "UNDETERMINED", "error_parse_state": "NOT_READ",
              "api_authentication": "NOT_ESTABLISHED"}
    mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if mime not in {"application/json", "application/xml", "text/xml"}:
        return result
    length = _integer(response.headers.get("Content-Length"))
    if length is not None and length > MAX_RESPONSE_BYTES:
        return {**result, "error_parse_state": "TOO_LARGE"}
    body = bytearray()
    try:
        for chunk in response.iter_bytes():
            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                return {**result, "error_parse_state": "TOO_LARGE"}
            body.extend(chunk)
        if mime == "application/json":
            payload = json.loads(body)
            error = payload.get("service-error", payload.get("error-response")) if isinstance(payload, dict) else None
            status = error.get("status", error) if isinstance(error, dict) else None
            if not isinstance(status, dict):
                return {**result, "error_parse_state": "UNKNOWN_SCHEMA"}
            code, text = status.get("statusCode", status.get("error-code")), status.get("statusText", "")
        else:
            # Reject non-UTF8 XML, DTDs and entity declarations before parsing.
            xml = bytes(body).decode("utf-8-sig")
            if "<!doctype" in xml.lower() or "<!entity" in xml.lower():
                return {**result, "error_parse_state": "UNSAFE_XML_REJECTED"}
            root = ET.fromstring(xml)
            if root.tag.rsplit("}", 1)[-1] not in {"service-error", "error-response"}:
                return {**result, "error_parse_state": "UNKNOWN_SCHEMA"}
            fields = {node.tag.rsplit("}", 1)[-1]: node.text for node in root.iter()}
            code, text = fields.get("statusCode", fields.get("error-code")), fields.get("statusText", "")
        result["error_parse_state"] = "PARSED"
        if code in ERROR_CODES:
            result["server_error_code"] = code
        text = text.lower() if isinstance(text, str) else ""
        if code == "INVALID_API_KEY" or "no apikey provided" in text or "invalid api key" in text or "invalid apikey" in text:
            result["error_category"] = "API_KEY_REJECTED_OR_MISSING"
        elif "does not resolve to an account" in text or "ip address is not recognized" in text:
            result["error_category"] = "INSTITUTIONAL_IP_MAPPING_UNRESOLVED"
        elif "unrecognized or has insufficient privileges" in text:
            result["error_category"] = "KEY_OR_API_PERMISSION_UNRESOLVED"
        elif "configuration settings insufficient" in text or "not authorized to access the requested view" in text:
            result["error_category"] = "API_RESOURCE_PERMISSION_INSUFFICIENT"
        elif code == "AUTHENTICATION_ERROR":
            result["error_category"] = "AUTHENTICATION_UNRESOLVED"
        elif code == "AUTHORIZATION_ERROR":
            result["error_category"] = "AUTHORIZATION_OR_ENTITLEMENT_UNRESOLVED"
        return result
    except (ValueError, UnicodeError, ET.ParseError, RecursionError, TypeError):
        return {**result, "error_parse_state": "INVALID_RESPONSE"}
    finally:
        body.clear()


class ElsevierDiagnostics:
    def __init__(self):
        self.lock = threading.RLock()
        self.requests = 0
        self.last_request = 0.0
        self.cooldown_until = 0.0
        self.denied_dois: set[str] = set()
        self.authentication_failed = False
        self.manual_review_required = False
        self.accepted_key: str | None = None  # memory-only; never returned/logged

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "ok": True,
                "source": "Elsevier API diagnostics",
                "api_key_configured": bool(_key()),
                "api_key_validated": bool(_key()) and _key() == self.accepted_key,
                "api_key_validation_state": "SERVER_ACCEPTED_FOR_ENTITLEMENT_CHECK" if _key() and _key() == self.accepted_key else "NOT_ESTABLISHED",
                "local_use_attestation": "CONFIRMED_BY_LOCAL_FLAG" if _approved() else "UNCONFIRMED",
                "legal_compliance": "NOT_AUTOMATICALLY_VERIFIED",
                "fulltext_download_authorized": False,
                "trust_env": False,
                "routing_isolation": "ENVIRONMENT_PROXY_ONLY_TUN_NOT_GUARANTEED",
                "requests_this_process": self.requests,
                "manual_review_required": self.manual_review_required,
                "max_requests_this_process": MAX_SESSION_REQUESTS,
                "cooldown_remaining_seconds": max(0, int(self.cooldown_until - time.time()) + 1) if self.cooldown_until else 0,
                "agreement_url": AGREEMENT_URL,
                "use_policy_url": POLICY_URL,
                "message": "仅本地配置检查；不验证或接受协议，不请求 Elsevier，不输出密钥。",
            }

    def _result(self, code: str, *, ok: bool = False, status: str = "UNKNOWN", doi: str | None = None, http_status: int | None = None, **extra) -> dict[str, Any]:
        result = {"ok": ok, "source": "Elsevier API diagnostics", "status": status, "code": code,
                  "downloaded_fulltext": False, "downloaded_pdf": False, "zotero_modified": False,
                  "trust_env": False, "used_browser_cookies": False,
                  "legal_compliance": "NOT_AUTOMATICALLY_VERIFIED", "fulltext_download_authorized": False,
                  "message": "权益响应不等于 PDF 格式权限、MCP/AI 使用许可或全文保存许可。"}
        if doi is not None:
            result["DOI"] = doi
        if http_status is not None:
            result["http_status"] = http_status
        result.update(extra)
        return result

    def check(self, doi: str) -> dict[str, Any]:
        value = _doi(doi)
        if value is None:
            return self._result("invalid_doi")
        if not _approved():
            return self._result("use_authorization_unconfirmed", doi=value)
        key = _key()
        if key is None:
            return self._result("api_key_not_configured_or_unsafe", doi=value)
        # Serialize the entire request. One process, no batch tool, no retry loop.
        with self.lock:
            if self.manual_review_required:
                return self._result("browser_or_support_review_required", doi=value)
            if self.authentication_failed:
                return self._result("previous_authentication_failure_not_retried", doi=value)
            if value in self.denied_dois:
                return self._result("previous_doi_denial_not_retried", doi=value)
            if self.cooldown_until > time.time():
                return self._result("cooldown_active", doi=value, retry_after_seconds=max(1, int(self.cooldown_until - time.time()) + 1))
            if self.requests >= MAX_SESSION_REQUESTS:
                return self._result("diagnostic_session_limit", doi=value)
            delay = max(0, self.last_request + MIN_INTERVAL_SECONDS - time.monotonic())
            if delay:
                time.sleep(delay)
            self.requests += 1
            self.last_request = time.monotonic()
            url = API_ORIGIN + "/content/article/doi/" + quote(value, safe="/")
            try:
                with _suppress_sensitive_http_logs(True), httpx.Client(trust_env=False, follow_redirects=False, timeout=TIMEOUT, headers={"Accept": "application/json", "X-ELS-APIKey": key, "User-Agent": "LiteratureMCP/0.8 (read-only entitlement diagnostic)"}) as client:
                    client.cookies.clear()
                    with client.stream("GET", url, params={"view": "ENTITLED"}) as response:
                        http_status = response.status_code
                        quota = _quota(response.headers)
                        if http_status == 429 or quota.get("remaining") == 0:
                            wait = _retry_after(response.headers)
                            self.cooldown_until = time.time() + wait
                        if http_status == 429:
                            return self._result("rate_limited", doi=value, http_status=http_status, retry_after_seconds=wait, quota=quota)
                        if http_status == 401:
                            self.authentication_failed = True
                            self.accepted_key = None
                            details = _safe_error_details(response)
                            if response.headers.get("Content-Type", "").lower().startswith("text/html"):
                                self.manual_review_required = True
                            return self._result("authentication_failed", doi=value, http_status=http_status, **details)
                        if http_status == 403:
                            self.denied_dois.add(value)
                            details = _safe_error_details(response)
                            if details["error_category"] == "API_KEY_REJECTED_OR_MISSING":
                                self.authentication_failed = True
                                self.accepted_key = None
                            if response.headers.get("Content-Type", "").lower().startswith("text/html"):
                                self.manual_review_required = True
                            return self._result("authorization_or_entitlement_denied", doi=value, http_status=http_status, **details)
                        if http_status == 404:
                            return self._result("not_found", doi=value, http_status=http_status)
                        if 300 <= http_status < 400:
                            self.manual_review_required = True
                            return self._result("redirect_not_followed", doi=value, http_status=http_status)
                        if http_status != 200:
                            return self._result("api_http_error", doi=value, http_status=http_status)
                        mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                        if mime != "application/json":
                            self.manual_review_required = True
                            return self._result("unexpected_content_type_not_read", doi=value, http_status=http_status)
                        length = _integer(response.headers.get("Content-Length"))
                        if length is not None and length > MAX_RESPONSE_BYTES:
                            return self._result("response_too_large", doi=value, http_status=http_status)
                        body = bytearray()
                        for chunk in response.iter_bytes():
                            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                                return self._result("response_too_large", doi=value, http_status=http_status)
                            body.extend(chunk)
                        payload = json.loads(body)
                        # ENTITLED has a dedicated response, not full-text-retrieval-response.
                        # Never return raw payload, error strings, account IDs or headers.
                        document = payload.get("document-entitlement") if isinstance(payload, dict) else None
                        state = document.get("status") if isinstance(document, dict) else None
                        if state == "ENTITLED":
                            self.accepted_key = key
                            return self._result("entitled", ok=True, status="AVAILABLE", doi=value, http_status=http_status, api_authentication="ACCEPTED_FOR_THIS_CHECK", entitlement_type="NOT_DISTINGUISHED_OA_OR_SUBSCRIPTION", quota=quota)
                        if state == "NOT_ENTITLED":
                            self.accepted_key = key
                            return self._result("not_entitled", ok=True, status="NOT_AVAILABLE", doi=value, http_status=http_status, api_authentication="ACCEPTED_FOR_THIS_CHECK", quota=quota)
                        return self._result("unrecognized_entitlement_response", doi=value, http_status=http_status, quota=quota)
            except httpx.TimeoutException:
                return self._result("timeout_no_retry", doi=value)
            except httpx.RequestError:
                return self._result("network_error_no_retry", doi=value)
            except (ValueError, UnicodeError, RecursionError):
                return self._result("invalid_json_or_response", doi=value)


diagnostics = ElsevierDiagnostics()
