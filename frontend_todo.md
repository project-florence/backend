# Frontend Yapılacaklar (Security Raporu) ✅

Dizin: `/home/efe/Documents/web/`

> Bu dosyadaki tum maddeler fixlenmistir (`c8b7ec9`). Detaylar asagida.

---

## 1. JWT localStorage -> httpOnly Cookie (ORTA #5) ✅

**Yapılanlar:**
- Backend (`auth.py`): Login yanıtına `Set-Cookie` header'ı eklendi (`HttpOnly; Secure; SameSite=Strict`)
- Backend (`deps.py`): `get_current_user` artık önce `Authorization: Bearer`, yoksa `Cookie: access_token`'ı dener
- Frontend (`api.ts`): `withCredentials: true`, `Authorization: Bearer` interceptor'u kaldırıldı, 401'de `useAuthStore.logout()` çağrılıyor
- Frontend (`authStore.ts`): localStorage kullanımı kalktı, `checkAuth()` ile `/profile` sorgulanıyor
- Frontend (`LoginPage.tsx`): `setToken()` kaldırıldı, sadece `navigate('/')` yapılıyor
- Frontend (`ProtectedRoute.tsx`): `checkAuth()` ile auth kontrolü, loading spinner eklendi

## 2. LLM-Sentiment URL Doğrulama (ORTA #6) ✅

**Yapılanlar:**
- `safeUrl()` fonksiyonu eklendi (`new URL()` ile parse, scheme `http:`/`https:` değilse null döner)
- Sentiment URL linki `safeUrl()`'den geçiriliyor, geçersiz URL'lerde `<span>` gösteriliyor

## 3. nginx.conf Düzeltmeleri (ORTA #7) ✅

**Yapılanlar:**
- `proxy_pass http://backend:8000` -> `http://api:7055`
- `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`
- CSP: `default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self'; script-src 'self'`
- Rate limiting: `auth` (5/dk), `reportgen` (1/dk), `api` (30/sn) — zone'lar ayri ayri
- `server_name florencex.com.tr`

## 4. npm Audit / CVE Fix (ORTA #9) ✅

**Yapılanlar:**
- `react-router-dom` `^7.18.1` -> `7.11.0` (GHSA-qwww-vcr4-c8h2 fix)
- Kalan CVE'ler RSC/SSR moduna ozel, SPA'da etkisiz

## 5. VITE_API_URL Production Build (İncelenmeli #7) ✅

**Yapılanlar:**
- `.env`'de `VITE_API_URL` boş bırakıldı
- `api.ts`'de `baseURL: import.meta.env.VITE_API_URL || ''` (production'da nginx proxy'si `/api/`'yi backend'e yönlendirir)

## 6. Google Fonts Self-Host (Düşük) ✅

**Yapılanlar:**
- `index.html`'deki Google Fonts `<link>` etiketleri kaldırıldı
- `src/index.css`'e `@import "@fontsource-variable/geist"` eklendi
- Font ailesi `'Geist Variable'` olarak güncellendi
- CSP'den `fonts.googleapis.com` / `fonts.gstatic.com` izinleri kaldırıldı

## 7. Frontend nginx.conf TLS (Production Runbook) ❌

- [ ] Certbot ile TLS sertifikası (`sudo certbot --nginx -d florencex.com.tr`)
- [ ] 80 -> 443 redirect (certbot otomatik ekler)
- [ ] HSTS header (`add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;`)
- [ ] CSP'yi production domain'e gore daralt
