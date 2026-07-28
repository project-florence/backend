# Production Öncesi Yapılacaklar (Güvenlik Raporu Kapsamı Dışı / Sonraki Adım)

## Nginx / TLS (henüz yazılmadı)

- [ ] TLS sertifikası (certbot) + 80 -> 443 redirect (`sudo certbot --nginx -d florencex.com.tr`)
- [ ] HSTS header (`add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;`)

## Incelenmesi Gerekenler

- [ ] Container ici host adlarini duzelt: `POSTGRES_HOST=postgres`, `REDIS_HOST=redis`, `NEWS_SEARCH_URL=http://searxng:8080/search` (su an `.env`'de `localhost` yaziyor, compose icinde servis adi kullanilmali)

## Dusuk Oncelikli

- [ ] `db_backup.sql`'i (73 MB) repo kokunden kaldir (NOT: kullanici tarafindan manuel yapilacak)

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

### Tamamlananlar (commit `c8b7ec9`)

- [x] **JWT localStorage -> httpOnly Cookie** — Backend `Set-Cookie`, frontend cookie-based auth, `checkAuth()` ile `/profile` sorgulama
- [x] **LLM-Sentiment URL Dogrulama** — `safeUrl()` ile scheme validation
- [x] **nginx.conf Duzeltmeleri** — `proxy_pass http://api:7055`, CSP, X-Content-Type-Options, X-Frame-Options, Referrer-Policy, rate limiting
- [x] **npm Audit / CVE Fix** — `react-router-dom` 7.11.0 (GHSA-qwww-vcr4-c8h2)
- [x] **VITE_API_URL Production Build** — `.env`'de bos, `baseURL: import.meta.env.VITE_API_URL || ''`. Not: nginx `/api/` proxy'si yonlendiriyor, ayri URL gerekmiyor. Sadece frontend ayri domain'de calisacaksa buraya backend URL'si girilir.
- [x] **Google Fonts Self-Host** — CDN linki kalkti, `@fontsource-variable/geist` import edildi, CSP guncellendi

### Kalanlar

- [ ] **TLS / Certbot** — `sudo certbot --nginx -d florencex.com.tr`
  - Certbot 80 -> 443 redirect'i otomatik ekler
  - Sonrasinda HSTS header'i manuel ekle: `add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;`
  - CSP'yi production domain'e gore daralt
