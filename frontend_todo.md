# Frontend Yapılacaklar (Security Raporu)

Dizin: `/home/efe/Documents/web/`

---

## 1. JWT localStorage -> httpOnly Cookie (ORTA #5)

**Mevcut:** Token `localStorage`'da saklanıyor (`authStore.ts`, `api.ts`). XSS ile çalınabilir.

**Yapılacaklar:**

- [ ] **Backend:** Login yanıtında `Set-Cookie` header'ı gönder (`access_token`'ı `httpOnly; Secure; SameSite=Strict` cookie olarak). Backend zaten `/auth/login`'de token dönüyor — yanıta `set_cookie()` eklenmeli.
- [ ] **Backend:** Token doğrulama middleware'inde cookie'den de `access_token` okuma desteği ekle (şu an sadece `Authorization: Bearer` header'ını destekliyor).
- [ ] **Frontend `api.ts`:** `withCredentials: true` ekle (cookie'yi otomatik göndersin).
- [ ] **Frontend `api.ts`:** `Authorization` header'ını manuel eklemeyi kaldır (cookie otomatik gider).
- [ ] **Frontend `authStore.ts`:** `localStorage` yerine sadece state'te token tut (gerçek token'a JS erişmesine gerek yok). `isAuthenticated` kontrolünü cookie'nin varlığına veya backend `/profile` çağrısına göre yap.
- [ ] **Frontend `api.ts`:** 401 interceptor'da `localStorage.removeItem` yerine logout state güncellemesi yap.

---

## 2. LLM-Sentiment URL Doğrulama (ORTA #6)

**Mevcut:** `ReportDetailPage.tsx:198` — `s.url` direkt `<a href>`'e basılıyor. LLM prompt injection ile `javascript:` URL'si yazılabilir.

**Yapılacaklar:**

- [ ] `ReportDetailPage.tsx`'de `s.url`'yi render öncesi `new URL(s.url)` ile parse et. Scheme `http:` veya `https:` değilse link yerine düz metin olarak göster.
- [ ] ReactMarkdown `urlTransform` prop'u ile aynı kısıtlamayı uygula (markdown içindeki linkler için de koruma).

---

## 3. nginx.conf Düzeltmeleri (ORTA #7)

**Mevcut:** `nginx.conf`'ta proxy_pass yanlış (`backend:8000` yerine `api:7055` olmalı), güvenlik header'ları yok.

**Yapılacaklar:**

- [ ] `proxy_pass http://backend:8000;` -> `proxy_pass http://api:7055;`
- [ ] `add_header X-Content-Type-Options nosniff;`
- [ ] `add_header Referrer-Policy strict-origin-when-cross-origin;`
- [ ] `add_header X-Frame-Options DENY;`
- [ ] CSP header ekle (başlangıç):
      `add_header Content-Security-Policy "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline' fonts.googleapis.com; font-src fonts.gstatic.com; script-src 'self';" always;`
- [ ] `limit_req_zone $binary_remote_addr zone=api:10m rate=30r/s;` tanımla, `/api/v1/auth/` için 5r/dk, `/api/v1/reports/generate` için 1r/dk limit koy.

---

## 4. npm Audit / CVE Fix (ORTA #9)

**Mevcut:** `npm audit --omit=dev` 7 bulgu (5 yüksek, 2 orta).

**Yapılacaklar:**

- [ ] `npm audit fix` (auto-fix breaking olmayanlar)
- [ ] `react-router-dom`'u manuel güncelle (GHSA-qwww-vcr4-c8h2) — SPA modunda RSC kullanılmıyor ama yine de güncel sürüme çek.
- [ ] Kalanları `npm audit` ile kontrol et.

---

## 5. VITE_API_URL Production Build (İncelenmeli #7)

**Mevcut:** `VITE_API_URL=http://localhost:7055` build'e gömülüyor. Production'da kullanıcı tarayıcısı kendi localhost'una istek atar.

**Yapılacaklar:**

- [ ] `web/.env`'de `VITE_API_URL` boş bırak.
- [ ] `src/config/api.ts`'de `baseURL`'i production'da boş string yap (nginx proxy'si `/api/`'yi backend'e yönlendirsin).
- [ ] CORS middleware backend'de eklendi (main.py) — production'da `allow_origins=[]` (same-origin), development'da `allow_origins=["*"]`.

---

## 6. Google Fonts Self-Host (Düşük)

**Mevcut:** `index.html` Google Fonts CDN'den yükleniyor. Kullanıcı IP'si Google'a gidiyor, CSP ile de çelişiyor.

**Yapılacaklar:**

- [ ] `@fontsource-variable/geist` zaten `package.json`'da bağımlılık olarak var. `index.html`'deki `<link>` etiketini kaldır, font'u CSS ile import et.
- [ ] CSP'den `fonts.googleapis.com` ve `fonts.gstatic.com` izinlerini kaldır (self-host sonrası).

---

## 7. Frontend nginx.conf TLS (Production Runbook)

- [ ] Certbot ile TLS sertifikası
- [ ] 80 -> 443 redirect
- [ ] HSTS header
- [ ] CSP'yi production domain'e göre daralt
