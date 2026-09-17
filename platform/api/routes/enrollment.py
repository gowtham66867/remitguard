"""
routes/enrollment.py
--------------------
FastAPI router for ERA / EFT enrollment management.

Prefix: /api/enrollment

Endpoints
---------
POST /import                 Upload CSV; bulk-import ERA+EFT records
GET  /era                    List ERA enrollments (filterable)
GET  /eft                    List EFT enrollments (filterable, with is_urgent)
PUT  /era/{enrollment_id}    Update ERA status
PUT  /eft/{enrollment_id}    Update EFT status / bank / deadline
GET  /alerts                 Fire list — urgent EFT + missing ERA
GET  /summary                Dashboard counts
"""

from __future__ import annotations

import logging
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field, validator
from sqlalchemy.orm import Session

from api.models.base import get_db
from api.services.enrollment_service import (
    ERA_STATUSES,
    EFT_STATUSES,
    EnrollmentServiceError,
    EnrollmentService,
    parse_csv_bytes,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/enrollment", tags=["Enrollment"])


# ---------------------------------------------------------------------------
# Pydantic response schemas
# ---------------------------------------------------------------------------


class ERAEnrollmentOut(BaseModel):
    id: int
    facility_id: Optional[int]
    payer_id: str
    payer_name: str
    status: str
    clearinghouse: Optional[str]
    enrolled_date: Optional[date]
    notes: Optional[str]

    class Config:
        from_attributes = True


class EFTEnrollmentOut(BaseModel):
    id: int
    facility_id: Optional[int]
    payer_id: str
    payer_name: str
    status: str
    bank_name: Optional[str]
    deadline: Optional[date]
    portal: Optional[str]
    notes: Optional[str]
    is_urgent: bool

    class Config:
        from_attributes = True


class ImportResult(BaseModel):
    imported: int
    message: str


class ERAUpdateIn(BaseModel):
    status: str = Field(..., description="enrolled | pending | missing")
    enrolled_date: Optional[date] = Field(
        None, description="ISO date when enrollment completed"
    )

    @validator("status")
    def validate_era_status(cls, v: str) -> str:  # noqa: N805
        if v not in ERA_STATUSES:
            raise ValueError(f"status must be one of {ERA_STATUSES}")
        return v


class EFTUpdateIn(BaseModel):
    status: str = Field(
        ..., description="enrolled | pending | missing | action_required"
    )
    bank_name: Optional[str] = Field(None, description="Target bank name")
    deadline: Optional[date] = Field(None, description="EFT enrollment deadline")

    @validator("status")
    def validate_eft_status(cls, v: str) -> str:  # noqa: N805
        if v not in EFT_STATUSES:
            raise ValueError(f"status must be one of {EFT_STATUSES}")
        return v


class AlertsOut(BaseModel):
    urgent_eft: List[EFTEnrollmentOut]
    missing_era: List[ERAEnrollmentOut]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/import",
    response_model=ImportResult,
    status_code=status.HTTP_201_CREATED,
    summary="Bulk-import ERA + EFT records from CSV upload",
    description=(
        "Upload a CSV file with columns: payer_id, payer_name, era_status, "
        "eft_status, bank_name, eft_deadline (YYYY-MM-DD), portal, notes. "
        "Existing records for the same (facility_id, payer_id) are updated. "
        "Returns the count of payer rows processed."
    ),
)
async def import_payer_csv(
    facility_id: int = Query(..., description="Target facility ID"),
    file: UploadFile = File(..., description="CSV file to import"),
    db: Session = Depends(get_db),
) -> ImportResult:
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only .csv files are accepted",
        )

    raw = await file.read()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty",
        )

    try:
        rows = parse_csv_bytes(raw)
    except Exception as exc:
        logger.exception("CSV parse failure")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Could not parse CSV: {exc}",
        ) from exc

    try:
        count = EnrollmentService.import_payer_list(db, facility_id=facility_id, rows=rows)
    except EnrollmentServiceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return ImportResult(
        imported=count,
        message=f"Successfully imported/updated {count} payer enrollments for facility {facility_id}",
    )


@router.get(
    "/era",
    response_model=List[ERAEnrollmentOut],
    summary="List ERA enrollments",
)
def list_era(
    facility_id: Optional[int] = Query(None, description="Filter by facility"),
    status: Optional[str] = Query(
        None, description="Filter: missing | pending | enrolled"
    ),
    db: Session = Depends(get_db),
) -> List[ERAEnrollmentOut]:
    if status is not None and status not in ERA_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status. Must be one of: {ERA_STATUSES}",
        )
    return EnrollmentService.get_era_status(db, facility_id=facility_id, status=status)  # type: ignore[return-value]


@router.get(
    "/eft",
    response_model=List[EFTEnrollmentOut],
    summary="List EFT enrollments",
    description="Includes is_urgent=true when deadline is within 7 days.",
)
def list_eft(
    facility_id: Optional[int] = Query(None, description="Filter by facility"),
    status: Optional[str] = Query(
        None, description="Filter: missing | pending | enrolled | action_required"
    ),
    db: Session = Depends(get_db),
) -> List[EFTEnrollmentOut]:
    if status is not None and status not in EFT_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Must be one of: {EFT_STATUSES}",
        )
    return EnrollmentService.get_eft_status(db, facility_id=facility_id, status=status)  # type: ignore[return-value]


@router.put(
    "/era/{enrollment_id}",
    response_model=ERAEnrollmentOut,
    summary="Update ERA enrollment status",
)
def update_era(
    enrollment_id: int,
    body: ERAUpdateIn,
    db: Session = Depends(get_db),
) -> ERAEnrollmentOut:
    try:
        return EnrollmentService.update_era(  # type: ignore[return-value]
            db,
            enrollment_id=enrollment_id,
            status=body.status,
            enrolled_date=body.enrolled_date,
        )
    except EnrollmentServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put(
    "/eft/{enrollment_id}",
    response_model=EFTEnrollmentOut,
    summary="Update EFT enrollment status, bank, or deadline",
)
def update_eft(
    enrollment_id: int,
    body: EFTUpdateIn,
    db: Session = Depends(get_db),
) -> EFTEnrollmentOut:
    try:
        return EnrollmentService.update_eft(  # type: ignore[return-value]
            db,
            enrollment_id=enrollment_id,
            status=body.status,
            bank_name=body.bank_name,
            deadline=body.deadline,
        )
    except EnrollmentServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/alerts",
    response_model=AlertsOut,
    summary="Fire list — urgent EFT deadlines + missing ERA enrollments",
    description=(
        "Returns two lists that demand immediate ops attention: "
        "(1) EFT enrollments with a deadline within 7 days — "
        "these can cause payment routing failures if missed; "
        "(2) ERA enrollments still in 'missing' state — "
        "these force manual payment posting and accelerate AR aging."
    ),
)
def get_alerts(
    facility_id: Optional[int] = Query(None, description="Filter by facility"),
    db: Session = Depends(get_db),
) -> AlertsOut:
    urgent_eft = EnrollmentService.get_urgent_eft(db, facility_id=facility_id)
    missing_era = EnrollmentService.get_missing_era(db, facility_id=facility_id)
    return AlertsOut(urgent_eft=urgent_eft, missing_era=missing_era)  # type: ignore[arg-type]


@router.get(
    "/summary",
    summary="Dashboard counts for ERA + EFT enrollment status",
    description=(
        "Returns aggregated counts by status for ERA and EFT enrollments. "
        "EFT 'urgent' count is the number of records with deadline ≤7 days out."
    ),
)
def get_summary(
    facility_id: Optional[int] = Query(None, description="Filter by facility"),
    db: Session = Depends(get_db),
) -> dict:
    return EnrollmentService.summary(db, facility_id=facility_id)
