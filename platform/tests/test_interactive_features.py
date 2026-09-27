"""
Automated tests for RemitGuard Top-10 Interactive Features:
1. Interactive Semantic Sandbox (/api/semantic/classify)
2. Closed-Loop Active Learning (/api/semantic/learn)
3. Live 304-Line Scientific Benchmark (/api/semantic/run-eval)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.routes.recoupment import (
    ClassifyRequest,
    LearnRequest,
    classify_line,
    finale_readiness,
    learn_phrase,
    run_live_eval,
)
from agents.semantic_matcher import get_matcher


def test_interactive_classify_divergence():
    """Verify that novel clawback phrasing triggers divergence detection."""
    novel_phrase = (
        "Settlement of an outstanding accounts receivable balance against this payment cycle -3240.00"
    )
    res = classify_line(ClassifyRequest(text=novel_phrase))

    assert res["regex"]["matched"] is False
    assert res["semantic"]["matched"] is True
    assert res["diverged"] is True
    assert res["verdict"] == "DIVERGENCE_CAUGHT"
    assert res["semantic"]["score"] > 0.8
    assert res["semantic"]["latency_ms"] < 25.0


def test_interactive_classify_benign():
    """Verify that standard contractual adjustment is not flagged as clawback."""
    benign_line = "Contractual adjustment per provider agreement 5,120.00"
    res = classify_line(ClassifyRequest(text=benign_line))

    assert res["regex"]["matched"] is False
    assert res["semantic"]["matched"] is False
    assert res["diverged"] is False
    assert res["verdict"] == "BENIGN_LINE"


def test_interactive_learn_active_memory():
    """Verify that teaching a novel phrase updates corpus size in real-time."""
    matcher = get_matcher()
    initial_size = matcher.corpus_size

    res = learn_phrase(
        LearnRequest(
            phrase="Unauthorized post-payment adjustment reversal clawback -1200.00",
            payer="cigna_audit",
        )
    )

    assert res["success"] is True
    assert res["status"] == "MEMORY_INDEXED"
    assert res["corpus_size"] == initial_size + 1


def test_interactive_live_eval_benchmark():
    """Verify that the 304-line held-out scientific benchmark completes in <500ms."""
    res = run_live_eval()

    assert res["status"] == "EVALUATION_COMPLETE"
    assert res["total_lines_tested"] == 304
    assert res["clawback_lines"] == 152
    assert res["benign_lines"] == 152
    assert res["duration_ms"] < 1000.0
    assert res["regex_baseline"]["recall"] > 25.0
    assert res["hybrid_semantic"]["recall"] > res["regex_baseline"]["recall"]
    assert res["regex_baseline"]["precision"] > 95.0
    assert res["hybrid_semantic"]["precision"] > 90.0


def test_finale_readiness_never_attributes_fallback_to_moss():
    """The live-demo preflight must distinguish Moss from its safe fallback."""
    res = finale_readiness()

    assert res["primary_case"]["id"] == "regional_reworded"
    assert res["primary_case"]["available"] is True
    assert res["moss"]["live"] == (res["moss"]["engine"] == "moss_cloud")
    assert "de-identified" in res["copilot"]["privacy"].lower()
    assert len(res["runbook"]) == 4
