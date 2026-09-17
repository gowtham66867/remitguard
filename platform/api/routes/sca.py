"""
SCA (Single Case Agreement) routes — /api/sca

Endpoints:
  POST   /api/sca/create              — create a new SCA
  PUT    /api/sca/{sca_id}/visits     — update used_visits count
  GET    /api/sca/                    — list all SCAs (+ computed fields)
  GET    /api/sca/alerts              — non-active SCAs with alert_reason
  GET    /api/sca/summary             — status count breakdown
  DELETE /api/sca/{sca_id}            — hard-delete an SCA

All request/response bodies use inline Pydantic schemas defined below.
Python 3.9-compatible (Optional[X] instead of X | None).
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.models.base import get_db
from api.services.sca_service import SCAService

router = APIRouter(prefix="/api/sca", tags=["SCA"])


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class SCACreate(BaseModel):
    """Fields required to open a new Single Case Agreement."""

    facility_id: Optional[int] = Field(None, description="FK to facilities table")
    patient_name: str = Field(..., min_length=1, example="Elias Lemanski")
    payer_name: str = Field(..., min_length=1, example="Aetna")
    payer_id: Optional[str] = Field(None, example="AETNA-001")
    provider_name: str = Field(..., min_length=1, example="Rebecca Madison Seawright")
    approved_visits: int = Field(..., gt=0, example=13)
    used_visits: int = Field(0, ge=0, example=0)
    start_date: date = Field(..., example="2026-05-01")
    end_date: date = Field(..., example="2026-08-01")
    medicaid_contracted: bool = Field(
        False,
        description=(
            "True only when the rendering provider is contracted with "
            "State Medicaid.  False triggers contracting_risk status."
        ),
    )
    notes: Optional[str] = Field(None, example="Approved via phone auth #XYZ")

    class Config:
        json_schema_extra = {
            "example": {
                "facility_id": 1,
                "patient_name": "Elias Lemanski",
                "payer_name": "Aetna",
                "payer_id": "AETNA-001",
                "provider_name": "Rebecca Madison Seawright",
                "approved_visits": 13,
                "used_visits": 0,
                "start_date": "2026-05-01",
                "end_date": "2026-08-01",
                "medicaid_contracted": False,
                "notes": "Approved via phone auth #XYZ — provider not yet credentialed with State Medicaid",
            }
        }


class VisitUpdate(BaseModel):
    """Body for updating the used_visits count on an SCA."""

    used_visits: int = Field(..., ge=0, example=5)


class SCAResponse(BaseModel):
    """Full SCA representation returned by the API, including computed fields."""

    id: int
    facility_id: Optional[int]
    patient_name: str
    payer_name: str
    payer_id: Optional[str]
    provider_name: str
    approved_visits: int
    used_visits: int
    remaining_visits: int           # computed: approved_visits - used_visits
    start_date: date
    end_date: date
    days_to_expiry: int             # computed: (end_date - today).days
    medicaid_contracted: bool
    notes: Optional[str]
    status: str                     # active | warning | exhausted | expired | contracting_risk
    created_at: Optional[str]
    updated_at: Optional[str]

    class Config:
        from_attributes = True


class AlertResponse(SCAResponse):
    """SCA response extended with a plain-English alert_reason."""

    alert_reason: Optional[str]


class StatusSummaryResponse(BaseModel):
    """Counts of each SCA status across the queried scope."""

    active: int
    warning: int
    exhausted: int
    expired: int
    contracting_risk: int
    total: int


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _serialize_sca(sca: Any) -> SCAResponse:
    """Map a SCA ORM object → SCAResponse, injecting computed fields."""
    today = date.today()
    return SCAResponse(
        id=sca.id,
        facility_id=sca.facility_id,
        patient_name=sca.patient_name,
        payer_name=sca.payer_name,
        payer_id=sca.payer_id,
        provider_name=sca.provider_name,
        approved_visits=sca.approved_visits,
        used_visits=sca.used_visits,
        remaining_visits=sca.remaining_visits,
        start_date=sca.start_date,
        end_date=sca.end_date,
        days_to_expiry=(sca.end_date - today).days,
        medicaid_contracted=sca.medicaid_contracted,
        notes=sca.notes,
        status=sca.status,
        created_at=sca.created_at.isoformat() if sca.created_at else None,
        updated_at=sca.updated_at.isoformat() if sca.updated_at else None,
    )


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

@router.post(
    "/create",
    response_model=SCAResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new Single Case Agreement",
)
def create_sca(
    payload: SCACreate,
    db: Session = Depends(get_db),
) -> SCAResponse:
    """
    Open a new SCA record.

    - **medicaid_contracted=false** will immediately flag the SCA as
      `contracting_risk`, which surfaces in `/alerts`.
    - `used_visits` defaults to 0 if omitted.
    """
    sca = SCAService.create_sca(db, payload.dict())
    return _serialize_sca(sca)


@router.put(
    "/{sca_id}/visits",
    response_model=SCAResponse,
    summary="Update used_visits for an SCA",
)
def update_visits(
    sca_id: int,
    payload: VisitUpdate,
    db: Session = Depends(get_db),
) -> SCAResponse:
    """
    Set the cumulative number of visits used under this SCA.

    Returns 404 when the SCA does not exist.
    Returns 422 when `used_visits` exceeds `approved_visits`.
    """
    try:
        sca = SCAService.update_visits(db, sca_id, payload.used_visits)
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=msg)
    return _serialize_sca(sca)


@router.get(
    "/alerts",
    response_model=List[AlertResponse],
    summary="List all SCAs that are not active",
)
def get_alerts(
    facility_id: Optional[int] = Query(None, description="Filter by facility"),
    db: Session = Depends(get_db),
) -> List[Dict]:
    """
    Returns every SCA whose status is **not** `active`, each with an
    `alert_reason` string explaining the specific risk:

    - **exhausted** — all approved visits consumed
    - **expired** — SCA end_date has passed
    - **contracting_risk** — provider not contracted with State Medicaid
    - **warning** — ≤3 visits remaining or ≤30 days to expiry

    Example real case: patient Elias Lemanski, provider Rebecca Madison
    Seawright, Aetna SCA 05/01/2026–08/01/2026 (13 visits) →
    `contracting_risk` because provider is not State Medicaid contracted.
    """
    return SCAService.get_alerts(db, facility_id=facility_id)


@router.get(
    "/summary",
    response_model=StatusSummaryResponse,
    summary="Status count breakdown across all SCAs",
)
def status_summary(
    facility_id: Optional[int] = Query(None, description="Filter by facility"),
    db: Session = Depends(get_db),
) -> StatusSummaryResponse:
    """
    Returns a dict with counts for each status bucket:
    `active`, `warning`, `exhausted`, `expired`, `contracting_risk`, `total`.
    """
    summary = SCAService.status_summary(db, facility_id=facility_id)
    return StatusSummaryResponse(**summary)


@router.get(
    "/",
    response_model=List[SCAResponse],
    summary="List all SCAs with computed status fields",
)
def list_scas(
    facility_id: Optional[int] = Query(None, description="Filter by facility"),
    db: Session = Depends(get_db),
) -> List[SCAResponse]:
    """
    Returns all SCA records ordered by `end_date` ascending (soonest expiring
    first), each enriched with:

    - `status` — active | warning | exhausted | expired | contracting_risk
    - `remaining_visits` — approved minus used
    - `days_to_expiry` — calendar days until end_date (negative = already expired)
    """
    scas = SCAService.get_all(db, facility_id=facility_id)
    return [_serialize_sca(s) for s in scas]


@router.delete(
    "/{sca_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an SCA record",
)
def delete_sca(
    sca_id: int,
    db: Session = Depends(get_db),
) -> None:
    """
    Permanently removes the SCA record.  Returns **204** on success,
    **404** when the record does not exist.
    """
    deleted = SCAService.delete_sca(db, sca_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"SCA with id={sca_id} not found.",
        )
