"""
FastAPI router — /api/review

Human-in-the-loop review queue for the behavioral health RCM platform.
All endpoints operate on HumanReview records created by EscalationAgent.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.models.base import get_db
from api.models.human_review import HumanReview
from agents.escalation_agent import EscalationAgent
from api.services.feedback_calibrator import FeedbackCalibrator

router = APIRouter(prefix="/api/review", tags=["human-review"])
_agent = EscalationAgent()


# ---------------------------------------------------------------------------
# Pydantic schemas (inline, Python 3.9 compatible)
# ---------------------------------------------------------------------------


class ReviewTicketOut(BaseModel):
    id: int
    ticket_type: str
    source_agent: str
    reason: str
    context: Optional[Dict[str, Any]]  # parsed from context_json
    status: str
    reviewer_notes: Optional[str]
    created_at: datetime
    reviewed_at: Optional[datetime]

    class Config:
        from_attributes = True  # Pydantic v2; also works as orm_mode alias


class ApproveRequest(BaseModel):
    reviewer_notes: Optional[str] = ""


class DismissRequest(BaseModel):
    reviewer_notes: Optional[str] = ""


class StatsOut(BaseModel):
    pending: int
    approved: int
    dismissed: int
    total: int
    avg_resolution_hours: Optional[float]  # None when no resolved tickets yet


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ticket_to_out(ticket: HumanReview) -> ReviewTicketOut:
    ctx: Optional[Dict[str, Any]] = None
    if ticket.context_json:
        try:
            ctx = json.loads(ticket.context_json)
        except (ValueError, TypeError):
            ctx = {"raw": ticket.context_json}

    return ReviewTicketOut(
        id=ticket.id,
        ticket_type=ticket.ticket_type,
        source_agent=ticket.source_agent,
        reason=ticket.reason,
        context=ctx,
        status=ticket.status,
        reviewer_notes=ticket.reviewer_notes,
        created_at=ticket.created_at,
        reviewed_at=ticket.reviewed_at,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/pending", response_model=List[ReviewTicketOut])
def list_pending(db: Session = Depends(get_db)):
    """Return all tickets currently awaiting human review."""
    tickets = _agent.get_pending(db)
    return [_ticket_to_out(t) for t in tickets]


@router.get("/stats", response_model=StatsOut)
def get_stats(db: Session = Depends(get_db)):
    """
    Aggregate counts and average resolution time.

    avg_resolution_hours is computed over all approved + dismissed tickets
    that have both created_at and reviewed_at populated.
    """
    all_tickets = db.query(HumanReview).all()

    pending = sum(1 for t in all_tickets if t.status == "pending")
    approved = sum(1 for t in all_tickets if t.status == "approved")
    dismissed = sum(1 for t in all_tickets if t.status == "dismissed")
    total = len(all_tickets)

    resolution_hours: List[float] = []
    for t in all_tickets:
        if t.status in ("approved", "dismissed") and t.created_at and t.reviewed_at:
            delta = t.reviewed_at - t.created_at
            resolution_hours.append(delta.total_seconds() / 3600)

    avg_res = (sum(resolution_hours) / len(resolution_hours)) if resolution_hours else None

    return StatsOut(
        pending=pending,
        approved=approved,
        dismissed=dismissed,
        total=total,
        avg_resolution_hours=round(avg_res, 2) if avg_res is not None else None,
    )


@router.get("/calibration")
def get_calibration(db: Session = Depends(get_db)):
    """
    Moat dashboard — shows how ValidatorAgent thresholds are self-improving
    based on accumulated human review decisions.

    Returns per-payer false-positive rates, threshold adjustments, and an
    auto-generated plain-English recommendation.
    """
    return FeedbackCalibrator.get_calibration_report(db)


@router.get("/{ticket_id}", response_model=ReviewTicketOut)
def get_ticket(ticket_id: int, db: Session = Depends(get_db)):
    """Retrieve a single ticket with context_json fully parsed."""
    ticket = db.query(HumanReview).filter(HumanReview.id == ticket_id).first()
    if not ticket:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Ticket #{ticket_id} not found.",
        )
    return _ticket_to_out(ticket)


@router.post("/{ticket_id}/approve", response_model=ReviewTicketOut)
def approve_ticket(
    ticket_id: int,
    body: ApproveRequest = ApproveRequest(),
    db: Session = Depends(get_db),
):
    """Approve a pending ticket."""
    try:
        ticket = _agent.approve(db, ticket_id, reviewer_notes=body.reviewer_notes or "")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return _ticket_to_out(ticket)


@router.post("/{ticket_id}/dismiss", response_model=ReviewTicketOut)
def dismiss_ticket(
    ticket_id: int,
    body: DismissRequest = DismissRequest(),
    db: Session = Depends(get_db),
):
    """Dismiss a ticket — no further processing will occur."""
    try:
        ticket = _agent.dismiss(db, ticket_id, reviewer_notes=body.reviewer_notes or "")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return _ticket_to_out(ticket)
