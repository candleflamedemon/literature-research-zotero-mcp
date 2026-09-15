"""Policy-first full-text resolution. Metadata and PDF transports are separate.

No publisher authentication, browser cookies, proxy changes, or database access.
trust_env=False bypasses environment HTTP proxies, NOT Windows TUN/routing.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
import network_access as network
import zotero_write as zotero

ROOT = Path(__file__).resolve().parent.parent
USER_AGENT = "LiteratureMCP/0.7 (legal full-text resolver)"
MAX_PDF_BYTES = 100 * 1024 * 1024
MAX_HTML_BYTES = 256 * 1024
TIMEOUT = httpx.Timeout(30, connect=5)
DENIED_HOSTS = ("sci-hub", "scihub", "libgen", "library.lol", "annas-archive")
SENSITIVE_QUERY = re.compile(r"token|password|secret|signature|credential|authorization|api[_-]?key|session|ticket", re.I)
BLOCK_MARKERS = ("captcha", "verify you are human", "checking your browser", "automated access is prohibited", "automated requests are not allowed", "bots are not permitted", "robot access denied", "cf-chl-", "access denied")


class StopSource(Exception):
    def __init__(self, code: str):
        self.code = code


def doi_value(value: str) -> str:
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value.strip(), flags=re.I).lower()
    if not re.fullmatch(r"10\.\d{4,9}/[^\s?#]+", value):
        raise ValueError("invalid_doi")
    return value


def display_url(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def safe_url(url: str) -> str:
    try:
        p = urlsplit(url)
        if p.scheme != "https" or not p.hostname or p.username or p.password or p.port not in (None, 443):
            raise ValueError()
        if any(s in p.hostname.lower() for s in DENIED_HOSTS):
            raise ValueError()
        if any(SENSITIVE_QUERY.search(k) for k, _ in parse_qsl(p.query)):
            raise ValueError()
        if not network._is_public_host(p.hostname, 443):
            raise ValueError()
        return urlunsplit(("https", p.netloc, p.path or "/", p.query, ""))
    except (ValueError, TypeError):
        raise StopSource("unsafe_url") from None


class PDFLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and a.get("name", "").lower() == "citation_pdf_url" and a.get("content"):
            self.links.insert(0, a["content"])
        if tag == "a" and a.get("href") and urlsplit(a["href"]).path.lower().endswith(".pdf"):
            self.links.append(a["href"])


class Resolver:
    def __init__(self, workspace: Path = ROOT):
        self.workspace = workspace.resolve()
        self.lock = threading.RLock()
        self.slots = threading.BoundedSemaphore(2)
        self.last_request: dict[str, float] = {}
        self.records: dict[str, dict] = {}
        self.blocked: dict[str, str] = {}
        self.candidates: dict[str, list[dict]] = {}
        self.folder: Path | None = None
        # Blocked-source policy survives server restarts; never restores secrets.
        artifacts = self.workspace / "任务成果"
        if artifacts.exists():
            for file in sorted(artifacts.glob("全文获取_*_codex/acquisition.json")):
                try:
                    old = json.loads(file.read_text(encoding="utf-8"))
                    self.blocked.update(old.get("stopped_sources", {}))
                    for record in old.get("records", []):
                        if isinstance(record, dict) and record.get("DOI"):
                            self.records[doi_value(record["DOI"])] = record
                except (OSError, ValueError, TypeError):
                    continue

    def _folder(self) -> Path:
        with self.lock:
            if self.folder is None:
                parent = self.workspace / "任务成果"
                parent.mkdir(exist_ok=True)
                versions = []
                for p in parent.glob("全文获取_*_codex"):
                    match = re.match(r"全文获取_\d{12}_(\d+)_", p.name)
                    if match:
                        versions.append(int(match[1]))
                version = max(versions, default=0) + 1
                stamp = datetime.now().strftime("%Y%m%d%H%M")
                while True:
                    folder = parent / f"全文获取_{stamp}_{version:03d}_解析器测试与获取记录_codex"
                    try:
                        folder.mkdir()
                        self.folder = folder
                        break
                    except FileExistsError:
                        version += 1
            return self.folder

    def _save(self):
        with self.lock:
            folder = self._folder()
            # Only our current version is updated; no previous version overwritten.
            temp = folder / "acquisition.json.tmp"
            temp.write_text(json.dumps(self.report(), ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(folder / "acquisition.json")

    def _record(self, doi: str, **fields) -> dict:
        with self.lock:
            old = self.records.get(doi, {"DOI": doi, "publisher_url": f"https://doi.org/{doi}", "access_type": "METADATA_ONLY", "pdf_status": "NO_ACCESS"})
            old.update(fields)
            self.records[doi] = old
            # Persist paper-level decisions locally; never sends them externally.
            self._save()
            return dict(old)

    def _stop(self, hosts: set[str], code: str):
        with self.lock:
            for host in hosts:
                self.blocked[host] = code
            self._save()

    def _wait(self, host: str, interval: float = 2):
        with self.lock:
            if host in self.blocked:
                raise StopSource(self.blocked[host])
            delay = max(0, self.last_request.get(host, 0) + interval - time.monotonic())
            if delay:
                time.sleep(delay)
            self.last_request[host] = time.monotonic()

    def _fulltext_client(self):
        # Uses Windows default routes (including user's official VPN). No cookies.
        return httpx.Client(trust_env=False, follow_redirects=False, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})

    def _robots(self, client, url: str, hosts: set[str]):
        p = urlsplit(url)
        self._wait(p.hostname)
        client.cookies.clear()
        with client.stream("GET", f"https://{p.netloc}/robots.txt") as response:
            if response.status_code in (403, 429):
                code = f"http_{response.status_code}"
                self._stop(hosts, code)
                raise StopSource(code)
            if response.status_code == 404:
                return
            # Fail closed on robots redirects, errors and oversized policies.
            if response.status_code != 200:
                raise StopSource("robots_unavailable")
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_HTML_BYTES:
                    raise StopSource("robots_too_large")
            text = body.decode("utf-8", errors="replace")
            if any(x in text.lower() for x in BLOCK_MARKERS):
                self._stop(hosts, "automation_blocked")
                raise StopSource("automation_blocked")
            policy = RobotFileParser()
            policy.parse(text.splitlines())
            if not policy.can_fetch(USER_AGENT, url):
                self._stop(hosts, "robots_disallowed")
                raise StopSource("robots_disallowed")
            delay = policy.crawl_delay(USER_AGENT) or policy.crawl_delay("*") or 2
            if delay > 60:
                raise StopSource("robots_requires_manual_access")
            self._wait(p.hostname, max(2, delay))

    def _get(self, url: str, *, download: bool, destination: Path | None = None) -> dict:
        """Bounded redirect/HTML resolver; never bypasses access or bot controls."""
        current = url
        hosts: set[str] = set()
        with self.slots, zotero._suppress_sensitive_http_logs(True), self._fulltext_client() as client:
            for step in range(8):
                current = safe_url(current)
                host = urlsplit(current).hostname
                hosts.add(host)
                if network.LOGIN_URL_MARKERS.search(current):
                    raise StopSource("needs_browser_login")
                self._robots(client, current, hosts)
                for attempt in range(3):
                    try:
                        self._wait(host)
                        client.cookies.clear()
                        with client.stream("GET", current) as response:
                            status = response.status_code
                            if status in (403, 429):
                                code = f"http_{status}"
                                self._stop(hosts, code)
                                raise StopSource(code)
                            if status == 401:
                                raise StopSource("needs_browser_login")
                            if status >= 500:
                                if attempt < 2:
                                    time.sleep(2 ** attempt)
                                    continue
                                raise StopSource("upstream_error")
                            if status in (301, 302, 303, 307, 308):
                                location = response.headers.get("Location")
                                if not location:
                                    raise StopSource("invalid_redirect")
                                current = urljoin(current, location)
                                break
                            if status != 200:
                                raise StopSource(f"http_{status}")
                            mime = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                            if mime == "application/pdf":
                                if not download:
                                    return {"pdf_url": display_url(current), "verified_headers_only": True}
                                size = response.headers.get("Content-Length", "")
                                if size.isdigit() and int(size) > MAX_PDF_BYTES:
                                    raise StopSource("pdf_too_large")
                                if destination is None:
                                    raise StopSource("missing_destination")
                                partial = destination.with_suffix(".part")
                                try:
                                    total = 0
                                    first = bytearray()
                                    digest = hashlib.sha256()
                                    with partial.open("xb") as handle:
                                        for chunk in response.iter_bytes():
                                            total += len(chunk)
                                            if total > MAX_PDF_BYTES:
                                                raise StopSource("pdf_too_large")
                                            if len(first) < 1024:
                                                first.extend(chunk[:1024-len(first)])
                                                if len(first) >= 5 and not bytes(first).startswith(b"%PDF-"):
                                                    if any(marker.encode() in bytes(first).lower() for marker in BLOCK_MARKERS):
                                                        self._stop(hosts, "automation_blocked")
                                                        raise StopSource("automation_blocked")
                                                    raise StopSource("invalid_pdf")
                                            digest.update(chunk)
                                            handle.write(chunk)
                                        if not first.startswith(b"%PDF-"):
                                            raise StopSource("invalid_pdf")
                                    partial.rename(destination)
                                    return {"pdf_url": display_url(current), "local_file": str(destination), "bytes": total, "sha256": digest.hexdigest()}
                                finally:
                                    if partial.exists():
                                        partial.unlink()  # only our incomplete temporary file
                            if "html" not in mime:
                                raise StopSource("not_a_pdf")
                            body = bytearray()
                            for chunk in response.iter_bytes():
                                body.extend(chunk[:MAX_HTML_BYTES-len(body)])
                                if len(body) >= MAX_HTML_BYTES:
                                    break
                            text = body.decode(response.encoding or "utf-8", errors="replace")
                            lowered = text.lower()
                            if any(x in lowered for x in BLOCK_MARKERS):
                                self._stop(hosts, "automation_blocked")
                                raise StopSource("automation_blocked")
                            if any(x in lowered for x in network.PAYWALL_MARKERS):
                                raise StopSource("no_access")
                            parser = PDFLinks()
                            parser.feed(text)
                            if not parser.links:
                                if any(x in lowered for x in network.LOGIN_MARKERS):
                                    raise StopSource("needs_browser_login")
                                raise StopSource("pdf_link_not_found")
                            # Only explicit PDF links; no endpoint guessing/JS/authentication.
                            current = urljoin(current, parser.links[0])
                            break
                    except (httpx.TimeoutException, httpx.RequestError):
                        if attempt == 2:
                            raise StopSource("network_error") from None
                        time.sleep(2 ** attempt)
            raise StopSource("redirect_or_link_limit")

    def _metadata(self, url: str, params: dict | None = None) -> tuple[dict | None, str | None]:
        # Metadata transport may inherit user's current proxy; never fetches PDFs.
        host = urlsplit(url).hostname
        with self.lock:
            if host in self.blocked:
                return None, f"source_stopped_{self.blocked[host]}"
        try:
            with zotero._suppress_sensitive_http_logs(True):
                with httpx.Client(trust_env=True, timeout=TIMEOUT, follow_redirects=False) as client:
                    with client.stream("GET", url, params=params, headers={"Accept": "application/json", "User-Agent": USER_AGENT}) as r:
                        if r.status_code == 429:
                            self._stop({host}, "http_429")
                            return None, "http_429"
                        if r.status_code != 200:
                            return None, f"http_{r.status_code}"
                        data = bytearray()
                        for chunk in r.iter_bytes():
                            data.extend(chunk)
                            if len(data) > 4 * 1024 * 1024:
                                return None, "metadata_too_large"
                        payload = json.loads(data)
                        return (payload, None) if isinstance(payload, dict) else (None, "invalid_json")
        except (httpx.RequestError, ValueError):
            return None, "metadata_query_failed"

    def find_oa(self, doi: str) -> dict:
        try:
            doi = doi_value(doi)
        except (ValueError, AttributeError):
            return {"ok": False, "error": {"code": "invalid_doi"}}
        found: list[dict] = []
        queries = []
        email = os.getenv("UNPAYWALL_EMAIL", "")
        if email and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            payload, error = self._metadata(f"https://api.unpaywall.org/v2/{quote(doi, safe='/')}", {"email": email})
            queries.append({"source": "Unpaywall", "error": error})
            if payload and payload.get("is_oa") is True and str(payload.get("doi", "")).lower() == doi:
                locations = [payload.get("best_oa_location")] + (payload.get("oa_locations") or [])
                for loc in locations:
                    if isinstance(loc, dict):
                        url = loc.get("url_for_pdf") or loc.get("url_for_landing_page")
                        if url:
                            found.append({"source": "Unpaywall", "url": url, "direct_pdf": bool(loc.get("url_for_pdf")), "license": loc.get("license"), "version": loc.get("version"), "host_type": loc.get("host_type"), "oa_evidence": True})
        else:
            queries.append({"source": "Unpaywall", "error": "email_not_configured", "message": "请由用户提供真实联系邮箱到 UNPAYWALL_EMAIL；不会伪造邮箱。"})
        params = {"api_key": os.environ["OPENALEX_API_KEY"]} if os.getenv("OPENALEX_API_KEY") else None
        payload, error = self._metadata(f"https://api.openalex.org/works/https://doi.org/{quote(doi, safe='/')}", params)
        queries.append({"source": "OpenAlex", "error": error})
        if payload:
            try:
                matched = doi_value(payload.get("doi", "")) == doi
            except (ValueError, AttributeError):
                matched = False
            if matched:
                locations = [payload.get("best_oa_location")] + (payload.get("locations") or [])
                for loc in locations:
                    if isinstance(loc, dict) and loc.get("is_oa") is True:
                        url = loc.get("pdf_url") or loc.get("landing_page_url")
                        source = loc.get("source") or {}
                        if url:
                            found.append({"source": "OpenAlex", "url": url, "direct_pdf": bool(loc.get("pdf_url")), "license": loc.get("license"), "version": loc.get("version"), "host_type": "repository" if source.get("type") == "repository" else "publisher", "oa_evidence": True})
        safe: list[dict] = []
        seen = set()
        for candidate in found:
            try:
                url = safe_url(candidate["url"])
            except StopSource:
                continue
            if url not in seen:
                if url in self.records.get(doi, {}).get("rejected_oa_urls", []):
                    continue
                seen.add(url)
                safe.append({**candidate, "url": url})
        # Prefer API order (Unpaywall first), then explicit PDF at each source.
        safe.sort(key=lambda x: (x["source"] != "Unpaywall", not x["direct_pdf"]))
        with self.lock:
            self.candidates[doi] = safe
        self._record(doi, oa_candidates=[{**x, "url": display_url(x["url"])} for x in safe], metadata_queries=queries)
        return {"ok": True, "DOI": doi, "candidates": [{**x, "url": display_url(x["url"])} for x in safe], "queries": queries, "downloaded_pdf": False}

    def _existing(self, doi: str, item_key: str | None) -> tuple[list[dict], dict | None]:
        if item_key:
            key = zotero._valid_key(item_key)
            if not key:
                return [], {"code": "invalid_item_key"}
            parent, error = zotero._read_json(f"users/0/items/{key}")
            if error:
                return [], error
            try:
                if doi_value(zotero._data(parent).get("DOI", "")) != doi:
                    return [], {"code": "parent_doi_mismatch"}
            except (ValueError, AttributeError):
                return [], {"code": "parent_doi_missing"}
            matches = [{"item_key": key}]
        else:
            matches, error = zotero.find_paper_by_doi(doi)
            if error:
                return [], error
        result = []
        for match in matches:
            key = match["item_key"]
            children, error = zotero._read_json(f"users/0/items/{key}/children")
            if error:
                return [], error
            if not isinstance(children, list):
                return [], {"code": "invalid_children"}
            for child in children:
                data = zotero._data(child)
                attachment = zotero._entry_key(child)
                if attachment in self.records.get(doi, {}).get("rejected_attachment_keys", []):
                    continue
                if data.get("itemType") != "attachment" or data.get("contentType") != "application/pdf" or not zotero._valid_key(attachment):
                    continue
                response, error = zotero._request("GET", f"users/0/items/{attachment}/file")
                if not error and response.status_code == 302:
                    location = response.headers.get("Location", "")
                    p = urlsplit(location)
                    # Local API is authoritative; only test file existence, never export path.
                    if p.scheme == "file" and not p.netloc:
                        path = unquote(p.path)
                        if re.match(r"^/[A-Za-z]:/", path):
                            path = path[1:]
                        if Path(path).is_file():
                            result.append({"parent_item_key": key, "attachment_key": attachment})
        return result, None

    def classify(self, doi: str, item_key: str | None = None, publisher_url: str | None = None, institutional_route: str = "IP") -> dict:
        try:
            doi = doi_value(doi)
            target = safe_url(publisher_url or f"https://doi.org/{doi}")
            if institutional_route not in ("IP", "VPN"):
                raise ValueError()
        except (ValueError, AttributeError, StopSource):
            return {"ok": False, "error": {"code": "invalid_input"}}
        existing, error = self._existing(doi, item_key)
        if error and error.get("code") in ("invalid_item_key", "parent_doi_mismatch", "parent_doi_missing"):
            return {"ok": False, "error": error}
        if existing:
            r = self._record(doi, access_type="MANUAL", pdf_status="FOUND", resolution="ZOTERO_ATTACHMENT", existing_attachments=existing)
            return {"ok": True, **r}
        previous = self.records.get(doi, {})
        if previous.get("pdf_status") == "DOWNLOADED" and previous.get("local_file") and previous.get("identity_validation") != "FAILED":
            path = Path(previous["local_file"])
            try:
                if path.is_file() and path.stat().st_size <= MAX_PDF_BYTES and hashlib.sha256(path.read_bytes()).hexdigest() == previous.get("sha256"):
                    return {"ok": True, **previous, "reused_local_download": True}
            except OSError:
                pass
        self.find_oa(doi)
        # FOUND is an OA candidate, not a guaranteed downloadable PDF.
        if self.candidates.get(doi):
            if all(urlsplit(c["url"]).hostname in self.blocked for c in self.candidates[doi]):
                r = self._record(doi, access_type="METADATA_ONLY", pdf_status="FAILED", resolution="SOURCE_STOPPED", reason="所有 OA 候选来源已停止自动访问；请人工合法访问。")
                return {"ok": True, **r}
            r = self._record(doi, access_type="OA", pdf_status="FOUND", resolution="OA_CANDIDATE", zotero_warning=error)
            return {"ok": True, **r}
        host = urlsplit(target).hostname
        if host in self.blocked:
            r = self._record(doi, access_type="METADATA_ONLY", pdf_status="FAILED", reason=self.blocked[host])
            return {"ok": True, **r}
        # Respect robots/rate policy BEFORE the institutional access diagnostic.
        try:
            visited = {host}
            def guard(client, url):
                safe_url(url)
                visited.add(urlsplit(url).hostname)
                self._robots(client, url, visited)
                self._wait(urlsplit(url).hostname)
            with self.slots, zotero._suppress_sensitive_http_logs(True):
                access = network.check_institution_access_target(publisher_url=target, request_guard=guard)
        except (StopSource, httpx.RequestError) as exc:
            access = {"status": "UNKNOWN", "reason": getattr(exc, "code", "network_error")}
        if access.get("http_status") in (403, 429) or access.get("automation_blocked"):
            self._stop(visited, "automation_blocked")
        status = access.get("status")
        if status == "LOGIN_REQUIRED":
            return self.queue(doi, target)
        r = self._record(doi, publisher_url=display_url(target), institution_check=access, zotero_warning=error,
            access_type=f"INSTITUTIONAL_{institutional_route}" if status == "AVAILABLE" else "METADATA_ONLY",
            pdf_status="FOUND" if status == "AVAILABLE" else "NO_ACCESS" if status == "NOT_AVAILABLE" else "FAILED",
            route_basis="USER_DECLARED_NOT_AUTOMATICALLY_PROVEN", routing_warning="trust_env=False 不绕过 TUN；保持 Windows 默认路由。")
        return {"ok": True, **r}

    def queue(self, doi: str, publisher_url: str | None = None) -> dict:
        try:
            doi = doi_value(doi)
            target = display_url(publisher_url or f"https://doi.org/{doi}")
            # Queue accepts public URL syntax without fetching login pages.
            p = urlsplit(target)
            if p.scheme != "https" or not p.hostname or p.username or p.password:
                raise ValueError()
            if any(x in p.hostname.lower() for x in DENIED_HOSTS):
                raise ValueError()
        except (ValueError, AttributeError):
            return {"ok": False, "error": {"code": "invalid_input"}}
        r = self._record(doi, publisher_url=target, access_type="WEB_PROXY", pdf_status="NEEDS_LOGIN", queue_status="NEEDS_BROWSER_LOGIN", instructions="请在浏览器中通过学校官方 WebVPN/EZproxy/SSO/MFA 登录，再使用 Zotero Connector 保存；服务不读取 Cookie 或保存凭据。")
        self._save()
        return {"ok": True, **r}

    def fetch_oa(self, doi: str, item_key: str | None = None) -> dict:
        try:
            doi = doi_value(doi)
        except (ValueError, AttributeError):
            return {"ok": False, "error": {"code": "invalid_doi"}}
        existing, error = self._existing(doi, item_key)
        if existing:
            return {"ok": True, **self._record(doi, access_type="MANUAL", pdf_status="FOUND", resolution="ZOTERO_ATTACHMENT", existing_attachments=existing, downloaded_pdf=False)}
        if error and error.get("code") in ("invalid_item_key", "parent_doi_mismatch", "parent_doi_missing"):
            return {"ok": False, "error": error}
        previous = self.records.get(doi, {})
        if previous.get("pdf_status") == "DOWNLOADED" and previous.get("local_file") and previous.get("identity_validation") != "FAILED":
            path = Path(previous["local_file"])
            try:
                if path.is_file() and path.stat().st_size <= MAX_PDF_BYTES and hashlib.sha256(path.read_bytes()).hexdigest() == previous.get("sha256"):
                    return {"ok": True, **previous, "reused_local_download": True}
            except OSError:
                pass
        self.find_oa(doi)
        failures = []
        for candidate in list(self.candidates.get(doi, [])):
            destination = self._folder() / f"{hashlib.sha256(doi.encode()).hexdigest()[:16]}_{time.time_ns()}.pdf"
            try:
                pdf = self._get(candidate["url"], download=True, destination=destination)
                r = self._record(doi, access_type="OA", pdf_status="DOWNLOADED", identity_validation="UNVERIFIED", resolution="OA_DOWNLOAD", **pdf, oa_provenance={**candidate, "url": display_url(candidate["url"])}, attempts=failures)
                self._save()
                return {"ok": True, **r, "attached_to_zotero": False}
            except (StopSource, httpx.RequestError, OSError) as exc:
                code = getattr(exc, "code", "local_or_network_error")
                failures.append({"source": candidate["source"], "url": display_url(candidate["url"]), "reason": code})
                # Other independent legal sources may be tried, but never blocked host.
                if code == "needs_browser_login":
                    return self.queue(doi)
        r = self._record(doi, access_type="METADATA_ONLY", pdf_status="FAILED" if failures else "NO_ACCESS", resolution="DOWNLOAD_FAILED" if failures else "NO_OA_CANDIDATE", attempts=failures)
        self._save()
        return {"ok": False, **r, "attached_to_zotero": False}

    def fetch_institutional(self, doi: str, publisher_url: str, institutional_route: str = "IP", item_key: str | None = None) -> dict:
        classification = self.classify(doi, item_key, publisher_url, institutional_route)
        if not classification.get("ok"):
            return classification
        if classification.get("resolution") == "ZOTERO_ATTACHMENT":
            return classification
        if classification.get("access_type") == "OA":
            return {**classification, "downloaded_pdf": False, "message": "合法 OA 优先；请使用 fetch_oa_pdf。"}
        if classification.get("institution_check", {}).get("status") != "AVAILABLE":
            return {**classification, "downloaded_pdf": False, "message": "机构检测不为 AVAILABLE，未尝试下载。"}
        doi = doi_value(doi)
        destination = self._folder() / f"{hashlib.sha256(doi.encode()).hexdigest()[:16]}_{time.time_ns()}.pdf"
        try:
            # Same canonical, query-free target tested by institution detection.
            pdf = self._get(display_url(publisher_url), download=True, destination=destination)
            r = self._record(doi, access_type=f"INSTITUTIONAL_{institutional_route}", pdf_status="DOWNLOADED", **pdf)
            self._save()
            return {"ok": True, **r, "attached_to_zotero": False}
        except (StopSource, httpx.RequestError, OSError) as exc:
            code = getattr(exc, "code", "local_or_network_error")
            if code == "needs_browser_login":
                return self.queue(doi, publisher_url)
            r = self._record(doi, pdf_status="FAILED", reason=code)
            self._save()
            return {"ok": False, **r}

    def attach(self, doi: str, parent_item_key: str, confirm: bool = False) -> dict:
        if not confirm:
            return {"ok": False, "error": {"code": "confirmation_required", "message": "附件写入需要 confirm=true，并在 Zotero 桌面端授权。"}}
        try:
            doi = doi_value(doi)
            r = self.records[doi]
            if r.get("identity_validation") == "FAILED":
                raise ValueError()
            path = Path(r["local_file"]).resolve()
            if r.get("pdf_status") != "DOWNLOADED" or r.get("access_type") not in ("OA", "INSTITUTIONAL_IP", "INSTITUTIONAL_VPN") or not path.is_relative_to((self.workspace / "任务成果").resolve()) or not re.fullmatch(r"全文获取_\d{12}_\d+_.+_codex", path.parent.name):
                raise ValueError()
            if path.stat().st_size > MAX_PDF_BYTES:
                raise ValueError()
            content = path.read_bytes()
            if len(content) > MAX_PDF_BYTES or hashlib.sha256(content).hexdigest() != r["sha256"] or not content.startswith(b"%PDF-"):
                raise ValueError()
        except (KeyError, ValueError, OSError):
            return {"ok": False, "error": {"code": "verified_download_required"}}
        with self.lock:
            result = zotero.attach_new_pdf(doi, parent_item_key, path, content, r.get("pending_attachment"))
            if result.get("pending_attachment"):
                self._record(doi, pending_attachment=result["pending_attachment"])
            if result.get("ok"):
                self._record(doi, attachment_key=result.get("attachment_key"), attached_to_zotero=True)
            self._save()
        return result

    def report(self, doi: str | None = None) -> dict:
        with self.lock:
            if doi:
                try:
                    target = doi_value(doi)
                except (ValueError, AttributeError):
                    return {"ok": False, "error": {"code": "invalid_doi"}}
                records = [self.records[target]] if target in self.records else []
            else:
                records = list(self.records.values())
            # Return deep copies; no credentials, public IP or browser data exists here.
            return json.loads(json.dumps({"ok": True, "records": records, "count": len(records), "stopped_sources": self.blocked, "max_concurrency": 2, "pdf_client_trust_env": False, "artifact_directory": str(self.folder) if self.folder else None, "routing_warning": "仅隔离环境 HTTP 代理，不能保证绕过 TUN；未修改系统路由。"}))


resolver = Resolver()
