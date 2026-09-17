from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from datetime import datetime
from .base import Base


class RecoupmentResult(Base):
    __tablename__ = "recoupment_results"

    id = Column(Integer, primary_key=True)
    facility_id = Column(Integer, ForeignKey("facilities.id"), nullable=True)
    filename = Column(String)
    claim_number = Column(String)
    date_of_service = Column(String)
    billed_amount = Column(Float)
    paid_amount = Column(Float)
    net_received = Column(Float)
    flagged = Column(Boolean, default=False)
    extraction_warning = Column(String)
    analyzed_at = Column(DateTime, default=datetime.utcnow)

    facility = relationship("Facility", back_populates="recoupment_results")
    flags = relationship("RecoupmentFlag", back_populates="result", cascade="all, delete-orphan")


class RecoupmentFlag(Base):
    __tablename__ = "recoupment_flags"

    id = Column(Integer, primary_key=True)
    result_id = Column(Integer, ForeignKey("recoupment_results.id"))
    line = Column(String)
    matched_phrase = Column(String)
    payer_tag = Column(String)
    amounts_found = Column(String)  # JSON string
    source = Column(String)  # pattern | ledger_mismatch

    result = relationship("RecoupmentResult", back_populates="flags")
