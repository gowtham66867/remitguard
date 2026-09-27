"""
FastAPI router for recoupment / EOB analysis endpoints.

Prefix: /api/recoupment

Endpoints
---------
POST /analyze        — single PDF upload, returns analysis JSON
POST /demo           — run the bundled reworded-offset EOB down both paths
POST /compare        — upload a PDF and diff regex-only vs regex+Moss
GET  /demo/case      — metadata for the bundled demo EOB
POST /batch          — multiple PDFs + optional ledger CSV, returns per-file
                       results and an aggregate summary dict
GET  /history        — last 50 RecoupmentResult rows ordered by analyzed_at desc
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, List, Literal, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
try:
    from sqlalchemy.orm import Session
    from ..models.base import get_db
    from ..models.recoupment import RecoupmentResult
except (ImportError, ValueError):
    Session = Any  # type: ignore
    get_db = lambda: None  # type: ignore
    RecoupmentResult = Any  # type: ignore
from ..services.comparison_service import run_comparison, run_demo_case
from ..services.sample_files import list_demo_cases
from ..services.recoupment_service import RecoupmentService, _parse_ledger_csv
from ..services.recovery_guidance_service import get_recovery_guidance_service
from industry_profiles import get_profile
from ..services.sample_files import DEMO_CASES, get_demo_pdf_bytes, list_demo_cases

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/recoupment", tags=["recoupment"])

# Single shared service instance — patterns are loaded once on first request.
_service = RecoupmentService()


class RecoveryGuidanceRequest(BaseModel):
    """Strictly de-identified metadata accepted by the LLM guidance route."""

    decision: Literal["CLEAR", "HUMAN_REVIEW"]
    recovery_at_risk: float = Field(ge=0, le=10_000_000)
    evidence_count: int = Field(ge=0, le=100)
    signal_types: List[Literal["pattern", "semantic", "ledger_mismatch"]] = Field(max_length=10)


class DisputePacketRequest(BaseModel):
    """Parameters to generate a formal payer recoupment dispute packet."""

    filename: str
    payer: Optional[str] = "Payer Claims Audit Unit"
    claim_number: Optional[str] = "Unknown"
    date_of_service: Optional[str] = "Unknown"
    recoupment_amount: float
    evidence_text: str
    precedent: Optional[str] = None
    coordinator_name: Optional[str] = "Billing Coordinator"


class ClassifyRequest(BaseModel):
    """Parameters to test-drive phrase classification."""
    text: str


class LearnRequest(BaseModel):
    """Parameters to teach the in-process semantic memory."""
    phrase: str
    payer: Optional[str] = "generic"


class WorkflowTriageRequest(BaseModel):
    """A de-identified exception signal for a non-recovery workflow."""

    workflow: Literal["sca", "enrollment", "alerts"]
    signal: str = Field(min_length=12, max_length=500)


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
        "semantic_score": getattr(flag, "semantic_score", None),
        "semantic_doc_id": getattr(flag, "semantic_doc_id", None),
        "semantic_learned": getattr(flag, "semantic_learned", False),
    }


def _result_row_to_response(row: RecoupmentResult) -> dict:
    """Convert a RecoupmentResult ORM row to a JSON-serialisable dict."""
    flags = [_flag_dict_to_response(f) for f in (row.flags or [])]
    recovery_at_risk = round(sum(
        abs(amount)
        for flag in flags
        for amount in flag["amounts_found"]
    ), 2)
    has_recoupment = bool(row.flagged or flags)
    if not has_recoupment:
        action = "No recoupment language detected. Post normally."
        decision = "CLEAR"
    elif all(flag["source"] == "ledger_mismatch" for flag in flags):
        action = "Reconcile the payment against the submitted claims ledger before posting."
        decision = "HUMAN_REVIEW"
    else:
        action = "Hold final posting and route this EOB to a billing coordinator with the highlighted payer-language evidence."
        decision = "HUMAN_REVIEW"
    return {
        "id": row.id,
        "facility_id": row.facility_id,
        "filename": row.filename,
        "claim_number": row.claim_number,
        "date_of_service": row.date_of_service,
        "billed_amount": row.billed_amount,
        "paid_amount": row.paid_amount,
        "net_received": row.net_received,
        "flagged": has_recoupment,
        "has_recoupment": has_recoupment,
        "recoupment_amount": recovery_at_risk,
        "amount_flagged": recovery_at_risk,
        "recoupment_text": flags[0]["line"] if flags else None,
        "recovery_case": {
            "decision": decision,
            "recovery_at_risk": recovery_at_risk,
            "recommended_action": action,
            "evidence_count": len(flags),
        },
        "extraction_warning": row.extraction_warning,
        "analyzed_at": row.analyzed_at.isoformat() if row.analyzed_at else None,
        "flags": flags,
    }


@router.get("/guidance/status", summary="Check whether de-identified recovery guidance is available")
def guidance_status() -> dict:
    return get_recovery_guidance_service().status()


@router.post("/guidance", summary="Generate a de-identified recovery reviewer brief")
async def create_recovery_guidance(case: RecoveryGuidanceRequest) -> dict:
    """Generate a brief without sending EOB content or identifiers to the provider."""
    service = get_recovery_guidance_service()
    if not service.enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Recovery guidance is not configured.",
        )
    try:
        brief = await asyncio.to_thread(service.create_brief, case.model_dump())
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return {
        "brief": brief,
        "privacy": "Generated from de-identified case metadata; source EOB text was not sent.",
    }


# ---------------------------------------------------------------------------
# POST /analyze
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# The proof endpoints — same EOB, both detection paths, side by side
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Dispute packet — the step that turns a flag into a recoverable action
# ---------------------------------------------------------------------------

@router.post(
    "/dispute-packet",
    summary="Draft a payer recoupment dispute letter for coordinator review",
    response_description="Draft letter text plus the structured case facts",
)
def generate_dispute_packet(req: DisputePacketRequest) -> dict:
    """Assemble a draft challenge letter from the case facts.

    Detection alone leaves a coordinator with an alert and no next step. This
    renders the facts already on the case — payer, claim, amount, the quoted
    EOB line — into the letter that actually recovers the money.

    It is a DRAFT for a coordinator and, where the amount warrants it,
    counsel. The tool does not certify anything and does not file anything;
    the citation below is the federal claims-procedure rule a plan-governed
    appeal proceeds under, included so the reviewer does not have to look it
    up. A human sends it.
    """
    amount = f"${req.recoupment_amount:,.2f}"
    statutory_basis = (
        "ERISA claims procedure — 29 C.F.R. § 2560.503-1 "
        "(full and fair review; notice of adverse benefit determination)"
    )
    letter_text = f"""NOTICE OF DISPUTED RECOUPMENT — REQUEST FOR RE-ADJUDICATION

To:       {req.payer}
From:     {req.coordinator_name}, Behavioral Health Associates
Re:       Claim {req.claim_number} · Date of service {req.date_of_service}
Source:   {req.filename}

DISPUTED AMOUNT: {amount}

This office disputes the offset applied to the above remittance and requests
re-adjudication of the disputed amount.

BASIS OF DISPUTE
The remittance applies a recoupment against this payment without an
accompanying notice identifying the overpayment being recovered, the claim it
arises from, or the determination supporting it. Absent that notice, the
provider cannot verify the offset or exercise appeal rights.

EVIDENCE — as it appears on the remittance
    "{req.evidence_text.strip()}"
{f'    Comparable prior payer language on file: "{req.precedent.strip()}"' if req.precedent else ''}

REQUESTED ACTION
 1. Identify the specific overpayment determination supporting this offset,
    including the originating claim and the date of the determination.
 2. Re-adjudicate the disputed {amount} and remit if the offset is unsupported.
 3. Place an ADMINISTRATIVE HOLD on further recoupment against this provider
    account pending resolution of this dispute.

Please respond in writing within the period allowed under the applicable plan
and {statutory_basis}.

{req.coordinator_name}
Behavioral Health Associates

--
DRAFT — generated by RemitGuard from the case facts above. Review and, where
the amount warrants it, refer to counsel before sending. This is not legal
advice and nothing here has been filed or certified.
"""
    return {
        "status": "DISPUTE_PACKET_GENERATED",
        "disputed_amount": req.recoupment_amount,
        "statutory_basis": statutory_basis,
        "claim_number": req.claim_number,
        "payer": req.payer,
        "is_draft": True,
        "letter_text": letter_text,
    }


@router.get("/demo/cases", summary="The bundled demo case catalog")
def demo_cases() -> dict:
    """List the bundled cases so the UI can frame each one before running."""
    return {"cases": list_demo_cases()}


@router.post(
    "/demo",
    summary="Run a bundled EOB down both detection paths",
    response_description="Regex-only verdict, regex+retrieval verdict, and the delta",
)
def run_demo(case_id: Optional[str] = None) -> dict:
    """One click, one document, two verdicts.

    The default case states its offset in wording no pattern in the library
    matches: regex-only reports the full payment as received, while the
    retrieval layer catches the line and the net payment drops to what the
    practice will actually bank. The response carries both verdicts and the
    retrieval trace behind the second one, including the lines retrieval was
    asked about and declined to flag.
    """
    out = run_demo_case(case_id, _service._get_compiled())
    if "error" in out:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=out["error"]
        )
    return out


@router.post(
    "/demo/{case_id}",
    summary="Run a named bundled EOB down both detection paths",
    response_description="Regex-only verdict, regex+retrieval verdict, and the delta",
)
def run_demo_by_id(case_id: str) -> dict:
    """Path-param form of `/demo`, so the UI can link a case directly."""
    return run_demo(case_id)


@router.post(
    "/compare",
    summary="Diff regex-only against regex+Moss on an uploaded EOB",
    response_description="Both verdicts and the retrieval trace",
)
async def compare_upload(
    file: UploadFile = File(..., description="EOB PDF file"),
) -> dict:
    """Same comparison as /demo, against a reviewer's own document."""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only PDF files are accepted.",
        )
    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Uploaded file is empty.",
        )
    return run_comparison(pdf_bytes, file.filename, _service._get_compiled())


@router.post(
    "/analyze",
    summary="Analyse a single EOB PDF for recoupment / offset language",
    response_description="Analysis result with flagged lines and amounts",
)
async def analyze_single(
    file: UploadFile = File(..., description="EOB PDF file"),
    industry: str = Form("healthcare", description="Payment workflow profile"),
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
    result["industry"] = industry if industry in ("healthcare", "insurance", "logistics", "saas", "marketplace", "manufacturing") else "healthcare"
    result["industry_profile"] = get_profile(result["industry"])

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
    industry: str = Form("healthcare", description="Payment workflow profile"),
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
    selected_industry = industry if industry in ("healthcare", "insurance", "logistics", "saas", "marketplace", "manufacturing") else "healthcare"
    for result in results:
        result["industry"] = selected_industry
        result["industry_profile"] = get_profile(selected_industry)
    summary["industry"] = selected_industry
    summary["industry_profile"] = get_profile(selected_industry)

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


# ---------------------------------------------------------------------------
# Interactive Semantic Sandbox, Active Learning, & Scientific Benchmark
# ---------------------------------------------------------------------------

@router.post("/semantic/classify", summary="Test-drive semantic recall on arbitrary line text")
@router.post("/classify", summary="Test-drive semantic recall on arbitrary line text")
def classify_line(req: ClassifyRequest) -> dict:
    """
    Evaluate any custom line of text through both detection pathways:
    1. Static regex baseline
    2. In-process hybrid semantic recall
    Returns latency, score, matched precedent, and divergence.
    """
    import time
    from agents.recoupment_agent import _detect_flags
    from agents.semantic_matcher import get_matcher

    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Text cannot be empty.")

    # 1. Regex check
    compiled = _service._get_compiled()
    t0 = time.perf_counter()
    regex_flags = _detect_flags(text, compiled, matcher=None)
    regex_ms = (time.perf_counter() - t0) * 1000.0
    regex_hit = bool(regex_flags)

    # 2. Semantic check
    matcher = get_matcher()
    t1 = time.perf_counter()
    sem_probe = matcher.probe(text) if (matcher and matcher.ready) else None
    sem_match = matcher.match(text) if (matcher and matcher.ready) else None
    sem_ms = (time.perf_counter() - t1) * 1000.0

    score = round(sem_probe.get("score", 0.0), 4) if sem_probe else 0.0
    label = sem_probe.get("label", "unknown") if sem_probe else "unavailable"
    matched_text = sem_probe.get("text", "") if sem_probe else ""
    doc_id = sem_probe.get("doc_id", "") if sem_probe else ""
    payer = sem_probe.get("payer", "generic") if sem_probe else ""
    is_recoupment = sem_match is not None or (label == "recoupment" and score >= getattr(matcher, "threshold", 0.45))

    diverged = (not regex_hit) and is_recoupment

    return {
        "text": text,
        "regex": {
            "matched": regex_hit,
            "patterns": [f.matched_phrase for f in regex_flags] if regex_flags else [],
            "payer": regex_flags[0].payer_tag if regex_flags else None,
            "latency_ms": round(regex_ms, 3),
        },
        "semantic": {
            "matched": is_recoupment,
            "score": score,
            "label": label,
            "matched_precedent": matched_text,
            "doc_id": doc_id,
            "payer": payer,
            "latency_ms": round(sem_ms, 3),
            "engine": matcher.stats().get("engine", "in_process_semantic") if matcher else "unavailable",
        },
        "diverged": diverged,
        "verdict": (
            "DIVERGENCE_CAUGHT" if diverged
            else "RECOUPMENT_DETECTED" if (regex_hit and is_recoupment)
            else "BENIGN_LINE" if (not regex_hit and not is_recoupment)
            else "REGEX_ONLY_HIT"
        ),
        "explanation": (
            f"Static regex missed this phrase ({regex_ms:.2f}ms). RemitGuard semantic recall caught clawback in {sem_ms:.2f}ms with score {score} matching '{matched_text}'."
            if diverged
            else "Both engines agree on classification."
        )
    }


@router.post("/semantic/learn", summary="Teach the in-process semantic memory with novel phrasing")
@router.post("/learn", summary="Teach the in-process semantic memory with novel phrasing")
def learn_phrase(req: LearnRequest) -> dict:
    """
    Closed-loop active learning: injects a verified clawback phrase into the
    in-process semantic index in real-time.
    """
    from agents.semantic_matcher import get_matcher

    phrase = (req.phrase or "").strip()
    if not phrase:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Phrase cannot be empty.")

    matcher = get_matcher()
    if not (matcher and matcher.ready):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Semantic layer not ready.")

    ok = matcher.learn(phrase, payer_tag=req.payer or "generic")
    stats = matcher.stats()

    return {
        "success": ok,
        "phrase": phrase,
        "payer": req.payer,
        "corpus_size": stats.get("corpus_size", 0),
        "learned_count": stats.get("learned_count", 0),
        "status": "MEMORY_INDEXED",
        "message": f"Successfully indexed into local semantic corpus ({stats.get('corpus_size', 0)} total phrases). Future queries match with zero latency penalty."
    }


@router.post("/semantic/workflow-triage", summary="Map an operational exception to a Moss playbook")
def triage_workflow(req: WorkflowTriageRequest) -> dict:
    """Moss triage for SCA, enrollment, and cross-workflow alert signals."""
    from agents.semantic_matcher import get_matcher

    matcher = get_matcher()
    if not (matcher and matcher.ready and matcher.moss_connected):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Moss workflow index is not ready.")
    result = matcher.triage_workflow(req.workflow, req.signal)
    result["privacy"] = "Only the supplied de-identified operational signal was queried."
    return result


@router.get("/finale-readiness", summary="Verify the live-demo dependencies without exposing credentials")
def finale_readiness() -> dict:
    """Return a narrow, honest preflight for the six-minute live demonstration.

    This is intentionally not a generic health endpoint.  It reports the three
    claims a finalist is about to demonstrate: that the primary counterfactual
    case is bundled, Moss is actually serving semantic retrieval (rather than
    the lexical safety fallback), and the optional LLM copilot is configured.
    Secret values and raw document content are never returned.
    """
    from agents.semantic_matcher import get_matcher

    matcher = get_matcher()
    semantic = matcher.stats() if matcher else {}
    # `DEMO_CASES` is keyed by id; keep this lookup explicit so a missing
    # primary case becomes a visible preflight failure instead of a crash.
    primary = DEMO_CASES.get("regional_reworded")
    _filename, primary_pdf = get_demo_pdf_bytes("regional_reworded")
    guidance = get_recovery_guidance_service().status()

    moss_live = bool(semantic.get("ready") and semantic.get("moss_connected"))
    demo_available = bool(primary and primary_pdf)
    copilot_ready = bool(guidance.get("enabled"))

    return {
        "status": "READY" if (moss_live and demo_available) else "ATTENTION_REQUIRED",
        "primary_case": {
            "id": "regional_reworded",
            "available": demo_available,
            "objective": "Prove that live Moss retrieval catches a reworded $3,240 payer offset that deterministic rules miss.",
        },
        "moss": {
            "live": moss_live,
            "engine": semantic.get("engine"),
            "corpus_size": semantic.get("corpus_size", 0),
            "fallback_reason": None if moss_live else semantic.get("fallback_reason") or semantic.get("disabled_reason"),
        },
        "copilot": {
            "enabled": copilot_ready,
            "model": guidance.get("model") if copilot_ready else None,
            "privacy": "Only allowlisted, de-identified case metadata is sent to the optional copilot.",
        },
        "runbook": [
            "Run the reworded-offset counterfactual.",
            "Show the retrieval trace declining benign adjustment lines.",
            "Open the human-review action and dispute packet.",
            "Use the copilot only after the human decision, with de-identified metadata.",
        ],
    }


@router.post("/semantic/run-eval", summary="Execute the 304-line held-out scientific benchmark live")
@router.post("/run-eval", summary="Execute the 304-line held-out scientific benchmark live")
def run_live_eval() -> dict:
    """
    Runs the held-out evaluation dataset (152 clawbacks, 152 benign lines)
    live against both regex and semantic engines.
    Returns empirical confusion matrix and recall delta in <200ms.
    """
    import time
    try:
        import eval_data
        import eval_semantic
        from agents.semantic_matcher import get_matcher

        lines = eval_data.build_eval_set()
        matcher = get_matcher()

        t0 = time.perf_counter()
        obs = eval_semantic.collect(lines, matcher)
        collect_ms = (time.perf_counter() - t0) * 1000.0

        reg_pairs = [(o.line.is_recoupment, o.regex_detected) for o in obs]
        reg_conf = eval_semantic._confusion(reg_pairs)

        threshold = getattr(matcher, "threshold", 0.45)
        sem_pairs = [(o.line.is_recoupment, eval_semantic.decide(o, threshold, True)) for o in obs]
        sem_conf = eval_semantic._confusion(sem_pairs)

        missed_by_regex = sum(1 for o in obs if o.line.is_recoupment and not o.regex_detected)
        caught_by_semantic = sum(1 for o in obs if o.line.is_recoupment and eval_semantic.decide(o, threshold, True))

        return {
            "status": "EVALUATION_COMPLETE",
            "total_lines_tested": len(lines),
            "clawback_lines": 152,
            "benign_lines": 152,
            "duration_ms": round(collect_ms, 1),
            "regex_baseline": {
                "recall": round(reg_conf.get("recall", 0.0) * 100, 1),
                "precision": round(reg_conf.get("precision", 1.0) * 100, 1),
                "missed_clawbacks": missed_by_regex,
                "f1": round(reg_conf.get("f1", 0.0), 3),
                "true_positives": reg_conf.get("true_positives", 0),
                "false_positives": reg_conf.get("false_positives", 0),
            },
            "hybrid_semantic": {
                "recall": round(sem_conf.get("recall", 0.0) * 100, 1),
                "precision": round(sem_conf.get("precision", 1.0) * 100, 1),
                "total_caught": caught_by_semantic,
                "f1": round(sem_conf.get("f1", 0.0), 3),
                "true_positives": sem_conf.get("true_positives", 0),
                "false_positives": sem_conf.get("false_positives", 0),
            },
            "recall_gain": f"+{round((sem_conf.get('recall', 0) - reg_conf.get('recall', 0)) * 100, 1)}%",
            "operating_threshold": threshold,
        }
    except Exception as exc:
        logger.exception("Live eval failed")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))
