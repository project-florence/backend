"""E-posta gonderme (smtplib tabanli, async uyumlu).

``MAIL_PROVIDER`` ortam degiskeni:
  - ``resend`` (varsayilan): Resend REST API (``RESEND_API_KEY``)
  - ``mailpit``: localhost:1025, kimlik dogrulamasiz (dev/UI)
  - ``smtp``: ``MAIL_HOST`` / ``MAIL_PORT`` / ``MAIL_USER`` / ``MAIL_PASS``
    (465 disinda STARTTLS denenir)
  - ``ses``: AWS SES — iskelet, henuz implemente edilmedi (bilincli olarak
    provider secimi kullanici kararina birakildi; burada yalnizca False doner)

``MAIL_PROVIDER`` ayarlanmamissa varsayilan ``resend``'dir.
Gonderici adresi ``MAIL_FROM`` (varsayilan ``support@florencex.com.tr``).

KRITIK KURAL: mail hatalari ASLA auth/kredi akisini kirmaz. Tüm hatalar
``logger.warning`` ile loglanir ve ``False`` donulur; cagiran taraf sessiz
devam eder.
"""

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent / "mail_templates"

DEFAULT_MAIL_FROM = "support@florencex.com.tr"


def render_template(name: str, **kwargs) -> str:
    """``%TOKEN%`` yer tutuculu basit sablon render'i (str.format CSS'i bozmaz)."""
    content = (_TEMPLATE_DIR / name).read_text(encoding="utf-8")
    for key, value in kwargs.items():
        content = content.replace(f"%{key.upper()}%", str(value))
    return content


def _send_smtp_sync(
    provider: str, from_addr: str, to_addr: str, subject: str, html: str, text: str | None
) -> bool:
    """SMTP gonderimi (sync; to_thread icinde calisir). Hata firlatir, basari True."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    host = os.getenv("MAIL_HOST", "localhost")
    port = int(os.getenv("MAIL_PORT", "1025"))
    user = os.getenv("MAIL_USER") or None
    password = os.getenv("MAIL_PASS") or None

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    if text:
        msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP(host, port, timeout=10) as server:
        server.ehlo()
        if provider == "smtp" and port != 465:
            server.starttls()
        if user:
            server.login(user, password or "")
        server.sendmail(from_addr, [to_addr], msg.as_string())
    return True


async def _send_resend(to: str, subject: str, html: str, text: str | None, from_addr: str | None = None) -> bool:
    """Resend REST API: POST https://api.resend.com/emails (Bearer ``RESEND_API_KEY``)."""
    api_key = os.getenv("RESEND_API_KEY")
    if not api_key:
        logger.warning("RESEND_API_KEY ayarli degil; mail gonderilemedi")
        return False

    from src.clients.http import get_client

    payload: dict = {
        "from": from_addr or os.getenv("MAIL_FROM", DEFAULT_MAIL_FROM),
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text

    try:
        client = await get_client()
        resp = await client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=15,
        )
    except Exception as e:
        logger.warning("Resend API request failed for %s: %s", to, e)
        return False

    if resp.status_code not in (200, 201):
        logger.warning(
            "Resend API error: status=%s body=%s", resp.status_code, resp.text[:500]
        )
        return False
    logger.info("Resend mail sent to %s (status=%s)", to, resp.status_code)
    return True


async def send_email(to: str, subject: str, html: str, text: str | None = None, from_addr: str | None = None) -> bool:
    """E-posta gonderir. Hata durumunda ASLA exception firlatmaz; ``False`` doner.

    ``from_addr`` verilmezse ``MAIL_FROM`` (default ``DEFAULT_MAIL_FROM``) kullanilir.
    """
    provider = (os.getenv("MAIL_PROVIDER") or "resend").lower()
    from_addr = from_addr or os.getenv("MAIL_FROM", DEFAULT_MAIL_FROM)
    try:
        if provider in ("smtp", "mailpit"):
            return await asyncio.to_thread(
                _send_smtp_sync, provider, from_addr, to, subject, html, text
            )
        if provider == "resend":
            return await _send_resend(to, subject, html, text, from_addr)
        if provider == "ses":
            # AWS SES entegrasyonu henuz implemente edilmedi; provider secimi
            # (Resend vs SES vs self-hosted Postal) kullanici kararidir.
            logger.warning("MAIL_PROVIDER=ses henuz implemente edilmedi; mail gonderilemedi")
            return False
        logger.warning("Bilinmeyen MAIL_PROVIDER=%r; mail gonderilemedi", provider)
        return False
    except Exception as e:
        logger.warning("send_email to %s failed: %s", to, e)
        return False
