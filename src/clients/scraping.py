import trafilatura
from urllib.parse import urlparse
import ipaddress
import socket


_PRIVATE_BLOCKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]


def _is_private_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return True
    try:
        host = parsed.hostname
        if host in ("localhost", "host.docker.internal"):
            return True
        ip = ipaddress.ip_address(socket.getaddrinfo(host, None)[0][4][0])
        return any(ip in block for block in _PRIVATE_BLOCKS)
    except Exception:
        return True


def get_text_from_url(url: str, timeout: int = 15) -> str:
    if _is_private_url(url):
        raise Exception(f"Access to private/internal URL is not allowed: {url}")

    downloaded = trafilatura.fetch_url(url, timeout=timeout)
    if not downloaded:
        raise Exception("Could not download from url")

    text = trafilatura.extract(downloaded)
    if not text:
        raise Exception("Could not extract text from url")

    return text
