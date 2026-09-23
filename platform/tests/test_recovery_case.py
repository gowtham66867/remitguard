"""Tests for the reviewer-facing recovery decision packet."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from api.services.recoupment_service import _EOBResult, _Flag


def test_recoupment_packet_quantifies_risk_and_preserves_evidence():
    result = _EOBResult(
        source_file="anthem-eob.pdf",
        full_text="",
        paid_amount=10000.0,
        claim_numbers=["CLM-123456"],
        dates_of_service=["09/01/2026"],
        flags=[_Flag(
            line="Prior balance recouped from this payment ($3,240.00)",
            matched_phrase="recouped",
            payer_tag="anthem",
            amounts_found=[-3240.0],
        )],
    )

    packet = result.to_dict()

    assert packet["claim_number"] == "CLM-123456"
    assert packet["recoupment_amount"] == 3240.0
    assert packet["recovery_case"]["decision"] == "HUMAN_REVIEW"
    assert packet["recovery_case"]["evidence_count"] == 1
    assert "Hold final posting" in packet["recovery_case"]["recommended_action"]


def test_clean_eob_has_explicit_post_normally_decision():
    packet = _EOBResult(source_file="clean.pdf", full_text="", paid_amount=92.0).to_dict()

    assert packet["recoupment_amount"] == 0.0
    assert packet["recovery_case"]["decision"] == "CLEAR"
    assert packet["recovery_case"]["recommended_action"] == "No recoupment language detected. Post normally."
