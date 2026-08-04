FROM python:3.12-slim

WORKDIR /app

# Sistem bağımlılıkları
RUN apt-get update && apt-get install -y --no-install-recommends \
    pandoc \
    texlive-latex-base \
    texlive-latex-extra \
    texlive-fonts-recommended \
    texlive-xetex \
    && rm -rf /var/lib/apt/lists/*

# Önce bağımlılıkları yükle (Cache optimizasyonu)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN groupadd --system --gid 10001 florence \
    && useradd --system --uid 10001 --gid 10001 --create-home florence

# Proje dosyalarını kopyala
COPY . .

RUN chown -R florence:florence /app

USER florence

EXPOSE 7055

# Varsayılan olarak main uygulamasını başlatır
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "7055"]
