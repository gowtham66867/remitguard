FROM python:3.9-slim

WORKDIR /app

# system deps for pdfplumber (pdfminer needs no extra, but pillow/pypdf2 may need libgomp)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# patterns.json is bundled inside the image at /app/patterns.json
# Update the hardcoded path in recoupment_service.py via env override
ENV PATTERNS_PATH=/app/patterns.json
ENV PORT=8080

EXPOSE 8080

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port $PORT"]
