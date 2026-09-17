"""
EscalationAgent — human-in-the-loop routing for uncertain RCM cases.

Escalation types
----------------
  low_confidence_extraction  — NLP/OCR confidence below threshold
  ambiguous_flag             — multiple possible denial reasons
  large_amount               — any flag where amount > $10,000
  new_payer_pattern          — payer_tag == "generic" AND amount > $5,000
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from api.models.human_review import HumanReview
from api.services.feedback_calibrator import FeedbackCalibrator

LARGE_AMOUNT_THRESHOLD = 10_000.00
NEW_PAYER_AMOUNT_THRESHOLD = 5_000.00

VALID_ESCALATION_TYPES = {
    "low_confidence_extraction",
    "ambiguous_flag",
    "large_amount",
    "new_payer_pattern",
}


class EscalationAgent:
    """Routes uncertain cases to a persistent human-review queue."""

    # ------------------------------------------------------------------
    # Core escalation
    # ------------------------------------------------------------------

    def escalate(
        self,
        db: Session,
        reason: str,
        context_dict: Dict,
        escalation_type: str,
        source_agent: str,
    ) -> HumanReview:
        """
        Create a HumanReview record with status="pending".

        Auto-escalation rules applied on top of the caller-supplied type:
          • amount > $10,000  → forces escalation_type = "large_amount"
          • payer_tag == "generic" AND amount > $5,000
                              → forces escalation_type = "new_payer_pattern"

        Returns the persisted HumanReview ORM object (ticket).
        """
        if escalation_type not in VALID_ESCALATION_TYPES:
            raise ValueError(
                f"Invalid escalation_type '{escalation_type}'. "
                f"Must be one of: {sorted(VALID_ESCALATION_TYPES)}"
            )

        # Apply automatic override rules
        amount = float(context_dict.get("amount", 0) or 0)
        payer_tag = str(context_dict.get("payer_tag", "") or "")

        if amount > LARGE_AMOUNT_THRESHOLD:
            escalation_type = "large_amount"
        elif payer_tag.lower() == "generic" and amount > NEW_PAYER_AMOUNT_THRESHOLD:
            escalation_type = "new_payer_pattern"

        ticket = HumanReview(
            ticket_type=escalation_type,
            source_agent=source_agent,
            reason=reason,
            context_json=json.dumps(context_dict),
            status="pending",
            reviewer_notes=None,
            created_at=datetime.utcnow(),
            reviewed_at=None,
        )
        db.add(ticket)
        db.commit()
        db.refresh(ticket)

        print(
            f"[EscalationAgent] ESCALATED ticket=#{ticket.id} "
            f"type={ticket.ticket_type} reason=\"{reason}\""
        )

        return ticket

    # ------------------------------------------------------------------
    # Queue management
    # ------------------------------------------------------------------

    def get_pending(self, db: Session) -> List[HumanReview]:
        """Return all tickets with status='pending', oldest first."""
        return (
            db.query(HumanReview)
            .filter(HumanReview.status == "pending")
            .order_by(HumanReview.created_at)
            .all()
        )

    def approve(
        self,
        db: Session,
        ticket_id: int,
        reviewer_notes: str = "",
    ) -> HumanReview:
        """
        Approve a pending ticket.

        Sets status='approved', stamps reviewed_at, stores reviewer_notes.
        Approved tickets are intended to trigger downstream auto-processing
        by the originating agent (polling or event-driven).
        """
        ticket = self._get_or_raise(db, ticket_id)
        ticket.status = "approved"
        ticket.reviewer_notes = reviewer_notes
        ticket.reviewed_at = datetime.utcnow()
        db.commit()
        db.refresh(ticket)

        print(
            f"[EscalationAgent] APPROVED ticket=#{ticket_id} "
            f"notes=\"{reviewer_notes}\""
        )

        FeedbackCalibrator.record_outcome(db, ticket)
        return ticket

    def dismiss(
        self,
        db: Session,
        ticket_id: int,
        reviewer_notes: str = "",
    ) -> HumanReview:
        """Dismiss a pending ticket (no further processing)."""
        ticket = self._get_or_raise(db, ticket_id)
        ticket.status = "dismissed"
        ticket.reviewer_notes = reviewer_notes
        ticket.reviewed_at = datetime.utcnow()
        db.commit()
        db.refresh(ticket)

        print(
            f"[EscalationAgent] DISMISSED ticket=#{ticket_id} "
            f"notes=\"{reviewer_notes}\""
        )

        FeedbackCalibrator.record_outcome(db, ticket)
        return ticket

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_or_raise(db: Session, ticket_id: int) -> HumanReview:
        ticket = db.query(HumanReview).filter(HumanReview.id == ticket_id).first()
        if ticket is None:
            raise ValueError(f"HumanReview ticket #{ticket_id} not found.")
        return ticket
