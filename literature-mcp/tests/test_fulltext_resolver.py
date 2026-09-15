"""Offline policy regression; never writes to a real Zotero instance."""
import asyncio
import hashlib
import json
import logging
from pathlib import Path

import httpx
import pytest

import fulltext_resolver as ft
import network_access
import server
import zotero_write as z

DOI = "10.1038/s41597-024-02998-7"
PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


@pytest.fixture
def resolver(tmp_path, monkeypatch):
    r = ft.Resolver(tmp_path)
    monkeypatch.setattr(network_access, "_is_public_host", lambda host, port: host not in ("localhost", "127.0.0.1", "private.example"))
    monkeypatch.setattr(r, "_wait", lambda *a, **k: (_ for _ in ()).throw(ft.StopSource(r.blocked[a[0]])) if a[0] in r.blocked else None)
    monkeypatch.setattr(r, "_existing", lambda *a: ([], None))
    return r


def client_for(r, handler, monkeypatch):
    factory = httpx.Client
    options = []
    def client():
        options.append({"trust_env": False, "follow_redirects": False})
        return factory(trust_env=False, follow_redirects=False, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(r, "_fulltext_client", client)
    return options


def test_doi_normalization_and_invalid_input(resolver):
    assert ft.doi_value("https://doi.org/10.3390/RS16213953") == "10.3390/rs16213953"
    assert resolver.find_oa("not a doi")["ok"] is False
    assert resolver.report("invalid")["ok"] is False


@pytest.mark.parametrize("url", ["http://example.com/a", "https://localhost/a", "https://private.example/a", "https://user:pw@example.com/a", "https://example.com/a?token=secret", "https://sci-hub.se/a", "https://example.com:8443/a"])
def test_unsafe_sources_rejected(resolver, url):
    with pytest.raises(ft.StopSource):
        ft.safe_url(url)


def test_metadata_oa_exact_doi_required_and_email_not_faked(resolver, monkeypatch):
    monkeypatch.delenv("UNPAYWALL_EMAIL", raising=False)
    calls = []
    def metadata(url, params=None):
        calls.append((url, params))
        return {"doi": f"https://doi.org/{DOI}", "locations": [{"is_oa": True, "pdf_url": "https://oa.example/a.pdf", "license": "cc-by", "source": {"type": "repository"}}]}, None
    monkeypatch.setattr(resolver, "_metadata", metadata)
    result = resolver.find_oa(DOI)
    assert len(calls) == 1 and "openalex" in calls[0][0]
    assert result["candidates"][0]["host_type"] == "repository"
    assert result["queries"][0]["error"] == "email_not_configured"
    monkeypatch.setattr(resolver, "_metadata", lambda *a: ({"doi": "https://doi.org/10.1000/wrong", "locations": [{"is_oa": True, "pdf_url": "https://oa.example/wrong.pdf"}]}, None))
    assert not resolver.find_oa(DOI)["candidates"]


def test_unpaywall_priority_and_redaction(resolver, monkeypatch):
    monkeypatch.setenv("UNPAYWALL_EMAIL", "contact@example.org")
    def metadata(url, params=None):
        if "unpaywall" in url:
            return {"doi": DOI, "is_oa": True, "best_oa_location": {"url_for_pdf": "https://oa.example/a.pdf"}}, None
        return {"doi": DOI, "locations": [{"is_oa": True, "pdf_url": "https://other.example/b.pdf"}]}, None
    monkeypatch.setattr(resolver, "_metadata", metadata)
    result = resolver.find_oa(DOI)
    assert result["candidates"][0]["source"] == "Unpaywall"
    assert "contact@example.org" not in json.dumps(result)


def test_existing_attachment_precedes_external_requests(resolver, monkeypatch):
    monkeypatch.setattr(resolver, "_existing", lambda *a: ([{"attachment_key": "ABCD1234"}], None))
    monkeypatch.setattr(resolver, "find_oa", lambda *a: pytest.fail("metadata must not be queried"))
    assert resolver.classify(DOI)["resolution"] == "ZOTERO_ATTACHMENT"
    assert resolver.fetch_oa(DOI)["downloaded_pdf"] is False


def test_pdf_download_robots_first_no_cookies(resolver, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /", headers={"Set-Cookie": "session=private"})
        return httpx.Response(200, content=PDF, headers={"Content-Type": "application/pdf"})
    options = client_for(resolver, handler, monkeypatch)
    path = resolver._folder() / "test.pdf"
    result = resolver._get("https://oa.example/a.pdf", download=True, destination=path)
    assert path.read_bytes() == PDF
    assert result["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert all("cookie" not in req.headers for req in calls)
    assert calls[0].url.path == "/robots.txt"
    assert options[0]["trust_env"] is False


@pytest.mark.parametrize("status", [403, 429])
def test_blocking_status_stops_source_and_survives_restart(resolver, monkeypatch, status):
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(404) if request.url.path == "/robots.txt" else httpx.Response(status)
    client_for(resolver, handler, monkeypatch)
    with pytest.raises(ft.StopSource):
        resolver._get("https://oa.example/a.pdf", download=True, destination=resolver._folder()/"x.pdf")
    before = len(calls)
    with pytest.raises(ft.StopSource):
        resolver._get("https://oa.example/b.pdf", download=True, destination=resolver._folder()/"y.pdf")
    assert len(calls) == before
    assert ft.Resolver(resolver.workspace).blocked["oa.example"] == f"http_{status}"


def test_robots_disallow_no_article_request(resolver, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, text="User-agent: *\nDisallow: /")
    client_for(resolver, handler, monkeypatch)
    with pytest.raises(ft.StopSource) as e:
        resolver._get("https://oa.example/a.pdf", download=False)
    assert e.value.code == "robots_disallowed"
    assert calls == ["/robots.txt"]


@pytest.mark.parametrize("body", ["<html>CAPTCHA verify you are human</html>", "automated access is prohibited"])
def test_automation_challenge_blocks(resolver, monkeypatch, body):
    client_for(resolver, lambda req: httpx.Response(404) if req.url.path == "/robots.txt" else httpx.Response(200, text=body, headers={"Content-Type": "text/html"}), monkeypatch)
    with pytest.raises(ft.StopSource) as e:
        resolver._get("https://oa.example/a", download=False)
    assert e.value.code == "automation_blocked"
    assert "oa.example" in resolver.blocked


def test_landing_explicit_link_and_redirect_policy(resolver, monkeypatch):
    calls = []
    def handler(req):
        calls.append((req.url.host, req.url.path))
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        if req.url.path == "/article":
            return httpx.Response(200, text='<meta name="citation_pdf_url" content="https://cdn.example/a.pdf">', headers={"Content-Type": "text/html"})
        return httpx.Response(200, content=PDF, headers={"Content-Type": "application/pdf"})
    client_for(resolver, handler, monkeypatch)
    resolver._get("https://oa.example/article", download=True, destination=resolver._folder()/"a.pdf")
    assert ("cdn.example", "/robots.txt") in calls


def test_private_redirect_and_login_not_followed(resolver, monkeypatch):
    target = ["https://private.example/a"]
    calls = []
    def handler(req):
        calls.append(req.url.host)
        return httpx.Response(404) if req.url.path == "/robots.txt" else httpx.Response(302, headers={"Location": target[0]})
    client_for(resolver, handler, monkeypatch)
    with pytest.raises(ft.StopSource) as e:
        resolver._get("https://oa.example/a", download=False)
    assert e.value.code == "unsafe_url" and "private.example" not in calls
    target[0] = "https://sso.example/login"
    with pytest.raises(ft.StopSource) as e:
        resolver._get("https://oa.example/a", download=False)
    assert e.value.code == "needs_browser_login" and "sso.example" not in calls


@pytest.mark.parametrize("content", [b"not a PDF", b""])
def test_invalid_pdf_cleanup(resolver, monkeypatch, content):
    client_for(resolver, lambda req: httpx.Response(404) if req.url.path == "/robots.txt" else httpx.Response(200, content=content, headers={"Content-Type": "application/pdf"}), monkeypatch)
    path = resolver._folder()/"bad.pdf"
    with pytest.raises(ft.StopSource):
        resolver._get("https://oa.example/a.pdf", download=True, destination=path)
    assert not path.exists() and not path.with_suffix(".part").exists()


def test_timeout_bounded_backoff(resolver, monkeypatch):
    calls = []
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        calls.append(req)
        raise httpx.ReadTimeout("timeout")
    client_for(resolver, handler, monkeypatch)
    delays = []
    monkeypatch.setattr(ft.time, "sleep", delays.append)
    with pytest.raises(ft.StopSource):
        resolver._get("https://oa.example/a.pdf", download=False)
    assert len(calls) == 3 and delays == [1, 2]


@pytest.mark.parametrize("status", ["UNKNOWN", "NOT_AVAILABLE", "LOGIN_REQUIRED"])
def test_institutional_gate_denies_download(resolver, monkeypatch, status):
    monkeypatch.setattr(resolver, "find_oa", lambda *a: {})
    monkeypatch.setattr(network_access, "check_institution_access_target", lambda **kwargs: {"status": status})
    monkeypatch.setattr(resolver, "_get", lambda *a, **k: pytest.fail("must not download"))
    result = resolver.fetch_institutional(DOI, "https://publisher.example/article")
    assert result["downloaded_pdf"] is False
    assert (result.get("queue_status") == "NEEDS_BROWSER_LOGIN") == (status == "LOGIN_REQUIRED")


def test_institutional_available_and_vpn_label(resolver, monkeypatch):
    monkeypatch.setattr(resolver, "find_oa", lambda *a: {})
    monkeypatch.setattr(network_access, "check_institution_access_target", lambda **kwargs: {"status": "AVAILABLE"})
    monkeypatch.setattr(resolver, "_get", lambda *a, **k: {"local_file": str(k["destination"]), "sha256": "test"})
    result = resolver.fetch_institutional(DOI, "https://publisher.example/article", "VPN")
    assert result["pdf_status"] == "DOWNLOADED" and result["access_type"] == "INSTITUTIONAL_VPN"


def test_queue_redacts_query_no_browser_calls(resolver):
    result = resolver.queue(DOI, "https://school.example/login?token=private")
    assert result["queue_status"] == "NEEDS_BROWSER_LOGIN"
    assert "private" not in json.dumps(result)
    assert result["pdf_status"] == "NEEDS_LOGIN"


def test_attach_requires_confirmation_and_verified_download(resolver):
    assert resolver.attach(DOI, "ABCD1234")["error"]["code"] == "confirmation_required"
    assert resolver.attach(DOI, "ABCD1234", True)["error"]["code"] == "verified_download_required"


def test_new_tools_actual_mcp_dispatch(monkeypatch, resolver):
    monkeypatch.setattr(server, "fulltext", resolver)
    result = asyncio.run(server.mcp.call_tool("acquisition_report", {}))
    assert result.is_error is False and result.structured_content["count"] == 0


def test_local_three_phase_upload(monkeypatch, tmp_path):
    z.clear_cached_authorization()
    calls = []
    secret = "z"*32
    path = tmp_path/"a.pdf"
    path.write_bytes(PDF)
    def request(method, path="", **kwargs):
        calls.append((method, path, kwargs))
        payload = None
        headers = {}
        status = 200
        if method == "GET":
            headers["Zotero-Server-ID"] = "server_test"
            payload = [] if path.endswith("children") else {"data": {"itemType": "journalArticle", "DOI": DOI}} if path.endswith("ABCD1234") else {} if path else None
        elif path == "local/authorize":
            payload = {"key": secret, "remember": True}
        elif path == "users/0/items":
            payload = {"successful": {"0": {"key": "EFGH5678"}}}
        elif path == "users/0/items/EFGH5678/file" and kwargs.get("form_body", {}).get("md5"):
            payload = {"url": "http://localhost:23119/api/local/uploads/upload_test", "uploadKey": "upload_test", "prefix": "", "suffix": "", "contentType": "application/octet-stream"}
        elif path == "local/uploads/upload_test":
            status = 201
            assert kwargs["raw_content"] == PDF
        elif path == "users/0/items/EFGH5678/file":
            status = 204
        else:
            pytest.fail(path)
        return httpx.Response(status, json=payload, headers=headers, request=httpx.Request(method, "http://localhost:23119/api/"+path)), None
    monkeypatch.setattr(z, "_request", request)
    result = z.attach_new_pdf(DOI, "ABCD1234", path, PDF)
    assert result["uploaded_locally"] is True
    assert secret not in json.dumps(result)
    writes = [(p, k) for m,p,k in calls if m == "POST" and p != "local/authorize"]
    assert len(writes) == 4
    for endpoint, kwargs in writes:
        assert kwargs["headers"]["Zotero-Server-ID"] == "server_test"
        assert kwargs["headers"]["Zotero-API-Key"] == secret
        if endpoint.endswith("file"):
            assert kwargs["headers"]["If-None-Match"] == "*"


def test_local_upload_rejects_external_endpoint(monkeypatch, tmp_path):
    path = tmp_path/"a.pdf"
    path.write_bytes(PDF)
    monkeypatch.setattr(z, "_server_identity", lambda: ("server_test", None))
    monkeypatch.setattr(z, "_read_json", lambda p, *a, **k: ([] if p.endswith("children") else {"data": {"DOI": DOI, "itemType": "journalArticle"}} if p.endswith("ABCD1234") else {}, None))
    monkeypatch.setattr(z, "_authorized_write", lambda *a, **k: (httpx.Response(200, json={"successful": {"0": {"key": "EFGH5678"}}}), None))
    calls = []
    def upload(*a, **k):
        calls.append(a)
        return httpx.Response(200, json={"url": "https://external.example/api/local/uploads/upload_test", "uploadKey": "upload_test"}), None
    monkeypatch.setattr(z, "_authorized_file_post", upload)
    result = z.attach_new_pdf(DOI, "ABCD1234", path, PDF)
    assert result["error"]["code"] == "unsafe_upload_url" and len(calls) == 1


def test_acquisition_history_restores_records(resolver):
    resolver._record(DOI, access_type="OA", pdf_status="FOUND")
    assert ft.Resolver(resolver.workspace).report()["records"][0]["DOI"] == DOI


def test_concurrent_log_suppression_is_reference_counted():
    logger = logging.getLogger("httpx")
    previous = logger.disabled
    with z._suppress_sensitive_http_logs(True):
        with z._suppress_sensitive_http_logs(True):
            assert logger.disabled
        assert logger.disabled
    assert logger.disabled == previous


def test_attachment_dedup_never_authorizes(monkeypatch, tmp_path):
    monkeypatch.setattr(z, "_server_identity", lambda: ("server_test", None))
    def read(p, *a, **k):
        if p.endswith("children"):
            return [{"key": "EFGH5678", "data": {"itemType": "attachment", "md5": hashlib.md5(PDF).hexdigest()}}], None
        return {"data": {"DOI": DOI, "itemType": "journalArticle"}}, None
    monkeypatch.setattr(z, "_read_json", read)
    monkeypatch.setattr(z, "_authorized_write", lambda *a, **k: pytest.fail("duplicate must not write"))
    assert z.attach_new_pdf(DOI, "ABCD1234", tmp_path/"x.pdf", PDF)["duplicate_found"]


def test_attachment_parent_mismatch_never_writes(monkeypatch, tmp_path):
    monkeypatch.setattr(z, "_server_identity", lambda: ("server_test", None))
    monkeypatch.setattr(z, "_read_json", lambda *a, **k: ({"data": {"DOI": "10.1000/wrong", "itemType": "journalArticle"}}, None))
    monkeypatch.setattr(z, "_authorized_write", lambda *a, **k: pytest.fail("must not write"))
    assert z.attach_new_pdf(DOI, "ABCD1234", tmp_path/"x.pdf", PDF)["error"]["code"] == "parent_doi_mismatch"


def test_file_post_401_reauthorizes_with_both_headers(monkeypatch):
    z.clear_cached_authorization()
    secrets = iter(["a"*32, "b"*32])
    writes = []
    def request(method, path="", **kwargs):
        payload = None
        headers = {}
        status = 200
        if method == "GET":
            headers["Zotero-Server-ID"] = "server_test"
        elif path == "local/authorize":
            payload = {"key": next(secrets), "remember": True}
        else:
            writes.append(kwargs)
            status = 401 if len(writes) == 1 else 201
        return httpx.Response(status, json=payload, headers=headers), None
    monkeypatch.setattr(z, "_request", request)
    result, error = z._authorized_file_post("local/uploads/upload_test", server_id="server_test", content=PDF)
    assert error is None and result.status_code == 201
    assert [w["headers"]["Zotero-API-Key"] for w in writes] == ["a"*32, "b"*32]
    assert all(w["headers"]["Zotero-Server-ID"] == "server_test" for w in writes)


def test_instance_change_prevents_creation(monkeypatch):
    monkeypatch.setattr(z, "_server_identity", lambda: ("new_server", None))
    monkeypatch.setattr(z, "_write_once", lambda *a, **k: pytest.fail("wrong instance must not write"))
    result, error = z._authorized_write("POST", "users/0/items", [], expected_server_id="old_server")
    assert result is None and error["error"]["code"] == "server_changed"
