# Production Öncesi Yapılacaklar (Güvenlik Raporu Kapsamı Dışı / Sonraki Adım)

## Nginx / TLS (henüz yazılmadı)

- [ ] TLS sertifikası (certbot) + 80 -> 443 redirect (`sudo certbot --nginx -d florencex.com.tr`)
- [ ] HSTS header (`add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;`)

## Production Runbook

- [ ] Port hijyeni dogrulama: `nmap <vds-ip>` ile 5433, 5434, 5435 disaridan kapali mi kontrol et
- [ ] Docker iptables bypass: `DOCKER-USER` chain'inde ek kural
- [ ] GCP service account key rotate et (GCP konsolundan yeni key al, `.env`'yi ve dosyayi guncelle, eskisini sil)
- [ ] `.env` dosya izinlerini `600` yap
- [ ] Structured logging (print yerine)
- [ ] Access log'da `Authorization` header'inin yazilmadigini dogrula
- [ ] Yedekten restore testi (Incelenmeli #6: `pgdata:/var/lib/postgresql/data`)

## Frontend (web reposu — `/home/efe/Documents/web/`)

Bu maddeler `frontend_todo.md`'den tasinmistir.

### Yapilacaklar

- [ ] **TLS / Certbot** — `sudo certbot --nginx -d florencex.com.tr`
  - Certbot 80 -> 443 redirect'i otomatik ekler
  - Sonrasinda HSTS header'i manuel ekle: `add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;`
  - CSP'yi production domain'e gore daralt
