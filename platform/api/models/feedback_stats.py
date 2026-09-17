"""
FeedbackStats — aggregated outcomes by (payer_tag, escalation_type, source_agent).

Drives the feedback calibration loop: every human review decision
(approve/dismiss) becomes a training signal that adjusts ValidatorAgent
confidence thresholds over time.

Python 3.9 compatible. No new pip dependencies.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint

from .base import Base


class FeedbackStats(Base):
    __tablename__ = "feedback_stats"

    # Composite uniqueness: one row per (payer_tag, escalation_type, source_agent)
    __table_args__ = (
        UniqueConstraint(
            "payer_tag", "escalation_type", "source_agent",
            name="uq_feedback_stats_combo",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    payer_tag = Column(String(64), nullable=False)          # "anthem", "aetna", "generic", "ledger", …
    escalation_type = Column(String(64), nullable=False)    # "large_amount", "ambiguous_flag", …
    source_agent = Column(String(64), nullable=False)       # "RecoupmentAgent", "ValidatorAgent", …
    total_escalated = Column(Integer, nullable=False, default=0)
    approved = Column(Integer, nullable=False, default=0)   # human said "yes, real recoupment"
    dismissed = Column(Integer, nullable=False, default=0)  # human said "false positive"
    last_updated = Column(DateTime, nullable=False, default=datetime.utcnow)

    # ------------------------------------------------------------------
    # Derived properties (not stored; computed on access)
    # ------------------------------------------------------------------

    @property
    def false_positive_rate(self) -> float:
        """dismissed / total_escalated; 0.0 when no data."""
        if self.total_escalated and self.total_escalated > 0:
            return round(self.dismissed / self.total_escalated, 4)
        return 0.0

    @property
    def confidence_adjustment(self) -> float:
        """
        How much to shift ValidatorAgent's 0.5 confidence threshold for
        this (payer_tag, escalation_type, source_agent) combo.

        fpr > 0.70  →  +0.20  (too many false positives — raise the bar)
        fpr > 0.50  →  +0.10
        fpr < 0.20  →  -0.10  (almost always real — lower the bar)
        else        →   0.00
        """
        fpr = self.false_positive_rate
        if fpr > 0.70:
            return 0.20
        if fpr > 0.50:
            return 0.10
        if fpr < 0.20:
            return -0.10
        return 0.0

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<FeedbackStats payer={self.payer_tag!r} type={self.escalation_type!r} "
            f"agent={self.source_agent!r} fpr={self.false_positive_rate:.2f} "
            f"adj={self.confidence_adjustment:+.2f}>"
        )
