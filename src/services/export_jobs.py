"""Veri disa aktarim iscisi: exports tablosundaki kayitlari arka planda isler.

Akis (``_run_export``):
  (a) status='processing' -> coverage kontrolu: yil icin DB'deki 1d mum
      sayisi beklenenin (ticker sayisi x 245) %97'sinden azsa bulk fill
      (``bulk.fill_year``) calistirilir, sonra sayim yenilenir.
  (b) Veri akisli uretilir (fetchmany 5000) ve gzip (zlib, wbits=31)
      dosyaya yazilir: CSV -> ``ticker,date,open,high,low,close,volume``;
      JSON -> ``{ticker: [...]}`` (ticker degisiminde blok kapatilir/acilir).
  (c) row_count + size_bytes kaydedilir, status='ready', expires_at=7 gun,
      token uretilir.
  (d) E-posta gonderilir (PUBLIC_BASE_URL + indirme linki); gonderildiyse
      status='sent' (hata loglanir, status ready kalir — link calisir).
  (e) Exception -> status='failed' + error (ilk 500 karakter).
  (f) Is basinda: expires_at gecmis kayitlarin DOSYALARI silinir (kayit
      silinmez — istatistik kalici).
"""

import asyncio
import json
import logging
import os
import secrets
import zlib
from datetime import datetime, timezone
from pathlib import Path

from src.clients.mail import send_email
from src.core.database import db
from src.services.bulk import _load_tickers, fill_year

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # backend/
EXPORTS_DIR = BASE_DIR / "data" / "exports"

CSV_HEADER = "ticker,date,open,high,low,close,volume"

# Coverage kurali: beklenen mum sayisi = ticker sayisi x 245 islem gunu.
EXPECTED_TRADING_DAYS = 245
COVERAGE_RATIO = 0.97


def _csv_val(v) -> str:
    return "" if v is None else str(v)


async def _cleanup_expired_files() -> None:
    """expires_at gecmis kayitlarin dosyalarini siler (kayit kalir)."""
    try:
        async with db.cursor(row_factory=None) as cur:
            await cur.execute(
                "SELECT id, file_path FROM exports "
                "WHERE expires_at IS NOT NULL AND expires_at < now() AND file_path IS NOT NULL"
            )
            rows = await cur.fetchall()
        for _id, file_path in rows:
            if not file_path:
                continue
            try:
                Path(file_path).unlink(missing_ok=True)
                logger.info("export %s: suresi dolan dosya silindi: %s", _id, file_path)
            except OSError as e:
                logger.warning("export %s: dosya silinemedi %s: %s", _id, file_path, e)
    except Exception as e:
        logger.warning("export dosya temizligi basarisiz: %s", e)


async def _coverage_count(year: int) -> int:
    """Yil icin DB'deki 1d mum sayisi."""
    async with db.cursor(row_factory=None) as cur:
        await cur.execute(
            "SELECT count(*) FROM price_candles WHERE interval = '1d' "
            "AND ts >= %s AND ts < %s",
            (
                datetime(year, 1, 1, tzinfo=timezone.utc),
                datetime(year + 1, 1, 1, tzinfo=timezone.utc),
            ),
        )
        row = await cur.fetchone()
        return row[0] if row else 0


async def _write_csv_stream(cur, file_path: Path) -> int:
    """CSV satirlarini gzip'li dosyaya akisli yazar; satir sayisini doner."""
    count = 0
    compressor = zlib.compressobj(level=6, wbits=31)  # gzip formati
    with open(file_path, "wb") as f:
        f.write(compressor.compress((CSV_HEADER + "\n").encode("utf-8")))
        while True:
            rows = await cur.fetchmany(5000)
            if not rows:
                break
            chunk = "".join(
                f"{ticker},{ts:%Y-%m-%d},{_csv_val(o)},{_csv_val(h)},"
                f"{_csv_val(l)},{_csv_val(c)},{_csv_val(v)}\n"
                for ticker, ts, o, h, l, c, v in rows
            )
            count += len(rows)
            f.write(compressor.compress(chunk.encode("utf-8")))
        f.write(compressor.flush())
    return count


async def _write_json_stream(cur, file_path: Path) -> int:
    """JSON satirlarini gzip'li dosyaya akisli yazar; satir sayisini doner.

    Cikti formati: ``{"TICKER": [{date, open, high, low, close, volume}, ...], ...}``.
    Satirlar ORDER BY ticker, ts geldigi icin ticker degisiminde blok
    kapatilir/acilir; fetchmany ile bellek sabit tutulur.
    """
    count = 0
    compressor = zlib.compressobj(level=6, wbits=31)
    with open(file_path, "wb") as f:
        f.write(compressor.compress(b"{"))
        current_ticker: str | None = None
        first_block = True
        block_first = True
        while True:
            rows = await cur.fetchmany(5000)
            if not rows:
                break
            buf: list[str] = []
            for ticker, ts, o, h, l, c, v in rows:
                if ticker != current_ticker:
                    if current_ticker is not None:
                        buf.append("]")
                    if not first_block:
                        buf.append(",")
                    buf.append(f"{json.dumps(ticker)}:[")
                    current_ticker = ticker
                    first_block = False
                    block_first = True
                if not block_first:
                    buf.append(",")
                block_first = False
                buf.append(json.dumps(
                    {
                        "date": ts.strftime("%Y-%m-%d"),
                        "open": o,
                        "high": h,
                        "low": l,
                        "close": c,
                        "volume": v,
                    },
                    ensure_ascii=False,
                ))
            count += len(rows)
            f.write(compressor.compress("".join(buf).encode("utf-8")))
        if current_ticker is not None:
            f.write(compressor.compress(b"]"))
        f.write(compressor.compress(b"}"))
        f.write(compressor.flush())
    return count


async def _run_export(export_id: int) -> None:
    """Tek export kaydini isler (arka plan task'i olarak cagrilir)."""
    try:
        # Is basinda: suresi dolan dosyalari temizle (best-effort).
        await _cleanup_expired_files()

        # Kaydi oku + processing'e al. Commit cursor blogu ICINDE.
        async with db.cursor(row_factory=None) as cur:
            await cur.execute(
                "SELECT e.id, e.user_id, e.year, e.format, u.email "
                "FROM exports e JOIN users u ON u.id = e.user_id WHERE e.id = %s",
                (export_id,),
            )
            row = await cur.fetchone()
            if row is None:
                logger.warning("export %s: kayit bulunamadi", export_id)
                return
            await cur.execute(
                "UPDATE exports SET status = 'processing', error = NULL, updated_at = now() WHERE id = %s",
                (export_id,),
            )
            await db.commit()

        _export_id, user_id, year, fmt, email = row

        # (a) Coverage kontrolu: oran < %97 ise bulk fill, sonra sayimi yenile.
        tickers = await asyncio.to_thread(_load_tickers)
        count = await _coverage_count(year)
        expected = len(tickers) * EXPECTED_TRADING_DAYS
        if expected > 0 and count / expected < COVERAGE_RATIO:
            logger.info(
                "export %s: coverage %d/%d (%.1f%%) < %%97 -> bulk fill baslatiliyor",
                export_id, count, expected, 100.0 * count / expected,
            )
            await fill_year(year, tickers)
            count = await _coverage_count(year)
            logger.info("export %s: fill sonrasi coverage: %d mum", export_id, count)
        else:
            logger.info(
                "export %s: coverage yeterli (%d/%d, %.1f%%) — fill yok",
                export_id, count, expected, 100.0 * count / expected if expected else 0.0,
            )

        # (b) Akisli uretim: token onceden uretilir (dosya adi token icerir),
        #     (c)'de kayda yazilir.
        token = secrets.token_urlsafe(24)
        EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        file_path = EXPORTS_DIR / f"{export_id}_{token}.{fmt}.gz"

        async with db.cursor(row_factory=None) as cur:
            await cur.execute(
                "SELECT ticker, ts, open, high, low, close, volume FROM price_candles "
                "WHERE interval = '1d' AND ts >= %s AND ts < %s AND close IS NOT NULL "
                "ORDER BY ticker, ts",
                (
                    datetime(year, 1, 1, tzinfo=timezone.utc),
                    datetime(year + 1, 1, 1, tzinfo=timezone.utc),
                ),
            )
            if fmt == "csv":
                row_count = await _write_csv_stream(cur, file_path)
            else:
                row_count = await _write_json_stream(cur, file_path)

        size_bytes = file_path.stat().st_size

        # (c) Sonucu kaydet: ready + 7 gun gecerlilik + token.
        async with db.cursor(row_factory=None) as cur:
            await cur.execute(
                "UPDATE exports SET status = 'ready', file_path = %s, token = %s, "
                "row_count = %s, size_bytes = %s, "
                "expires_at = now() + interval '7 days', updated_at = now() "
                "WHERE id = %s",
                (str(file_path), token, row_count, size_bytes, export_id),
            )
            await db.commit()

        # (d) E-posta: indirme linki. Hata loglanir, status ready kalir.
        base_url = os.getenv("PUBLIC_BASE_URL", "https://florencex.com.tr").rstrip("/")
        link = f"{base_url}/api/v1/data/export/download/{token}"
        subject = f"Florence — {year} yıllık veri dışa aktarımınız hazır"
        html = (
            f"<p>Merhaba,</p>"
            f"<p><b>{year}</b> yılı günlük (1d) veri dışa aktarımınız hazır: "
            f"<b>{row_count:,}</b> satır ({fmt.upper()}).</p>"
            f'<p><a href="{link}">İndirmek için tıklayın</a></p>'
            f"<p>Bağlantı <b>7 gün</b> geçerlidir. Florence</p>"
        )
        text = (
            f"Florence — {year} yıllık veri dışa aktarımınız hazır "
            f"({row_count} satır, {fmt.upper()}).\n"
            f"İndirme linki: {link}\n"
            f"Bağlantı 7 gün geçerlidir."
        )
        sent = await send_email(email, subject, html, text=text)
        if sent:
            async with db.cursor(row_factory=None) as cur:
                await cur.execute(
                    "UPDATE exports SET status = 'sent', updated_at = now() WHERE id = %s",
                    (export_id,),
                )
                await db.commit()
            logger.info("export %s: mail gonderildi -> sent (user_id=%s)", export_id, user_id)
        else:
            # Mail hatasi export'u oldurmez; link calisir (status ready).
            logger.warning("export %s: mail gonderilemedi; status ready kaldi", export_id)

    except Exception as e:
        logger.exception("export %s basarisiz", export_id)
        try:
            async with db.cursor(row_factory=None) as cur:
                await cur.execute(
                    "UPDATE exports SET status = 'failed', error = %s, updated_at = now() WHERE id = %s",
                    (str(e)[:500], export_id),
                )
                await db.commit()
        except Exception:
            logger.exception("export %s: failed durumu yazilamadi", export_id)
