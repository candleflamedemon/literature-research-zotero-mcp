"""Network-isolated, read-only institutional access diagnostics."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
from typing import Any, Callable
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import httpx


INSTITUTIONAL_TRUST_ENV = False
INSTITUTIONAL_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
MAX_REDIRECTS = 5
MAX_HTML_BYTES = 64 * 1024
PROXY_ENV_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
TUN_KEYWORDS = re.compile(
    r"(?:\btun\b|\btap\b|wintun|wireguard|\bvpn\b|tailscale|zerotier|"
    r"cloudflare warp|clash|sing-box|v2ray|openvpn)",
    re.IGNORECASE,
)
IGNORED_WINDOWS_TUNNELS = (
    "wan miniport",
    "teredo tunneling",
    "6to4 adapter",
    "ip-https platform",
    "wi-fi direct virtual",
)
LOGIN_MARKERS = (
    "institutional login",
    "institutional sign in",
    "sign in through your institution",
    "sign in via your institution",
    "log in through your institution",
    "access through your institution",
    "openathens",
    "shibboleth",
    "ezproxy",
    "single sign-on",
    "single sign on",
    "sso login",
)
ACCESS_MARKERS = (
    "access provided by",
    "you have full access",
    "you have access",
)
PAYWALL_MARKERS = (
    "purchase this article",
    "rent this article",
    "buy this article",
    "subscribe to read",
    "get access to this article",
    "access denied",
    "you do not have access",
)
LOGIN_URL_MARKERS = re.compile(
    r"(?:/login|/signin|/sign-in|/sso|shibboleth|openathens|ezproxy|institutional-login)",
    re.IGNORECASE,
)


def proxy_environment_summary() -> dict[str, Any]:
    """Report proxy-variable presence without exposing any values."""

    present = sorted({name.upper() for name in PROXY_ENV_NAMES if os.environ.get(name)})
    return {
        "variables_present": present,
        "values_redacted": True,
        "institutional_client_trust_env": INSTITUTIONAL_TRUST_ENV,
        "institutional_client_inherits_environment_proxy": False,
    }


def _read_windows_adapters() -> list[dict[str, Any]] | None:
    executable = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
    if not executable:
        return None
    command = (
        "$ProgressPreference='SilentlyContinue'; "
        "Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue | "
        "Select-Object Name,InterfaceDescription,Status | ConvertTo-Json -Compress"
    )
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    try:
        payload = json.loads(completed.stdout)
    except ValueError:
        return None
    if isinstance(payload, dict):
        return [payload]
    return payload if isinstance(payload, list) else None


def detect_vpn_tun_interfaces() -> dict[str, Any]:
    """Detect possible VPN/TUN adapters without changing interfaces or routes."""

    adapters = _read_windows_adapters()
    if adapters is None:
        try:
            names = [name for _, name in socket.if_nameindex()]
        except OSError:
            return {
                "detection": "UNKNOWN",
                "active_candidate_detected": None,
                "installed_candidate_detected": None,
                "active_candidate_count": None,
                "inactive_candidate_count": None,
                "note": "无法读取网络接口；trust_env=False 仍不能证明已绕过 TUN。",
            }
        candidates = [name for name in names if TUN_KEYWORDS.search(name)]
        return {
            "detection": "POSSIBLE" if candidates else "NONE_DETECTED",
            "active_candidate_detected": None,
            "installed_candidate_detected": bool(candidates),
            "active_candidate_count": None,
            "inactive_candidate_count": len(candidates),
            "note": "回退检测无法可靠判断接口是否已连接。",
        }

    active_count = 0
    inactive_count = 0
    for adapter in adapters:
        if not isinstance(adapter, dict):
            continue
        text = " ".join(
            str(adapter.get(field) or "") for field in ("Name", "InterfaceDescription")
        )
        lowered = text.lower()
        if any(marker in lowered for marker in IGNORED_WINDOWS_TUNNELS):
            continue
        if not TUN_KEYWORDS.search(text):
            continue
        if str(adapter.get("Status") or "").lower() == "up":
            active_count += 1
        else:
            inactive_count += 1

    installed = active_count + inactive_count > 0
    return {
        "detection": "POSSIBLE" if installed else "NONE_DETECTED",
        "active_candidate_detected": active_count > 0,
        "installed_candidate_detected": installed,
        "active_candidate_count": active_count,
        "inactive_candidate_count": inactive_count,
        "note": (
            "检测仅基于接口名称、描述和当前状态；透明代理、WFP 或未暴露接口的 TUN 仍可能存在。"
        ),
    }


def _access_error(code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "UNKNOWN",
        "error": {"code": code, "message": message},
    }


def _is_public_host(host: str, port: int) -> bool:
    if host.lower() == "localhost":
        return False
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    if not addresses:
        return False
    for entry in addresses:
        address = entry[4][0].split("%", 1)[0]
        try:
            if not ipaddress.ip_address(address).is_global:
                return False
        except ValueError:
            return False
    return True


def _sanitise_public_url(value: str) -> tuple[str | None, str | None, bool]:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except (TypeError, ValueError):
        return None, None, False
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None, None, False
    if parsed.username or parsed.password:
        return None, None, False
    effective_port = port or (443 if parsed.scheme.lower() == "https" else 80)
    if effective_port not in {80, 443}:
        return None, None, False
    if not _is_public_host(parsed.hostname, effective_port):
        return None, None, False
    netloc = parsed.hostname
    if port is not None:
        netloc = f"{netloc}:{port}"
    sanitised = urlunsplit(
        (parsed.scheme.lower(), netloc, parsed.path or "/", "", "")
    )
    return sanitised, parsed.hostname, bool(parsed.query or parsed.fragment)


def _target_from_input(
    doi: str | None, publisher_url: str | None
) -> tuple[str | None, str | None, bool, dict[str, Any] | None]:
    if bool(doi) == bool(publisher_url):
        return None, None, False, _access_error(
            "invalid_input", "请只提供 test_doi 或 publisher_url 其中一个。"
        )
    if doi:
        normalised = re.sub(
            r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", doi.strip(), flags=re.I
        )
        if not re.fullmatch(r"10\.\d{4,9}/\S+", normalised):
            return None, None, False, _access_error("invalid_doi", "测试 DOI 格式无效。")
        target = f"https://doi.org/{quote(normalised, safe='/')}"
        return target, "doi.org", False, None
    sanitised, host, stripped = _sanitise_public_url(publisher_url or "")
    if not sanitised:
        return None, None, False, _access_error(
            "unsafe_or_invalid_url",
            "出版社 URL 必须是使用 80/443 端口的公网 HTTP(S) 地址，且不能包含账号信息。",
        )
    return sanitised, host, stripped, None


def _read_limited_html(response: httpx.Response) -> str:
    content = bytearray()
    for chunk in response.iter_bytes():
        remaining = MAX_HTML_BYTES - len(content)
        if remaining <= 0:
            break
        content.extend(chunk[:remaining])
        if len(content) >= MAX_HTML_BYTES:
            break
    encoding = response.encoding or "utf-8"
    try:
        return bytes(content).decode(encoding, errors="replace").lower()
    except LookupError:
        return bytes(content).decode("utf-8", errors="replace").lower()


def _result(
    status: str,
    reason: str,
    *,
    tested_host: str,
    final_host: str,
    http_status: int,
    content_type: str,
    redirect_count: int,
    input_query_removed: bool,
) -> dict[str, Any]:
    return {
        "ok": True,
        "status": status,
        "reason": reason,
        "tested_host": tested_host,
        "final_host": final_host,
        "http_status": http_status,
        "content_type": content_type.split(";", 1)[0] or None,
        "redirect_count": redirect_count,
        "input_query_or_fragment_removed": input_query_removed,
        "trust_env": INSTITUTIONAL_TRUST_ENV,
        "used_browser_cookies": False,
        "downloaded_pdf": False,
    }


def check_institution_access_target(
    test_doi: str | None = None, publisher_url: str | None = None,
    *, request_guard: Callable[[httpx.Client, str], None] | None = None,
) -> dict[str, Any]:
    """Conservatively inspect public publisher access without authentication or PDF reads."""

    current, tested_host, stripped, error = _target_from_input(test_doi, publisher_url)
    if error:
        return error
    assert current is not None and tested_host is not None

    try:
        with httpx.Client(
            trust_env=INSTITUTIONAL_TRUST_ENV,
            timeout=INSTITUTIONAL_TIMEOUT,
            follow_redirects=False,
            headers={
                "User-Agent": "LiteratureMCP/0.4 (institutional access diagnostic; no authentication)",
                "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.5,*/*;q=0.1",
            },
        ) as client:
            for redirect_count in range(MAX_REDIRECTS + 1):
                sanitised, final_host, _ = _sanitise_public_url(current)
                if not sanitised or not final_host:
                    return _access_error(
                        "unsafe_redirect",
                        "检测到非公网、非标准端口或格式异常的跳转，已停止访问。",
                    )
                current = sanitised
                if request_guard:
                    request_guard(client, current)
                client.cookies.clear()
                with client.stream("GET", current) as response:
                    status_code = response.status_code
                    content_type = response.headers.get("Content-Type", "").lower()
                    location = response.headers.get("Location")

                    if status_code in {301, 302, 303, 307, 308} and location:
                        next_url = urljoin(current, location)
                        if LOGIN_URL_MARKERS.search(next_url):
                            return _result(
                                "LOGIN_REQUIRED",
                                "页面跳转到机构登录、SSO 或代理登录入口。",
                                tested_host=tested_host,
                                final_host=final_host,
                                http_status=status_code,
                                content_type=content_type,
                                redirect_count=redirect_count,
                                input_query_removed=stripped,
                            )
                        if redirect_count >= MAX_REDIRECTS:
                            return _result(
                                "UNKNOWN",
                                "重定向次数超过安全上限。",
                                tested_host=tested_host,
                                final_host=final_host,
                                http_status=status_code,
                                content_type=content_type,
                                redirect_count=redirect_count,
                                input_query_removed=stripped,
                            )
                        current = next_url
                        continue

                    if status_code in {403, 429}:
                        return {**_result("UNKNOWN", "来源拒绝自动化访问，立即停止该来源。", tested_host=tested_host, final_host=final_host, http_status=status_code, content_type=content_type, redirect_count=redirect_count, input_query_removed=stripped), "automation_blocked": True}
                    is_pdf = "application/pdf" in content_type
                    if status_code in {200, 206} and is_pdf:
                        return _result(
                            "AVAILABLE",
                            "服务器确认 PDF 资源可访问；响应正文未被读取。",
                            tested_host=tested_host,
                            final_host=final_host,
                            http_status=status_code,
                            content_type=content_type,
                            redirect_count=redirect_count,
                            input_query_removed=stripped,
                        )

                    body = ""
                    if "html" in content_type or "text/plain" in content_type:
                        body = _read_limited_html(response)
                    has_access = any(marker in body for marker in ACCESS_MARKERS)
                    needs_login = any(marker in body for marker in LOGIN_MARKERS)
                    has_paywall = any(marker in body for marker in PAYWALL_MARKERS)
                    if any(marker in body for marker in ("captcha", "verify you are human", "cf-chl-", "automated access is prohibited", "automated requests are not allowed")):
                        return {**_result("UNKNOWN", "来源要求人工验证或禁止自动化，立即停止。", tested_host=tested_host, final_host=final_host, http_status=status_code, content_type=content_type, redirect_count=redirect_count, input_query_removed=stripped), "automation_blocked": True}

                    if status_code == 401 or needs_login:
                        return _result(
                            "LOGIN_REQUIRED",
                            "页面要求登录、机构认证、SSO、OpenAthens、Shibboleth 或 EZproxy。",
                            tested_host=tested_host,
                            final_host=final_host,
                            http_status=status_code,
                            content_type=content_type,
                            redirect_count=redirect_count,
                            input_query_removed=stripped,
                        )
                    if status_code in {200, 206} and has_access and not has_paywall:
                        return _result(
                            "AVAILABLE",
                            "页面包含明确的全文或 PDF 访问标志。",
                            tested_host=tested_host,
                            final_host=final_host,
                            http_status=status_code,
                            content_type=content_type,
                            redirect_count=redirect_count,
                            input_query_removed=stripped,
                        )
                    if has_paywall:
                        return _result(
                            "NOT_AVAILABLE",
                            "页面明确显示购买、订阅或无权访问提示。",
                            tested_host=tested_host,
                            final_host=final_host,
                            http_status=status_code,
                            content_type=content_type,
                            redirect_count=redirect_count,
                            input_query_removed=stripped,
                        )
                    return _result(
                        "UNKNOWN",
                        "未获得足以判断机构订阅权限的明确信号。",
                        tested_host=tested_host,
                        final_host=final_host,
                        http_status=status_code,
                        content_type=content_type,
                        redirect_count=redirect_count,
                        input_query_removed=stripped,
                    )
    except httpx.TimeoutException:
        return _access_error("timeout", "机构访问检测超时，无法判断当前权限。")
    except httpx.RequestError:
        return _access_error("network_error", "机构访问检测发生网络错误，无法判断当前权限。")
