"""
FeedbackCalibrator — translates human review outcomes into ValidatorAgent
confidence threshold adjustments.

This is the "moat compounding" mechanism: every approve/dismiss decision
upserts FeedbackStats and recomputes per-payer threshold deltas so that
ValidatorAgent learns from past human judgement automatically.

Python 3.9 compatible. No new pip dependencies.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from api.models.feedback_stats import FeedbackStats
from api.models.human_review import HumanReview

logger = logging.getLogger(__name__)

_DEFAULT_PAYER_TAG = "generic"


class FeedbackCalibrator:
    """Static-method service; no instance state."""

    # ------------------------------------------------------------------
    # Write path — called after approve / dismiss
    # ------------------------------------------------------------------

    @staticmethod
    def record_outcome(db: Session, ticket: HumanReview) -> None:
        """
        Upsert FeedbackStats for the ticket's
        (payer_tag, escalation_type, source_agent) combo.

        Extracts payer_tag from ticket.context_json when present,
        falling back to "generic".

        Should be called after ticket.status has been set to
        "approved" or "dismissed" and committed to the DB.
        """
        if ticket.status not in ("approved", "dismissed"):
            logger.warning(
                "[FeedbackCalibrator] record_outcome called on ticket #%s "
                "with status=%r — skipping (expected approved/dismissed).",
                ticket.id,
                ticket.status,
            )
            return

        # --- Resolve payer_tag from context_json ----------------------
        payer_tag: str = _DEFAULT_PAYER_TAG
        if ticket.context_json:
            try:
                ctx: Dict[str, Any] = json.loads(ticket.context_json)
                raw = ctx.get("payer_tag") or ctx.get("payer") or _DEFAULT_PAYER_TAG
                payer_tag = str(raw).strip().lower() or _DEFAULT_PAYER_TAG
            except (ValueError, TypeError):
                pass  # keep default

        escalation_type = ticket.ticket_type or "unknown"
        source_agent = ticket.source_agent or "unknown"

        # --- Upsert row -----------------------------------------------
        row: Optional[FeedbackStats] = (
            db.query(FeedbackStats)
            .filter(
                FeedbackStats.payer_tag == payer_tag,
                FeedbackStats.escalation_type == escalation_type,
                FeedbackStats.source_agent == source_agent,
            )
            .first()
        )

        if row is None:
            row = FeedbackStats(
                payer_tag=payer_tag,
                escalation_type=escalation_type,
                source_agent=source_agent,
                total_escalated=0,
                approved=0,
                dismissed=0,
                last_updated=datetime.utcnow(),
            )
            db.add(row)

        row.total_escalated += 1
        if ticket.status == "approved":
            row.approved += 1
        else:
            row.dismissed += 1
        row.last_updated = datetime.utcnow()

        db.commit()
        db.refresh(row)

        print(
            f"[FeedbackCalibrator] Recorded outcome ticket=#{ticket.id} "
            f"payer_tag={payer_tag!r} type={escalation_type!r} "
            f"status={ticket.status!r} → "
            f"fpr={row.false_positive_rate:.2f} adj={row.confidence_adjustment:+.2f}"
        )

    # ------------------------------------------------------------------
    # Read path — consumed by ValidatorAgent
    # ------------------------------------------------------------------

    @staticmethod
    def get_threshold_adjustments(db: Session) -> Dict[str, float]:
        """
        Return a dict of net confidence-threshold adjustments keyed by payer_tag.

        The adjustment is the mean of confidence_adjustment across all
        escalation_type / source_agent rows for that payer.

        Example return value:
            {
                "generic": 0.15,   # high false-positive rate → raise threshold
                "anthem":  -0.05,  # almost always real → lower threshold
                "ledger":   0.10,
            }
        """
        rows: List[FeedbackStats] = db.query(FeedbackStats).all()

        # Bucket adjustments by payer_tag
        by_payer: Dict[str, List[float]] = {}
        for row in rows:
            tag = row.payer_tag
            by_payer.setdefault(tag, []).append(row.confidence_adjustment)

        # Average per payer; round to 2 dp
        return {
            tag: round(sum(adjs) / len(adjs), 2)
            for tag, adjs in by_payer.items()
            if adjs
        }

    # ------------------------------------------------------------------
    # Calibration report — moat dashboard
    # ------------------------------------------------------------------

    @staticmethod
    def get_calibration_report(db: Session) -> Dict[str, Any]:
        """
        Return full calibration state suitable for display in a dashboard.

        Shape:
            {
                "total_reviewed": int,
                "overall_false_positive_rate": float,
                "by_payer": [
                    {
                        "payer_tag": str,
                        "escalated": int,
                        "approved": int,
                        "dismissed": int,
                        "fpr": float,
                        "adjustment": float,
                    },
                    ...  # sorted by escalated desc
                ],
                "recommendation": str,
            }
        """
        rows: List[FeedbackStats] = db.query(FeedbackStats).all()

        total_escalated = sum(r.total_escalated for r in rows)
        total_dismissed = sum(r.dismissed for r in rows)
        overall_fpr = (
            round(total_dismissed / total_escalated, 4)
            if total_escalated > 0
            else 0.0
        )

        # Aggregate to payer level
        payer_map: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            tag = row.payer_tag
            if tag not in payer_map:
                payer_map[tag] = {
                    "payer_tag": tag,
                    "escalated": 0,
                    "approved": 0,
                    "dismissed": 0,
                    "_adj_sum": 0.0,
                    "_adj_count": 0,
                }
            payer_map[tag]["escalated"] += row.total_escalated
            payer_map[tag]["approved"] += row.approved
            payer_map[tag]["dismissed"] += row.dismissed
            payer_map[tag]["_adj_sum"] += row.confidence_adjustment
            payer_map[tag]["_adj_count"] += 1

        by_payer = []
        for tag, agg in payer_map.items():
            esc = agg["escalated"]
            dis = agg["dismissed"]
            fpr = round(dis / esc, 4) if esc > 0 else 0.0
            adj = (
                round(agg["_adj_sum"] / agg["_adj_count"], 2)
                if agg["_adj_count"] > 0
                else 0.0
            )
            by_payer.append(
                {
                    "payer_tag": tag,
                    "escalated": esc,
                    "approved": agg["approved"],
                    "dismissed": dis,
                    "fpr": fpr,
                    "adjustment": adj,
                }
            )

        by_payer.sort(key=lambda x: x["escalated"], reverse=True)

        # Generate a plain-English recommendation
        recommendation = _build_recommendation(by_payer, overall_fpr)

        return {
            "total_reviewed": total_escalated,
            "overall_false_positive_rate": overall_fpr,
            "by_payer": by_payer,
            "recommendation": recommendation,
        }


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------


def _build_recommendation(
    by_payer: List[Dict[str, Any]],
    overall_fpr: float,
) -> str:
    if not by_payer:
        return "No review data yet — calibration will begin once tickets are resolved."

    high_fpr = [p for p in by_payer if p["fpr"] > 0.5 and p["escalated"] >= 5]
    low_fpr = [p for p in by_payer if p["fpr"] < 0.2 and p["escalated"] >= 5]

    parts: List[str] = []

    if high_fpr:
        tags = ", ".join(p["payer_tag"] for p in high_fpr)
        parts.append(
            f"High false-positive rate on [{tags}] — "
            "consider tightening patterns.json phrases or raising the confidence gate."
        )

    if low_fpr:
        tags = ", ".join(p["payer_tag"] for p in low_fpr)
        parts.append(
            f"Near-zero false positives on [{tags}] — "
            "threshold can be safely lowered to catch more edge cases."
        )

    if overall_fpr > 0.5:
        parts.append(
            "Overall false-positive rate is above 50 % — "
            "review detection patterns for overly broad matches."
        )

    if not parts:
        parts.append(
            f"Overall false-positive rate is {overall_fpr:.0%}. "
            "System is performing within normal bounds."
        )

    return "  ".join(parts)
