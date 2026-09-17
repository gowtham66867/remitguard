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
        self._teach_semantic_layer(ticket)
        return ticket

    # ------------------------------------------------------------------
    # Retrieval-side learning loop
    # ------------------------------------------------------------------

    @staticmethod
    def _teach_semantic_layer(ticket: HumanReview) -> int:
        """
        Add the approved ticket's flagged line(s) to the Moss index.

        This is the retrieval counterpart to FeedbackCalibrator: dismissals
        tighten confidence thresholds, approvals widen what the system can
        recognise. A coordinator confirming one Anthem rewording means the next
        EOB carrying that phrasing — from any payer — is caught on the first
        pass instead of slipping through.

        Best-effort and never raises: an unavailable Moss layer just means the
        phrase is not learned.
        """
        try:
            from agents.semantic_matcher import get_matcher
            matcher = get_matcher()
            if not matcher.ready:
                return 0

            context = json.loads(ticket.context_json) if ticket.context_json else {}
            candidates = []
            if isinstance(context.get("flag"), dict):
                candidates.append(context["flag"])
            if isinstance(context.get("flags"), list):
                candidates.extend(f for f in context["flags"] if isinstance(f, dict))

            learned = 0
            for flag in candidates:
                # Only learn genuine EOB text. Ledger-mismatch flags are
                # synthesised sentences, not payer wording.
                if flag.get("source") == "ledger_mismatch":
                    continue
                line = (flag.get("line") or "").strip()
                if not line:
                    continue
                if matcher.learn(line, payer_tag=flag.get("payer_tag") or "generic"):
                    learned += 1

            if learned:
                print(
                    f"[EscalationAgent] taught Moss {learned} confirmed "
                    f"clawback phrase(s) from ticket #{ticket.id}"
                )
            return learned
        except Exception as exc:  # pragma: no cover - optional layer
            print(f"[EscalationAgent] semantic learning skipped: {exc}")
            return 0

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
