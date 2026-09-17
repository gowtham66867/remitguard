from sqlalchemy import Column, Integer, String, Float, Date, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from .base import Base


class SCA(Base):
    __tablename__ = "scas"

    id = Column(Integer, primary_key=True)
    facility_id = Column(Integer, ForeignKey("facilities.id"), nullable=True)
    patient_name = Column(String, nullable=False)
    payer_name = Column(String, nullable=False)
    payer_id = Column(String)
    provider_name = Column(String, nullable=False)
    approved_visits = Column(Integer, nullable=False)
    used_visits = Column(Integer, default=0)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    medicaid_contracted = Column(Boolean, default=False)
    notes = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    facility = relationship("Facility", back_populates="scas")

    @property
    def remaining_visits(self):
        return max(0, self.approved_visits - self.used_visits)

    @property
    def status(self):
        from datetime import date
        today = date.today()
        days_to_expiry = (self.end_date - today).days
        if self.used_visits >= self.approved_visits:
            return "exhausted"
        if days_to_expiry < 0:
            return "expired"
        if not self.medicaid_contracted:
            return "contracting_risk"
        if self.remaining_visits <= 3 or days_to_expiry <= 30:
            return "warning"
        return "active"
