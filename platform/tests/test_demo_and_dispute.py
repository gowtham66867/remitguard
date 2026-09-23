"""
End-to-end tests for 1-Click Demo cases, Shepherd Split-Screen comparison,
in-process semantic matching, and ERISA dispute packet generation.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.services.sample_files import list_demo_cases, get_demo_pdf_bytes, DEMO_CASES
from api.services.comparison_service import run_comparison
from api.services.recoupment_service import RecoupmentService
from api.routes.recoupment import generate_dispute_packet, DisputePacketRequest
from agents.semantic_matcher import get_matcher


def test_list_demo_cases():
    cases = list_demo_cases()
    assert len(cases) >= 3
    ids = [c["id"] for c in cases]
    assert "regional_reworded" in ids
    assert "anthem_offset" in ids
    assert "paraphrase_gap" in ids


def test_get_demo_pdf_bytes():
    for case_id in DEMO_CASES:
        fname, pbytes = get_demo_pdf_bytes(case_id)
        assert fname.endswith(".pdf")
        assert len(pbytes) > 100
        assert pbytes.startswith(b"%PDF")


def test_shepherd_divergence_regional_reworded():
    service = RecoupmentService()
    compiled = service._get_compiled()
    fname, pbytes = get_demo_pdf_bytes("regional_reworded")

    result = run_comparison(pbytes, fname, compiled)

    # 1. Baseline regex misses the reworded phrase
    assert result["baseline"]["flag_count"] == 0
    assert result["baseline"]["decision"] == "CLEAR"
    assert result["baseline"]["recovery_at_risk"] == 0.0

    # 2. Semantic engine catches the $3,240 clawback
    assert result["semantic"]["flag_count"] >= 1
    assert result["semantic"]["decision"] == "HUMAN_REVIEW"
    assert result["semantic"]["recovery_at_risk"] == 3240.0

    # 3. Delta shows causality divergence
    assert result["delta"]["diverged"] is True
    assert result["delta"]["verdict_changed"] is True
    assert result["delta"]["cash_recovered"] == 3240.0
    assert len(result["delta"]["missed_by_regex"]) >= 1
    assert result["delta"]["missed_by_regex"][0]["amount"] == 3240.0


def test_generate_dispute_packet():
    req = DisputePacketRequest(
        filename="regional_reworded_sample.pdf",
        payer="Meridian Regional Health Plan",
        claim_number="MRH-2026-9921",
        date_of_service="2026-02-03",
        recoupment_amount=3240.0,
        evidence_text="Prior period shortfall netted out of this disbursement -3240.00",
    )
    packet = generate_dispute_packet(req)

    assert packet["status"] == "DISPUTE_PACKET_GENERATED"
    assert packet["disputed_amount"] == 3240.0
    assert "ERISA" in packet["statutory_basis"]
    assert "29 C.F.R. § 2560.503-1" in packet["statutory_basis"]
    assert "MRH-2026-9921" in packet["letter_text"]
    assert "$3,240.00" in packet["letter_text"]
    assert "ADMINISTRATIVE HOLD" in packet["letter_text"]


def test_semantic_matcher_telemetry():
    matcher = get_matcher()
    assert matcher.ready is True
    stats = matcher.stats()
    assert stats["enabled"] is True
    assert stats["ready"] is True
    assert stats["corpus_size"] >= 20
    # Renamed so the backend cannot be misread: "local_lexical" is the
    # in-process TF-IDF fallback, "moss_cloud" is Moss itself.
    assert stats["engine"] in ("local_lexical", "moss_cloud")
    assert stats["moss_connected"] is (stats["engine"] == "moss_cloud")
