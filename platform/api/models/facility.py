from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.orm import relationship
from datetime import datetime
from .base import Base


class Facility(Base):
    __tablename__ = "facilities"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    npi = Column(String)
    tax_id = Column(String)
    ehr_system = Column(String)  # athena, simplepractice, collaboratemd, tebra
    created_at = Column(DateTime, default=datetime.utcnow)

    recoupment_results = relationship("RecoupmentResult", back_populates="facility")
    scas = relationship("SCA", back_populates="facility")
    era_enrollments = relationship("ERAEnrollment", back_populates="facility")
    eft_enrollments = relationship("EFTEnrollment", back_populates="facility")
