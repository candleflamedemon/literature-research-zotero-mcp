"""No real keys, no external requests. Exercise privacy, gating and policy."""
import json
import logging

import httpx
import pytest

import elsevier_api as e

DOI = "10.1016/j.still.2022.105374"
FAKE = "unit-test-placeholder-not-a-real-key"

@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv(e.KEY_ENV, FAKE)
    monkeypatch.setenv(e.APPROVAL_ENV, "yes")
    monkeypatch.setattr(e, "MIN_INTERVAL_SECONDS", 0)
    return e.ElsevierDiagnostics()

def install(monkeypatch, response_fn):
    original = httpx.Client
    captured = []
    def factory(**options):
        captured.append(options.copy())
        return original(**options, transport=httpx.MockTransport(response_fn))
    monkeypatch.setattr(e.httpx, "Client", factory)
    return captured

def test_status_never_connects_or_reveals_key(setup, monkeypatch):
    monkeypatch.setattr(e.httpx, "Client", lambda **kw: pytest.fail("No HTTP"))
    r = setup.status()
    assert r["api_key_configured"] and not r["api_key_validated"]
    assert FAKE not in json.dumps(r)

@pytest.mark.parametrize("bad", ["wrong", "10.1016/../x", "10.1016/a,b", "10.1016/x?token=foo", "10.1016/x#fragment"])
def test_invalid_doi_no_network(setup, monkeypatch, bad):
    monkeypatch.setattr(e.httpx, "Client", lambda **kw: pytest.fail("No HTTP"))
    assert setup.check(bad)["code"] == "invalid_doi"

def test_approval_and_key_fail_closed(setup, monkeypatch):
    monkeypatch.setattr(e.httpx, "Client", lambda **kw: pytest.fail("No HTTP"))
    monkeypatch.delenv(e.APPROVAL_ENV)
    assert setup.check(DOI)["code"] == "use_authorization_unconfirmed"
    monkeypatch.setenv(e.APPROVAL_ENV, "yes")
    monkeypatch.delenv(e.KEY_ENV)
    assert setup.check(DOI)["code"] == "api_key_not_configured_or_unsafe"
    monkeypatch.setenv(e.KEY_ENV, "unsafe\nheader")
    assert setup.check(DOI)["code"] == "api_key_not_configured_or_unsafe"

@pytest.mark.parametrize("state,expected", [("ENTITLED", "AVAILABLE"), ("NOT_ENTITLED", "NOT_AVAILABLE"), ("unexpected", "UNKNOWN")])
def test_fixed_get_entitled_and_sanitized_response(setup, monkeypatch, caplog, state, expected):
    def handler(req):
        assert req.method == "GET" and req.url.host == "api.elsevier.com"
        assert req.url.params == httpx.QueryParams(view="ENTITLED")
        assert req.headers["X-ELS-APIKey"] == FAKE
        assert req.headers["Accept"] == "application/json" and "cookie" not in req.headers
        assert FAKE not in str(req.url)
        return httpx.Response(200, json={"document-entitlement": {"status": state, "account": "PRIVATE_ACCOUNT", "token": FAKE}})
    capture = install(monkeypatch, handler)
    caplog.set_level(logging.DEBUG)
    r = setup.check("https://doi.org/" + DOI)
    assert r["status"] == expected
    assert capture[0]["trust_env"] is False and capture[0]["follow_redirects"] is False
    assert not r["downloaded_pdf"] and not r["fulltext_download_authorized"]
    assert FAKE not in json.dumps(r) + caplog.text
    assert "PRIVATE_ACCOUNT" not in json.dumps(r)

@pytest.mark.parametrize("status,code", [(401, "authentication_failed"), (403, "authorization_or_entitlement_denied"), (404, "not_found"), (302, "redirect_not_followed"), (500, "api_http_error")])
def test_errors_do_not_echo_server_body_or_retry(setup, monkeypatch, status, code):
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(status, content=(FAKE + " PRIVATE_IP PRIVATE_ACCOUNT").encode(), headers={"Location": "https://www.sciencedirect.com/"})
    install(monkeypatch, handler)
    r = setup.check(DOI)
    assert r["code"] == code and len(calls) == 1
    assert FAKE not in json.dumps(r) and "PRIVATE_IP" not in json.dumps(r)
    if status in (401, 403):
        setup.check(DOI)
        assert len(calls) == 1

def test_rate_limit_cooldown_and_reset(setup, monkeypatch):
    calls = []
    install(monkeypatch, lambda req: (calls.append(req) or httpx.Response(429, headers={"Retry-After": "120", "X-RateLimit-Reset": str(int(e.time.time()) + 200)}, content=FAKE.encode())))
    r = setup.check(DOI)
    assert r["retry_after_seconds"] >= 199
    assert setup.check("10.1016/j.still.2026.107173")["code"] == "cooldown_active"
    assert len(calls) == 1

def test_retry_after_http_date():
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone
    h = httpx.Headers({"Retry-After": format_datetime(datetime.now(timezone.utc) + timedelta(seconds=90), usegmt=True)})
    assert 89 <= e._retry_after(h) <= 91
    assert e._retry_after(httpx.Headers({"Retry-After": "secret-value"})) == 3600

@pytest.mark.parametrize("mime", ["application/pdf", "text/html"])
def test_unexpected_mime_body_is_not_read(setup, monkeypatch, mime):
    class ForbiddenBody(httpx.SyncByteStream):
        def __iter__(self):
            pytest.fail("Unexpected/PDF body must never be consumed")
    install(monkeypatch, lambda req: httpx.Response(200, headers={"Content-Type": mime}, stream=ForbiddenBody()))
    assert setup.check(DOI)["code"] == "unexpected_content_type_not_read"
    assert setup.check(DOI)["code"] == "browser_or_support_review_required"

def test_body_size_bound(setup, monkeypatch):
    install(monkeypatch, lambda req: httpx.Response(200, headers={"Content-Type": "application/json"}, content=b"x" * (e.MAX_RESPONSE_BYTES + 1)))
    assert setup.check(DOI)["code"] == "response_too_large"

def test_session_bound(setup, monkeypatch):
    calls = []
    install(monkeypatch, lambda req: (calls.append(req) or httpx.Response(200, json={"full-text-retrieval-response": {"originalText": FAKE}})))
    for _ in range(3):
        assert setup.check(DOI)["code"] == "unrecognized_entitlement_response"
    assert setup.check(DOI)["code"] == "diagnostic_session_limit"
    assert len(calls) == 3

@pytest.mark.parametrize("exc,code", [(httpx.ConnectTimeout, "timeout_no_retry"), (httpx.ConnectError, "network_error_no_retry")])
def test_exceptions_do_not_leak(setup, monkeypatch, exc, code):
    def handler(req):
        raise exc(FAKE + " PRIVATE_IP", request=req)
    install(monkeypatch, handler)
    r = setup.check(DOI)
    assert r["code"] == code and FAKE not in json.dumps(r)
