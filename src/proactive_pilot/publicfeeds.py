"""Bounded public-source discovery + monitoring (read-only, standard library).

Scope:
- Public RSS/Atom feeds, the unauthenticated GitHub API, and two fixed vetted
  example news endpoints: an example university newsroom JSON and one CNA
  category RSS (technology-relevance gated). No new credentials, no login, no Graph/private
  access, no proxies, no loops, no article-body following.
- Strict HTTPS canonical URL allowlist. Credentials, non-443 ports, IP
  literals, localhost/private hosts and secret-like query keys are rejected.
  Every redirect hop is re-validated with the same rules.
- Conservative Reddit politeness: at most ONE Reddit request per run and a
  cadence of at least one hour. 429 responses are never retried or bypassed.
- Parsed source text is untrusted evidence: HTML/script is stripped, text is
  bounded, and nothing here is treated as instruction.

This module does not read private accounts, does not send anything and does not
store raw bodies. It returns ``SourceResult`` objects (kind ``feed``).
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import copy
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import unescape
from typing import Any, Callable

from .collectors import SourceResult, safe_source_error

SCHEMA = 1
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT = 20
DEFAULT_MAX_ITEMS = 15
DEFAULT_REGISTRY_LIMIT = 12
MIN_ATTEMPT_SECONDS = 1.0
REDDIT_CADENCE_MINUTES = 60

USER_AGENT = "proactive-pilot/0.2 (public feeds; read-only)"

# Exact official LTA DataMall live train-service-alert endpoint. This is a
# CREDENTIALED source: the AccountKey is read ONLY from the environment variable
# below and sent ONLY as a request header. The host/path are declared as a fixed
# EXACT endpoint (never authority to crawl datamall2.mytransport.sg).
LTA_DATAMALL_HOST = "datamall2.mytransport.sg"
LTA_TRAIN_ALERTS_PATH = "/ltaodataservice/TrainServiceAlerts"
LTA_TRAIN_ALERTS_URL = f"https://{LTA_DATAMALL_HOST}{LTA_TRAIN_ALERTS_PATH}"
LTA_ACCOUNT_KEY_ENV = "LTA_DATAMALL_ACCOUNT_KEY"
LTA_TRAIN_ALERTS_KIND = "lta_train_alerts"

# Canonical host -> permitted path prefixes (strict). Extend only via policy
# opt-in for generic websites, never at runtime from feed content.
PUBLIC_HOST_PATHS: dict[str, tuple[str, ...]] = {
    "api.github.com": ("/repos/", "/search/"),
    "github.com": ("/",),
    "www.reddit.com": ("/r/", "/search.rss", "/user/", "/.rss"),
    "reddit.com": ("/r/", "/search.rss", "/user/", "/.rss"),
    "old.reddit.com": ("/r/", "/search.rss", "/user/", "/.rss"),
    # Fixed official public news endpoints, added as a VETTED allowlist. This is
    # not a broad URL opt-in: arbitrary hosts still require the explicit generic
    # policy opt-in, and every SSRF/credential/port/redirect/secret-query check
    # below is unchanged.
    "www.university.example": (
        "/news/",
        # EXACT reviewed example-university scholarship pages only (bounded opportunity evidence;
        # not authority to crawl the scholarships-and-awards subtree). The prior
        # broad "/admissions/undergraduate/scholarships-and-awards/" prefix was
        # wider than the claimed fixed-page scope and is deliberately removed.
        "/admissions/undergraduate/scholarships-and-awards/scholarship-application-process",
        "/admissions/undergraduate/scholarships-and-awards/scholarship-opportunities",
        # Exact reviewed official opportunity page (parent-fetched public
        # fixture). One fixed page only: this is NOT a prefix or subtree opt-in.
        "/admissions/undergraduate/fees-and-funding/financial-aid/overseas-student-programme-loan-travel-award",
        # Owner-approved exact replacement for the redirected Travel Award page.
        "/admissions/undergraduate/fees-and-funding/financial-aid/travel-award-overseas-student-programme-exchange",
    ),
    "www.channelnewsasia.com": ("/api/v1/rss-outbound-feed",),
    # Exact official keyless NEA forecast API and one reviewed SMRT planned
    # notice. These are fixed paths, not authority to crawl their domains.
    "api-open.data.gov.sg": ("/v2/real-time/api/twenty-four-hr-forecast",),
    "www.smrt.com.sg": (
        "/news-publications/newsroom/service-announcements/last-bus-train-timing-extension-for-2026-f1-singapore-grand-prix/",
    ),
    # Exact official credentialed LTA DataMall live train-service-alert endpoint.
    # A single fixed EXACT path (enforced in ``validate_public_url`` below), NOT
    # authority to crawl the domain.
    LTA_DATAMALL_HOST: (LTA_TRAIN_ALERTS_PATH,),
}

REDDIT_HOSTS = {"www.reddit.com", "reddit.com", "old.reddit.com"}
GITHUB_HOSTS = {"api.github.com", "github.com"}

# Vetted fixed public news sources permitted in the registry (seed sources).
VETTED_NEWS_HOSTS = {"www.university.example", "www.channelnewsasia.com"}
VETTED_FIXED_PUBLIC_HOSTS = {"api-open.data.gov.sg", "www.smrt.com.sg", LTA_DATAMALL_HOST}
# Hosts permitted for DISPLAY-ONLY article links. They are normalized/validated
# but NEVER fetched and article bodies are NEVER followed.
ARTICLE_HOSTS = {"www.university.example", "www.channelnewsasia.com"}
UNIVERSITY_BASE = "https://www.university.example"
UNIVERSITY_STORY_ENDPOINT = "https://www.university.example/news/GetStories/?page=1"
CNA_BUSINESS_RSS = (
    "https://www.channelnewsasia.com/api/v1/rss-outbound-feed"
    "?_format=xml&category=6936"
)
NEA_FORECAST_URL = "https://api-open.data.gov.sg/v2/real-time/api/twenty-four-hr-forecast"
SMRT_F1_NOTICE_URL = (
    "https://www.smrt.com.sg/news-publications/newsroom/service-announcements/"
    "last-bus-train-timing-extension-for-2026-f1-singapore-grand-prix/"
)
UNIVERSITY_SCHOLARSHIP_PROCESS_URL = (
    "https://www.university.example/admissions/undergraduate/"
    "scholarships-and-awards/scholarship-application-process"
)
UNIVERSITY_TRAVEL_AWARD_URL = (
    "https://www.university.example/admissions/undergraduate/fees-and-funding/"
    "financial-aid/travel-award-overseas-student-programme-exchange"
)

# Query keys that would smuggle a credential/token into a URL.
_SECRET_QUERY_KEYS = {
    "token", "access_token", "api_key", "apikey", "key", "secret", "password",
    "passwd", "auth", "authorization", "session", "sessionid", "sid", "code",
    "otp", "signature", "sig", "credential",
}

_FEED_GENERIC_HOST_PREFIXES: dict[str, tuple[str, ...]] = {}


class FeedError(RuntimeError):
    pass


class URLRejected(FeedError):
    """A URL failed allowlist / SSRF validation. Never fetched."""


class FetchFailed(FeedError):
    """Bounded fetch failed (network, status, redirect or size)."""


class FeedFailed(FeedError):
    """A fetched public body could not be parsed as the declared feed type."""


class FeedAPIError(FeedError):
    """The official API returned an explicit non-success payload."""


class FeedExpired(FeedError):
    """The source item is outside its explicit validity period."""


@dataclass
class FetchResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    final_url: str


# --------------------------------------------------------------------------- #
# URL safety
# --------------------------------------------------------------------------- #


def canonical_host(host: str) -> str:
    return str(host or "").strip().lower().rstrip(".")


def _forbidden_hostname(host: str) -> bool:
    if not host:
        return True
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local") or host.endswith(".internal"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
    )


def validate_public_url(
    url: str,
    *,
    allowed_hosts: dict[str, tuple[str, ...]] | None = None,
    generic_opt_in: bool = False,
    generic_hosts: dict[str, tuple[str, ...]] | None = None,
) -> urllib.parse.SplitResult:
    """Validate a public HTTPS URL. Raises ``URLRejected`` on any violation."""
    try:
        parsed = urllib.parse.urlsplit(str(url or ""))
    except ValueError as exc:
        raise URLRejected(f"unparseable url: {exc}") from exc
    if parsed.scheme != "https":
        raise URLRejected("only https is allowed")
    if parsed.username or parsed.password:
        raise URLRejected("credentials in url are forbidden")
    host = canonical_host(parsed.hostname or "")
    if not host:
        raise URLRejected("missing host")
    if _forbidden_hostname(host):
        raise URLRejected(f"forbidden host: {host}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise URLRejected(f"invalid port: {exc}") from exc
    if port not in (None, 443):
        raise URLRejected("only the default https port is allowed")
    allow = dict(allowed_hosts or PUBLIC_HOST_PATHS)
    if generic_opt_in and generic_hosts:
        allow.update(generic_hosts)
    # ALL safety checks run before returning, including for opt-in generic
    # hosts: generic_opt_in relaxes only the host/path allowlist, never the
    # credential/port/SSRF/secret-query checks.
    if host in allow:
        path = parsed.path or "/"
        prefixes = allow.get(host, ())
        if host == "www.university.example" and not path.startswith("/news/"):
            # Scholarship entries are reviewed individual pages, not prefixes.
            if path not in prefixes:
                raise URLRejected(f"path not exactly allowlisted for {host}: {path}")
        if host == LTA_DATAMALL_HOST and path != LTA_TRAIN_ALERTS_PATH:
            # One EXACT credentialed endpoint: any other path on this host (even
            # one that shares the prefix) is refused.
            raise URLRejected(f"path not exactly allowlisted for {host}: {path}")
        if prefixes and not any(path.startswith(prefix) for prefix in prefixes):
            raise URLRejected(f"path not allowlisted for {host}: {path}")
    elif not generic_opt_in:
        raise URLRejected(f"host not allowlisted: {host}")
    for key in urllib.parse.parse_qs(parsed.query, keep_blank_values=True):
        if key.strip().lower() in _SECRET_QUERY_KEYS:
            raise URLRejected(f"secret-like query key forbidden: {key}")
    return parsed


def safe_article_link(base: str, raw: Any) -> str:
    """Normalize a DISPLAY-ONLY article link from untrusted feed payload.

    Relative paths are resolved against the fixed official ``base``; an absolute
    link is accepted only when it is https, credential-free, on the default port,
    free of secret-like query keys, and on a vetted article host. Anything else
    (private IPs, localhost, off-host, http, credentials) becomes ``""``. The
    result is never fetched and no article body is ever followed.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.startswith("//"):
        text = "https:" + text
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError:
        return ""
    if not parsed.scheme:
        text = urllib.parse.urljoin(str(base or "").rstrip("/") + "/", text)
    elif parsed.scheme != "https":
        return ""
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError:
        return ""
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return ""
    host = canonical_host(parsed.hostname or "")
    if host not in ARTICLE_HOSTS or _forbidden_hostname(host):
        return ""
    try:
        if parsed.port not in (None, 443):
            return ""
    except ValueError:
        return ""
    for key in urllib.parse.parse_qs(parsed.query, keep_blank_values=True):
        if key.strip().lower() in _SECRET_QUERY_KEYS:
            return ""
    return text


def sanitize_evidence_link(raw: Any, *, max_chars: int = 2048) -> str:
    """Bound/canonicalize a DISPLAY-ONLY evidence link from untrusted payload.

    Rejects dangerous schemes (``javascript:``/``data:``/``file:``/etc), control
    characters, credentials and over-long values; returns the canonical URL text
    or ``""``. The result is never fetched by the parser, so this is provenance
    hardening for cached findings, not a fetch gate.
    """
    text = str(raw or "").strip()
    if not text or len(text) > int(max_chars):
        return ""
    if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in text):
        return ""
    lowered = text.lower()
    if (lowered.startswith("javascript:") or lowered.startswith("data:")
            or lowered.startswith("file:") or lowered.startswith("vbscript:")):
        return ""
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError:
        return ""
    if parsed.username or parsed.password:
        return ""
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        return ""
    if parsed.scheme:
        for key in urllib.parse.parse_qs(parsed.query, keep_blank_values=True):
            if key.strip().lower() in _SECRET_QUERY_KEYS:
                return ""
        return parsed.geturl()
    # Relative reference: keep as-is (display only, resolved by the caller).
    return text


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect hop; cap the number of hops."""

    def __init__(self, allowed_hosts: dict[str, tuple[str, ...]], *, max_redirects: int = 3,
                 generic_opt_in: bool = False, generic_hosts: dict[str, tuple[str, ...]] | None = None):
        super().__init__()
        self.allowed_hosts = allowed_hosts
        self.max_redirects = max(0, int(max_redirects))
        self.generic_opt_in = generic_opt_in
        self.generic_hosts = generic_hosts
        self.hops = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        validate_public_url(
            newurl,
            allowed_hosts=self.allowed_hosts,
            generic_opt_in=self.generic_opt_in,
            generic_hosts=self.generic_hosts,
        )
        self.hops += 1
        if self.hops > self.max_redirects:
            raise URLRejected("too many redirects")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_bytes(
    url: str,
    *,
    allowed_hosts: dict[str, tuple[str, ...]] | None = None,
    generic_opt_in: bool = False,
    generic_hosts: dict[str, tuple[str, ...]] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    etag: str = "",
    last_modified: str = "",
) -> FetchResponse:
    """Bounded, no-credential HTTPS GET. Raises ``FetchFailed``/``URLRejected``."""
    validate_public_url(
        url, allowed_hosts=allowed_hosts,
        generic_opt_in=generic_opt_in, generic_hosts=generic_hosts,
    )
    handler = _SafeRedirectHandler(
        dict(allowed_hosts or PUBLIC_HOST_PATHS),
        generic_opt_in=generic_opt_in, generic_hosts=generic_hosts,
    )
    opener = urllib.request.build_opener(handler)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/atom+xml, application/rss+xml, application/json, text/xml, */*"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with opener.open(request, timeout=max(1, int(timeout))) as response:
            status = int(getattr(response, "status", 200) or 200)
            raw = response.read(int(max_bytes) + 1)
            final_url = str(getattr(response, "url", url) or url)
            resp_headers = {str(k): str(v) for k, v in dict(response.headers).items()}
    except urllib.error.HTTPError as exc:
        status = int(getattr(exc, "code", 0) or 0)
        if status == 304:
            return FetchResponse(304, {str(k): str(v) for k, v in dict(exc.headers).items()}, b"", url)
        raise FetchFailed(f"http {status}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchFailed(f"network error: {exc}") from exc
    # A redirect could still have landed somewhere unexpected; confirm the final
    # URL passes validation before we trust the body.
    validate_public_url(
        final_url, allowed_hosts=allowed_hosts,
        generic_opt_in=generic_opt_in, generic_hosts=generic_hosts,
    )
    if len(raw) > int(max_bytes):
        raw = raw[: int(max_bytes)]
    return FetchResponse(status, resp_headers, raw, final_url)


# --------------------------------------------------------------------------- #
# Credentialed fetch path (SEPARATE from fetch_bytes; exactly one source)
# --------------------------------------------------------------------------- #
#
# ``fetch_bytes`` above is a documented NO-CREDENTIAL bounded GET and is kept
# exactly that way. The one credentialed source (LTA DataMall train alerts) gets
# its OWN explicitly named path below. It never weakens ``fetch_bytes`` and is
# usable ONLY for a single fixed host + exact path; every vetting failure raises
# (fail closed) rather than degrading, and the key is never placed in a URL.


class CredentialMissing(FeedError):
    """A credentialed source's key env var is absent/empty. Fails closed."""


def _redact_credential(text: Any, secret: str) -> str:
    """Remove the secret value (and the account header name) from any text."""
    out = str(text or "")
    if secret:
        out = out.replace(secret, "[redacted]")
    return re.sub(r"(?i)accountkey", "[redacted-header]", out)


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect for the credentialed source (never follow)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise FeedFailed("redirect refused for credentialed source")


def fetch_lta_train_alerts(
    *,
    environ: Any = None,
    opener: Any = None,
    url: str = LTA_TRAIN_ALERTS_URL,
    timeout: int = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> FetchResponse:
    """Bounded credentialed GET for the ONE reviewed LTA DataMall endpoint.

    Security contract (all violations raise, none degrade):

    - the AccountKey is read ONLY from ``os.environ[LTA_ACCOUNT_KEY_ENV]`` (or an
      injected mapping for tests) and sent ONLY as a request header;
    - exactly one host and one EXACT path (no query string) is permitted; any
      other host/port/scheme/path is refused before any socket is opened;
    - redirects are refused outright;
    - the existing byte cap and timeout are enforced;
    - a missing/empty key raises ``CredentialMissing`` with a non-secret
      diagnostic (the env var NAME only) and NO network call is attempted.
    """
    env = os.environ if environ is None else environ
    key = str(env.get(LTA_ACCOUNT_KEY_ENV) or "").strip()
    if not key:
        # Diagnostic carries the variable NAME only, never a value or length.
        raise CredentialMissing(
            f"credentialed feed unavailable: {LTA_ACCOUNT_KEY_ENV} is not set")
    allowed = {LTA_DATAMALL_HOST: (LTA_TRAIN_ALERTS_PATH,)}
    parsed = validate_public_url(url, allowed_hosts=allowed)
    if canonical_host(parsed.hostname or "") != LTA_DATAMALL_HOST:
        raise URLRejected("credentialed source host mismatch")
    if (parsed.path or "/") != LTA_TRAIN_ALERTS_PATH or parsed.query:
        raise URLRejected("credentialed source path must be exact and query-free")
    opener_obj = opener if opener is not None else urllib.request.build_opener(_RefuseRedirects())
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "AccountKey": key,
    }
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with opener_obj.open(request, timeout=max(1, int(timeout))) as response:
            status = int(getattr(response, "status", 200) or 200)
            raw = response.read(int(max_bytes) + 1)
            final_url = str(getattr(response, "url", url) or url)
            resp_headers = {str(k): str(v) for k, v in dict(response.headers).items()}
    except urllib.error.HTTPError as exc:
        raise FeedFailed(
            _redact_credential(f"http {int(getattr(exc, 'code', 0) or 0)}", key)) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FeedFailed(_redact_credential(f"network error: {exc}", key)) from exc
    # A redirect is refused, so the final URL must still be the one vetted target.
    final = validate_public_url(final_url, allowed_hosts=allowed)
    if canonical_host(final.hostname or "") != LTA_DATAMALL_HOST:
        raise URLRejected("credentialed source final host mismatch")
    if (final.path or "/") != LTA_TRAIN_ALERTS_PATH:
        raise URLRejected("credentialed source final path mismatch")
    if len(raw) > int(max_bytes):
        raw = raw[: int(max_bytes)]
    return FetchResponse(status, resp_headers, raw, final_url)


# --------------------------------------------------------------------------- #
# Parsing (untrusted text -> bounded structured evidence)
# --------------------------------------------------------------------------- #

_TAG_RE = re.compile(r"(?s)<[^>]+>")
_SCRIPT_RE = re.compile(r"(?is)<(script|style)\b.*?</\1>")
_WS_RE = re.compile(r"\s+")

# Block/table boundaries whose removal would merge unrelated local context (for
# example a table category cell like "Travel awards" with the neighbouring date
# cell). They are projected as newlines so a date window is classified from its
# OWN row/cell rather than a distant word. Inline tags, including ``<br>``, stay
# whitespace so a cell's own note remains attached to its date.
_STRUCT_BOUNDARY_RE = re.compile(
    r"(?is)</(?:p|div|li|tr|td|th|h[1-6]|table|thead|tbody|tfoot|section|article|"
    r"header|footer|nav|main|aside|ul|ol|blockquote|dl|dt|dd|figure|figcaption|"
    r"form|fieldset|legend|address|caption|details|summary|pre)\s*>"
)
_SPACE_RUN_RE = re.compile(r"[^\S\n]+")
_NL_TRIM_RE = re.compile(r" *\n *")
_NL_RUN_RE = re.compile(r"\n+")


def strip_html(text: Any, *, max_chars: int = 280) -> str:
    raw = str(text or "")
    raw = _SCRIPT_RE.sub(" ", raw)
    raw = _TAG_RE.sub(" ", raw)
    raw = unescape(raw)
    raw = _WS_RE.sub(" ", raw).strip()
    return raw[: max(1, int(max_chars))]


def strip_html_structured(text: Any, *, max_chars: int = 200_000) -> str:
    """Strip HTML but preserve block/table boundaries as single newlines.

    Same untrusted-text handling as :func:`strip_html` (scripts/styles dropped,
    tags removed, entities unescaped, output bounded) except that block-level and
    table cell/row boundaries survive as newlines. This keeps a date window tied
    to its own row/cell instead of a flattened whole-page run, without inventing
    any context that is not present in the source.
    """
    raw = str(text or "")
    raw = _SCRIPT_RE.sub(" ", raw)
    raw = _STRUCT_BOUNDARY_RE.sub("\n", raw)
    raw = _TAG_RE.sub(" ", raw)
    raw = unescape(raw)
    raw = _SPACE_RUN_RE.sub(" ", raw)
    raw = _NL_TRIM_RE.sub("\n", raw)
    raw = _NL_RUN_RE.sub("\n", raw)
    return raw.strip()[: max(1, int(max_chars))]


def _local(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1].lower()


def _first_text(node: ET.Element, names: tuple[str, ...], *, max_chars: int = 280) -> str:
    for child in list(node):
        if _local(child.tag) in names:
            if child.text and child.text.strip():
                return strip_html(child.text, max_chars=max_chars)
            # Atom <content>/<summary> may hold nested HTML as text already.
            if child.attrib.get("href") and "link" in names:
                return str(child.attrib.get("href"))
    return ""


def _first_attr(node: ET.Element, tag: str, attr: str = "href") -> str:
    for child in list(node):
        if _local(child.tag) == tag:
            value = child.attrib.get(attr)
            if value:
                return str(value)
    return ""


def parse_rss_atom(body: bytes, *, max_items: int = DEFAULT_MAX_ITEMS) -> list[dict[str, Any]]:
    """Parse RSS 2.0 ``item`` or Atom ``entry`` into bounded evidence dicts."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise FeedFailed(f"xml parse error: {exc}") from exc
    entries: list[ET.Element] = []
    for node in root.iter():
        if _local(node.tag) in {"entry", "item"}:
            entries.append(node)
    out: list[dict[str, Any]] = []
    for entry in entries[: max(1, int(max_items))]:
        link = _first_attr(entry, "link") or _first_text(entry, ("link",))
        title = _first_text(entry, ("title",), max_chars=200)
        summary = _first_text(entry, ("summary", "content", "description"), max_chars=400)
        published = _first_text(entry, ("published", "updated", "pubdate", "date"), max_chars=60)
        ident = _first_text(entry, ("id", "guid"), max_chars=200)
        out.append({
            "title": title,
            "summary": summary,
            "link": link,
            "id": ident or link or title,
            "published": published,
        })
    return out


def parse_github_releases(body: bytes, *, max_items: int = DEFAULT_MAX_ITEMS) -> list[dict[str, Any]]:
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError as exc:
        raise FeedFailed(f"github json parse error: {exc}") from exc
    if not isinstance(data, list):
        raise FeedFailed("github releases: expected a list")
    out: list[dict[str, Any]] = []
    for rel in data[: max(1, int(max_items))]:
        if not isinstance(rel, dict):
            continue
        out.append({
            "title": strip_html(rel.get("name") or rel.get("tag_name") or "release", max_chars=200),
            "summary": strip_html(rel.get("body") or "", max_chars=400),
            "link": str(rel.get("html_url") or ""),
            "id": str(rel.get("id") or rel.get("tag_name") or ""),
            "published": str(rel.get("published_at") or rel.get("created_at") or ""),
        })
    return out


def parse_github_search(body: bytes, *, max_items: int = DEFAULT_MAX_ITEMS) -> list[dict[str, Any]]:
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError as exc:
        raise FeedFailed(f"github json parse error: {exc}") from exc
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise FeedFailed("github search: expected items list")
    out: list[dict[str, Any]] = []
    for item in items[: max(1, int(max_items))]:
        if not isinstance(item, dict):
            continue
        out.append({
            "title": strip_html(item.get("full_name") or item.get("name") or "", max_chars=200),
            "summary": strip_html(item.get("description") or "", max_chars=400),
            "link": str(item.get("html_url") or ""),
            "id": str(item.get("id") or item.get("full_name") or ""),
            "published": str(item.get("pushed_at") or item.get("updated_at") or ""),
        })
    return out



_MONTHS: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_DISPLAY_DATE_RE = re.compile(r"^(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})$")


def parse_university_date(raw: Any) -> str:
    """Conservative known-SG date normalization for example-university display dates.

    Accepts an ISO date prefix or the exact displayed ``DD Mon YYYY`` form (e.g.
    ``02 Oct 2026``) and returns a stable ``YYYY-MM-DD`` string. The date is
    treated as a Singapore CALENDAR DATE only: no clock time is ever invented.
    Malformed or impossible values return ``""``.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    match = _ISO_DATE_RE.match(text)
    if match:
        candidate = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        try:
            date.fromisoformat(candidate)
        except ValueError:
            return ""
        return candidate
    match = _DISPLAY_DATE_RE.match(text)
    if match:
        month = _MONTHS.get(match.group(2).lower()[:3])
        if month is None:
            return ""
        try:
            return date(int(match.group(3)), month, int(match.group(1))).isoformat()
        except ValueError:
            return ""
    return ""


def parse_university_stories(
    body: bytes,
    *,
    max_items: int = DEFAULT_MAX_ITEMS,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Parse the example-university newsroom JSON envelope into bounded evidence dicts.

    Expected top-level keys are ``totalPages``/``totalItems``/``items[]`` with
    per-story ``title``/``url``/``description``/``date``/``contentType`` (plus
    many nullable/image fields). Unknown/null fields are tolerated. A body that
    is not an object with a list ``items`` fails closed with ``FeedFailed``.

    Dates are date-only; a future date beyond the allowed calendar day (used to
    reject malformed/fabricated future timestamps) is blanked so it can never
    masquerade as fresh. Relative item URLs are normalized against the fixed
    official base and unsafe/private/off-host links are blanked.
    """
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError as exc:
        raise FeedFailed(f"university json parse error: {exc}") from exc
    if not isinstance(data, dict):
        raise FeedFailed("university json: expected an object")
    stories = data.get("items")
    if not isinstance(stories, list):
        raise FeedFailed("university json: items is not a list")
    out: list[dict[str, Any]] = []
    for story in stories[: max(1, int(max_items))]:
        if not isinstance(story, dict):
            continue
        title = strip_html(story.get("title"), max_chars=200)
        summary = strip_html(story.get("description"), max_chars=400)
        link = safe_article_link(UNIVERSITY_BASE, story.get("url"))
        published = parse_university_date(story.get("date"))
        precision = "date" if published else "unknown"
        if published and now is not None:
            try:
                if date.fromisoformat(published) > now.date() + timedelta(days=1):
                    # Beyond the allowed calendar day: reject rather than present
                    # a fabricated-fresh future date.
                    published = ""
                    precision = "unknown"
            except ValueError:
                published = ""
                precision = "unknown"
        out.append({
            "title": title,
            "summary": summary,
            "link": link,
            "id": link or str(story.get("url") or story.get("title") or ""),
            "published": published,
            "date_precision": precision,
            "content_type": strip_html(story.get("contentType"), max_chars=60),
        })
    return out


def _json_rows(data: Any) -> list[Any]:
    """Extract a list payload only from KNOWN envelope keys.

    An arbitrary undocumented key is deliberately NOT treated as a row list, so a
    discovered JSON endpoint cannot smuggle unrelated content into evidence.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("web", "items", "results", "entries", "articles", "stories"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        nested = data.get("data")
        if isinstance(nested, dict):
            for key in ("web", "items", "results", "entries", "articles", "stories"):
                value = nested.get(key)
                if isinstance(value, list):
                    return value
    return []


_NORMALIZED_CONTENT_TYPES: dict[str, tuple[str, ...]] = {
    "web_json": ("json",),
    "web_page": ("html", "xml", "xhtml"),
    "rss": ("xml", "rss", "atom", "html"),
    "reddit_rss": ("xml", "rss", "atom", "html"),
    "cna_rss": ("xml", "rss", "atom", "html"),
    "nea_forecast": ("json",),
    "smrt_notice": ("html",),
    "university_opportunity": ("html", "xml", "xhtml"),
    # Official credentialed LTA DataMall live train alerts: JSON only.
    "lta_train_alerts": ("json",),
}


def normalized_content_type(headers: dict[str, str]) -> str:
    for key, value in (headers or {}).items():
        if str(key).lower() == "content-type":
            return str(value).split(";", 1)[0].strip().lower()
    return ""


def content_type_allowed(kind: str, content_type: str) -> bool:
    """True when a discovered body's Content-Type matches its declared route.

    An empty/unknown header is tolerated (many feeds omit it and the parser then
    fails closed on its own), but a CLEARLY mismatched type (binary, or HTML
    served for a JSON route) is refused before parse. ``text/plain`` is tolerated
    because some public endpoints serve JSON/XML without a precise subtype.
    """
    if not content_type:
        return True
    if content_type == "text/plain":
        return True
    expected = _NORMALIZED_CONTENT_TYPES.get(str(kind or ""))
    if expected is None:
        return True
    return any(token in content_type for token in expected)


def parse_web_json(body: bytes, *, max_items: int = DEFAULT_MAX_ITEMS) -> list[dict[str, Any]]:
    """Parse a discovered public JSON endpoint into bounded evidence dicts.

    Tolerant of several common envelopes; a body that exposes no list fails
    closed with ``FeedFailed`` rather than claiming an empty (healthy) result.
    """
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError as exc:
        raise FeedFailed(f"web json parse error: {exc}") from exc
    rows = _json_rows(data)
    if not rows:
        raise FeedFailed("web json: no list payload")
    out: list[dict[str, Any]] = []
    for row in rows[: max(1, int(max_items))]:
        if isinstance(row, str):
            out.append({"title": strip_html(row, max_chars=200), "summary": "",
                        "link": "", "id": row[:200], "published": ""})
            continue
        if not isinstance(row, dict):
            continue
        title = row.get("title") or row.get("name") or row.get("headline")
        summary = row.get("summary") or row.get("description") or row.get("snippet")
        link = sanitize_evidence_link(row.get("url") or row.get("link") or row.get("html_url"))
        published = row.get("published") or row.get("date") or row.get("updated") or ""
        out.append({
            "title": strip_html(title, max_chars=200),
            "summary": strip_html(summary, max_chars=400),
            "link": link,
            "id": str(row.get("id") or link or title or ""),
            "published": str(published or "")[:60],
        })
    return out


def _aware_datetime(value: Any, *, field: str) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FeedFailed(f"{field} timestamp malformed") from exc
    if parsed.tzinfo is None:
        raise FeedFailed(f"{field} timestamp missing timezone")
    return parsed


def parse_nea_forecast(
    body: bytes,
    *,
    now: datetime,
    max_age_hours: int = 12,
) -> list[dict[str, Any]]:
    """Project one bounded official NEA 24-hour forecast; never a warning.

    Only the documented ``code/data.records/general`` envelope is accepted.
    Old/expired observations become stale coverage and are not returned as
    actionable candidates.
    """
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, TypeError) as exc:
        raise FeedFailed("NEA JSON parse error") from exc
    if not isinstance(payload, dict):
        raise FeedFailed("NEA payload is not an object")
    if payload.get("code") != 0:
        raise FeedAPIError("NEA API returned a non-success code")
    data = payload.get("data")
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, list) or not records:
        raise FeedFailed("NEA records missing")

    usable: list[tuple[datetime, dict[str, Any], dict[str, Any]]] = []
    for record in records[:3]:
        if not isinstance(record, dict) or not isinstance(record.get("general"), dict):
            continue
        general = record["general"]
        valid = general.get("validPeriod")
        forecast = general.get("forecast")
        if not isinstance(valid, dict) or not isinstance(forecast, dict):
            continue
        try:
            updated = _aware_datetime(record.get("updatedTimestamp"), field="updated")
            starts = _aware_datetime(valid.get("start"), field="valid start")
            ends = _aware_datetime(valid.get("end"), field="valid end")
        except FeedFailed:
            continue
        if ends <= starts or not str(forecast.get("text") or "").strip():
            continue
        usable.append((updated, record, general))
    if not usable:
        raise FeedFailed("NEA forecast record malformed")
    updated, record, general = max(usable, key=lambda row: row[0])
    valid = general["validPeriod"]
    starts = _aware_datetime(valid.get("start"), field="valid start")
    ends = _aware_datetime(valid.get("end"), field="valid end")
    current = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    if updated > current + timedelta(minutes=15):
        raise FeedFailed("NEA update timestamp is in the future")
    if current >= ends:
        raise FeedExpired("NEA forecast validity period expired")
    if current - updated > timedelta(hours=max(1, int(max_age_hours))):
        raise FeedExpired("NEA forecast update is stale")

    temperature = general.get("temperature") if isinstance(general.get("temperature"), dict) else {}
    wind = general.get("wind") if isinstance(general.get("wind"), dict) else {}
    wind_speed = wind.get("speed") if isinstance(wind.get("speed"), dict) else {}
    humidity = general.get("relativeHumidity") if isinstance(general.get("relativeHumidity"), dict) else {}
    details = [
        f"Forecast: {str(general['forecast'].get('text'))[:80]}",
        f"Valid: {starts.isoformat()} to {ends.isoformat()}",
        f"NEA updated: {updated.isoformat()}",
    ]
    if temperature.get("low") is not None and temperature.get("high") is not None:
        details.append(f"Temperature: {temperature['low']}–{temperature['high']} {str(temperature.get('unit') or '')[:32]}".strip())
    if wind_speed.get("low") is not None and wind_speed.get("high") is not None:
        details.append(f"Wind: {str(wind.get('direction') or '')[:8]} {wind_speed['low']}–{wind_speed['high']}")
    if humidity.get("low") is not None and humidity.get("high") is not None:
        details.append(f"Humidity: {humidity['low']}–{humidity['high']}%")
    return [{
        "title": f"NEA 24-hour forecast — {str(general['forecast'].get('text'))[:80]}",
        "summary": "; ".join(details),
        "link": NEA_FORECAST_URL,
        "id": f"nea-forecast:{str(record.get('date') or '')}:{updated.isoformat()}",
        "published": updated.isoformat(),
        "date_precision": "timestamp",
        "category": "weather_forecast",
        "publisher": "Singapore National Environment Agency (NEA)",
        "valid_from": starts.isoformat(),
        "valid_until": ends.isoformat(),
        "forecast_only": True,
    }]


# All-clear marker item id (stable) so a standing disruption can be resolved
# downstream when the official status returns to normal.
LTA_ALL_CLEAR_ID = "lta-train-all-clear"

# Documented per-cluster attributes (LTA API User Guide v6.10 section 2.11).
_LTA_CLUSTER_FIELD_KEYS = (
    "Line", "Direction", "Stations", "FreePublicBus", "FreeMRTShuttle",
    "MRTShuttleDirection",
)


def _lta_scalar_text(value: Any, *, max_chars: int = 200) -> str:
    if isinstance(value, (dict, list)):
        return ""
    return strip_html(value, max_chars=max_chars)


def _lta_list_text(value: Any, *, item_chars: int = 60, max_chars: int = 120) -> str:
    """Render a field that may be a scalar or a list (bounded, sanitized)."""
    if isinstance(value, list):
        return strip_html(
            ", ".join(_lta_scalar_text(v, max_chars=item_chars) for v in value[:10]),
            max_chars=max_chars,
        )
    return strip_html(value, max_chars=max_chars)


def _lta_cluster_text(cluster: dict[str, Any]) -> str:
    """Bounded detail from the documented per-cluster attributes.

    ``Stations`` is accepted as either a string or a list (per the guide) and the
    cluster keys are projected verbatim so no context is invented.
    """
    parts: list[str] = []
    for key in _LTA_CLUSTER_FIELD_KEYS:
        if key not in cluster:
            continue
        text = _lta_list_text(cluster.get(key))
        if not text:
            continue
        if key in ("Line", "Direction", "Stations"):
            parts.append(f"{key}: {text}")
        else:
            parts.append(text)
    return " — ".join(parts)[:280]


def _lta_segment_text(segment: Any) -> str:
    """Bounded, sanitized description of one untrusted AffectedSegments entry."""
    if isinstance(segment, dict):
        parts: list[str] = []
        for key in ("Line", "Direction", "Station", "StationCode", "Stations", "Message"):
            raw = segment.get(key)
            if isinstance(raw, list):
                text = strip_html(
                    ", ".join(_lta_scalar_text(v, max_chars=60) for v in raw[:10]),
                    max_chars=120,
                )
            else:
                text = strip_html(raw, max_chars=120)
            if text:
                parts.append(text)
        return " — ".join(parts)[:280]
    if isinstance(segment, list):
        return strip_html(
            ", ".join(_lta_scalar_text(v, max_chars=60) for v in segment[:10]),
            max_chars=280,
        )
    return strip_html(segment, max_chars=280)


def _lta_message_text(message: Any) -> tuple[str, str]:
    if isinstance(message, dict):
        content = strip_html(
            message.get("Content") or message.get("Message") or message.get("Text") or "",
            max_chars=400,
        )
        created = strip_html(
            message.get("CreatedDate") or message.get("Date") or "", max_chars=60)
        return content, created
    return strip_html(message, max_chars=400), ""


def _lta_message_entries(cluster: dict[str, Any]) -> list[Any]:
    """Message entries from a cluster/object: a list, a single dict or a string."""
    raw = cluster.get("Message")
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    return [raw]


def _lta_segment_entries(cluster: dict[str, Any]) -> list[Any]:
    raw = cluster.get("AffectedSegments")
    return raw if isinstance(raw, list) else []


def _lta_message_item(content: str, created: str, *, normal: bool) -> dict[str, Any]:
    label = "LTA train service notice" if normal else "LTA train disruption notice"
    prefix = "lta-recover-" if normal else "lta-message-"
    return {
        "title": f"{label}: {content[:120]}",
        "summary": content,
        "link": LTA_TRAIN_ALERTS_URL,
        "id": prefix + hashlib.sha256(
            (content + "|" + created).encode("utf-8")).hexdigest()[:16],
        "published": created,
        "date_precision": "timestamp" if created else "unknown",
        "category": "live_transport",
        "publisher": "Singapore Land Transport Authority (LTA)",
        "service_status": "normal" if normal else "disruption",
        "live_service_alert": True,
        "all_clear": False,
        "recover_message": bool(normal),
    }


def parse_lta_train_alerts(
    body: bytes,
    *,
    now: datetime | None = None,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> list[dict[str, Any]]:
    """Parse the official LTA DataMall ``TrainServiceAlerts`` envelope.

    Accepts BOTH documented shapes for ``value`` and fails closed on anything
    else:

    - an OBJECT (single cluster; the live all-clear body), read directly; or
    - a LIST of per-line cluster objects (the shape the LTA API User Guide
      documents for a disruption: "each affected line ... published as separate
      clusters within the single API response").

    Detail is read from the per-cluster documented fields (``Line``,
    ``Direction``, ``Stations`` -- string or list, ``FreePublicBus``,
    ``FreeMRTShuttle``, ``MRTShuttleDirection``, ``Message``) AND from any
    ``AffectedSegments`` entries. ``Message`` entries accept a dict (``Content``
    / ``CreatedDate``) or a plain string. Every field is untrusted: sanitized and
    length-bounded, with deterministic content-derived ids.

    Overall status: for the object shape, ``value["Status"]``; for the list
    shape, ``2`` if ANY cluster reports 2, else ``1`` if at least one reports 1,
    else fail closed. Status 1 yields EXACTLY ONE all-clear marker (plus bounded
    recover-message items when the body carries them). Status 2 with no
    extractable detail fails closed -- never "no news". A malformed/garbage
    Status (bool, non-int, or outside {1,2}) fails closed.
    """
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, TypeError) as exc:
        raise FeedFailed("LTA train alerts JSON parse error") from exc
    if not isinstance(payload, dict):
        raise FeedFailed("LTA train alerts payload is not an object")
    value = payload.get("value")
    if isinstance(value, dict):
        clusters: list[dict[str, Any]] = [value]
    elif isinstance(value, list):
        if not value:
            raise FeedFailed("LTA train alerts value list is empty")
        if not all(isinstance(entry, dict) for entry in value):
            raise FeedFailed("LTA train alerts cluster is not an object")
        clusters = list(value)
    else:
        raise FeedFailed("LTA train alerts value missing")

    statuses: list[int] = []
    for cluster in clusters:
        status = cluster.get("Status")
        if status is None:
            continue
        if isinstance(status, bool) or not isinstance(status, int) or status not in (1, 2):
            raise FeedFailed(f"LTA train alerts status malformed: {status!r}")
        statuses.append(status)
    if not statuses:
        raise FeedFailed("LTA train alerts status missing")
    if 2 in statuses:
        overall = 2
    elif 1 in statuses:
        overall = 1
    else:  # pragma: no cover - statuses only ever contains 1/2
        raise FeedFailed("LTA train alerts unexpected status")

    observed = (now or datetime.now(timezone.utc)).isoformat()
    limit = max(1, int(max_items))

    if overall == 1:
        # Exactly ONE all-clear marker, always first, regardless of max_items.
        out: list[dict[str, Any]] = [{
            "title": "LTA train service status: normal service (all-clear)",
            "summary": (
                "Official LTA DataMall TrainServiceAlerts reports Status 1: no "
                "train service disruption is currently reported (all-clear "
                "marker). This is a status marker, not a fabricated incident."
            ),
            "link": LTA_TRAIN_ALERTS_URL,
            "id": LTA_ALL_CLEAR_ID,
            "published": observed,
            "date_precision": "timestamp",
            "category": "live_transport",
            "publisher": "Singapore Land Transport Authority (LTA)",
            "service_status": "normal",
            "live_service_alert": True,
            "all_clear": True,
            "recover_message": False,
        }]
        # A recover message may still ride a Status-1 body: append it bounded
        # rather than silently dropping it, keeping the one all-clear marker.
        for cluster in clusters:
            for message in _lta_message_entries(cluster):
                content, created = _lta_message_text(message)
                if not content:
                    continue
                out.append(_lta_message_item(content, created, normal=True))
                if len(out) >= limit:
                    return out
        return out

    # overall == 2: a real disruption; detail is mandatory.
    out = []
    seen: set[str] = set()

    def _append(item: dict[str, Any]) -> bool:
        """Add a deduped disruption item; True when the cap is now reached."""
        if item["id"] in seen:
            return False
        seen.add(item["id"])
        out.append(item)
        return len(out) >= limit

    for cluster in clusters:
        for segment in _lta_segment_entries(cluster):
            text = _lta_segment_text(segment)
            if not text:
                continue
            if _append({
                "title": f"LTA train disruption — affected segment: {text[:120]}",
                "summary": f"Affected segment: {text}",
                "link": LTA_TRAIN_ALERTS_URL,
                "id": "lta-segment-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
                "published": "",
                "date_precision": "unknown",
                "category": "live_transport",
                "publisher": "Singapore Land Transport Authority (LTA)",
                "service_status": "disruption",
                "live_service_alert": True,
                "all_clear": False,
                "recover_message": False,
            }):
                return out
        text = _lta_cluster_text(cluster)
        if text:
            if _append({
                "title": f"LTA train disruption — affected line: {text[:120]}",
                "summary": text,
                "link": LTA_TRAIN_ALERTS_URL,
                "id": "lta-line-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
                "published": "",
                "date_precision": "unknown",
                "category": "live_transport",
                "publisher": "Singapore Land Transport Authority (LTA)",
                "service_status": "disruption",
                "live_service_alert": True,
                "all_clear": False,
                "recover_message": False,
            }):
                return out
        for message in _lta_message_entries(cluster):
            content, created = _lta_message_text(message)
            if not content:
                continue
            if _append(_lta_message_item(content, created, normal=False)):
                return out

    if not out:
        # Status 2 with no described detail must never degrade to "no news".
        raise FeedFailed("LTA train alerts status 2 with no detail")
    return out


_NOTICE_PUB_DATE_RE = re.compile(r"\b(\d{1,2})\s+(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{4})\b", re.I)
_NOTICE_VALID_RE = re.compile(r"\bfrom\s+(\d{1,2})\s+to\s+(\d{1,2})\s+(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{4})\b", re.I)
_NOTICE_MONTH = {name.lower(): number for number, names in enumerate((
    ("January", "Jan"), ("February", "Feb"), ("March", "Mar"),
    ("April", "Apr"), ("May",), ("June", "Jun"), ("July", "Jul"),
    ("August", "Aug"), ("September", "Sep"), ("October", "Oct"),
    ("November", "Nov"), ("December", "Dec"),
), 1) for name in names}


def parse_smrt_planned_notice(body: bytes, *, now: datetime) -> list[dict[str, Any]]:
    """Parse the one-reviewed SMRT event advisory with separate pub/valid dates."""
    text = strip_html(body.decode("utf-8", "replace"), max_chars=200_000)
    if "SMRT Travel Advisory for Formula One 2026" not in text:
        raise FeedFailed("SMRT notice title missing")
    # The official page repeats the title in <head><title>, so a raw
    # first-occurrence lookup that assumed the publication date precedes the
    # title mis-located the article header. Instead locate the single validity
    # range and read the publication date from OUTSIDE it: the validity phrase
    # itself embeds a DD Month YYYY fragment ("...to 11 October 2026") that must
    # never be mistaken for the publication date.
    valid_matches = list(_NOTICE_VALID_RE.finditer(text))
    if len(valid_matches) != 1:
        raise FeedFailed("SMRT validity dates missing/ambiguous")
    valid = valid_matches[0]
    pub_matches = [
        match for match in _NOTICE_PUB_DATE_RE.finditer(text)
        if not (valid.start() <= match.start() < valid.end())
    ]
    if len(pub_matches) != 1:
        raise FeedFailed("SMRT publication date missing/ambiguous")
    pub = pub_matches[0]
    try:
        publication = date(int(pub.group(3)), _NOTICE_MONTH[pub.group(2).lower()], int(pub.group(1)))
        start = date(int(valid.group(4)), _NOTICE_MONTH[valid.group(3).lower()], int(valid.group(1)))
        end = date(int(valid.group(4)), _NOTICE_MONTH[valid.group(3).lower()], int(valid.group(2)))
    except (ValueError, KeyError) as exc:
        raise FeedFailed("SMRT notice date invalid") from exc
    if end < start:
        raise FeedFailed("SMRT notice date range inconsistent")
    current = now.date()
    # Expiry is checked before the publication/validity ordering so a genuinely
    # past validity period fails closed as FeedExpired rather than being masked as
    # a generic parse failure.
    if end < current:
        raise FeedExpired("SMRT planned notice validity period expired")
    if start < publication:
        raise FeedFailed("SMRT notice date range inconsistent")

    lower = text.lower()
    route_labels = (
        ("north-south line", "North-South Line"),
        ("east-west line", "East-West Line"),
        ("circle line", "Circle Line"),
        ("thomson-east coast line", "Thomson-East Coast Line"),
    )
    lines = [label for marker, label in route_labels if marker in lower]
    if not lines:
        raise FeedFailed("SMRT affected lines missing")
    lines_text = ", ".join(lines)
    if "some bus services" in lower or "following smrt bus services" in lower:
        lines_text += "; selected SMRT bus services (notice details only)"
    title = "SMRT planned travel advisory: Formula One service-hour extensions"
    return [{
        "title": title,
        "summary": (
            f"Planned advisory from SMRT; not a live incident or all-clear. "
            f"Published {publication.isoformat()}; valid {start.isoformat()} through {end.isoformat()}; "
            f"affected services: {lines_text}."
        ),
        "link": SMRT_F1_NOTICE_URL,
        "id": "smrt-f1-2026-planned-extension",
        "published": publication.isoformat(),
        "date_precision": "date",
        "category": "planned_transport",
        "publisher": "SMRT",
        "valid_from": start.isoformat(),
        "valid_until": end.isoformat(),
        "affected_lines": lines,
        "planned_notice": True,
    }]


_MONTH_NAMES = (
    r"(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)"
)
# Explicit, year-bearing date windows only. A recurrence such as "from 1 to 31
# July every year" has no 4-digit year and deliberately does NOT match.
_OPP_RANGE_FULL_RE = re.compile(
    r"(?<!\d)(?:from\s+)?(\d{1,2})\s+(" + _MONTH_NAMES + r")\s+(\d{4})\s+to\s+"
    r"(\d{1,2})\s+(" + _MONTH_NAMES + r")\s+(\d{4})\b", re.I)
_OPP_RANGE_SAME_RE = re.compile(
    r"(?<!\d)(?:from\s+)?(\d{1,2})\s+to\s+(\d{1,2})\s+(" + _MONTH_NAMES + r")\s+(\d{4})\b", re.I)
_OPP_SINGLE_RE = re.compile(
    r"(?:deadline|closes|close[sd]?|apply by|applications?\s+(?:close|open until)|due)\b"
    r"[^.\n]{0,40}?(?<!\d)(\d{1,2})\s+(" + _MONTH_NAMES + r")\s+(\d{4})\b", re.I)
_OPP_REDIRECT_MARKERS = (
    "object moved", "moved permanently", "301 moved", "302 found",
    "document has moved", "redirecting you", "meta http-equiv=\"refresh\"",
)
_OPP_AUDIENCE_MARKERS = (
    "eligib", "citizen", "permanent resident", "current student",
    "undergraduate", "postgraduate", "polytechnic", "diploma", "open to",
    "who can apply", "audience", "nationality", "full-time",
)
# Stronger eligibility phrasing used only to RANK candidate sentences: a page
# title/nav fragment that merely contains "undergraduate" must not outrank a real
# eligibility sentence. Never used to invent a claim.
_OPP_AUDIENCE_STRONG = (
    "eligib", "open to", "who can apply", "must be", "citizen",
    "permanent resident", "financial", "full-time", "current student",
    "nationality",
)
_H1_RE = re.compile(r"(?is)<h1\b[^>]*>(.*?)</h1>")

# Local-context binding for a candidate window. A date window is an APPLICATION
# window only when its own clause is application/submission context; a window
# whose clause is an interview/programme/event/award context is NOT an obligation
# and is never surfaced as a deadline. This is bound to the clause that actually
# contains the dates, never inferred from the word "scholarship" elsewhere.
_OPP_APPLICATION_MARKERS = (
    "application", "applications", "apply", "applying", "submission",
    "submit", "deadline", "closes", "closed", "closing", "register",
    "registration", "enrol", "enroll", "nomination", "open until",
    "open from", "accepting",
)
_OPP_NONAPPLICATION_MARKERS = (
    "interview", "interviews", "interviewing", "programme", "program",
    "event", "events", "award", "awards", "ceremony", "briefing",
    "orientation", "camp", "workshop", "test", "tests", "assessment",
    "examination", "exam", "seminar", "conference", "symposium",
    "results", "shortlist", "shortlisted", "audition", "rehearsal",
    "session", "sessions", "semester", "term", "holiday", "graduation",
    "convocation", "commencement",
)
# Explicit, bounded clock-time forms. A time is only trusted when it also carries
# an unambiguous Singapore timezone token; a bare clock time fails closed rather
# than being declared expired/active. ``HH:MM`` and ``H am/pm`` are supported.
_OPP_TZ_OFFSETS = {
    "sgt": 8, "sst": 8, "sin": 8,
    "gmt+8": 8, "utc+8": 8, "gmt+08": 8, "utc+08": 8,
    "+08:00": 8, "+0800": 8, "+08": 8, "+8": 8,
    "singaporetime": 8, "singaporestandardtime": 8,
}
_OPP_CLOCK_RE = re.compile(
    r"(?<![\d:])(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(am|pm)?"
    r"\s*(sgt|sst|sin|gmt\s*\+\s*0?8|utc\s*\+\s*0?8|\+\s*0?8:?0?0?"
    r"|singapore(?:\s+standard)?\s+time)?",
    re.I,
)
_OPP_AMPM_RE = re.compile(
    r"(?<![\d:])(\d{1,2})\s*(am|pm)\s*"
    r"(sgt|sst|sin|gmt\s*\+\s*0?8|utc\s*\+\s*0?8|\+\s*0?8:?0?0?"
    r"|singapore(?:\s+standard)?\s+time)?",
    re.I,
)
# A time-like phrase that cannot be resolved to an unambiguous local clock time.
# When present it makes the deadline an unsupported precision (fail closed), so a
# vague close time is never silently declared expired.
_OPP_LOOSE_TIME_RE = re.compile(
    r"(?<![a-z0-9])(noon|midnight|midday|eod|end\s+of\s+day|close\s+of\s+business)"
    r"(?![a-z0-9])",
    re.I,
)


def _opp_month(name: str) -> int | None:
    key = str(name or "").strip().lower()
    if key == "sept":
        return 9
    return _NOTICE_MONTH.get(key)


def _opp_clause(text: str, start: int, end: int, *, max_chars: int = 200) -> tuple[int, str]:
    """Return ``(left_offset, clause)`` for the sentence holding ``[start,end)``."""
    left = start
    limit = max(0, start - max_chars)
    while left > limit and text[left - 1] not in ".!?\n":
        left -= 1
    right = end
    limit_r = min(len(text), end + max_chars)
    while right < limit_r and text[right] not in ".!?\n":
        right += 1
    return left, text[left:right]


def _opp_nearest_marker(lowered: str, markers: tuple[str, ...], anchor: int) -> int:
    """Position of the whole-word marker nearest to ``anchor``, or ``-1``."""
    best_pos = -1
    best_dist: int | None = None
    for marker in markers:
        for match in re.finditer(
            r"(?<![a-z0-9])" + re.escape(marker) + r"(?![a-z0-9])", lowered
        ):
            dist = abs(match.start() - anchor)
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best_pos = match.start()
    return best_pos


def _opp_window_context_class(text: str, start: int, end: int) -> str:
    """Classify a window clause as application / non_application / unknown.

    A window is an application window only when its own clause is closer to an
    application/submission marker than to any interview/programme/event/award
    marker. A clause with no such marker is ``unknown`` and is never accepted as
    an obligation on its own.
    """
    left, clause = _opp_clause(text, start, end)
    lowered = clause.lower()
    anchor = start - left
    app = _opp_nearest_marker(lowered, _OPP_APPLICATION_MARKERS, anchor)
    non = _opp_nearest_marker(lowered, _OPP_NONAPPLICATION_MARKERS, anchor)
    if app < 0 and non < 0:
        return "unknown"
    if app < 0:
        return "non_application"
    if non < 0:
        return "application"
    # Both present: nearest to the dates wins; a tie is treated conservatively as
    # non-application so a programme/interview window is never fabricated.
    if abs(non - anchor) <= abs(app - anchor):
        return "non_application"
    return "application"


def _opp_tz_offset(token: str) -> int | None:
    text = str(token or "").strip().lower().replace(" ", "")
    if not text:
        return None
    if text in _OPP_TZ_OFFSETS:
        return _OPP_TZ_OFFSETS[text]
    if re.fullmatch(r"(?:gmt|utc)\+0?8", text):
        return 8
    if re.fullmatch(r"\+0?8(?::?0?0)?", text):
        return 8
    return None


def _opp_explicit_deadline_time(
    text: str, date_end: int, deadline: date
) -> tuple[str, str]:
    """Parse an explicit local clock time immediately after a single date.

    Returns ``(iso, "time")`` for an unambiguous timezone-bearing time,
    ``("", "unsupported")`` when a time-ish expression exists but cannot be
    interpreted unambiguously (never silently treated as expired), or ``("", "")``
    when there is no time expression at all (date-only deadline).
    """
    tail = text[date_end: date_end + 60]
    match = _OPP_CLOCK_RE.search(tail)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        second = int(match.group(3) or 0)
        ampm = (match.group(4) or "").lower()
        tz_token = match.group(5) or ""
    else:
        match = _OPP_AMPM_RE.search(tail)
        if not match:
            if _OPP_LOOSE_TIME_RE.search(tail):
                return "", "unsupported"
            return "", ""
        hour, minute, second = int(match.group(1)), 0, 0
        ampm = (match.group(2) or "").lower()
        tz_token = match.group(3) or ""
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    offset = _opp_tz_offset(tz_token)
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59) or offset is None:
        return "", "unsupported"
    combined = datetime(
        deadline.year, deadline.month, deadline.day, hour, minute, second,
        tzinfo=timezone(timedelta(hours=offset)),
    )
    return combined.isoformat(), "time"


def _opp_explicit_windows(text: str) -> list[dict[str, Any]]:
    """Every explicit year-bearing date window with its local context class."""
    found: list[tuple[int, int, date, date, bool]] = []
    for match in _OPP_RANGE_FULL_RE.finditer(text):
        d1, mo1, y1, d2, mo2, y2 = match.groups()
        try:
            start = date(int(y1), _opp_month(mo1) or 0, int(d1))
            end = date(int(y2), _opp_month(mo2) or 0, int(d2))
        except (ValueError, TypeError):
            continue
        found.append((match.start(), match.end(), start, end, False))
    for match in _OPP_RANGE_SAME_RE.finditer(text):
        d1, d2, mo, y = match.groups()
        try:
            start = date(int(y), _opp_month(mo) or 0, int(d1))
            end = date(int(y), _opp_month(mo) or 0, int(d2))
        except (ValueError, TypeError):
            continue
        found.append((match.start(), match.end(), start, end, False))
    for match in _OPP_SINGLE_RE.finditer(text):
        d, mo, y = match.groups()
        try:
            when = date(int(y), _opp_month(mo) or 0, int(d))
        except (ValueError, TypeError):
            continue
        found.append((match.start(), match.end(), when, when, True))
    found.sort(key=lambda row: row[0])
    out: list[dict[str, Any]] = []
    seen: set[tuple[date, date]] = set()
    for pos, span_end, start, end, is_single in found:
        if (start, end) in seen:
            continue
        seen.add((start, end))
        _left, clause = _opp_clause(text, pos, span_end)
        out.append({
            "pos": pos,
            "date_end": span_end,
            "start": start,
            "end": end,
            "single": is_single,
            "kind": _opp_window_context_class(text, pos, span_end),
            # Bounded quote of the window's OWN structural row/cell: the evidence
            # linkage is the local clause the dates came from, never the whole page.
            "context": clause.strip()[:300],
        })
    return out


def _opp_title(text: str, body: bytes) -> str:
    match = _H1_RE.search(body.decode("utf-8", "replace"))
    if match:
        title = strip_html(match.group(1), max_chars=200)
        if title:
            return title
    match = _PAGE_TITLE_RE.search(body.decode("utf-8", "replace"))
    return strip_html(match.group(1), max_chars=200) if match else ""


def _opp_audience(text: str, *, max_chars: int = 300) -> tuple[str, bool]:
    """Bounded source-declared audience/eligibility sentence(s). Never personal.

    Sentences are split on punctuation AND structural newlines, and the strongest
    eligibility sentence wins, so a page title/nav fragment ("... Undergraduate
    Admissions ...") cannot shadow the actual audience statement. The returned
    text is verbatim source evidence only; it never asserts a personal fit.
    """
    best = ""
    best_score = -1
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        lowered = sentence.lower()
        markers = [marker for marker in _OPP_AUDIENCE_MARKERS if marker in lowered]
        if not markers:
            continue
        cleaned = sentence.strip()
        if not cleaned:
            continue
        score = len(markers) + 2 * sum(1 for marker in _OPP_AUDIENCE_STRONG if marker in lowered)
        if score > best_score:
            best_score = score
            best = cleaned
    return (best[:max_chars], True) if best else ("", False)


def parse_university_opportunity(body: bytes, *, now: datetime) -> list[dict[str, Any]]:
    """Project one bounded, explicit example-university opportunity deadline/eligibility record.

    Conservative fail-closed rules:
    - redirect/challenge/error skeletons and non-opportunity pages are unusable;
    - a dated deadline MUST carry an explicit 4-digit year. An annual recurrence
      ("every year") is NOT a currently open individual opportunity;
    - a date window counts ONLY when its own clause is application/submission
      context; interview/programme/event/award windows are never deadlines, and a
      clause with no application marker is not accepted (so a window is never
      inferred from the word "scholarship" elsewhere on the page);
    - more than one remaining (non-non-application) window is ambiguous and fails
      closed;
    - past/exact-deadline windows are ``FeedExpired`` (closed), never rolled
      forward or presented as currently open. A single deadline with an explicit
      unambiguous local time is honoured to the minute; a single deadline with no
      time is date-only (closed on the deadline day, closing time disclosed as
      unknown); a time-like value that cannot be interpreted fails closed as an
      unsupported precision rather than being declared expired. A single deadline
      never invents a start date;
    - audience/eligibility text is retained verbatim as SOURCE evidence and never
      asserts that any individual personally qualifies.
    """
    classification = classify_web_page(body)
    if classification:
        raise WebPageUnavailable(classification)
    # Structural projection: block/table-cell boundaries survive as newlines so a
    # date window is classified from its own row/cell, not a flattened whole-page
    # ws-run (where a neighbouring "award" label could win nearest-word).
    text = strip_html_structured(body.decode("utf-8", "replace"), max_chars=200_000)
    lowered = text.lower()
    if any(marker in lowered for marker in _OPP_REDIRECT_MARKERS):
        raise WebPageUnavailable(PAGE_FAILURE, "redirect/unusable page")
    if not any(word in lowered for word in ("scholarship", "award", "opportunity", "application")):
        raise FeedFailed("opportunity page marker missing")
    windows = _opp_explicit_windows(text)
    if not windows:
        raise FeedFailed("opportunity explicit deadline missing (year required)")
    # Drop interview/programme/event/award windows; they are not obligations.
    remaining = [w for w in windows if w["kind"] != "non_application"]
    if not remaining:
        raise FeedFailed("opportunity application window missing (non-application dates only)")
    if len(remaining) > 1:
        raise FeedFailed("opportunity deadline ambiguous")
    window = remaining[0]
    if window["kind"] != "application":
        raise FeedFailed("opportunity application context missing")
    current = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    start, end = window["start"], window["end"]
    if window["single"]:
        # A single deadline states no opening date: never invent one.
        valid_from_iso = ""
        time_iso, time_state = _opp_explicit_deadline_time(text, window["date_end"], end)
        if time_state == "time":
            deadline_dt = datetime.fromisoformat(time_iso)
            if current >= deadline_dt:
                raise FeedExpired("opportunity deadline passed")
            deadline_iso, precision, closing_time_known = time_iso, "time", True
        elif time_state == "unsupported":
            # A time is present but not unambiguously local: fail closed as an
            # unsupported precision, never as a falsely expired deadline.
            raise FeedFailed("opportunity deadline time precision unsupported")
        else:
            if current.date() >= end:
                raise FeedExpired("opportunity deadline passed")
            deadline_iso, precision, closing_time_known = end.isoformat(), "date", False
        if closing_time_known:
            window_text = (
                f"Single application deadline {deadline_iso} (no stated opening "
                f"date; explicit local closing time)."
            )
        else:
            window_text = (
                f"Single application deadline {deadline_iso} (no stated opening "
                f"date; closing time not stated, treated as closed on the "
                f"deadline day)."
            )
    else:
        if end < start:
            raise FeedFailed("opportunity deadline range inconsistent")
        if current.date() >= end:
            raise FeedExpired("opportunity deadline passed")
        valid_from_iso = start.isoformat()
        deadline_iso, precision, closing_time_known = end.isoformat(), "date", False
        window_text = (
            f"Explicit application window {start.isoformat()} to {end.isoformat()} "
            f"(deadline {end.isoformat()})."
        )
    title = _opp_title(text, body) or "University scholarship opportunity"
    audience, audience_declared = _opp_audience(text)
    quote = str(window.get("context") or "").strip()
    evidence_text = f' Source row evidence: "{quote}".' if quote else ""
    summary = (
        f"{window_text}{evidence_text} Source-declared audience/eligibility: "
        f"{audience or 'not stated'}. Source evidence only; not a personal "
        f"eligibility determination."
    )
    return [{
        "title": title,
        "summary": summary[:700],
        "link": "",
        "id": f"university-opportunity:{valid_from_iso or 'unknown'}:{deadline_iso}:{title[:80]}",
        "published": "",
        "date_precision": precision,
        "category": "opportunity",
        "publisher": "Example University",
        "valid_from": valid_from_iso,
        "valid_until": deadline_iso,
        "deadline": deadline_iso,
        "deadline_precision": precision,
        "closing_time_known": closing_time_known,
        "opportunity_only": True,
        "audience": audience,
        "audience_declared": bool(audience_declared),
        "personal_qualification": False,
    }]


_PAGE_TITLE_RE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
_META_DESC_RE = re.compile(
    r"(?is)<meta[^>]+name=[\"']description[\"'][^>]+content=[\"'](.*?)[\"']")
_ANCHOR_RE = re.compile(r"(?is)<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>")

# Known anti-bot / interstitial skeletons. An HTTP 200 response whose body is one
# of these is NOT usable public content: it must never produce a candidate, be
# reported as a healthy empty source, or become a material watch finding. Every
# match is an explicit, bounded substring; a legitimate page that merely carries
# ``noindex``/robots metadata is deliberately NOT rejected here.
PAGE_BLOCKED = "blocked"
PAGE_DYNAMIC = "dynamic"
PAGE_UNSUPPORTED = "unsupported"
PAGE_FAILURE = "failure"

_ANTIBOT_BLOCKED_MARKERS = (
    "incapsula", "_incapsula_resource", "request unsuccessful", "incapsula incident",
    "just a moment", "cf-browser-verification", "cf_chl", "cf-chl",
    "checking your browser before accessing", "attention required! | cloudflare",
    "cloudflare ray id", "enable javascript and cookies to continue",
    "unusual traffic from your computer network",
    "recaptcha", "hcaptcha", "g-recaptcha", "captcha", "are you a human",
    "sign in to continue", "log in to continue", "login to continue",
    "subscribe to continue", "subscribe to read", "for subscribers only",
    "paywall", "access denied", "you have been blocked",
)
_ANTIBOT_DYNAMIC_MARKERS = (
    "javascript is required", "enable javascript", "please enable javascript",
    "your browser does not support javascript", "requires javascript",
    "this page requires javascript", "turn on javascript",
)
_ANTIBOT_UNSUPPORTED_MARKERS = (
    "unsupported browser", "browser is not supported",
    "your browser is out of date", "browser is no longer supported",
)
_ANTIBOT_FAILURE_MARKERS = (
    "404 not found", "page not found", "the page you are looking for",
    "service unavailable", "internal server error", "bad gateway",
    "error 404", "error 403", "error 500",
)


class WebPageUnavailable(FeedFailed):
    """A fetched 200 page is a challenge/error skeleton, not usable content.

    ``classification`` is one of ``blocked``/``dynamic``/``unsupported``/
    ``failure`` so the caller can report the real reason distinctly instead of a
    healthy empty result or a material finding.
    """

    def __init__(self, classification: str, detail: str = ""):
        super().__init__(f"web page {classification}: {detail or 'content unavailable'}")
        self.classification = classification


def classify_web_page(body: bytes) -> str:
    """'' for usable HTML, else a distinct challenge/error classification.

    Runs BEFORE any extraction so a 200 anti-bot/challenge/login/paywall/error
    interstitial can never produce a candidate. Only explicit known markers are
    matched; ``noindex`` and ordinary robots metadata are not treated as a
    challenge.
    """
    text = body.decode("utf-8", "replace").lower()
    if not text.strip():
        return PAGE_FAILURE
    for group, label in (
        (_ANTIBOT_BLOCKED_MARKERS, PAGE_BLOCKED),
        (_ANTIBOT_DYNAMIC_MARKERS, PAGE_DYNAMIC),
        (_ANTIBOT_UNSUPPORTED_MARKERS, PAGE_UNSUPPORTED),
        (_ANTIBOT_FAILURE_MARKERS, PAGE_FAILURE),
    ):
        for marker in group:
            if marker in text:
                return label
    return ""


def parse_web_page(body: bytes, *, max_items: int = DEFAULT_MAX_ITEMS) -> list[dict[str, Any]]:
    """Deterministic bounded metadata from a discovered public HTML page.

    Extracts the page title/meta description and up to ``max_items`` anchors.
    All text is stripped and bounded; links are returned but are NEVER fetched by
    this adapter (a discovered page is untrusted evidence, not instructions). A
    known anti-bot/challenge/error skeleton fails closed with a distinct
    classification before any extraction, so it is never mistaken for content.
    """
    classification = classify_web_page(body)
    if classification:
        raise WebPageUnavailable(classification)
    text = body.decode("utf-8", "replace")
    title_match = _PAGE_TITLE_RE.search(text)
    page_title = strip_html(title_match.group(1), max_chars=200) if title_match else ""
    desc_match = _META_DESC_RE.search(text)
    page_desc = strip_html(desc_match.group(1), max_chars=400) if desc_match else ""
    out: list[dict[str, Any]] = []
    if page_title or page_desc:
        out.append({"title": page_title or page_desc, "summary": page_desc,
                    "link": "", "id": page_title or page_desc, "published": ""})
    for href, label in _ANCHOR_RE.findall(text):
        if len(out) >= max(1, int(max_items)):
            break
        anchor = strip_html(label, max_chars=200)
        if not anchor:
            continue
        out.append({"title": anchor, "summary": page_desc,
                    "link": sanitize_evidence_link(href),
                    "id": f"{anchor}|{href}", "published": ""})
    if not out:
        raise FeedFailed("web page: no bounded metadata found")
    return out


_MARKDOWN_HEADING_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def parse_markdown_page(body: bytes, *, max_items: int = DEFAULT_MAX_ITEMS) -> list[dict[str, Any]]:
    """Bounded evidence from a PUBLIC page already converted to markdown.

    Used ONLY by the opt-in native MCP fallback for a page the direct transport
    served as a JavaScript shell. It re-runs the same anti-bot classification on
    the markdown text, never follows links, and never fabricates metadata: the
    title is the first markdown heading (or first non-empty line) and the summary
    is bounded, de-linked plain text.
    """
    classification = classify_web_page(body)
    if classification:
        raise WebPageUnavailable(classification)
    text = body.decode("utf-8", "replace")
    heading = _MARKDOWN_HEADING_RE.search(text)
    title = strip_html(heading.group(1), max_chars=200) if heading else ""
    if not title:
        for line in text.splitlines():
            cleaned = strip_html(line, max_chars=200)
            if cleaned:
                title = cleaned
                break
    summary = strip_html(_MARKDOWN_LINK_RE.sub(lambda m: m.group(1), text), max_chars=400)
    if not title and not summary:
        raise FeedFailed("markdown page: no bounded content found")
    return [{
        "title": title or summary[:200],
        "summary": summary,
        "link": "",
        "id": hashlib.sha256((title or summary).encode("utf-8")).hexdigest()[:16],
        "published": "",
    }]


def parse_source(
    body: bytes,
    kind: str,
    *,
    max_items: int = DEFAULT_MAX_ITEMS,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    if kind == "markdown_page":
        return parse_markdown_page(body, max_items=max_items)
    if kind == "github_releases":
        return parse_github_releases(body, max_items=max_items)
    if kind == "github_search":
        return parse_github_search(body, max_items=max_items)
    if kind == "university_json":
        return parse_university_stories(body, max_items=max_items, now=now)
    if kind == "nea_forecast":
        return parse_nea_forecast(body, now=now or datetime.now(timezone.utc))
    if kind == "smrt_notice":
        return parse_smrt_planned_notice(body, now=now or datetime.now(timezone.utc))
    if kind == "university_opportunity":
        return parse_university_opportunity(body, now=now or datetime.now(timezone.utc))
    if kind == LTA_TRAIN_ALERTS_KIND:
        return parse_lta_train_alerts(body, now=now, max_items=max_items)
    if kind == "web_json":
        return parse_web_json(body, max_items=max_items)
    if kind == "web_page":
        return parse_web_page(body, max_items=max_items)
    return parse_rss_atom(body, max_items=max_items)


# --------------------------------------------------------------------------- #
# Sanitized, deterministic topic discovery (evidence-guided, not "intelligence")
# --------------------------------------------------------------------------- #

_SENSITIVE_MARKERS = (
    "otp", "password", "passcode", "verification code", "token", "secret",
    "credit card", "cvv", "iban", "account number",
)
_STOPWORDS = {
    "the", "and", "for", "with", "your", "you", "are", "was", "were", "this",
    "that", "from", "have", "has", "had", "will", "would", "should", "could",
    "about", "into", "over", "under", "please", "thanks", "thank", "regards",
    "dear", "hello", "hi", "re", "fwd", "fw", "not", "but", "our", "their",
    "his", "her", "its", "been", "being", "more", "most", "some", "such",
    "than", "then", "them", "they", "what", "when", "where", "which", "while",
    "who", "whom", "why", "how", "all", "any", "each", "few", "other", "own",
    "same", "too", "very", "can", "just", "now", "new", "update", "updated",
    "notification", "notifications", "reminder", "important", "alert", "email",
    "mail", "message", "message from", "no-reply", "noreply",
}
_WORD_RE = re.compile(r"[a-z][a-z0-9\-]{2,23}")
_EMAIL_RE = re.compile(r"\S+@\S+")
_URL_RE = re.compile(r"https?://\S+|\bwww\.\S+")
_LONGNUM_RE = re.compile(r"\d{3,}")

# Closed, vetted generic-interest vocabulary. A task title is NEVER sent as a
# public query: only tokens that appear here may become one. This prevents
# private names, addresses, order IDs and arbitrary text from leaving the
# machine. Extend deliberately, never from raw user text.
VETTED_INTERESTS: frozenset[str] = frozenset({
    "ai", "ml", "llm", "agents", "agent", "robotics", "python", "rust", "golang",
    "javascript", "typescript", "linux", "docker", "kubernetes", "database",
    "security", "privacy", "cryptography", "quantum", "computing", "distributed",
    "systems", "networking", "compilers", "kernel", "opensource", "open-source",
    "github", "gitlab", "research", "science", "physics", "mathematics", "math",
    "statistics", "engineering", "software", "hardware", "chips", "semiconductor",
    "energy", "climate", "biology", "medicine", "neuroscience", "economics",
    "scholarship", "scholarships", "grant", "grants", "fellowship", "hackathon",
    "conference", "workshop", "seminar", "course", "courses", "university",
    "internship", "career", "jobs", "startup", "startups", "venture", "product",
    "design", "writing", "reading", "language", "languages", "history",
    "philosophy", "art", "music", "travel", "running", "cycling", "swimming",
    "fitness", "health", "nutrition", "finance", "investing", "productivity",
    "automation", "tools", "tooling", "framework", "library", "release",
    "releases", "changelog", "roadmap", "community", "opensource",
})


def is_vetted_interest(token: str) -> bool:
    return token in VETTED_INTERESTS


def sanitize_topic_text(text: Any) -> str:
    """Strip PII, URLs, emails, codes and sensitive markers from context.

    Only generic lower-case tokens survive; this is what feeds public search
    queries so no raw private mail text can leak.
    """
    raw = str(text or "")
    lowered = raw.lower()
    if any(marker in lowered for marker in _SENSITIVE_MARKERS):
        return ""
    raw = _EMAIL_RE.sub(" ", raw)
    raw = _URL_RE.sub(" ", raw)
    raw = _LONGNUM_RE.sub(" ", raw)
    return " ".join(_WORD_RE.findall(raw.lower()))


def discover_topics(context: list[str] | None, *, max_topics: int = 4) -> list[str]:
    """Deterministic evidence-guided topics from sanitized context.

    Only tokens present in the closed ``VETTED_INTERESTS`` vocabulary survive.
    A raw task title can therefore never become a public search query; private
    names, addresses, order IDs, emails and codes are structurally excluded.

    LIMIT: this is a lexicographic frequency heuristic over sanitized surface
    tokens intersected with a closed vocabulary, not semantic intelligence. It
    never claims otherwise; provenance is labelled ``deterministic-context``.
    """
    counts: dict[str, int] = {}
    order: list[str] = []
    for line in list(context or [])[:20]:
        for token in sanitize_topic_text(line).split():
            if token not in VETTED_INTERESTS or len(token) < 3:
                continue
            if token not in counts:
                counts[token] = 0
                order.append(token)
            counts[token] += 1
    ranked = sorted(order, key=lambda t: (-counts[t], order.index(t)))
    return ranked[: max(0, int(max_topics))]


# --------------------------------------------------------------------------- #
# Deterministic public-news relevance gates
# --------------------------------------------------------------------------- #
#
# A general business headline (earnings, equity moves, crime) must NOT become a
# candidate merely because it arrived on a vetted feed. These gates are closed
# token sets derived from the existing generic-interest vocabulary; they never
# learn from user text and never fabricate a new user-specific interest.

_TECH_RELEVANCE_TERMS: tuple[str, ...] = (
    "ai", "artificial intelligence", "machine learning", "deep learning",
    "neural", "llm", "large language model", "chatbot", "robot", "robotics",
    "drone", "software", "hardware", "chip", "chips", "semiconductor",
    "semiconductors", "microchip", "processor", "quantum", "computing",
    "computer", "supercomputer", "cyber", "cybersecurity", "cyberattack",
    "hacker", "hacking", "malware", "encryption", "privacy", "data centre",
    "data center", "algorithm", "algorithms", "technology", "technologies",
    "tech", "digital", "startup", "startups", "fintech", "blockchain",
    "cryptocurrency", "crypto", "cloud", "5g", "6g", "telecom",
    "telecommunications", "satellite", "spacecraft", "rocket",
    "electric vehicle", "electric vehicles", "battery", "batteries", "solar",
    "renewable", "smartphone", "smartphones", "gadget", "gadgets", "app",
    "apps", "gaming", "gamer", "esports", "internet", "online", "network",
    "networks", "broadband", "wifi", "biotech", "biotechnology", "genomics",
    "nanotech", "innovation", "research and development", "r&d", "engineer",
    "engineering", "developer", "programming", "coding", "open source",
    "opensource",
)

_CAMPUS_RELEVANCE_TERMS: tuple[str, ...] = (
    "student", "students", "undergraduate", "undergraduates", "postgraduate",
    "postgraduates", "scholarship", "scholarships", "bursary", "bursaries",
    "financial aid", "tuition", "internship", "internships", "career",
    "careers", "career fair", "job", "jobs", "employment", "recruitment",
    "admission", "admissions", "enrolment", "enrollment", "matriculation",
    "course", "courses", "module", "modules", "curriculum", "programme",
    "programmes", "program", "programs", "degree", "degrees", "major",
    "minors", "semester", "academic", "campus", "hall", "hostel", "dormitory",
    "student life", "orientation", "graduation", "convocation",
    "commencement", "alumni", "club", "clubs", "society", "societies",
    "student union", "sports", "athletics", "welfare", "counselling",
    "counseling", "health services", "library", "libraries", "hackathon",
    "competition", "competitions", "contest", "workshop", "workshops",
    "seminar", "seminars", "symposium", "conference", "conferences", "talk",
    "talks", "lecture", "lectures", "event", "events", "festival", "fest",
    "exhibition", "exchange", "study abroad", "overseas", "entrepreneurship",
    "incubator", "accelerator", "opportunity", "opportunities", "application",
    "applications", "apply", "award", "awards", "prize", "prizes", "medal",
    "medals", "research", "laboratory", "lab", "labs", "project", "projects",
    "grant", "grants", "fellowship", "fellowships", "initiative",
    "initiatives", "launch", "launches", "partnership", "partnerships",
    "collaboration", "community", "outreach", "volunteer", "volunteering",
    "mentorship", "mentoring", "talent", "training",
)

# Pure researcher-achievement markers: excluded for campus news UNLESS a strong
# technology term is also present (generic tech relevance overrides).
_RESEARCH_ACHIEVEMENT_MARKERS: tuple[str, ...] = (
    "professor", "professors", "researcher", "researchers", "scientist",
    "scientists", "faculty", "dean", "provost", "postdoctoral", "postdoc",
    "doctorate", "doctoral", "paper published", "published in", "journal",
    "highly cited", "citation", "citations", "h-index", "ranking", "ranked",
    "named fellow", "elected fellow", "best paper",
)

_TERM_BOUNDARY_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _term_present(text: str, term: str) -> bool:
    """Whole-word (non-alphanumeric boundary) term match on lower-cased text."""
    if not term:
        return False
    pattern = _TERM_BOUNDARY_RE_CACHE.get(term)
    if pattern is None:
        pattern = re.compile(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])")
        _TERM_BOUNDARY_RE_CACHE[term] = pattern
    return pattern.search(text) is not None


def _matches_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(_term_present(text, term) for term in terms)


def _news_class(source: Any) -> str:
    text = str(source or "").strip().lower()
    if text in {"campus", "university", "university_json", "www.university.example"}:
        return "campus"
    if text in {"technology", "tech", "cna", "cna_rss", "www.channelnewsasia.com"}:
        return "technology"
    return ""


def public_item_relevant(source: Any, title: Any, summary: Any = "", content_type: Any = "") -> bool:
    """Deterministic relevance gate for vetted public news feeds.

    - ``technology`` (CNA): the TITLE must carry a concrete technology term;
      generic business/crime headlines are rejected regardless of source.
    - ``campus`` (example university): student-life/opportunity/tech titles are kept; a pure
      researcher-achievement title is rejected unless a strong technology term
      is present. This is not a blanket campus-news drop.
    - any other source: unchanged (returns True) so existing feeds are unaffected.
    """
    cls = _news_class(source)
    if not cls:
        return True
    text = str(title or "").lower()
    if not text.strip():
        return False
    if cls == "technology":
        return _matches_any(text, _TECH_RELEVANCE_TERMS)
    if _matches_any(text, _TECH_RELEVANCE_TERMS):
        return True
    if _matches_any(text, _RESEARCH_ACHIEVEMENT_MARKERS):
        return False
    return _matches_any(text, _CAMPUS_RELEVANCE_TERMS)


def cna_title_relevant(title: Any) -> bool:
    return public_item_relevant("technology", title)


def university_title_relevant(title: Any) -> bool:
    return public_item_relevant("campus", title)


# --------------------------------------------------------------------------- #
# Bounded source registry (persistent, provenance + cadence + cache)
# --------------------------------------------------------------------------- #


def new_registry() -> dict[str, Any]:
    return {"schema": SCHEMA, "updated_at": "", "sources": {}}


def load_registry(store: Any | None) -> dict[str, Any]:
    if store is None:
        return new_registry()
    data = store.load("public_registry.json", new_registry())
    if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
        return new_registry()
    return data


def save_registry(store: Any | None, registry: dict[str, Any]) -> None:
    if store is None:
        return
    store.save("public_registry.json", registry)


def _source_id(kind: str, url: str) -> str:
    digest = hashlib.sha256(f"{kind}|{url}".encode("utf-8")).hexdigest()[:12]
    return f"{kind}:{digest}"


def registry_add(
    registry: dict[str, Any],
    *,
    kind: str,
    url: str,
    provenance: str,
    interest: str,
    now: datetime,
    cadence_minutes: int,
    tentative: bool = True,
    auto_added: bool = True,
    limit: int = DEFAULT_REGISTRY_LIMIT,
    generic_opt_in: bool = False,
) -> bool:
    """Add a validated public source, rotating out the oldest auto-added one.

    Only public Reddit/GitHub feeds are auto-added by default. Generic websites
    require an explicit policy opt-in. Returns False when rejected.
    """
    try:
        validate_public_url(url, generic_opt_in=generic_opt_in)
    except URLRejected:
        return False
    host = canonical_host(urllib.parse.urlsplit(url).hostname or "")
    if not auto_added_ok(host, generic_opt_in=generic_opt_in):
        return False
    sid = _source_id(kind, url)
    if sid in registry["sources"]:
        return True
    live_count = sum(1 for rec in registry["sources"].values()
                     if isinstance(rec, dict) and not rec.get("parked"))
    if live_count >= max(1, int(limit)):
        # Rotate: drop the oldest non-seed auto-added source, if any. Parked
        # lineage records never count toward capacity and are never removed.
        candidates = [
            (key, rec) for key, rec in registry["sources"].items()
            if rec.get("auto_added") and not rec.get("seed") and not rec.get("parked")
        ]
        if not candidates:
            return False
        oldest = min(candidates, key=lambda kv: str(kv[1].get("added_at", "")))[0]
        registry["sources"].pop(oldest, None)
    registry["sources"][sid] = {
        "id": sid,
        "kind": kind,
        "url": url,
        "host": host,
        "provenance": provenance,
        "interest": interest,
        "tentative": bool(tentative),
        "auto_added": bool(auto_added),
        "cadence_minutes": max(1, int(cadence_minutes)),
        "added_at": now.isoformat(),
        "last_fetch_at": "",
        "etag": "",
        "last_modified": "",
        "cache_at": "",
        "cache_items": [],
        "failures": 0,
    }
    return True


def auto_added_ok(host: str, *, generic_opt_in: bool = False) -> bool:
    # Vetted fixed public hosts (GitHub/Reddit + the two official news feeds) are
    # allowed without the generic URL opt-in. Arbitrary hosts still require it.
    if (host in GITHUB_HOSTS or host in REDDIT_HOSTS or host in VETTED_NEWS_HOSTS
            or host in VETTED_FIXED_PUBLIC_HOSTS):
        return True
    return bool(generic_opt_in)


def ensure_seed_sources(registry: dict[str, Any], policy: dict[str, Any], now: datetime) -> None:
    """Install policy-declared seeds. Seeds are provenance-labelled/tentative."""
    for seed in list(policy.get("feed_seed_sources", []))[:12]:
        if not isinstance(seed, dict):
            continue
        registry_add(
            registry,
            kind=str(seed.get("kind") or "rss"),
            url=str(seed.get("url") or ""),
            provenance=str(seed.get("provenance") or "configured seed (tentative)"),
            interest=str(seed.get("interest") or "seed"),
            now=now,
            cadence_minutes=int(seed.get("cadence_minutes") or policy.get("feed_cadence_minutes", 60)),
            tentative=True,
            auto_added=False,
            limit=int(policy.get("max_public_registry", DEFAULT_REGISTRY_LIMIT)),
            generic_opt_in=bool(policy.get("generic_websites_opt_in", False)),
        )
        sid = _source_id(str(seed.get("kind") or "rss"), str(seed.get("url") or ""))
        if sid in registry["sources"]:
            registry["sources"][sid]["seed"] = True
            registry["sources"][sid]["provenance"] = str(seed.get("provenance") or "configured seed (tentative)")


def ensure_discovery_sources(
    registry: dict[str, Any], topics: list[str], policy: dict[str, Any], now: datetime
) -> None:
    """Rotate in bounded Reddit/GitHub public search feeds for sanitized topics."""
    cap = int(policy.get("discovery_topics_cap", 4))
    cadence = int(policy.get("discovery_cadence_minutes", 360))
    for topic in topics[:cap]:
        if not topic:
            continue
        quoted = urllib.parse.quote(topic)
        registry_add(
            registry,
            kind="reddit_search",
            url=f"https://www.reddit.com/search.rss?q={quoted}&sort=new&t=week",
            provenance="deterministic-context sanitized topic; not model intelligence",
            interest=topic,
            now=now,
            cadence_minutes=max(REDDIT_CADENCE_MINUTES, cadence),
            limit=int(policy.get("max_public_registry", DEFAULT_REGISTRY_LIMIT)),
            generic_opt_in=False,
        )
        registry_add(
            registry,
            kind="github_search",
            url=f"https://api.github.com/search/repositories?q={quoted}+in%3Aname%2Cdescription&sort=updated",
            provenance="deterministic-context sanitized topic; not model intelligence",
            interest=topic,
            now=now,
            cadence_minutes=max(60, cadence),
            limit=int(policy.get("max_public_registry", DEFAULT_REGISTRY_LIMIT)),
            generic_opt_in=False,
        )


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #


class RedditLedger:
    """One shared per-cycle Reddit allowance across ALL public acquisition paths.

    Ordinary feeds and adaptive evidence draw on the same ledger so a cycle can
    never issue one ordinary Reddit request PLUS one adaptive Reddit request.
    """

    def __init__(self, limit: int = 1):
        self.limit = max(0, int(limit))
        self.used = 0

    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def take(self, want: int = 1) -> int:
        got = min(self.remaining(), max(0, int(want)))
        self.used += got
        return got


def _record_key(rec: dict[str, Any]) -> str:
    return str(rec.get("id") or rec.get("url") or "")


def _stable_due_order(full_sources: list[dict[str, Any]],
                      due: list[dict[str, Any]], cursor_id: Any,
                      fallback: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rotate after the last-examined SOURCE ID in stable full-registry order.

    A numeric index into the filtered due list aliases whenever cadence removes
    an earlier row. Identity keeps progression stable across such changes; the
    old numeric cursor remains the compatibility fallback when the id is absent.
    """
    if not due:
        return []
    key = str(cursor_id or "")
    if key:
        full_keys = [_record_key(rec) for rec in full_sources]
        if key in full_keys:
            pos = full_keys.index(key)
            due_keys = {_record_key(rec) for rec in due}
            rotated = full_sources[pos + 1:] + full_sources[:pos + 1]
            ordered = [rec for rec in rotated if _record_key(rec) in due_keys]
            if ordered:
                return ordered
    return fallback


def _age_minutes(now: datetime, iso: str) -> int:
    if not iso:
        return 0
    try:
        parsed = datetime.fromisoformat(str(iso))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, int((now - parsed).total_seconds() // 60))


_STRUCTURED_FEED_CATEGORIES = {"weather_forecast", "planned_transport", "opportunity"}


def _structured_validity_unsafe(item: dict[str, Any], now: datetime) -> str:
    """Return a non-empty reason when a structured cached item is unsafe to serve.

    Reviewed forecast/planned-notice records carry an explicit validity window.
    When such a record is carried from cache (cadence / source cap / shared
    budget) without a fresh fetch, the window must be re-checked against the
    current clock: an expired or malformed window is not healthy evidence.
    Returns ``""`` for generic (non-structured) cache so RSS/other carry is
    unaffected.
    """
    category = str(item.get("category") or "")
    is_forecast = category == "weather_forecast" or bool(item.get("forecast_only"))
    is_planned = category == "planned_transport" or bool(item.get("planned_notice"))
    is_opportunity = category == "opportunity" or bool(item.get("opportunity_only"))
    if not (is_forecast or is_planned or is_opportunity):
        return ""
    valid_from = str(item.get("valid_from") or "").strip()
    valid_until = str(item.get("valid_until") or "").strip()
    current = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    if is_opportunity:
        # A single-deadline opportunity legitimately has NO stated opening date,
        # so only the close bound is required; a start is never invented. The
        # close bound may be a precise timezone-bearing timestamp (explicit local
        # closing time) or a date-only deadline (closed on the deadline day).
        if not valid_until:
            return "structured cache validity missing"
        try:
            end = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
        except ValueError:
            return "structured cache validity invalid"
        if end.tzinfo is not None:
            if current >= end:
                return "structured cache expired"
            return ""
        if current.date() >= end.date():
            return "structured cache expired"
        return ""
    if not valid_from or not valid_until:
        return "structured cache validity missing"
    try:
        start = datetime.fromisoformat(valid_from.replace("Z", "+00:00"))
        end = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
    except ValueError:
        return "structured cache validity invalid"
    if is_forecast:
        # A forecast window is a precise timestamp: comparing an aware clock
        # against a naive/ambiguous bound is unsafe.
        if start.tzinfo is None or end.tzinfo is None:
            return "structured cache validity timezone missing"
        if end <= start:
            return "structured cache validity ambiguous"
        if current >= end:
            return "structured cache expired"
        return ""
    # Planned notice: a date-only window retained through the end calendar day.
    if end.date() < start.date():
        return "structured cache validity ambiguous"
    if current.date() > end.date():
        return "structured cache expired"
    return ""


def _usable_cache_items(items: list[Any], now: datetime) -> tuple[list[dict[str, Any]], str]:
    """Partition cached items into (usable, first_unsafe_reason).

    Any structured item whose validity is expired/missing/invalid/ambiguous makes
    the whole structured source unavailable (its cache is not served), while
    generic items are always usable.
    """
    usable: list[dict[str, Any]] = []
    unsafe = ""
    for item in items:
        if not isinstance(item, dict):
            continue
        reason = _structured_validity_unsafe(item, now)
        if reason:
            unsafe = unsafe or reason
            continue
        usable.append(item)
    return usable, unsafe


def _cached_result(rec: dict[str, Any], now: datetime, name: str, *, reason: str) -> SourceResult:
    cache_at = str(rec.get("cache_at") or "")
    items = list(rec.get("cache_items") or [])
    if not cache_at:
        # Never polled / never observed: there is NO cache to serve. Reporting a
        # fresh ``ok`` check with a fabricated ``checked_at`` (or ``cached=True``)
        # would claim a healthy check that never happened, so this is an explicit
        # partial carry-forward with no observation time.
        return SourceResult(
            name, "ok", count=0, items=[], checked_at="",
            truncated=True, cached=False, cache_at="", cache_age_minutes=0,
            failure=reason,
        )
    usable, unsafe = _usable_cache_items(items, now)
    if unsafe:
        # A previously healthy structured cache that has since expired (or whose
        # validity became unusable) must not be carried as a healthy candidate;
        # this is a LOCAL validity check against the true observation time, not a
        # fresh fetch.
        return SourceResult(
            name, "stale", count=0, items=[], checked_at=cache_at,
            truncated=False, cached=True, cache_at=cache_at,
            cache_age_minutes=_age_minutes(now, cache_at), failure=unsafe,
        )
    age = _age_minutes(now, cache_at)
    return SourceResult(
        name, "ok", count=len(usable), items=usable, checked_at=cache_at,
        truncated=False, cached=True, cache_at=cache_at, cache_age_minutes=age, failure=reason,
    )


def _collect_one(
    rec: dict[str, Any],
    *,
    policy: dict[str, Any],
    clock: Any,
    now: datetime,
    fetcher: Callable[..., FetchResponse],
    reddit_budget: int,
    reddit_ledger: Any = None,
    renderer: Callable[[str], Any] | None = None,
    firecrawl: Any = None,
    credentialed_fetcher: Callable[..., FetchResponse] | None = None,
    transport_gate: Any = None,
) -> tuple[SourceResult, int]:
    sid = str(rec.get("id") or "")
    kind = str(rec.get("kind") or "rss")
    name = f"feed:{sid}"
    cadence = max(1, int(rec.get("cadence_minutes") or policy.get("feed_cadence_minutes", 60)))
    since = _age_minutes(now, str(rec.get("last_fetch_at") or ""))
    host = canonical_host(rec.get("host") or "")
    is_reddit = host in REDDIT_HOSTS
    used = 0

    stale_reason = str(rec.get("stale_reason") or "")
    if stale_reason and rec.get("last_fetch_at") and since < cadence:
        cache_at = str(rec.get("cache_at") or "")
        return SourceResult(name, "stale", count=0, items=[], checked_at=cache_at,
                            cached=bool(cache_at), cache_at=cache_at,
                            failure=stale_reason, attempted=False), used

    if rec.get("last_fetch_at") and since < cadence:
        res = _cached_result(rec, now, name, reason=f"cadence ({cadence}m)")
        res.attempted = False
        return res, used
    # One shared per-cycle Reddit allowance across ordinary + adaptive acquisition
    # (never 1 ordinary PLUS 1 adaptive). Falls back to the legacy local budget
    # when no shared ledger is supplied.
    reddit_left = reddit_ledger.remaining() if reddit_ledger is not None else int(reddit_budget)
    if is_reddit and reddit_left <= 0:
        res = _cached_result(rec, now, name, reason="reddit per-cycle limit (1)")
        res.attempted = False
        return res, used

    # A dynamically discovered source is fetched only because the discovery
    # layer already vetted it (public https host, all-resolved-IP public, no
    # credentials/secrets). The flag is set only by that layer, never from feed
    # content, and it relaxes ONLY the static host allowlist -- every other URL
    # safety check below is unchanged.
    vetted_opt_in = bool(policy.get("generic_websites_opt_in", False)) or bool(rec.get("vetted_public"))
    try:
        url = validate_public_url(
            str(rec.get("url") or ""),
            generic_opt_in=vetted_opt_in,
        ).geturl()
    except URLRejected as exc:
        return SourceResult(name, "error", error=f"url rejected: {exc}", checked_at=now.isoformat(), attempted=False), used

    dispatch = policy.get("_source_dispatch")
    if dispatch is not None and not dispatch(name):
        return SourceResult(name, "deferred", failure="source-health cooldown", attempted=False), used

    # Consume the single per-cycle Reddit slot BEFORE any attempt, so a Reddit
    # failure (timeout/429/parse) can never allow a second Reddit request.
    if is_reddit:
        used = 1
        if reddit_ledger is not None:
            reddit_ledger.take(1)
        rec["last_fetch_at"] = now.isoformat()

    lta_decision = None
    try:
        if kind == LTA_TRAIN_ALERTS_KIND:
            # The credentialed API must NOT be polled on a fixed cadence (owner
            # directive). An agentic gate (candidate pre-filter + auxiliary model
            # lane verdict + persisted hard floors) must permit each fetch. With
            # no gate configured the source is skipped, never polled
            # unconditionally. The ONE credentialed source still uses its own
            # isolated, explicitly named path (never ``fetch_bytes``, never the
            # generic fetcher); tests inject ``credentialed_fetcher``.
            if transport_gate is None:
                return SourceResult(name, "deferred",
                                    failure="transport gate not configured",
                                    attempted=False), used
            lta_decision = transport_gate.decide(rec=rec, now=now)
            if not getattr(lta_decision, "allow", False):
                label = (lta_decision.to_source_failure()
                         if hasattr(lta_decision, "to_source_failure")
                         else "transport gate skipped")
                return SourceResult(name, "deferred", failure=label[:200],
                                    attempted=False), used
            cred_fetch = credentialed_fetcher or fetch_lta_train_alerts
            response = cred_fetch(
                timeout=int(policy.get("feed_timeout_seconds", DEFAULT_TIMEOUT)),
                max_bytes=int(policy.get("feed_max_bytes", DEFAULT_MAX_BYTES)),
            )
        else:
            response = fetcher(
                url,
                allowed_hosts=None,
                generic_opt_in=vetted_opt_in,
                timeout=int(policy.get("feed_timeout_seconds", DEFAULT_TIMEOUT)),
                max_bytes=int(policy.get("feed_max_bytes", DEFAULT_MAX_BYTES)),
                etag=str(rec.get("etag") or ""),
                last_modified=str(rec.get("last_modified") or ""),
            )
    except URLRejected:
        return SourceResult(name, "error", error="feed url rejected", checked_at=now.isoformat()), used
    except CredentialMissing as exc:
        # Fail closed with the non-secret env-var diagnostic. No key value, no
        # key length and no raw exception text are surfaced.
        label = str(exc)
        observe = policy.get("_source_observe")
        if observe is not None:
            observe(SourceResult(name, "error", error=label,
                                 checked_at=now.isoformat(), attempted=False))
        rec["last_fetch_at"] = now.isoformat()
        rec["failures"] = int(rec.get("failures", 0)) + 1
        rec["stale_reason"] = label
        if rec.get("cache_items"):
            res = _cached_result(rec, now, name, reason=label)
            res.status = "stale"
            return res, used
        return SourceResult(name, "error", error=label, checked_at=now.isoformat(),
                            failure=label, attempted=False), used
    except FeedError as exc:
        observe = policy.get("_source_observe")
        if observe is not None:
            observe(SourceResult(name, "error", error=safe_source_error(exc, kind="feed"),
                                 checked_at=now.isoformat(), attempted=True))
        rec["last_fetch_at"] = now.isoformat()
        rec["failures"] = int(rec.get("failures", 0)) + 1
        rec["stale_reason"] = "feed fetch failed"
        if rec.get("cache_items"):
            res = _cached_result(rec, now, name, reason="feed fetch failed")
            res.status = "stale"
            return res, used
        return SourceResult(name, "error", error="feed fetch failed", checked_at=now.isoformat(),
                            failure="feed fetch failed"), used

    rec["last_fetch_at"] = now.isoformat()
    if response.status == 304:
        # Not modified: evidence is byte-identical, so its content version must
        # not churn. The source is nevertheless healthily re-checked now, so the
        # health ``checked_at`` is refreshed while ``cache_at`` stays the true
        # observation time of the unchanged body.
        if rec.get("stale_reason"):
            cache_at = str(rec.get("cache_at") or "")
            return SourceResult(name, "stale", count=0, items=[], checked_at=cache_at,
                                cached=bool(cache_at), cache_at=cache_at,
                                failure=str(rec["stale_reason"])), used
        res = _cached_result(rec, now, name, reason="not modified (304)")
        res.checked_at = now.isoformat()
        return res, used
    if response.status == 429:
        # Record the real attempted failure separately from any served cache.
        # The health circuit owns the deadline even when cadence is shorter.
        retry = str(next((v for k, v in response.headers.items()
                          if str(k).lower() == "retry-after"), ""))
        seconds = 0
        try:
            seconds = max(0, min(int(retry), 86400))
        except ValueError:
            try:
                from email.utils import parsedate_to_datetime
                seconds = max(0, min(int((parsedate_to_datetime(retry) - now).total_seconds()), 86400))
            except (ValueError, TypeError, OverflowError):
                pass
        observe = policy.get("_source_observe")
        if observe is not None:
            observe(SourceResult(name, "error", checked_at=now.isoformat(), attempted=True,
                                 error="429 rate limited" + (f"; Retry-After: {seconds}" if seconds else "")))
        # Never retry or bypass; keep cache; enforce cadence via last_fetch_at.
        rec["failures"] = int(rec.get("failures", 0)) + 1
        rec["stale_reason"] = "rate limited (429)"
        res = _cached_result(rec, now, name, reason="rate limited (429)")
        res.status = "stale"
        return res, used
    if response.status < 200 or response.status >= 300:
        observe = policy.get("_source_observe")
        if observe is not None:
            observe(SourceResult(name, "error", error=f"http {response.status}",
                                 checked_at=now.isoformat(), attempted=True))
        rec["failures"] = int(rec.get("failures", 0)) + 1
        rec["stale_reason"] = f"http {response.status}"
        if rec.get("cache_items"):
            res = _cached_result(rec, now, name, reason=f"http {response.status}")
            res.status = "stale"
            return res, used
        return SourceResult(name, "error", error=f"http {response.status}", checked_at=now.isoformat(),
                            failure=f"http {response.status}"), used

    if rec.get("discovered") or kind == LTA_TRAIN_ALERTS_KIND:
        # A body must match its declared route before parse. A clearly mismatched
        # type (binary body, or HTML served for a JSON route) fails closed and is
        # never recorded as healthy. The credentialed JSON source is always
        # checked (it is a seed, not a discovered source).
        ctype = normalized_content_type(response.headers)
        if not content_type_allowed(kind, ctype):
            rec["failures"] = int(rec.get("failures", 0)) + 1
            label = f"content-type mismatch ({ctype or 'unknown'})"
            if rec.get("cache_items"):
                res = _cached_result(rec, now, name, reason=label)
                res.status = "stale"
                return res, used
            return SourceResult(name, "error", error=label, checked_at=now.isoformat(),
                                failure=label), used

    try:
        # V0.4.0 routed fallback: a 200 JavaScript shell on a PUBLIC page may be
        # rendered by an opt-in, consent-gated, injected approved renderer. A
        # challenge/interstitial/paywall/login page (`blocked`) is terminal and is
        # NEVER rendered, bypassed, or treated as evidence. Defaults (renderer
        # None, flags off) leave the direct-transport byte path unchanged.
        parse_body = response.body
        parse_kind = kind
        if (renderer is not None
                and kind in {"web_page", "university_opportunity"}
                and bool(policy.get("native_render_fallback_enabled", False))
                and bool(policy.get("native_render_consent", False))
                and classify_web_page(parse_body) == PAGE_DYNAMIC):
            try:
                from .routed_retrieval import render_js_shell
                rendered = render_js_shell(
                    url, renderer=renderer, policy=policy,
                    timeout=int(policy.get("feed_timeout_seconds", DEFAULT_TIMEOUT)),
                    max_bytes=int(policy.get("retrieval_fallback_max_bytes", DEFAULT_MAX_BYTES)),
                )
            except Exception:  # noqa: BLE001 - failed fallback keeps the honest failure
                rendered = b""
            if rendered:
                parse_body = rendered
        # V0.4.0 metered Firecrawl/native-MCP fallback: attempted ONLY when the
        # direct fetch (and any approved local render) still shows a JavaScript
        # shell on a PUBLIC page. The adapter itself enforces enable/consent,
        # configured server/tool, the persisted per-cycle/day attempt ceilings,
        # concurrency and the provider 429 cooldown. A `blocked` challenge or a
        # private/rejected URL is never eligible here and never consumes budget.
        if (firecrawl is not None
                and kind in {"web_page", "university_opportunity"}
                and classify_web_page(parse_body) == PAGE_DYNAMIC):
            try:
                scraped = firecrawl.scrape(
                    url,
                    timeout=int(policy.get("feed_timeout_seconds", DEFAULT_TIMEOUT)),
                    max_bytes=int(policy.get("retrieval_fallback_max_bytes", DEFAULT_MAX_BYTES)),
                    now=now,
                )
            except Exception:  # noqa: BLE001 - failed fallback keeps the honest failure
                scraped = b""
            if scraped:
                parse_body = scraped
                if kind == "web_page":
                    # Firecrawl returns markdown, not HTML: parse it as bounded
                    # markdown evidence rather than pretending it is a web page.
                    parse_kind = "markdown_page"
        parsed = parse_source(parse_body, parse_kind,
                              max_items=int(policy.get("feed_max_items", DEFAULT_MAX_ITEMS)),
                              now=now)
    except FeedExpired:
        rec["failures"] = int(rec.get("failures", 0)) + 1
        rec["stale_reason"] = "feed expired/stale"
        if rec.get("cache_items"):
            res = _cached_result(rec, now, name, reason=rec["stale_reason"])
            res.status = "stale"
            return res, used
        return SourceResult(name, "stale", error=rec["stale_reason"],
                            checked_at=now.isoformat(), failure=rec["stale_reason"]), used
    except FeedAPIError:
        rec["failures"] = int(rec.get("failures", 0)) + 1
        rec["stale_reason"] = "feed API error"
        if rec.get("cache_items"):
            res = _cached_result(rec, now, name, reason=rec["stale_reason"])
            res.status = "stale"
            return res, used
        return SourceResult(name, "error", error=rec["stale_reason"],
                            checked_at=now.isoformat(), failure=rec["stale_reason"]), used
    except WebPageUnavailable as exc:
        # A 200 challenge/error skeleton is NOT content: report its distinct
        # classification and never fall through to a candidate or healthy empty.
        rec["failures"] = int(rec.get("failures", 0)) + 1
        label = f"web page {exc.classification}"
        rec["stale_reason"] = label
        if rec.get("cache_items"):
            res = _cached_result(rec, now, name, reason=label)
            res.status = "stale"
            return res, used
        return SourceResult(name, "error", error=label, checked_at=now.isoformat(),
                            failure=label), used
    except FeedError:
        rec["failures"] = int(rec.get("failures", 0)) + 1
        cap = int(policy.get("feed_max_bytes", DEFAULT_MAX_BYTES))
        label = "feed parse error" + (" (body cap exceeded)" if len(response.body) >= cap else "")
        rec["stale_reason"] = label
        if rec.get("cache_items"):
            res = _cached_result(rec, now, name, reason=label)
            res.status = "stale"
            return res, used
        return SourceResult(name, "error", error=label[:200], checked_at=now.isoformat(),
                            failure=label[:200]), used

    # Deterministic relevance gate: a generic headline must not become a
    # candidate merely because it arrived on a vetted news feed. Non-news sources
    # are unaffected (public_item_relevant returns True for unknown classes).
    item_class = _news_class(host) or _news_class(kind)
    if item_class and kind != "university_opportunity":
        parsed = [
            row for row in parsed
            if public_item_relevant(item_class, row.get("title"), row.get("summary"),
                                    row.get("content_type"))
        ]

    items = []
    for row in parsed:
        link = str(row.get("link") or "")
        if host in ARTICLE_HOSTS:
            # Display-only: normalize/validate the link; never fetch it.
            link = safe_article_link(UNIVERSITY_BASE, link)
        rid = str(row.get("id") or "").strip() or hashlib.sha256(
            str(link or row.get("title") or "").encode("utf-8")
        ).hexdigest()[:16]
        items.append({
            "ref": f"feed:{sid}:{rid}",
            "id": rid,
            "title": str(row.get("title") or "")[:200],
            "subject": str(row.get("title") or "")[:200],
            "summary": str(row.get("summary") or "")[:400],
            "link": link,
            "date": str(row.get("published") or ""),
            "interest": str(rec.get("interest") or ""),
            "provenance": str(rec.get("provenance") or ""),
            "tentative": bool(rec.get("tentative", True)),
            "source": sid,
            # Explicit date precision: date-only example-university evidence is labelled so it
            # is never mistaken for an exact timestamp.
            "date_precision": str(row.get("date_precision") or ""),
            "content_type": str(row.get("content_type") or ""),
            "category": str(row.get("category") or ""),
            "publisher": str(row.get("publisher") or ""),
            "valid_from": str(row.get("valid_from") or ""),
            "valid_until": str(row.get("valid_until") or ""),
            "affected_lines": list(row.get("affected_lines") or [])[:8],
            "forecast_only": bool(row.get("forecast_only", False)),
            "planned_notice": bool(row.get("planned_notice", False)),
            "opportunity_only": bool(row.get("opportunity_only", False)),
            "deadline": str(row.get("deadline") or ""),
            "deadline_precision": str(row.get("deadline_precision") or ""),
            "closing_time_known": bool(row.get("closing_time_known", False)),
            "audience": str(row.get("audience") or "")[:300],
            "audience_declared": bool(row.get("audience_declared", False)),
            "personal_qualification": False,
            # Live transport service status (LTA train alerts): the all-clear
            # marker and disruption items both carry these bounded flags so a
            # standing disruption can be resolved downstream.
            "service_status": str(row.get("service_status") or ""),
            "live_service_alert": bool(row.get("live_service_alert", False)),
            "all_clear": bool(row.get("all_clear", False)),
            "recover_message": bool(row.get("recover_message", False)),
        })
    # Refresh cache.
    rec["cache_items"] = items
    rec["cache_at"] = now.isoformat()
    rec["etag"] = str(response.headers.get("ETag") or response.headers.get("etag") or "")
    rec["last_modified"] = str(response.headers.get("Last-Modified") or response.headers.get("last-modified") or "")
    rec["failures"] = 0
    rec.pop("stale_reason", None)
    if kind == LTA_TRAIN_ALERTS_KIND and transport_gate is not None and lta_decision is not None:
        # Bounded telemetry: whether an actual disruption was found. Never fatal.
        try:
            transport_gate.record_outcome(
                decision=lta_decision, now=now,
                disruption_found=any(not bool(i.get("all_clear")) for i in items))
        except Exception:  # noqa: BLE001 - telemetry must never break a run
            pass
    return SourceResult(name, "ok", count=len(items), items=items, checked_at=now.isoformat(),
                        truncated=bool(len(parsed) >= int(policy.get("feed_max_items", DEFAULT_MAX_ITEMS)))), used


def collect_public_feeds(
    policy: dict[str, Any],
    clock: Any,
    *,
    store: Any | None = None,
    context: list[str] | None = None,
    fetcher: Callable[..., FetchResponse] | None = None,
    now: datetime | None = None,
    budget: Any | None = None,
    attempt_ledger: Any | None = None,
    reddit_ledger: Any | None = None,
    renderer: Callable[[str], Any] | None = None,
    firecrawl: Any = None,
    credentialed_fetcher: Callable[..., FetchResponse] | None = None,
    transport_gate: Any = None,
    search_bridge: Any = None,
    search_budget: Any = None,
    overall_budget: Any = None,
    maintenance_decider: Any = None,
    resolver: Callable[[str], list[str]] | None = None,
    maintenance_context: list[str] | None = None,
    maintenance_privacy_verifier: Any = None,
) -> list[SourceResult]:
    """Collect bounded public feeds. Never raises; failures are labelled.

    At most ``feed_max_sources_per_run`` sources are attempted per run
    (default 2) and at most one Reddit request, so the registry can never cost
    ``12 x timeout`` in a single scheduled run. A persisted cursor rotates which
    due sources are attempted, guaranteeing eventual coverage without a burst.

    ``attempt_ledger`` (when supplied by the main integration) is the ONE shared
    source-attempt allowance for this cycle; ordinary feeds draw their attempts
    from it so the COMBINED ordinary+adaptive attempts never exceed
    ``feed_max_sources_per_run``. Standalone callers pass ``None`` and keep the
    ordinary-only cap unchanged.
    """
    if not bool(policy.get("feeds_enabled", True)):
        return []
    now = now or clock.now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if fetcher is None:
        # Ordinary feeds use the existing DNS-pinned TLS transport by default;
        # tests continue to inject local fake fetchers.
        from .publictransport import PinnedHTTPSTransport
        fetcher = PinnedHTTPSTransport().fetch
    registry = load_registry(store)
    before = copy.deepcopy(registry)
    ensure_seed_sources(registry, policy, now)
    topics = discover_topics(context, max_topics=int(policy.get("discovery_topics_cap", 4)))
    ensure_discovery_sources(registry, topics, policy, now)
    limit = int(policy.get("max_public_registry", DEFAULT_REGISTRY_LIMIT))
    sources = [rec for rec in registry["sources"].values()
               if not rec.get("parked")][:limit]

    # Only sources whose cadence has elapsed are "due"; rotate across them.
    due: list[dict[str, Any]] = []
    not_due: list[dict[str, Any]] = []
    for rec in sources:
        if (_age_minutes(now, str(rec.get("last_fetch_at") or "")) >= int(rec.get("cadence_minutes") or 1)
                or not rec.get("last_fetch_at")):
            due.append(rec)
        else:
            not_due.append(rec)
    per_run = max(1, int(policy.get("feed_max_sources_per_run", 2)))
    if attempt_ledger is not None:
        # Draw only from this path's reserved share. Skipped rows do not consume
        # quota; unused spare/borrowed share still preserves the combined total.
        per_run = min(per_run, attempt_ledger.remaining_for("ordinary"))
    cursor = int(registry.get("feed_cursor", 0)) % max(1, len(due)) if due else 0
    fallback_order = due[cursor:] + due[:cursor]
    ordered = _stable_due_order(sources, due, registry.get("feed_cursor_id"),
                                fallback_order)

    def _deferred_result(rec: dict[str, Any], reason: str) -> SourceResult:
        name = f"feed:{rec.get('id','unknown')}"
        cache_at = str(rec.get("cache_at") or "")
        if rec.get("stale_reason"):
            res = SourceResult(name, "stale", count=0, items=[], checked_at=cache_at,
                               cached=bool(cache_at), cache_at=cache_at,
                               cache_age_minutes=_age_minutes(now, cache_at) if cache_at else 0,
                               failure=str(rec["stale_reason"]))
        elif cache_at:
            _usable, unsafe = _usable_cache_items(rec.get("cache_items") or [], now)
            if unsafe:
                res = SourceResult(name, "stale", count=0, items=[], checked_at=cache_at,
                                   cached=True, cache_at=cache_at,
                                   cache_age_minutes=_age_minutes(now, cache_at),
                                   failure=unsafe)
            else:
                res = SourceResult(name, "ok",
                                   count=len(rec.get("cache_items") or []),
                                   items=list(rec.get("cache_items") or []),
                                   checked_at=cache_at, truncated=True, cached=True,
                                   cache_at=cache_at, failure=reason)
        else:
            res = SourceResult(name, "ok", count=0, items=[], checked_at="",
                               truncated=True, cached=False, cache_at="", failure=reason)
        res.attempted = False
        return res

    results: list[SourceResult] = []
    reddit_budget = 1  # conservative: at most one Reddit request per run
    examined = 0
    actual = 0
    budget_deferred = False
    for rec in ordered:
        if actual >= per_run:
            break
        if budget is not None and budget.remaining() < MIN_ATTEMPT_SECONDS:
            budget_deferred = True
            break
        examined += 1
        if budget is not None:
            rec_cap = min(int(policy.get("feed_timeout_seconds", DEFAULT_TIMEOUT)),
                          max(1, int(budget.remaining())))
            policy = dict(policy)
            policy["feed_timeout_seconds"] = rec_cap
        try:
            res, used = _collect_one(rec, policy=policy, clock=clock, now=now,
                                     fetcher=fetcher, reddit_budget=reddit_budget,
                                     reddit_ledger=reddit_ledger, renderer=renderer,
                                     firecrawl=firecrawl,
                                     credentialed_fetcher=credentialed_fetcher,
                                     transport_gate=transport_gate)
        except Exception as exc:  # noqa: BLE001 - one bad source never breaks the run
            res = SourceResult(f"feed:{rec.get('id','unknown')}", "error",
                               error=safe_source_error(exc, kind="feed"), checked_at=now.isoformat())
            used = 0
        if used:
            reddit_budget = 0
        if res.attempted is False:
            # Cadence/cooldown/reddit/transport/budget skips advance examination
            # progress but are NEVER charged as source attempts.
            results.append(res)
            continue
        if attempt_ledger is not None:
            attempt_ledger.take(1, path="ordinary")
        actual += 1
        results.append(res)
    new_cursor = ((cursor + examined) % max(1, len(due))) if due else 0
    if registry.get("feed_cursor") != new_cursor:
        registry["feed_cursor"] = new_cursor
    if examined:
        last_key = _record_key(ordered[examined - 1])
        if registry.get("feed_cursor_id") != last_key:
            registry["feed_cursor_id"] = last_key
    for rec in ordered[examined:]:
        results.append(_deferred_result(
            rec, "budget deferred before attempt" if budget_deferred
            else "deferred by per-run source cap"))
    # Sources still inside their cadence are not polled, but their last known
    # cache is carried forward so downstream coverage/evidence stays complete.
    # This is NOT a fresh check: ``checked_at`` is the true observation time.
    for rec in not_due:
        name = f"feed:{rec.get('id','unknown')}"
        if rec.get("stale_reason"):
            # A source that failed (expired/API/rate-limit/http) on its last
            # actual attempt must remain stale across its cadence window. Serving
            # the old cache as ``ok`` here is exactly the defect where an expired
            # status silently reverts to healthy on the next pass.
            cache_at = str(rec.get("cache_at") or "")
            results.append(SourceResult(name, "stale", count=0, items=[],
                                        checked_at=cache_at, cached=bool(cache_at),
                                        cache_at=cache_at,
                                        cache_age_minutes=_age_minutes(now, cache_at) if cache_at else 0,
                                        failure=str(rec["stale_reason"])))
            continue
        if not rec.get("cache_at"):
            continue
        results.append(_cached_result(rec, now, name,
                                      reason=f"cadence ({int(rec.get('cadence_minutes') or 1)}m)"))
    # Registered-feed cache is retained at most public_findings_retention_days.
    retention_minutes = int(policy.get("public_findings_retention_days", 30)) * 24 * 60
    for rec in registry["sources"].values():
        if rec.get("cache_at") and _age_minutes(now, rec["cache_at"]) > retention_minutes:
            rec["cache_items"] = []
            rec["cache_at"] = ""
    # Scheduled public maintenance seam: a persistently defective ordinary
    # source may be replaced with a freshly verified equivalent from the SAME
    # interest. This runs only inside the production collection path, uses the
    # remaining shared public budget/attempt allowance, and never triggers on a
    # transient/rate-limit/deferred observation (see source_maintenance).
    from .source_maintenance import attempt_replacements, maintenance_enabled
    if maintenance_enabled(policy):
        def _can_attempt() -> bool:
            if overall_budget is not None and overall_budget.remaining() <= MIN_ATTEMPT_SECONDS:
                return False
            if budget is not None and budget.remaining() <= MIN_ATTEMPT_SECONDS:
                return False
            if attempt_ledger is not None and attempt_ledger.remaining_for("ordinary") <= 0:
                return False
            return True

        def _probe(kind, url, interest):
            if not _can_attempt():
                raise RuntimeError("maintenance budget deferred")
            probe_policy = dict(policy)
            remaining = float(budget.remaining()) if budget is not None else 30.0
            if overall_budget is not None:
                remaining = min(remaining, float(overall_budget.remaining()))
            probe_policy["feed_timeout_seconds"] = max(
                1, int(min(int(policy.get("feed_timeout_seconds", DEFAULT_TIMEOUT)),
                           remaining)))
            probe_rec = {
                "id": f"replacement-probe:{hashlib.sha256(url.encode()).hexdigest()[:8]}",
                "kind": kind, "url": url, "interest": interest,
                "host": canonical_host(urllib.parse.urlsplit(url).hostname or ""),
                "cadence_minutes": 360, "vetted_public": True,
                "provenance": "automatic replacement probe",
            }
            result, _used = _collect_one(
                probe_rec, policy=probe_policy, clock=clock, now=now, fetcher=fetcher,
                reddit_budget=reddit_budget, reddit_ledger=reddit_ledger,
                renderer=renderer, firecrawl=firecrawl,
                credentialed_fetcher=credentialed_fetcher,
                transport_gate=transport_gate)
            if result.attempted is not False and attempt_ledger is not None:
                if attempt_ledger.take(1, path="ordinary") != 1:
                    raise RuntimeError("shared source-attempt allowance exhausted")
            return result

        attempt_replacements(
            registry, policy=policy, now=now, probe=_probe,
            decider=maintenance_decider, search_bridge=search_bridge,
            search_budget=search_budget, overall_budget=overall_budget,
            resolver=resolver, can_attempt=_can_attempt,
            max_probes=int(policy.get("source_replacement_max_probes_per_cycle", 1)),
            origin="ordinary", context=maintenance_context,
            privacy_verifier=maintenance_privacy_verifier)
    # A pure deferral (no seed/discovery/attempt/cursor/maintenance change) must
    # not rewrite the registry or churn ``updated_at``. Real state changes still
    # save normally, including safety/maintenance adoption updates.
    comparable_before = dict(before)
    comparable_now = dict(registry)
    comparable_before.pop("updated_at", None)
    comparable_now.pop("updated_at", None)
    if comparable_now != comparable_before:
        registry["updated_at"] = now.isoformat()
        save_registry(store, registry)
    return results


def default_seed_sources() -> list[dict[str, Any]]:
    """Evidence-backed initial seeds, explicitly tentative/configurable.

    Owner/repo and subreddit names are configuration placeholders; provenance
    marks them tentative so no fabricated intelligence is claimed. They are
    public endpoints only.
    """
    return [
        {
            "kind": "github_releases",
            "url": "https://api.github.com/repos/NousResearch/hermes-agent/releases",
            "provenance": "configured seed (tentative; confirm canonical owner/repo)",
            "interest": "hermes releases",
            "cadence_minutes": 360,
        },
        {
            "kind": "reddit_rss",
            "url": "https://www.reddit.com/r/hermesagent/.rss",
            "provenance": "configured seed (tentative; confirm canonical subreddit)",
            "interest": "hermes community",
            "cadence_minutes": 60,
        },
        {
            # Example university newsroom JSON. Campus NEWS only: it is explicitly NOT
            # a student internship/scholarship/opportunity or calendar feed, and
            # dates are date-only. Relevance-gated to campus/student/tech titles.
            "kind": "university_json",
            "url": UNIVERSITY_STORY_ENDPOINT,
            "provenance": "example seed (example university newsroom JSON; campus news, not an opportunity feed)",
            "interest": "university campus news",
            "cadence_minutes": 360,
        },
        {
            # One vetted CNA category (business feed, technology-relevance gated).
            # Not an official Singapore live advisory or transport source.
            "kind": "cna_rss",
            "url": CNA_BUSINESS_RSS,
            "provenance": "public vetted seed (CNA category RSS; technology-relevance gated; not an official advisory feed)",
            "interest": "cna technology news",
            "cadence_minutes": 360,
        },
        {
            "kind": "nea_forecast",
            "url": NEA_FORECAST_URL,
            "provenance": "public vetted seed (official keyless NEA 24-hour forecast API; forecast only, not an active warning)",
            "interest": "singapore weather forecast",
            "cadence_minutes": 360,
        },
        {
            "kind": "smrt_notice",
            "url": SMRT_F1_NOTICE_URL,
            "provenance": "public vetted seed (reviewed official SMRT planned notice; not live incident coverage)",
            "interest": "planned transport advisory",
            "cadence_minutes": 360,
        },
        {
            # Official credentialed LTA DataMall live train-service alerts.
            # The AccountKey is env-only (LTA_DATAMALL_ACCOUNT_KEY) and header-only;
            # if absent the source fails closed without any network call. Status 1
            # is an explicit all-clear marker, Status 2 describes each affected
            # segment/message -- never a fabricated incident.
            "kind": LTA_TRAIN_ALERTS_KIND,
            "url": LTA_TRAIN_ALERTS_URL,
            "provenance": "public vetted seed (official LTA DataMall TrainServiceAlerts; credentialed via env-only header; live status, not a plan or forecast)",
            "interest": "singapore train service alerts",
            "cadence_minutes": 360,
        },
    ]
