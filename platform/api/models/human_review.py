from sqlalchemy import Column, Integer, String, Text, DateTime
from datetime import datetime
from .base import Base


class HumanReview(Base):
    __tablename__ = "human_reviews"

    id = Column(Integer, primary_key=True, index=True)
    ticket_type = Column(String(64), nullable=False)  # escalation_type
    source_agent = Column(String(64), nullable=False)
    reason = Column(Text, nullable=False)
    context_json = Column(Text, nullable=True)  # JSON-serialized context dict
    status = Column(String(16), nullable=False, default="pending")  # pending | approved | dismissed
    reviewer_notes = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    reviewed_at = Column(DateTime, nullable=True)
