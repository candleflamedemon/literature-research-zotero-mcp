from fulltext_resolver import Resolver
def test_metadata_respects_persisted_source_stop(tmp_path, monkeypatch):
    import httpx
    resolver=Resolver(tmp_path)
    resolver.blocked["api.openalex.org"]="http_429"
    def forbidden(*args, **kwargs):
        raise AssertionError("A stopped source must not receive HTTP requests")
    monkeypatch.setattr(httpx,"Client",forbidden)
    payload,error=resolver._metadata("https://api.openalex.org/works/test")
    assert payload is None and error=="source_stopped_http_429"

