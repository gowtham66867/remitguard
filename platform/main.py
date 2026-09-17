"""
RemitGuard Platform — behavioral health RCM operating system.
Purpose-built platform for behavioral health billing teams that catches
hidden payer clawbacks, tracks SCA lifecycles, and manages ERA/EFT
enrollment status across all active payers.

Run:
    uvicorn main:app --reload --port 8000
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os

from api.models.base import Base, engine
from api.models.human_review import HumanReview  # noqa: F401 — registers table
from api.routes.recoupment import router as recoupment_router
from api.routes.sca import router as sca_router
from api.routes.enrollment import router as enrollment_router
from api.routes.review import router as review_router
from api.routes.ws_pipeline import router as ws_pipeline_router

# Create all tables on startup
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="RemitGuard Platform",
    description="Behavioral health RCM operating system — recoupment detection, SCA lifecycle, ERA/EFT enrollment tracking.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(recoupment_router)
app.include_router(sca_router)
app.include_router(enrollment_router)
app.include_router(review_router)
app.include_router(ws_pipeline_router)

# Health check — registered before the SPA catch-all
@app.get("/api/health")
def health():
    return {"status": "ok", "service": "RemitGuard Platform"}

# Serve frontend — plain HTML file (no build step needed)
# Must be registered AFTER all /api/* routes so it doesn't shadow them.
frontend_dir = os.path.join(os.path.dirname(__file__), "frontend")
frontend_index = os.path.join(frontend_dir, "index.html")

if os.path.exists(frontend_index):
    @app.get("/", include_in_schema=False)
    def serve_root():
        return FileResponse(frontend_index)

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_frontend(full_path: str):
        static = os.path.join(frontend_dir, full_path)
        if os.path.isfile(static):
            return FileResponse(static)
        return FileResponse(frontend_index)
