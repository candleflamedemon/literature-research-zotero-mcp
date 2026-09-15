import zotero_write as z
def test_local_template_404_uses_local_schema(monkeypatch):
    calls=[]
    def read(path, params=None):
        calls.append(path)
        if path=="items/new":
            return None,z._error("not_found","missing",status_code=404)
        assert path=="schema"
        return {"itemTypes":[{"itemType":"journalArticle","fields":[{"field":"title"},{"field":"DOI"}],"creatorTypes":[{"creatorType":"author"}]}]},None
    monkeypatch.setattr(z,"_read_json",read)
    template,error=z._template("journalArticle")
    assert error is None
    assert template["title"]=="" and template["DOI"]==""
    assert template["creators"]==[] and template["itemType"]=="journalArticle"
    assert calls==["items/new","schema"]
def test_template_does_not_fallback_on_authorization_error(monkeypatch):
    calls=[]
    def read(path,params=None):
        calls.append(path)
        return None,z._error("authorization_required","denied",status_code=401)
    monkeypatch.setattr(z,"_read_json",read)
    template,error=z._template("journalArticle")
    assert template is None and error["error"]["status_code"]==401
    assert calls==["items/new"]
def test_schema_missing_type_fails_closed(monkeypatch):
    monkeypatch.setattr(z,"_read_json",lambda path,params=None:(None,z._error("not_found","missing",status_code=404)) if path=="items/new" else ({"itemTypes":[]},None))
    template,error=z._template("journalArticle")
    assert template is None and error["error"]["code"]=="invalid_response"

