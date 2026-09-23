#!/bin/bash
set -e

# Load local deployment settings when present. Values already exported in the
# shell take precedence, so CI and Cloud Shell can inject credentials safely.
DEPLOY_ENV_FILE="$(cd "$(dirname "$0")" && pwd)/.env"
if [ -f "$DEPLOY_ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$DEPLOY_ENV_FILE"
  set +a
fi

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
gcloud builds submit "$(cd "$(dirname "$0")" && pwd)" \
  --tag "$IMAGE" \
  --project "$PROJECT_ID"

# 1b. Moss credentials — read from the deploy shell, never baked into the image.
#     For production prefer Secret Manager:
#       gcloud secrets create moss-project-key --data-file=-
#       gcloud run deploy ... --set-secrets MOSS_PROJECT_KEY=moss-project-key:latest
MOSS_ENV=""
if [ -n "$MOSS_PROJECT_ID" ] && [ -n "$MOSS_PROJECT_KEY" ]; then
  MOSS_ENV=",MOSS_PROJECT_ID=${MOSS_PROJECT_ID},MOSS_PROJECT_KEY=${MOSS_PROJECT_KEY},MOSS_INDEX_NAME=${MOSS_INDEX_NAME:-voice_ai-starter-mp0ar}"
  echo "    Moss    : enabled (semantic recall on)"
else
  echo "    Moss    : not configured — deploying regex-only"
fi

# Optional HiDevs virtual LLM key. The secret value is never passed as a build
# argument or environment value; Cloud Run resolves it at runtime. This remains
# disabled until a secret version has been added in Secret Manager.
HIDEVS_SECRET="remitguard-hidevs-llm-api-key"
HIDEVS_SECRETS=""
if gcloud secrets versions list "$HIDEVS_SECRET" --project "$PROJECT_ID" --limit 1 --format='value(name)' | grep -q .; then
  HIDEVS_SECRETS="--set-secrets HIDEVS_LLM_API_KEY=${HIDEVS_SECRET}:latest"
  echo "    Guidance: enabled (de-identified virtual LLM brief)"
else
  echo "    Guidance: not configured — no Secret Manager version yet"
fi

# 2. Deploy to Cloud Run
echo ""
echo "[2/3] Deploying to Cloud Run..."
gcloud run deploy "$SERVICE_NAME" \
  --image "$IMAGE" \
  --platform managed \
  --region "$REGION" \
  --allow-unauthenticated \
  --memory 1Gi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 10 \
  --timeout 300 \
  --set-env-vars "PATTERNS_PATH=/app/patterns.json${MOSS_ENV},HIDEVS_LLM_BASE_URL=https://llm.hidevs.xyz,HIDEVS_LLM_MODEL=gemini-3.5-flash" \
  $HIDEVS_SECRETS \
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
