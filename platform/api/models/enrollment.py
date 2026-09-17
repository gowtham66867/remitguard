from sqlalchemy import Column, Integer, String, Date, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from datetime import datetime
from .base import Base


class ERAEnrollment(Base):
    __tablename__ = "era_enrollments"

    id = Column(Integer, primary_key=True)
    facility_id = Column(Integer, ForeignKey("facilities.id"), nullable=True)
    payer_id = Column(String, nullable=False)
    payer_name = Column(String, nullable=False)
    status = Column(String, default="missing")  # enrolled | pending | missing
    clearinghouse = Column(String)  # availity, trizetto, claimmd, officeally
    enrolled_date = Column(Date)
    notes = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    facility = relationship("Facility", back_populates="era_enrollments")


class EFTEnrollment(Base):
    __tablename__ = "eft_enrollments"

    id = Column(Integer, primary_key=True)
    facility_id = Column(Integer, ForeignKey("facilities.id"), nullable=True)
    payer_id = Column(String, nullable=False)
    payer_name = Column(String, nullable=False)
    status = Column(String, default="missing")  # enrolled | pending | missing | action_required
    bank_name = Column(String)
    deadline = Column(Date)
    portal = Column(String)  # trizetto, echo, id_me, payerenroll
    notes = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    facility = relationship("Facility", back_populates="eft_enrollments")

    @property
    def is_urgent(self):
        if self.deadline is None:
            return False
        from datetime import date
        return (self.deadline - date.today()).days <= 7
