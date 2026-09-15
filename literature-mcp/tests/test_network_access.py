"""网络隔离和机构访问保守判定测试。"""

import httpx

import network_access


class _Cookies:
    def __init__(self):
        self.clear_calls = 0

    def clear(self):
        self.clear_calls += 1


class _Response:
    def __init__(self, status_code=200, headers=None, chunks=None, forbid_body=False):
        self.status_code = status_code
        self.headers = headers or {}
        self.encoding = "utf-8"
        self._chunks = chunks or []
        self._forbid_body = forbid_body

    def iter_bytes(self):
        if self._forbid_body:
            raise AssertionError("PDF body must not be read")
        yield from self._chunks


class _Stream:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self.response

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class _Client:
    def __init__(self, responses, capture, **kwargs):
        self.responses = iter(responses)
        self.capture = capture
        self.capture["options"] = kwargs
        self.capture["requests"] = []
        self.cookies = _Cookies()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def stream(self, method, url):
        self.capture["requests"].append((method, url))
        return _Stream(next(self.responses))


def test_proxy_environment_is_redacted(monkeypatch) -> None:
    for name in network_access.PROXY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://user:password@private-proxy.invalid:1234")

    result = network_access.proxy_environment_summary()

    assert result["variables_present"] == ["HTTPS_PROXY"]
    assert result["values_redacted"] is True
    assert result["institutional_client_inherits_environment_proxy"] is False
    assert "password" not in str(result)
    assert "private-proxy" not in str(result)


def test_tun_detection_ignores_windows_miniports_but_reports_vpn(monkeypatch) -> None:
    monkeypatch.setattr(
        network_access,
        "_read_windows_adapters",
        lambda: [
            {"Name": "Local", "InterfaceDescription": "WAN Miniport (IP)", "Status": "Up"},
            {"Name": "Example VPN", "InterfaceDescription": "TAP-Windows Adapter", "Status": "Disconnected"},
        ],
    )

    result = network_access.detect_vpn_tun_interfaces()

    assert result["active_candidate_detected"] is False
    assert result["installed_candidate_detected"] is True
    assert result["inactive_candidate_count"] == 1
    assert "Example VPN" not in str(result)


def test_active_wintun_is_reported(monkeypatch) -> None:
    monkeypatch.setattr(
        network_access,
        "_read_windows_adapters",
        lambda: [{"Name": "Adapter", "InterfaceDescription": "Wintun Userspace Tunnel", "Status": "Up"}],
    )
    result = network_access.detect_vpn_tun_interfaces()
    assert result["active_candidate_detected"] is True
    assert result["active_candidate_count"] == 1


def test_private_or_local_target_is_rejected() -> None:
    result = network_access.check_institution_access_target(
        publisher_url="http://localhost:23119/api/"
    )
    assert result["status"] == "UNKNOWN"
    assert result["error"]["code"] == "unsafe_or_invalid_url"


def test_pdf_availability_reads_headers_only_and_disables_env_proxy(monkeypatch) -> None:
    capture = {}
    response = _Response(
        200,
        headers={"Content-Type": "application/pdf"},
        forbid_body=True,
    )
    monkeypatch.setattr(network_access, "_is_public_host", lambda host, port: True)
    monkeypatch.setattr(
        network_access.httpx,
        "Client",
        lambda **kwargs: _Client([response], capture, **kwargs),
    )

    result = network_access.check_institution_access_target(
        publisher_url="https://publisher.example/article.pdf?token=not-forwarded"
    )

    assert result["status"] == "AVAILABLE"
    assert result["downloaded_pdf"] is False
    assert result["input_query_or_fragment_removed"] is True
    assert capture["options"]["trust_env"] is False
    assert capture["options"]["follow_redirects"] is False
    assert capture["requests"] == [("GET", "https://publisher.example/article.pdf")]


def test_login_page_is_login_required(monkeypatch) -> None:
    capture = {}
    response = _Response(
        200,
        headers={"Content-Type": "text/html; charset=utf-8"},
        chunks=[b"<html>Sign in through your institution</html>"],
    )
    monkeypatch.setattr(network_access, "_is_public_host", lambda host, port: True)
    monkeypatch.setattr(
        network_access.httpx,
        "Client",
        lambda **kwargs: _Client([response], capture, **kwargs),
    )
    result = network_access.check_institution_access_target(
        publisher_url="https://publisher.example/article"
    )
    assert result["status"] == "LOGIN_REQUIRED"
    assert result["used_browser_cookies"] is False


def test_explicit_paywall_is_not_available(monkeypatch) -> None:
    capture = {}
    response = _Response(
        200,
        headers={"Content-Type": "text/html"},
        chunks=[b"<html>Purchase this article</html>"],
    )
    monkeypatch.setattr(network_access, "_is_public_host", lambda host, port: True)
    monkeypatch.setattr(
        network_access.httpx,
        "Client",
        lambda **kwargs: _Client([response], capture, **kwargs),
    )
    result = network_access.check_institution_access_target(
        publisher_url="https://publisher.example/article"
    )
    assert result["status"] == "NOT_AVAILABLE"


def test_ambiguous_page_is_unknown(monkeypatch) -> None:
    capture = {}
    response = _Response(
        200,
        headers={"Content-Type": "text/html"},
        chunks=[b"<html>Article abstract only</html>"],
    )
    monkeypatch.setattr(network_access, "_is_public_host", lambda host, port: True)
    monkeypatch.setattr(
        network_access.httpx,
        "Client",
        lambda **kwargs: _Client([response], capture, **kwargs),
    )
    result = network_access.check_institution_access_target(
        publisher_url="https://publisher.example/article"
    )
    assert result["status"] == "UNKNOWN"
