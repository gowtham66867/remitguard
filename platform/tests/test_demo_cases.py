"""
Guards on the bundled demo cases.

The demo is the submission's proof surface, so the failure mode that matters
is not a crash — it is a card that claims something its own PDF does not
support. That happened once already: two of three cases shipped dollar
figures and a "3ms" latency that no measurement produced, and one named a
precedent phrase that was absent from its document entirely.

These tests make that class of drift a build failure.
"""

from __future__ import annotations

import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agents.semantic_matcher import get_matcher  # noqa: E402
from api.services.comparison_service import run_comparison, run_demo_case  # noqa: E402
from api.services.recoupment_service import RecoupmentService  # noqa: E402
from api.services.sample_files import (  # noqa: E402
    DEMO_CASES,
    get_demo_pdf_bytes,
    list_demo_cases,
)

CORPUS_PATH = os.path.join(os.path.dirname(__file__), "..", "recoupment_corpus.json")


@pytest.fixture(scope="module")
def compiled():
    return RecoupmentService()._get_compiled()


@pytest.fixture(scope="module", autouse=True)
def warm_matcher():
    get_matcher().warm()


def _tokens(text: str) -> set:
    return set(re.findall(r"[a-z]{3,}", text.lower()))


# ── the catalog must describe the documents it ships ────────────────────────

@pytest.mark.parametrize("case_id", sorted(DEMO_CASES))
def test_demo_pdf_is_bundled(case_id):
    _, pdf = get_demo_pdf_bytes(case_id)
    assert pdf, f"{case_id}: PDF missing from demo_assets/ — the demo 503s without it"


@pytest.mark.parametrize("case_id", sorted(DEMO_CASES))
def test_catalog_matches_the_actual_document(case_id, compiled):
    """Declared figures must equal measured ones.

    This is the test that would have caught the Anthem card claiming a
    $6,479.89 net against a document that actually nets -$145.02.
    """
    out = run_demo_case(case_id, compiled)
    assert "error" not in out, out.get("error")
    check = out["expectation_check"]
    assert check["matches"], (
        f"{case_id}: catalog disagrees with the document.\n"
        f"  expected: {check['expected']}\n"
        f"  measured: {check['measured']}"
    )


@pytest.mark.parametrize("case_id", sorted(DEMO_CASES))
def test_declared_phrase_appears_in_the_document(case_id, compiled):
    """The phrase the card advertises must actually be in the PDF.

    The retired Cigna case advertised "FUNDS RECOVERY INITIATED AGAINST
    PROVIDER ACCOUNT" against a document whose only detail line read "See
    attached schedule for details."
    """
    from api.services.recoupment_service import _extract_text_from_bytes

    _, pdf = get_demo_pdf_bytes(case_id)
    text = _extract_text_from_bytes(pdf)
    phrase_tokens = _tokens(DEMO_CASES[case_id]["novel_phrase"])
    doc_tokens = _tokens(text)
    missing = phrase_tokens - doc_tokens
    assert not missing, (
        f"{case_id}: advertised phrase is not in its own PDF (missing {sorted(missing)})"
    )


# ── the corpus must not be allowed to recognise itself ──────────────────────

def test_declared_lexical_proximity_is_accurate():
    """Each case must disclose its real distance from the corpus.

    Case A scores 0.884 because its wording sits close to corpus doc r029 —
    that is a fair test of the regex baseline, which gets no such help, but
    it is not evidence that retrieval generalises. The number is published in
    the catalog so nobody has to discover it by reading the corpus, and this
    test keeps the published number honest.
    """
    with open(CORPUS_PATH) as fh:
        corpus = json.load(fh)["documents"]

    for case in DEMO_CASES.values():
        declared = case.get("lexical_proximity")
        assert declared, f"{case['id']}: must disclose its proximity to the corpus"
        phrase = _tokens(case["novel_phrase"])
        best, best_id = max(
            (len(phrase & _tokens(d["text"])) / len(phrase | _tokens(d["text"])), d["id"])
            for d in corpus
        )
        assert best_id == declared["nearest_corpus_doc"], (
            f"{case['id']}: declares {declared['nearest_corpus_doc']}, "
            f"nearest is actually {best_id}"
        )
        assert abs(best - declared["token_jaccard"]) <= 0.02, (
            f"{case['id']}: declares Jaccard {declared['token_jaccard']}, "
            f"measured {best:.2f}"
        )


def test_no_demo_phrase_is_a_verbatim_corpus_copy():
    """A demo line lifted verbatim from the corpus measures nothing at all.

    An earlier case C used the exact text of corpus doc r011 and scored
    0.9967 — the index recognising itself. The ceiling here (0.60) is the
    same one the eval harness enforces between its phrases and the corpus.
    """
    with open(CORPUS_PATH) as fh:
        corpus = json.load(fh)["documents"]

    for case in DEMO_CASES.values():
        phrase = _tokens(case["novel_phrase"])
        worst, worst_id = max(
            (len(phrase & _tokens(d["text"])) / len(phrase | _tokens(d["text"])), d["id"])
            for d in corpus
        )
        assert worst <= 0.60, (
            f"demo case '{case['id']}' overlaps corpus doc '{worst_id}' at "
            f"Jaccard {worst:.2f} — the demo is measuring the corpus against itself"
        )


def test_at_least_one_case_is_a_genuine_paraphrase():
    """The suite must include wording the corpus does not nearly contain.

    Without this, every bundled case could sit near a corpus entry and the
    demo would only ever prove that near-copies match.
    """
    distances = [c["lexical_proximity"]["token_jaccard"] for c in DEMO_CASES.values()]
    assert min(distances) <= 0.20, (
        f"closest-to-farthest proximities are {sorted(distances)} — no case "
        "tests generalisation to unfamiliar wording"
    )


# ── the comparison itself must stay a real comparison ───────────────────────

def test_wedge_case_separates_the_two_paths(compiled):
    """The primary case must actually differ between paths, or it proves nothing."""
    out = run_demo_case("regional_reworded", compiled)
    assert out["baseline"]["decision"] == "CLEAR"
    assert out["baseline"]["flag_count"] == 0
    assert out["baseline"]["net_received"] == 9480.00
    assert out["semantic"]["decision"] == "HUMAN_REVIEW"
    assert out["delta"]["cash_recovered"] == 3240.00
    assert out["delta"]["verdict_changed"] is True


def test_control_case_is_caught_by_regex_alone(compiled):
    """The baseline must win at least one case, or it is a strawman."""
    out = run_demo_case("anthem_offset", compiled)
    assert out["baseline"]["flag_count"] == 1
    assert out["delta"]["cash_recovered"] == 0.0
    assert out["delta"]["verdict_changed"] is False


def test_benign_lines_are_declined_not_flagged(compiled):
    """Recall bought by flagging every money line is not recall."""
    out = run_demo_case("regional_reworded", compiled)
    trace = out["retrieval"]["trace"]
    declined = [t for t in trace if t["decision"] == "PASS"]
    assert len(declined) >= 2, "the benign adjustment lines must be queried and declined"
    for t in declined:
        assert t["top_label"] == "benign", (
            f"{t['line']!r} was declined for the wrong reason: {t.get('reason')}"
        )


# ── no fabricated telemetry ─────────────────────────────────────────────────

def test_gap_case_is_the_moss_acceptance_test(compiled):
    """Case C is bundled to fail on the lexical fallback and pass on Moss.

    It asserts whichever is true for the backend actually in use, so the
    suite stays green either way — and the day Moss is connected, a case C
    that still misses is a real failure rather than a silent one.
    """
    out = run_demo_case("paraphrase_gap", compiled)
    assert out["baseline"]["decision"] == "CLEAR", "regex must miss this one"
    assert out["baseline"]["flag_count"] == 0

    trace = out["retrieval"]["trace"]
    hit = next((t for t in trace if "legacy debit" in t["line"]), None)
    assert hit is not None, "the paraphrase line must at least be queried"

    if get_matcher().moss_connected:
        assert hit["decision"] == "FLAG", (
            f"Moss is connected but still missed the paraphrase at "
            f"score {hit.get('score')} — embeddings are not earning their place"
        )
        assert out["semantic"]["recovery_at_risk"] == 4150.00
    else:
        assert hit["decision"] == "PASS", (
            "the lexical fallback is expected to miss this; if it now catches "
            "it, the case no longer tests generalisation and needs rewording"
        )
        # The label is still right — only the confidence is short. That
        # distinction is the whole argument for embeddings.
        assert hit["top_label"] == "recoupment"
        assert hit["score"] < out["retrieval"]["threshold"]


def test_latency_is_measured_not_synthesised(compiled):
    """Reported engine time must not exceed measured wall-clock time.

    A prior revision added a fixed 2.2ms to the local engine's reported time,
    which drove bridge overhead to -2.027ms — an impossible published number.
    """
    out = run_demo_case("regional_reworded", compiled)
    r = out["retrieval"]
    if r["query_ms_p50"] is None or r["engine_ms_p50"] is None:
        pytest.skip("no latency samples")
    assert r["engine_ms_p50"] <= r["query_ms_p50"] + 1e-6, (
        f"reported engine time {r['engine_ms_p50']}ms exceeds wall clock "
        f"{r['query_ms_p50']}ms — a synthetic constant is being added"
    )
    assert r["bridge_overhead_ms_p50"] >= 0


def test_moss_is_only_credited_when_moss_is_connected():
    """`moss_connected` gates every Moss attribution, including latency."""
    stats = get_matcher().stats()
    assert "moss_connected" in stats
    if not stats["moss_connected"]:
        assert stats["engine"] == "local_lexical"
        assert "not connected" in (stats["provider"] or "").lower()
        assert stats["moss_engine_ms_p50"] is None, (
            "a Moss latency figure is being reported without Moss"
        )
        assert stats["fallback_reason"], "the fallback must say why it is the fallback"


def test_every_case_is_available_to_the_ui():
    assert all(c["available"] for c in list_demo_cases())
