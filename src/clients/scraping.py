import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import certifi
import trafilatura
import urllib3


_PRIVATE_BLOCKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

_MAX_REDIRECTS = 5
_MAX_CONTENT_BYTES = 5 * 1024 * 1024


def _is_private_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return any(ip in block for block in _PRIVATE_BLOCKS) or ip.is_private or ip.is_loopback or ip.is_link_local


def _resolve_public_ip(host: str, port: int) -> str:
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, socket.gaierror) as exc:
        raise ValueError("Could not resolve URL host") from exc

    resolved = {address[4][0] for address in addresses}
    if not resolved or any(_is_private_ip(address) for address in resolved):
        raise ValueError("Access to private/internal URL is not allowed")
    return next(iter(resolved))


def _validate_url(url: str) -> tuple:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Only HTTP and HTTPS URLs are allowed")
    if parsed.hostname.lower() in ("localhost", "host.docker.internal"):
        raise ValueError("Access to private/internal URL is not allowed")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    ip = _resolve_public_ip(parsed.hostname, port)
    return parsed, ip, port


def _is_private_url(url: str) -> bool:
    try:
        _validate_url(url)
        return False
    except (ValueError, TypeError):
        return True


def _download_url(url: str, timeout: int) -> str:
    current_url = url
    manager = urllib3.PoolManager(cert_reqs="CERT_REQUIRED", ca_certs=certifi.where())

    for _ in range(_MAX_REDIRECTS + 1):
        parsed, resolved_ip, port = _validate_url(current_url)
        path = parsed.path or "/"
        if parsed.query:
            path += f"?{parsed.query}"
        headers = {
            "Host": parsed.netloc,
            "User-Agent": "Mozilla/5.0",
        }
        pool = urllib3.HTTPSConnectionPool(
            resolved_ip,
            port=port,
            cert_reqs="CERT_REQUIRED",
            ca_certs=certifi.where(),
            assert_hostname=parsed.hostname,
            server_hostname=parsed.hostname,
        ) if parsed.scheme == "https" else urllib3.HTTPConnectionPool(resolved_ip, port=port)

        response = pool.request(
            "GET",
            path,
            headers=headers,
            redirect=False,
            preload_content=True,
            timeout=urllib3.Timeout(connect=timeout, read=timeout),
        )
        if response.status in (301, 302, 303, 307, 308):
            location = response.headers.get("Location")
            if not location:
                raise ValueError("Redirect without a destination")
            current_url = urljoin(current_url, location)
            continue
        if response.status >= 400:
            raise ValueError(f"Could not download URL (HTTP {response.status})")
        if int(response.headers.get("Content-Length", 0) or 0) > _MAX_CONTENT_BYTES:
            raise ValueError("Downloaded content is too large")
        if len(response.data) > _MAX_CONTENT_BYTES:
            raise ValueError("Downloaded content is too large")
        return response.data.decode("utf-8", errors="replace")

    raise ValueError("Too many redirects")


def get_text_from_url(url: str, timeout: int = 15) -> str:
    try:
        downloaded = _download_url(url, timeout)
    except (ValueError, urllib3.exceptions.HTTPError) as exc:
        raise Exception(str(exc)) from exc

    text = trafilatura.extract(downloaded)
    if not text:
        raise Exception("Could not extract text from url")
    return text
