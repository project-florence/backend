FROM python:3.12-slim

WORKDIR /app

# Önce bağımlılıkları yükle (Cache optimizasyonu)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Proje dosyalarını kopyala
COPY . .

EXPOSE 7055

# Varsayılan olarak main uygulamasını başlatır
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "7055"]
