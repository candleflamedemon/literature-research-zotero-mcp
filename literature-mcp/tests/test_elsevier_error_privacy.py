"""Synthetic responses only: no actual API keys or external requests."""
import json
import logging

import httpx
import pytest

import elsevier_api as e
from test_elsevier_api import DOI, FAKE, install, setup


@pytest.mark.parametrize("code,text,category", [
    ("INVALID_API_KEY", "invalid API key", "API_KEY_REJECTED_OR_MISSING"),
    ("AUTHORIZATION_ERROR", "No APIKey provided for request", "API_KEY_REJECTED_OR_MISSING"),
    ("AUTHENTICATION_ERROR", "Client IP Address does not resolve to an account", "INSTITUTIONAL_IP_MAPPING_UNRESOLVED"),
    ("AUTHENTICATION_ERROR", "Requestor configuration settings insufficient for access", "API_RESOURCE_PERMISSION_INSUFFICIENT"),
    ("AUTHORIZATION_ERROR", "not authorized to access the requested view", "API_RESOURCE_PERMISSION_INSUFFICIENT"),
    ("AUTHORIZATION_ERROR", "unrecognized or has insufficient privileges", "KEY_OR_API_PERMISSION_UNRESOLVED"),
    ("AUTHORIZATION_ERROR", "unspecified", "AUTHORIZATION_OR_ENTITLEMENT_UNRESOLVED"),
    ("AUTHENTICATION_ERROR", "unspecified", "AUTHENTICATION_UNRESOLVED"),
])
@pytest.mark.parametrize("xml", [False, True])
def test_classification_never_echoes_sensitive_text(setup, monkeypatch, caplog, code, text, category, xml):
    secret_text = text + " " + FAKE + " PRIVATE_ACCOUNT 203.0.113.99"
    if xml:
        body = f'<service-error xmlns="urn:elsevier"><status><statusCode>{code}</statusCode><statusText>{secret_text}</statusText></status></service-error>'
        response = httpx.Response(403, text=body, headers={"Content-Type": "text/xml"})
    else:
        response = httpx.Response(403, json={"service-error": {"status": {"statusCode": code, "statusText": secret_text}}})
    calls = []
    install(monkeypatch, lambda req: (calls.append(req) or response))
    caplog.set_level(logging.DEBUG)
    result = setup.check(DOI)
    assert result["error_category"] == category
    assert result["server_error_code"] == code
    assert result["status"] == "UNKNOWN" and not result["downloaded_fulltext"]
    assert result["api_authentication"] == "NOT_ESTABLISHED"
    output = json.dumps(result) + caplog.text
    for secret in (FAKE, "PRIVATE_ACCOUNT", "203.0.113.99", secret_text):
        assert secret not in output
    setup.check(DOI)
    assert len(calls) == 1


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("mime", ["text/html", "application/pdf"])
def test_denied_html_and_pdf_never_read(setup, monkeypatch, status, mime):
    class Forbidden(httpx.SyncByteStream):
        def __iter__(self):
            pytest.fail("Never read HTML/PDF denial body")
    install(monkeypatch, lambda req: httpx.Response(status, headers={"Content-Type": mime}, stream=Forbidden()))
    result = setup.check(DOI)
    assert result["error_parse_state"] == "NOT_READ"
    assert result["error_category"] == "UNDETERMINED"
    if mime == "text/html":
        assert setup.status()["manual_review_required"]


@pytest.mark.parametrize("declared", [True, False])
def test_error_size_limit(setup, monkeypatch, declared):
    headers = {"Content-Type": "application/json"}
    if declared:
        headers["Content-Length"] = str(e.MAX_RESPONSE_BYTES + 1)
    class Oversized(httpx.SyncByteStream):
        def __iter__(self):
            if declared:
                pytest.fail("Oversized declared body must not be read")
            yield b"x" * (e.MAX_RESPONSE_BYTES + 1)
    install(monkeypatch, lambda req: httpx.Response(403, headers=headers, stream=Oversized()))
    assert setup.check(DOI)["error_parse_state"] == "TOO_LARGE"


def test_xml_entities_rejected(setup, monkeypatch):
    body = '<!DOCTYPE service-error [<!ENTITY secret SYSTEM "file:///private">]><service-error><statusText>&secret;</statusText></service-error>'
    install(monkeypatch, lambda req: httpx.Response(403, text=body, headers={"Content-Type": "text/xml"}))
    assert setup.check(DOI)["error_parse_state"] == "UNSAFE_XML_REJECTED"


def test_unknown_error_code_not_echoed(setup, monkeypatch):
    install(monkeypatch, lambda req: httpx.Response(403, json={"service-error": {"status": {"statusCode": FAKE, "statusText": FAKE}}}))
    result = setup.check(DOI)
    assert "server_error_code" not in result and FAKE not in json.dumps(result)


@pytest.mark.parametrize("state", ["ENTITLED", "NOT_ENTITLED"])
def test_validation_evidence_tracks_current_key(setup, monkeypatch, state):
    install(monkeypatch, lambda req: httpx.Response(200, json={"document-entitlement": {"status": state}}))
    assert not setup.status()["api_key_validated"]
    setup.check(DOI)
    assert setup.status()["api_key_validated"]
    monkeypatch.setenv(e.KEY_ENV, "different-unit-test-placeholder")
    assert not setup.status()["api_key_validated"]
    assert setup.status()["api_key_validation_state"] == "NOT_ESTABLISHED"


def test_incorrect_fulltext_error_schema_not_returned(setup, monkeypatch):
    install(monkeypatch, lambda req: httpx.Response(403, json={"full-text-retrieval-response": {"originalText": FAKE}}))
    result = setup.check(DOI)
    assert result["error_parse_state"] == "UNKNOWN_SCHEMA"
    assert FAKE not in json.dumps(result)
