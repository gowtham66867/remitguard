"""
enrollment_service.py
---------------------
Business logic for ERA (Electronic Remittance Advice) and
EFT (Electronic Funds Transfer) enrollment management.

ERA enrollment = automatic payment posting per payer/provider.
EFT enrollment = bank account routing for payer remittances.

Missing ERA → manual posting → AR aging risk.
Missing EFT → paper checks or wrong bank account routing.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import date, timedelta
from typing import Dict, List, Optional

from sqlalchemy import and_
from sqlalchemy.orm import Session

from api.models.enrollment import EFTEnrollment, ERAEnrollment

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Valid status literals (kept here so routes can import them too)
# ---------------------------------------------------------------------------
ERA_STATUSES = {"enrolled", "pending", "missing"}
EFT_STATUSES = {"enrolled", "pending", "missing", "action_required"}

URGENT_DAYS = 7  # deadline within this many days → urgent


class EnrollmentServiceError(Exception):
    """Raised for domain-level errors inside EnrollmentService."""


class EnrollmentService:
    """
    Stateless service layer for ERA/EFT enrollment operations.

    All methods accept an SQLAlchemy ``Session`` as their first argument so
    callers control transaction boundaries (FastAPI dependency injection,
    unit tests, CLI scripts, etc.).
    """

    # ------------------------------------------------------------------
    # Bulk import
    # ------------------------------------------------------------------

    @staticmethod
    def import_payer_list(
        db: Session,
        facility_id: int,
        rows: List[Dict[str, str]],
    ) -> int:
        """
        Bulk-import ERA + EFT records from a list of CSV row dicts.

        Expected keys per row (all optional except payer_id / payer_name):
            payer_id, payer_name, era_status, eft_status,
            bank_name, eft_deadline (YYYY-MM-DD), portal, notes

        If a record for (facility_id, payer_id) already exists it is
        **updated** rather than duplicated.  Returns the number of
        payer rows processed (each row creates/updates 1 ERA + 1 EFT record).
        """
        if not rows:
            return 0

        imported = 0
        for row in rows:
            payer_id: str = (row.get("payer_id") or "").strip()
            payer_name: str = (row.get("payer_name") or "").strip()

            if not payer_id or not payer_name:
                logger.warning("Skipping row missing payer_id or payer_name: %s", row)
                continue

            era_status = _clean_status(row.get("era_status"), ERA_STATUSES, "missing")
            eft_status = _clean_status(row.get("eft_status"), EFT_STATUSES, "missing")
            bank_name: Optional[str] = row.get("bank_name") or None
            portal: Optional[str] = row.get("portal") or None
            notes: Optional[str] = row.get("notes") or None
            eft_deadline: Optional[date] = _parse_date(row.get("eft_deadline"))

            # --- ERA ---
            era = (
                db.query(ERAEnrollment)
                .filter(
                    ERAEnrollment.facility_id == facility_id,
                    ERAEnrollment.payer_id == payer_id,
                )
                .first()
            )
            if era is None:
                era = ERAEnrollment(
                    facility_id=facility_id,
                    payer_id=payer_id,
                    payer_name=payer_name,
                    status=era_status,
                    notes=notes,
                )
                db.add(era)
            else:
                era.payer_name = payer_name
                era.status = era_status
                if notes is not None:
                    era.notes = notes

            # --- EFT ---
            eft = (
                db.query(EFTEnrollment)
                .filter(
                    EFTEnrollment.facility_id == facility_id,
                    EFTEnrollment.payer_id == payer_id,
                )
                .first()
            )
            if eft is None:
                eft = EFTEnrollment(
                    facility_id=facility_id,
                    payer_id=payer_id,
                    payer_name=payer_name,
                    status=eft_status,
                    bank_name=bank_name,
                    deadline=eft_deadline,
                    portal=portal,
                    notes=notes,
                )
                db.add(eft)
            else:
                eft.payer_name = payer_name
                eft.status = eft_status
                if bank_name is not None:
                    eft.bank_name = bank_name
                if eft_deadline is not None:
                    eft.deadline = eft_deadline
                if portal is not None:
                    eft.portal = portal
                if notes is not None:
                    eft.notes = notes

            imported += 1

        db.commit()
        logger.info(
            "import_payer_list: processed %d payers for facility_id=%s",
            imported,
            facility_id,
        )
        return imported

    # ------------------------------------------------------------------
    # ERA queries
    # ------------------------------------------------------------------

    @staticmethod
    def get_era_status(
        db: Session,
        facility_id: Optional[int] = None,
        status: Optional[str] = None,
    ) -> List[ERAEnrollment]:
        """Return ERA enrollments, optionally filtered by facility and status."""
        q = db.query(ERAEnrollment)
        if facility_id is not None:
            q = q.filter(ERAEnrollment.facility_id == facility_id)
        if status is not None:
            q = q.filter(ERAEnrollment.status == status)
        return q.order_by(ERAEnrollment.payer_name).all()

    @staticmethod
    def get_missing_era(
        db: Session,
        facility_id: Optional[int] = None,
    ) -> List[ERAEnrollment]:
        """Return all ERA records with status == 'missing'."""
        return EnrollmentService.get_era_status(
            db, facility_id=facility_id, status="missing"
        )

    # ------------------------------------------------------------------
    # EFT queries
    # ------------------------------------------------------------------

    @staticmethod
    def get_eft_status(
        db: Session,
        facility_id: Optional[int] = None,
        status: Optional[str] = None,
    ) -> List[EFTEnrollment]:
        """Return EFT enrollments, optionally filtered by facility and status."""
        q = db.query(EFTEnrollment)
        if facility_id is not None:
            q = q.filter(EFTEnrollment.facility_id == facility_id)
        if status is not None:
            q = q.filter(EFTEnrollment.status == status)
        return q.order_by(EFTEnrollment.payer_name).all()

    @staticmethod
    def get_urgent_eft(
        db: Session,
        facility_id: Optional[int] = None,
    ) -> List[EFTEnrollment]:
        """
        Return EFT records whose deadline falls within URGENT_DAYS days from today.

        This is the "fire list" — these enrollments need immediate action to
        avoid payment disruption (e.g. the US Bank → Frost Bank crisis where
        25+ payers needed re-enrollment by end-of-week).
        """
        cutoff = date.today() + timedelta(days=URGENT_DAYS)
        q = db.query(EFTEnrollment).filter(
            EFTEnrollment.deadline != None,  # noqa: E711
            EFTEnrollment.deadline <= cutoff,
        )
        if facility_id is not None:
            q = q.filter(EFTEnrollment.facility_id == facility_id)
        # Soonest deadline first — highest priority at top
        return q.order_by(EFTEnrollment.deadline.asc()).all()

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    @staticmethod
    def update_era(
        db: Session,
        enrollment_id: int,
        status: str,
        enrolled_date: Optional[date] = None,
    ) -> ERAEnrollment:
        """
        Update the status (and optionally enrolled_date) of an ERA record.

        Raises EnrollmentServiceError if the record does not exist or the
        status value is not in ERA_STATUSES.
        """
        if status not in ERA_STATUSES:
            raise EnrollmentServiceError(
                f"Invalid ERA status '{status}'. Must be one of: {ERA_STATUSES}"
            )

        era = db.query(ERAEnrollment).filter(ERAEnrollment.id == enrollment_id).first()
        if era is None:
            raise EnrollmentServiceError(
                f"ERAEnrollment id={enrollment_id} not found"
            )

        era.status = status
        if enrolled_date is not None:
            era.enrolled_date = enrolled_date
        elif status == "enrolled" and era.enrolled_date is None:
            # Auto-stamp today when marking enrolled and no date was given
            era.enrolled_date = date.today()

        db.commit()
        db.refresh(era)
        logger.info("update_era id=%d → status=%s", enrollment_id, status)
        return era

    @staticmethod
    def update_eft(
        db: Session,
        enrollment_id: int,
        status: str,
        bank_name: Optional[str] = None,
        deadline: Optional[date] = None,
    ) -> EFTEnrollment:
        """
        Update status, bank_name, and/or deadline of an EFT record.

        Raises EnrollmentServiceError if the record does not exist or the
        status value is not in EFT_STATUSES.
        """
        if status not in EFT_STATUSES:
            raise EnrollmentServiceError(
                f"Invalid EFT status '{status}'. Must be one of: {EFT_STATUSES}"
            )

        eft = db.query(EFTEnrollment).filter(EFTEnrollment.id == enrollment_id).first()
        if eft is None:
            raise EnrollmentServiceError(
                f"EFTEnrollment id={enrollment_id} not found"
            )

        eft.status = status
        if bank_name is not None:
            eft.bank_name = bank_name
        if deadline is not None:
            eft.deadline = deadline

        db.commit()
        db.refresh(eft)
        logger.info(
            "update_eft id=%d → status=%s bank=%s deadline=%s",
            enrollment_id,
            status,
            bank_name,
            deadline,
        )
        return eft

    # ------------------------------------------------------------------
    # Summary / dashboard
    # ------------------------------------------------------------------

    @staticmethod
    def summary(
        db: Session,
        facility_id: Optional[int] = None,
    ) -> Dict:
        """
        Return a dashboard-ready summary dict.

        Shape:
        {
            "era": {"enrolled": int, "pending": int, "missing": int},
            "eft": {"enrolled": int, "pending": int, "missing": int,
                    "action_required": int, "urgent": int},
            "total_payers": int,
        }

        ``total_payers`` is the distinct payer count in EFT (one EFT per payer
        is the canonical source of payer identity).
        """
        era_q = db.query(ERAEnrollment)
        eft_q = db.query(EFTEnrollment)

        if facility_id is not None:
            era_q = era_q.filter(ERAEnrollment.facility_id == facility_id)
            eft_q = eft_q.filter(EFTEnrollment.facility_id == facility_id)

        all_era = era_q.all()
        all_eft = eft_q.all()

        era_counts: Dict[str, int] = {"enrolled": 0, "pending": 0, "missing": 0}
        for record in all_era:
            key = record.status if record.status in era_counts else "missing"
            era_counts[key] += 1

        eft_counts: Dict[str, int] = {
            "enrolled": 0,
            "pending": 0,
            "missing": 0,
            "action_required": 0,
            "urgent": 0,
        }
        for record in all_eft:
            key = record.status if record.status in eft_counts else "missing"
            eft_counts[key] += 1
            if record.is_urgent:
                eft_counts["urgent"] += 1

        return {
            "era": era_counts,
            "eft": eft_counts,
            "total_payers": len(all_eft),
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _clean_status(
    value: Optional[str],
    valid: set,
    default: str,
) -> str:
    """Normalise a status string; fall back to *default* if invalid or missing."""
    if not value:
        return default
    cleaned = value.strip().lower().replace(" ", "_")
    return cleaned if cleaned in valid else default


def _parse_date(value: Optional[str]) -> Optional[date]:
    """Parse an ISO-8601 date string (YYYY-MM-DD); return None on any failure."""
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            from datetime import datetime as _dt
            return _dt.strptime(value, fmt).date()
        except ValueError:
            continue
    logger.warning("Could not parse date value: %r", value)
    return None


def parse_csv_bytes(raw: bytes) -> List[Dict[str, str]]:
    """
    Helper: decode raw CSV bytes → list of dicts (header row as keys).

    Exposed at module level so the route layer can call it without
    instantiating the service class.
    """
    text = raw.decode("utf-8-sig")  # strip BOM if Excel-exported
    reader = csv.DictReader(io.StringIO(text))
    return [
        {k.strip().lower().replace(" ", "_"): (v or "").strip() for k, v in row.items()}
        for row in reader
    ]
