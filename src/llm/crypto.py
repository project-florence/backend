"""AES-256-GCM ile LLM saglayici API anahtarlarinin sifrelenmesi.

Ana anahtar ``FLORENCE_MASTER_KEY`` ortam degiskeninden okunur (base64 ile
kodlanmis, cozulunce tam 32 bayt / 256 bit olmali). Ana anahtar veritabaninin
DISINDA tutulur -- bu kasitli: veritabanini ele gecirmek tek basina sifreli
saglayici anahtarlarini cozmeye yetmemeli. ``SECRET_KEY`` (JWT imzasi) ile
karistirilmamali; ikisinin yasam dongusu farkli (bkz. REFACTOR_PLAN.md 2.2).

Saklama formati: tek bir BYTEA sutununda ``nonce (12 bayt) || ciphertext || tag``
(``AESGCM.encrypt`` ciphertext'in sonuna 16 baytlik tag'i zaten ekliyor).

AAD (associated data) olarak HER ZAMAN saglayici id'si kullanilir: bir
satirin sifreli degeri baska bir saglayici satirina kopyalanip o id ile
cozulmeye calisilirsa ``cryptography`` ``InvalidTag`` firlatir ve bu
``DecryptionFailed`` olarak yukari tasinir. Bu, satirlar arasi anahtar
kopyalama/karistirma hatalarina karsi kasitli bir savunma katmanidir.

Ana anahtar uretmek icin (kurulum / ``llm rotate-key`` -- Adim 4):
    python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"

Not: AES-GCM kucuk payload'larda (birkac yuz baytlik API anahtari) mikro-
saniyeler surer -- bu modulde ``asyncio.to_thread`` KULLANILMAZ, cagrilar
dogrudan senkron yapilir.
"""

import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_LEN = 12
_KEY_LEN = 32


class LLMCryptoError(Exception):
    """Bu moduldeki tum hatalarin ortak taban sinifi."""


class MasterKeyMissing(LLMCryptoError):
    """``FLORENCE_MASTER_KEY`` ortam degiskeni tanimli degil."""


class MasterKeyInvalid(LLMCryptoError):
    """``FLORENCE_MASTER_KEY`` gecerli base64 degil veya 32 bayta cozulmuyor."""


class DecryptionFailed(LLMCryptoError):
    """Sifre cozme basarisiz: yanlis AAD (ornegin baska saglayicinin verisi),
    bozuk/kisa veri veya (dolayli olarak) yanlis ana anahtar."""


def generate_master_key() -> str:
    """Yeni bir ana anahtar uretir (base64 kodlu, 32 ham bayt).

    Yalnizca ilk kurulum / ``llm rotate-key`` akislarinda kullanilir; bu
    fonksiyon anahtari hicbir yere yazmaz, sadece uretir ve doner.
    """
    return base64.b64encode(os.urandom(_KEY_LEN)).decode("ascii")


def _load_master_key() -> bytes:
    """``FLORENCE_MASTER_KEY``'i okur ve dogrular. Bulunamaz/gecersizse acik
    bir istisna firlatir -- uygulamayi cokertmez, karar cagirana ait."""
    raw = os.getenv("FLORENCE_MASTER_KEY")
    if not raw:
        raise MasterKeyMissing("FLORENCE_MASTER_KEY ortam degiskeni tanimli degil")
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MasterKeyInvalid("FLORENCE_MASTER_KEY gecerli base64 degil") from exc
    if len(key) != _KEY_LEN:
        raise MasterKeyInvalid(
            f"FLORENCE_MASTER_KEY {len(key)} bayta cozuluyor, {_KEY_LEN} bekleniyor"
        )
    return key


def encrypt(plaintext: str, *, aad: str) -> bytes:
    """Duz metni sifreler. Donen deger: ``nonce || ciphertext || tag`` (BYTEA).

    ``aad`` cagiran tarafindan saglayici id'si olarak verilmeli -- ayni deger
    ``decrypt``'e verilmezse cozme basarisiz olur.
    """
    key = _load_master_key()
    nonce = os.urandom(_NONCE_LEN)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), aad.encode("utf-8"))
    return nonce + ciphertext


def decrypt(blob: bytes, *, aad: str) -> str:
    """Sifreli bloku cozer. AAD uyusmazliginda/bozuk veride ``DecryptionFailed``."""
    key = _load_master_key()
    blob = bytes(blob)
    if len(blob) < _NONCE_LEN:
        raise DecryptionFailed("sifreli veri cok kisa (nonce eksik)")
    nonce, ciphertext = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    aesgcm = AESGCM(key)
    try:
        plaintext = aesgcm.decrypt(nonce, ciphertext, aad.encode("utf-8"))
    except InvalidTag as exc:
        raise DecryptionFailed(
            "sifre cozme basarisiz: yanlis AAD, bozuk veri veya yanlis ana anahtar"
        ) from exc
    return plaintext.decode("utf-8")
