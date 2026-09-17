# Python 3.11 — moss requires >= 3.10
FROM python:3.11-slim

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

# Moss semantic recall. MOSS_PROJECT_ID / MOSS_PROJECT_KEY are injected at
# deploy time (see deploy.sh) and are deliberately NOT baked into the image.
# Without them the container still serves normally on the regex-only path.
ENV MOSS_CORPUS_PATH=/app/recoupment_corpus.json
ENV MOSS_CACHE_PATH=/tmp/moss-index

EXPOSE 8080

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port $PORT"]
