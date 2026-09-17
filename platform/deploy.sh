#!/bin/bash
set -e

# ── RemitGuard Cloud Run Deployment ─────────────────────────────────────
PROJECT_ID="gowthamaccount"
REGION="us-central1"
SERVICE_NAME="remitguard"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE_NAME}"

echo ""
echo "🚀  RemitGuard → Google Cloud Run"
echo "    Project : $PROJECT_ID"
echo "    Region  : $REGION"
echo "    Service : $SERVICE_NAME"
echo "────────────────────────────────────────────────────────"

# 1. Build & push image via Cloud Build (no local Docker needed)
echo ""
echo "[1/3] Building image via Cloud Build..."
gcloud builds submit . \
  --tag "$IMAGE" \
  --project "$PROJECT_ID"

# 2. Deploy to Cloud Run
echo ""
echo "[2/3] Deploying to Cloud Run..."
gcloud run deploy "$SERVICE_NAME" \
  --image "$IMAGE" \
  --platform managed \
  --region "$REGION" \
  --allow-unauthenticated \
  --memory 512Mi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 10 \
  --timeout 300 \
  --set-env-vars "PATTERNS_PATH=/app/patterns.json" \
  --project "$PROJECT_ID"

# 3. Print the live URL
echo ""
echo "[3/3] Fetching service URL..."
URL=$(gcloud run services describe "$SERVICE_NAME" \
  --platform managed \
  --region "$REGION" \
  --project "$PROJECT_ID" \
  --format "value(status.url)")

echo ""
echo "────────────────────────────────────────────────────────"
echo "✅  Deployed!"
echo "    URL      : $URL"
echo "    Health   : $URL/api/health"
echo "    API docs : $URL/docs"
echo "    WS       : wss://$(echo $URL | sed 's|https://||')/ws/pipeline"
echo "────────────────────────────────────────────────────────"
echo ""
