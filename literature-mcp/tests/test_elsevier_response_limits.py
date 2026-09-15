import httpx

from test_elsevier_api import DOI, FAKE, install, setup
import elsevier_api as e


def test_streamed_json_size_limit_without_content_length(setup, monkeypatch):
    class Oversized(httpx.SyncByteStream):
        def __iter__(self):
            yield b"{" * 100
            yield b"x" * e.MAX_RESPONSE_BYTES
    install(monkeypatch, lambda req: httpx.Response(200, headers={"Content-Type": "application/json"}, stream=Oversized()))
    assert setup.check(DOI)["code"] == "response_too_large"


def test_invalid_json_does_not_echo_body(setup, monkeypatch):
    install(monkeypatch, lambda req: httpx.Response(200, headers={"Content-Type": "application/json"}, content=FAKE.encode()))
    result = setup.check(DOI)
    assert result["code"] == "invalid_json_or_response"
    assert FAKE not in str(result)


def test_zero_remaining_prevents_next_request(setup, monkeypatch):
    calls = []
    install(monkeypatch, lambda req: (calls.append(req) or httpx.Response(200, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(e.time.time()) + 120)}, json={"document-entitlement": {"status": "ENTITLED"}})))
    assert setup.check(DOI)["status"] == "AVAILABLE"
    assert setup.check("10.1016/j.still.2026.107173")["code"] == "cooldown_active"
    assert len(calls) == 1
