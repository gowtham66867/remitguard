"""
SCA (Single Case Agreement) service layer.

Business context: payers approve a fixed number of visits for a specific
patient with a specific provider between specific dates.  A claim can deny
mid-treatment when:
  - visits are exhausted (used_visits >= approved_visits)
  - the agreement has expired (today > end_date)
  - the provider is not contracted with State Medicaid even though the SCA
    itself is approved (contracting_risk)
  - fewer than 4 visits remain OR fewer than 30 days to expiry (warning)
"""

from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from api.models.sca import SCA


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _alert_reason(sca: SCA) -> Optional[str]:
    """Return a human-readable reason string when an SCA is not 'active',
    or None when the SCA is active (no alert needed)."""
    status = sca.status  # computed property on the model
    today = date.today()
    days_to_expiry = (sca.end_date - today).days

    if status == "exhausted":
        return (
            f"All {sca.approved_visits} approved visits have been used. "
            "No further claims will be paid under this SCA."
        )
    if status == "expired":
        return (
            f"SCA expired {abs(days_to_expiry)} day(s) ago "
            f"(end date {sca.end_date}).  Claims after expiry will deny."
        )
    if status == "contracting_risk":
        return (
            f"Provider '{sca.provider_name}' is not contracted with State "
            "Medicaid.  Claims may deny despite an approved SCA."
        )
    if status == "warning":
        parts: List[str] = []
        if sca.remaining_visits <= 3:
            parts.append(f"only {sca.remaining_visits} visit(s) remaining")
        if days_to_expiry <= 30:
            parts.append(f"expires in {days_to_expiry} day(s)")
        return "Warning: " + "; ".join(parts) + ".  Request renewal or extension."
    return None  # active — no alert


# ---------------------------------------------------------------------------
# service class
# ---------------------------------------------------------------------------

class SCAService:
    """All database operations for the SCA domain."""

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------

    @staticmethod
    def create_sca(db: Session, data: dict) -> SCA:
        """Persist a new SCA record and return it."""
        sca = SCA(**data)
        db.add(sca)
        db.commit()
        db.refresh(sca)
        return sca

    @staticmethod
    def update_visits(db: Session, sca_id: int, used_visits: int) -> SCA:
        """
        Set used_visits for the given SCA.

        Raises ValueError when sca_id is not found.
        Raises ValueError when used_visits exceeds approved_visits (guard
        against data-entry errors — the column can legitimately hit the cap,
        but should never exceed it).
        """
        sca: Optional[SCA] = db.query(SCA).filter(SCA.id == sca_id).first()
        if sca is None:
            raise ValueError(f"SCA with id={sca_id} not found.")
        if used_visits > sca.approved_visits:
            raise ValueError(
                f"used_visits ({used_visits}) cannot exceed "
                f"approved_visits ({sca.approved_visits})."
            )
        sca.used_visits = used_visits
        db.commit()
        db.refresh(sca)
        return sca

    @staticmethod
    def delete_sca(db: Session, sca_id: int) -> bool:
        """Hard-delete an SCA record.  Returns True if deleted, False if not found."""
        sca: Optional[SCA] = db.query(SCA).filter(SCA.id == sca_id).first()
        if sca is None:
            return False
        db.delete(sca)
        db.commit()
        return True

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    @staticmethod
    def get_all(db: Session, facility_id: Optional[int] = None) -> List[SCA]:
        """Return all SCA records, optionally filtered by facility."""
        q = db.query(SCA)
        if facility_id is not None:
            q = q.filter(SCA.facility_id == facility_id)
        return q.order_by(SCA.end_date.asc()).all()

    @staticmethod
    def get_by_id(db: Session, sca_id: int) -> Optional[SCA]:
        return db.query(SCA).filter(SCA.id == sca_id).first()

    @staticmethod
    def get_alerts(
        db: Session, facility_id: Optional[int] = None
    ) -> List[Dict]:
        """
        Return all SCAs whose status is not 'active', each enriched with an
        alert_reason string that explains why the SCA is at risk.

        Shape of each item:
            {
                "id": int,
                "patient_name": str,
                "provider_name": str,
                "payer_name": str,
                "status": str,
                "remaining_visits": int,
                "days_to_expiry": int,
                "alert_reason": str,
                ... (all other SCA scalar fields)
            }
        """
        today = date.today()
        scas = SCAService.get_all(db, facility_id=facility_id)
        alerts = []
        for sca in scas:
            if sca.status == "active":
                continue
            reason = _alert_reason(sca)
            alerts.append(
                {
                    "id": sca.id,
                    "facility_id": sca.facility_id,
                    "patient_name": sca.patient_name,
                    "payer_name": sca.payer_name,
                    "payer_id": sca.payer_id,
                    "provider_name": sca.provider_name,
                    "approved_visits": sca.approved_visits,
                    "used_visits": sca.used_visits,
                    "remaining_visits": sca.remaining_visits,
                    "start_date": sca.start_date.isoformat(),
                    "end_date": sca.end_date.isoformat(),
                    "days_to_expiry": (sca.end_date - today).days,
                    "medicaid_contracted": sca.medicaid_contracted,
                    "notes": sca.notes,
                    "status": sca.status,
                    "alert_reason": reason,
                    "created_at": sca.created_at.isoformat() if sca.created_at else None,
                    "updated_at": sca.updated_at.isoformat() if sca.updated_at else None,
                }
            )
        return alerts

    @staticmethod
    def status_summary(
        db: Session, facility_id: Optional[int] = None
    ) -> Dict[str, int]:
        """
        Return a count breakdown of all SCA statuses.

        Keys: active, warning, exhausted, expired, contracting_risk, total
        """
        scas = SCAService.get_all(db, facility_id=facility_id)
        summary: Dict[str, int] = {
            "active": 0,
            "warning": 0,
            "exhausted": 0,
            "expired": 0,
            "contracting_risk": 0,
            "total": len(scas),
        }
        for sca in scas:
            status = sca.status
            if status in summary:
                summary[status] += 1
        return summary
