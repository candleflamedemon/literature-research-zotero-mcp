"""Reuse diagnostic fakes to check policy guard on every redirect."""
import network_access as net
from test_network_access import _Client, _Response


def test_guard_runs_before_each_institution_request(monkeypatch):
    capture = {}
    guarded = []
    responses = [_Response(302, headers={"Location": "https://publisher.example/a.pdf"}), _Response(200, headers={"Content-Type": "application/pdf"}, forbid_body=True)]
    monkeypatch.setattr(net, "_is_public_host", lambda *a: True)
    monkeypatch.setattr(net.httpx, "Client", lambda **k: _Client(responses, capture, **k))
    result = net.check_institution_access_target(publisher_url="https://doi.org/10.1000/test", request_guard=lambda client, url: guarded.append(url))
    assert result["status"] == "AVAILABLE"
    assert guarded == [u for _,u in capture["requests"]]
    assert len(guarded) == 2


def test_plain_pdf_button_does_not_prove_subscription(monkeypatch):
    capture = {}
    responses = [_Response(200, headers={"Content-Type": "text/html"}, chunks=[b"<a>Download PDF</a>"])]
    monkeypatch.setattr(net, "_is_public_host", lambda *a: True)
    monkeypatch.setattr(net.httpx, "Client", lambda **k: _Client(responses, capture, **k))
    assert net.check_institution_access_target(publisher_url="https://publisher.example/a")["status"] == "UNKNOWN"
