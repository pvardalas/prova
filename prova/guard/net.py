"""The only egress for agents: https, domain allowlist, no private / loopback / link-local addresses (also after
DNS resolution and on every redirect), size and time limits, text content types only.

    fetch(url, allow=None) -> {"url", "status", "content_type", "text"}   raises GuardError (a ValueError)
    validate_url(url, allow=None, resolve=True) -> (host, [ip, ...])      no request is made

`allow`: extra domains permitted besides LAW_DOMAINS (normally the company's own site). A host matches a domain
if it equals it or is a subdomain of it. With allow=None only the URL's own host is allowed (and its redirects).
"""
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

MAX_BYTES = 2 * 1024 * 1024
TIMEOUT_S = 20
MAX_REDIRECTS = 4
TEXT_TYPES = ("text/", "application/xhtml+xml", "application/json", "application/xml")
LAW_DOMAINS = ("legislation.gov.uk", "eur-lex.europa.eu", "gdpr-info.eu", "dsanet.gr", "taxheaven.gr",
               "digital-strategy.ec.europa.eu", "artificialintelligenceact.eu")


class GuardError(ValueError):
    def __init__(self, rule: str, msg: str):
        super().__init__(msg)
        self.rule = rule


def _blocked_ip(ip) -> bool:
    ip = ipaddress.ip_address(ip)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified
            or str(ip).startswith("169.254.") or (isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10")))


def _resolve(host: str) -> list:
    try:
        return sorted({a[4][0] for a in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
    except socket.gaierror as e:
        raise GuardError("dns", f"cannot resolve {host}: {e}") from e


def host_allowed(host: str, domains) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in (x.lower() for x in domains))


def validate_url(url: str, allow=None, resolve: bool = True):
    p = urlparse(str(url or ""))
    if p.scheme != "https":
        raise GuardError("https_only", "only https URLs may be fetched")
    host = (p.hostname or "").lower()
    if not host or p.username or p.password:
        raise GuardError("bad_url", "URL has no host or carries credentials")
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False
    if literal:
        if _blocked_ip(host):
            raise GuardError("private_address", f"{host} is a private, loopback or link-local address")
        raise GuardError("ip_literal", "fetching bare IP addresses is not allowed")
    if host in ("localhost",) or host.endswith((".local", ".internal", ".localhost")):
        raise GuardError("private_address", f"{host} is a local name")
    domains = list(LAW_DOMAINS) + list(allow or [host])
    if not host_allowed(host, domains):
        raise GuardError("domain_not_allowed", f"{host} is not the company domain or an allowlisted law source")
    ips = _resolve(host) if resolve else []
    for ip in ips:
        if _blocked_ip(ip):
            raise GuardError("private_address", f"{host} resolves to a private address ({ip})")
    return host, ips


def _get(url, ip, host, headers):
    """One request pinned to the validated IP (no second DNS lookup); returns (status, headers, body_bytes)."""
    import httpx
    pinned = url.replace(f"//{host}", f"//{ip}" if ":" not in ip else f"//[{ip}]", 1)
    h = {"Host": host, "User-Agent": "Mozilla/5.0 (compatible; Prova)", **headers}
    with httpx.Client(timeout=TIMEOUT_S, follow_redirects=False) as c:
        with c.stream("GET", pinned, headers=h, extensions={"sni_hostname": host}) as r:
            body = b""
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    raise GuardError("too_large", f"response is larger than {MAX_BYTES // 1024} KB")
            return r.status_code, dict(r.headers), body


def fetch(url: str, allow=None, _get_fn=None) -> dict:
    """Fetch a page as text. `_get_fn` exists for tests (no network); tests also monkeypatch `_resolve`."""
    allow_domains = list(allow) if allow else None
    host0 = urlparse(url).hostname or ""
    if allow_domains is None:
        allow_domains = [host0]
    cur = url
    for _ in range(MAX_REDIRECTS + 1):
        host, ips = validate_url(cur, allow_domains)
        status, headers, body = (_get_fn or _get)(cur, ips[0] if ips else "", host, {})
        headers = {k.lower(): v for k, v in headers.items()}
        if status in (301, 302, 303, 307, 308) and headers.get("location"):
            cur = urljoin(cur, headers["location"])
            continue
        if status >= 400:
            raise GuardError("http_error", f"HTTP {status}")
        ctype = headers.get("content-type", "").split(";")[0].strip().lower()
        if not ctype.startswith(TEXT_TYPES):
            raise GuardError("content_type", f"content type {ctype or 'unknown'} is not text")
        return {"url": cur, "status": status, "content_type": ctype, "text": body.decode("utf-8", errors="replace")}
    raise GuardError("too_many_redirects", "too many redirects")
