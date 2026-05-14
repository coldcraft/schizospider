from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import (
    quote,
    unquote,
    urljoin,
    urlsplit,
    urlunsplit,
    parse_qsl,
    urlencode,
)

import tldextract

# tldextract caches PSL on disk; this disables the network refresh that would
# otherwise reach out to publicsuffix.org on first import.
_TLD = tldextract.TLDExtract(suffix_list_urls=())

UrlClass = Literal["http", "external_scheme", "invalid"]
HostMode = Literal["registrable", "strict"]

# Regex helpers for fishing real URLs out of javascript: hrefs and inline scripts.
_JS_URL_RE = re.compile(
    r"""(?:window\.open|location\s*(?:\.href)?\s*=|location\.replace)\s*\(?\s*['"]([^'"]+)['"]""",
    re.IGNORECASE,
)
# Form-action URLs embedded inside script bodies (e.g. jodi's document.write
# scheme that picks one of N "<form action='wN.html'>" alternates randomly).
_JS_FORM_ACTION_RE = re.compile(
    r"""<form\b[^>]*?\baction\s*=\s*(?:\\?['"])([^'"\\]+)(?:\\?['"])""",
    re.IGNORECASE,
)
_META_REFRESH_RE = re.compile(
    r"""(?i)^\s*\d+\s*;\s*url\s*=\s*['"]?([^'"\s]+)['"]?\s*$"""
)

EXTERNAL_SCHEMES = {"mailto", "tel", "sms", "javascript", "data", "ftp", "file", "irc"}


@dataclass(frozen=True)
class ParsedUrl:
    canonical: str
    scheme: str
    host: str
    registrable: str
    kind: UrlClass


def _normalize_host(host: str) -> str:
    if not host:
        return ""
    host = host.strip().lower()
    try:
        host = host.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        pass
    # Strip default ports.
    if host.endswith(":80") or host.endswith(":443"):
        host_no_port, _, port = host.rpartition(":")
        if (port == "80" and host.startswith("http:")) or port in {"80", "443"}:
            host = host_no_port
    return host


def _normalize_path(path: str) -> str:
    if not path:
        return "/"
    # Collapse repeated slashes (but keep the leading one).
    path = re.sub(r"/{2,}", "/", path)
    # Resolve . / .. segments.
    parts: list[str] = []
    for seg in path.split("/"):
        if seg == "" and parts:
            continue
        if seg == ".":
            continue
        if seg == "..":
            if parts and parts[-1] != "":
                parts.pop()
            continue
        parts.append(seg)
    out = "/".join(parts) if parts and parts[0] == "" else "/" + "/".join(parts)
    if not out:
        out = "/"
    return out


def _normalize_query(query: str) -> str:
    if not query:
        return ""
    pairs = parse_qsl(query, keep_blank_values=True)
    pairs.sort()
    return urlencode(pairs, doseq=True, quote_via=quote)


def canonicalize(url: str, base: str | None = None) -> str | None:
    """Resolve `url` against `base`, normalize, return canonical form or None if invalid."""
    if url is None:
        return None
    url = url.strip()
    if not url:
        return None
    # Bare fragment or empty.
    if url.startswith("#"):
        return None
    if base:
        try:
            url = urljoin(base, url)
        except ValueError:
            return None
    try:
        s = urlsplit(url)
    except ValueError:
        return None
    if not s.scheme:
        return None
    scheme = s.scheme.lower()
    if scheme not in ("http", "https"):
        # Not a navigable web URL.
        return None
    host = _normalize_host(s.netloc)
    if not host:
        return None
    path = _normalize_path(unquote(s.path))
    # Re-quote path conservatively.
    path = quote(path, safe="/-._~%!$&'()*+,;=:@")
    query = _normalize_query(s.query)
    return urlunsplit((scheme, host, path, query, ""))


def classify(url: str) -> UrlClass:
    if not url:
        return "invalid"
    u = url.strip()
    if not u:
        return "invalid"
    if u.startswith("#"):
        return "invalid"
    scheme_match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):", u)
    if not scheme_match:
        # Relative — treated as http after resolution; caller decides.
        return "http"
    scheme = scheme_match.group(1).lower()
    if scheme in ("http", "https"):
        return "http"
    if scheme in EXTERNAL_SCHEMES:
        return "external_scheme"
    return "invalid"


def registrable_domain(url_or_host: str) -> str:
    if not url_or_host:
        return ""
    if "://" in url_or_host:
        host = urlsplit(url_or_host).netloc
    else:
        host = url_or_host
    host = host.split("@")[-1]  # strip userinfo
    host = host.split(":")[0]  # strip port
    ext = _TLD(host)
    # `top_domain_under_public_suffix` is the newer name for `registered_domain`
    # in tldextract >= 5.2. Only fall through to the deprecated attribute if
    # the new one doesn't exist at all (older tldextract), not just because it
    # returned an empty string — otherwise we re-trigger the deprecation warning.
    if hasattr(ext, "top_domain_under_public_suffix"):
        rd = ext.top_domain_under_public_suffix
    else:
        rd = getattr(ext, "registered_domain", "")
    if rd:
        return rd.lower()
    return host.lower()


def host_of(url: str) -> str:
    try:
        return urlsplit(url).netloc.lower()
    except ValueError:
        return ""


def same_site(a: str, b: str, mode: HostMode = "registrable") -> bool:
    if mode == "strict":
        return host_of(a) == host_of(b)
    return registrable_domain(a) == registrable_domain(b)


def extract_js_urls(raw: str) -> list[str]:
    """Pull plausible URLs out of a javascript: href or inline <script> body."""
    if not raw:
        return []
    found: list[str] = []
    for m in _JS_URL_RE.finditer(raw):
        candidate = m.group(1).strip()
        if candidate and not candidate.startswith("javascript:"):
            found.append(candidate)
    # Form-action URLs embedded inside script bodies (jodi's pattern).
    for m in _JS_FORM_ACTION_RE.finditer(raw):
        candidate = m.group(1).strip()
        if candidate and not candidate.startswith("javascript:"):
            found.append(candidate)
    return found


def extract_meta_refresh(content: str) -> str | None:
    """Parse a `<meta http-equiv='refresh' content='5; url=foo'>` value."""
    if not content:
        return None
    m = _META_REFRESH_RE.match(content)
    return m.group(1).strip() if m else None
