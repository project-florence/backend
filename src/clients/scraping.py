import trafilatura
from urllib.parse import urlparse
import ipaddress
import socket
import requests


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


def _validate_redirect(r, *args, **kwargs):
    if r.is_redirect:
        location = r.headers.get("Location")
        if location and _is_private_url(location):
            raise ValueError(f"Redirect to private/internal URL blocked: {location}")


def get_text_from_url(url: str, timeout: int = 15) -> str:
    if _is_private_url(url):
        raise Exception(f"Access to private/internal URL is not allowed: {url}")

    session = requests.Session()
    session.max_redirects = 5
    session.hooks["response"] = [_validate_redirect]

    try:
        resp = session.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=True)
        resp.raise_for_status()
        downloaded = resp.text
    except ValueError as e:
        raise Exception(str(e))
    except requests.RequestException as e:
        raise Exception(f"Could not download from url: {e}")
    finally:
        session.close()

    text = trafilatura.extract(downloaded)
    if not text:
        raise Exception("Could not extract text from url")

    return text
