"""E-posta gonderme (smtplib tabanli, async uyumlu).

``MAIL_PROVIDER`` ortam degiskeni:
  - ``mailpit`` (varsayilan): localhost:1025, kimlik dogrulamasiz (dev/UI)
  - ``smtp``: ``MAIL_HOST`` / ``MAIL_PORT`` / ``MAIL_USER`` / ``MAIL_PASS``
    (465 disinda STARTTLS denenir)
  - ``resend``: Resend REST API (``RESEND_API_KEY``)
  - ``ses``: AWS SES — iskelet, henuz implemente edilmedi (bilincli olarak
    provider secimi kullanici kararina birakildi; burada yalnizca False doner)

KRITIK KURAL: mail hatalari ASLA auth/kredi akisini kirmaz. Tüm hatalar
``logger.warning`` ile loglanir ve ``False`` donulur; cagiran taraf sessiz
devam eder.
"""

import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent / "mail_templates"


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


async def _send_resend(to: str, subject: str, html: str, text: str | None) -> bool:
    """Resend REST API (iskelet; anahtar ``RESEND_API_KEY`` env'den)."""
    api_key = os.getenv("RESEND_API_KEY")
    if not api_key:
        logger.warning("RESEND_API_KEY ayarli degil; mail gonderilemedi")
        return False

    from src.clients.http import get_client

    client = await get_client()
    resp = await client.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "from": os.getenv("MAIL_FROM", "noreply@florence.local"),
            "to": [to],
            "subject": subject,
            "html": html,
            "text": text,
        },
        timeout=15,
    )
    if resp.status_code >= 400:
        logger.warning("Resend API error: %s %s", resp.status_code, resp.text)
        return False
    return True


async def send_email(to: str, subject: str, html: str, text: str | None = None) -> bool:
    """E-posta gonderir. Hata durumunda ASLA exception firlatmaz; ``False`` doner."""
    provider = (os.getenv("MAIL_PROVIDER") or "mailpit").lower()
    from_addr = os.getenv("MAIL_FROM", "noreply@florence.local")
    try:
        if provider in ("smtp", "mailpit"):
            return await asyncio.to_thread(
                _send_smtp_sync, provider, from_addr, to, subject, html, text
            )
        if provider == "resend":
            return await _send_resend(to, subject, html, text)
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
