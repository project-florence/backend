# Production Öncesi Yapılacaklar (Güvenlik Raporu Kapsamı Dışı / Sonraki Adım)

## Nginx / TLS (henüz yazılmadı)

- [ ] TLS sertifikası (certbot) + 80 -> 443 redirect
- [ ] `proxy_pass http://api:7055;` (servis adı `api`, port `7055`)
- [ ] HSTS header (`max-age=31536000; includeSubDomains`)
- [ ] CSP header (baslangic: `default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline' fonts.googleapis.com; font-src fonts.gstatic.com`)
- [ ] `X-Content-Type-Options: nosniff`
- [ ] `X-Frame-Options: DENY`
- [ ] `Referrer-Policy: strict-origin-when-cross-origin`
- [ ] Rate limiting: `limit_req_zone` - `/api/v1/auth/login` (5/dk), `/api/v1/reports/generate` (1/dk), genel API (30/sn)

## Orta Oncelikli Guvenlik

- [ ] Frontend: JWT'yi `localStorage` yerine `httpOnly` cookie'ye tasi
- [ ] LLM-sentiment URL'lerini render oncesi dogrula (javascript: scheme engeli)
- [ ] Frontend `nginx.conf`'taki `proxy_pass` URL'sini duzelt (`http://backend:8000` -> `http://api:7055`)
- [ ] `npm audit fix` (react-router, postcss, fast-uri, brace-expansion)
- [ ] Google Fonts'u self-host et (CSP + KVKK)

## Incelenmesi Gerekenler

- [ ] Container ici host adlarini duzelt: `POSTGRES_HOST=postgres`, `REDIS_HOST=redis`, `NEWS_SEARCH_URL=http://searxng:8080/search` (su an `.env`'de `localhost` yaziyor, compose icinde servis adi kullanilmali)
- [ ] Frontend `VITE_API_URL` production build'inde relative `/api/...` yap
- [ ] CORS middleware backend'de eklendi — production'da `allow_origins=[]`, development'da `allow_origins=["*"]`

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
