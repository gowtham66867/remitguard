"""
FastAPI router for recoupment / EOB analysis endpoints.

Prefix: /api/recoupment

Endpoints
---------
POST /analyze        — single PDF upload, returns analysis JSON
POST /batch          — multiple PDFs + optional ledger CSV, returns per-file
                       results and an aggregate summary dict
GET  /history        — last 50 RecoupmentResult rows ordered by analyzed_at desc
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from ..models.base import get_db
from ..models.recoupment import RecoupmentResult
from ..services.recoupment_service import RecoupmentService, _parse_ledger_csv

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/recoupment", tags=["recoupment"])

# Single shared service instance — patterns are loaded once on first request.
_service = RecoupmentService()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _flag_dict_to_response(flag) -> dict:
    """Convert a RecoupmentFlag ORM row to a JSON-serialisable dict."""
    try:
        amounts = json.loads(flag.amounts_found) if flag.amounts_found else []
    except (ValueError, TypeError):
        amounts = []
    return {
        "line": flag.line,
        "matched_phrase": flag.matched_phrase,
        "payer_tag": flag.payer_tag,
        "amounts_found": amounts,
        "source": flag.source,
    }


def _result_row_to_response(row: RecoupmentResult) -> dict:
    """Convert a RecoupmentResult ORM row to a JSON-serialisable dict."""
    return {
        "id": row.id,
        "facility_id": row.facility_id,
        "filename": row.filename,
        "claim_number": row.claim_number,
        "date_of_service": row.date_of_service,
        "billed_amount": row.billed_amount,
        "paid_amount": row.paid_amount,
        "net_received": row.net_received,
        "flagged": row.flagged,
        "extraction_warning": row.extraction_warning,
        "analyzed_at": row.analyzed_at.isoformat() if row.analyzed_at else None,
        "flags": [_flag_dict_to_response(f) for f in (row.flags or [])],
    }


# ---------------------------------------------------------------------------
# POST /analyze
# ---------------------------------------------------------------------------

@router.post(
    "/analyze",
    summary="Analyse a single EOB PDF for recoupment / offset language",
    response_description="Analysis result with flagged lines and amounts",
)
async def analyze_single(
    file: UploadFile = File(..., description="EOB PDF file"),
    facility_id: Optional[int] = Form(None, description="Optional facility FK"),
    db: Session = Depends(get_db),
) -> dict:
    """
    Upload a single PDF EOB and receive a structured analysis.

    The result is persisted to the database and returned in the response.
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only PDF files are accepted.",
        )

    try:
        pdf_bytes = await file.read()
    except Exception as exc:
        logger.exception("Failed to read uploaded file: %s", file.filename)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not read uploaded file: {exc}",
        )

    if not pdf_bytes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Uploaded file is empty.",
        )

    result = _service.analyze_pdf(pdf_bytes, file.filename)

    try:
        db_row = _service.save_result(db, facility_id, result)
        db.commit()
        result["id"] = db_row.id
    except Exception as exc:
        db.rollback()
        logger.exception("DB persist failed for %s", file.filename)
        # Return the analysis result even if persistence fails; log the error.
        result["db_error"] = f"Analysis succeeded but could not be saved: {exc}"

    return result


# ---------------------------------------------------------------------------
# POST /batch
# ---------------------------------------------------------------------------

@router.post(
    "/batch",
    summary="Analyse multiple EOB PDFs in a single request",
    response_description="Per-file results plus an aggregate summary",
)
async def analyze_batch(
    files: List[UploadFile] = File(..., description="One or more EOB PDF files"),
    ledger: Optional[UploadFile] = File(
        None,
        description="Optional claims ledger CSV (columns: claim_number, expected_amount)",
    ),
    facility_id: Optional[int] = Form(None, description="Optional facility FK"),
    db: Session = Depends(get_db),
) -> dict:
    """
    Upload multiple PDF EOBs (and an optional ledger CSV) for batch analysis.

    Returns per-file analysis dicts **and** an aggregate summary:

    ```json
    {
      "results": [...],
      "summary": {
        "total_files": 5,
        "total_paid": 12345.67,
        "total_flagged": 890.00,
        "flagged_count": 2
      }
    }
    ```
    """
    if not files:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No files provided.",
        )

    # Validate that all uploads are PDFs
    non_pdfs = [
        f.filename for f in files
        if not (f.filename or "").lower().endswith(".pdf")
    ]
    if non_pdfs:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Only PDF files are accepted. Non-PDF uploads: {non_pdfs}",
        )

    # Parse optional ledger CSV
    ledger_dict = None
    if ledger is not None:
        try:
            ledger_bytes = await ledger.read()
            if ledger_bytes:
                ledger_dict = _parse_ledger_csv(ledger_bytes)
                logger.info(
                    "Loaded ledger with %d claim entries from %s",
                    len(ledger_dict),
                    ledger.filename,
                )
        except Exception as exc:
            logger.warning("Could not parse ledger CSV: %s", exc)
            # Non-fatal: continue without ledger reconciliation

    # Read all PDF bytes
    file_tuples: List[tuple] = []
    for upload in files:
        try:
            pdf_bytes = await upload.read()
        except Exception as exc:
            logger.exception("Failed to read %s", upload.filename)
            # Include a failed placeholder so the caller sees every filename
            file_tuples.append((upload.filename or "unknown.pdf", b""))
            continue
        file_tuples.append((upload.filename or "unknown.pdf", pdf_bytes))

    results, summary = _service.batch_analyze(file_tuples, ledger=ledger_dict)

    # Persist each result; collect DB ids
    saved_ids: List[Optional[int]] = []
    for result in results:
        try:
            db_row = _service.save_result(db, facility_id, result)
            db.flush()
            saved_ids.append(db_row.id)
        except Exception as exc:
            db.rollback()
            logger.exception("DB persist failed for %s", result.get("filename"))
            saved_ids.append(None)
            result.setdefault("db_error", f"Could not save: {exc}")

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("Batch DB commit failed")
        for result in results:
            result.setdefault("db_error", f"Commit failed: {exc}")

    # Attach DB ids to results where available
    for result, db_id in zip(results, saved_ids):
        if db_id is not None:
            result["id"] = db_id

    return {"results": results, "summary": summary}


# ---------------------------------------------------------------------------
# GET /history
# ---------------------------------------------------------------------------

@router.get(
    "/history",
    summary="Retrieve the most recent recoupment analysis records",
    response_description="List of up to 50 RecoupmentResult rows",
)
def get_history(
    facility_id: Optional[int] = None,
    db: Session = Depends(get_db),
) -> dict:
    """
    Return the last 50 RecoupmentResult rows ordered by `analyzed_at` desc.

    Optionally filter by `facility_id` query parameter.
    """
    query = db.query(RecoupmentResult).order_by(RecoupmentResult.analyzed_at.desc())

    if facility_id is not None:
        query = query.filter(RecoupmentResult.facility_id == facility_id)

    rows = query.limit(50).all()

    return {
        "count": len(rows),
        "results": [_result_row_to_response(row) for row in rows],
    }
