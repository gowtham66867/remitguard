"""
The bundled demo case catalog.

Zero-friction demo: a reviewer clicks one button and a real EOB goes down both
detection paths. Nobody has to find, download, or upload a PDF.

WHAT THIS FILE IS AND IS NOT
----------------------------
It is metadata: which document, who the payer is, what the case is meant to
demonstrate. It is NOT results. An earlier revision shipped strings like
"maps novel wording to historical precedent in 3ms" and a hardcoded matched
precedent id — numbers asserted as copy rather than measured at runtime. Two
of the three cases then disagreed with what their own PDFs actually produced.

So: every figure a reviewer sees comes from `comparison_service.run_comparison`
against the real document. The `expected` block below exists only so
`tests/test_demo_cases.py` can fail the build when the catalog and the
documents drift apart again.

THE THREE CASES ARE NOT INTERCHANGEABLE
---------------------------------------
Each proves something different, and case C is deliberately one the current
fallback engine loses. Shipping only wins would make the demo a sales reel.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SAMPLE_DIR = os.environ.get(
    "DEMO_ASSETS_PATH",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "demo_assets")),
)

DEMO_CASES: Dict[str, Dict[str, Any]] = {
    # ── A. the wedge: regex misses, retrieval catches ───────────────────────
    "regional_reworded": {
        "id": "regional_reworded",
        "title": "Reworded offset — $3,240",
        "payer": "Meridian Regional Health Plan",
        "file": "regional_reworded_sample.pdf",
        "role": "wedge",
        "badge": "Primary proof",
        "novel_phrase": (
            "Prior period shortfall netted out of this disbursement against "
            "the current payment cycle"
        ),
        "failure_mode": (
            "No phrase in the pattern library matches this wording, so "
            "regex-only detection reports the full $9,480.00 as received on "
            "a document that is short $3,240.00."
        ),
        "what_it_proves": (
            "Retrieval recovers a clawback the rule library cannot see, and "
            "declines the two benign adjustment lines on the same page."
        ),
        # Disclosed, not buried. A genuine paraphrase: the nearest corpus
        # entry by token overlap is r002 at 0.36, and retrieval resolves the
        # rendered line to r038 ("this payment is short paid to recover an
        # earlier excess disbursement") at 0.510 against a 0.450 threshold.
        # An earlier revision worded this line at 0.78 from r029 and scored
        # 0.884 — a number that mostly measured the corpus against itself.
        "lexical_proximity": {"nearest_corpus_doc": "r002", "token_jaccard": 0.36},
        "expected": {
            "paid_amount": 9480.00,
            "baseline_decision": "CLEAR",
            "baseline_flag_count": 0,
            "cash_at_risk": 3240.00,
        },
    },
    # ── B. the control: regex catches it unaided ────────────────────────────
    "anthem_offset": {
        "id": "anthem_offset",
        "title": "Known offset — $18,020.11",
        "payer": "Anthem Blue Cross Blue Shield",
        "file": "anthem_offset_sample.pdf",
        "role": "control",
        "badge": "Baseline control",
        "novel_phrase": "OUTSTANDING NEGBAL WITH DIFFER",
        "lexical_proximity": {"nearest_corpus_doc": "r001", "token_jaccard": 0.20},
        "failure_mode": (
            "Nothing is wrong with the detection here — the pattern library "
            "catches this unaided. The failure was operational: it sat "
            "unnoticed for 90 days until a collection letter arrived."
        ),
        "what_it_proves": (
            "The regex baseline is a real opponent, not a strawman. A "
            "comparison where the baseline always scores zero proves nothing. "
            "Note the payment goes negative: the clawback exceeds the cheque."
        ),
        "expected": {
            "paid_amount": 17875.09,
            "baseline_decision": "HUMAN_REVIEW",
            "baseline_flag_count": 1,
            "cash_at_risk": 18020.11,
        },
    },
    # ── C. the gap: both paths miss it, and that is the point ──────────────
    "paraphrase_gap": {
        "id": "paraphrase_gap",
        "title": "True paraphrase — $4,150",
        "payer": "Cigna Healthcare",
        "file": "paraphrase_gap_sample.pdf",
        "role": "gap",
        "badge": "Known limitation",
        "novel_phrase": (
            "Clearing a legacy debit carried on the provider ledger against "
            "the current remittance cycle"
        ),
        "failure_mode": (
            "A true paraphrase: token-Jaccard 0.13 against the whole corpus, "
            "and no phrase in the pattern library. Regex misses it and so "
            "does the lexical fallback."
        ),
        "what_it_proves": (
            "The boundary, measured. The fallback retrieves the semantically "
            "correct neighbour — r024, 'provider owes the plan and the debt "
            "is applied here' — and scores it 0.289 against a 0.450 "
            "threshold. Right meaning, nowhere near the confidence needed. "
            "Character n-grams cannot bridge wording that shares no "
            "substrings; embeddings can. This case is bundled to fail, and "
            "it is the acceptance test for connecting Moss: with real "
            "embeddings it must start flagging $4,150."
        ),
        "lexical_proximity": {"nearest_corpus_doc": "r011", "token_jaccard": 0.13},
        "expected": {
            "paid_amount": 12100.00,
            "baseline_decision": "CLEAR",
            "baseline_flag_count": 0,
            # 0.0 is the CURRENT truth on the lexical fallback, asserted so a
            # regression is visible. Connecting Moss should move this to
            # 4150.00 — see tests/test_demo_cases.py::test_gap_case_is_the_
            # moss_acceptance_test, which tracks whichever is true.
            "cash_at_risk": 0.0,
        },
    },
}

DEFAULT_CASE_ID = "regional_reworded"


def list_demo_cases() -> List[Dict[str, Any]]:
    """Catalog entries, each tagged with whether its PDF is actually present."""
    return [
        {**case, "available": _resolve(case["file"]) is not None}
        for case in DEMO_CASES.values()
    ]


def get_case(case_id: Optional[str]) -> Dict[str, Any]:
    return DEMO_CASES.get(case_id or "", DEMO_CASES[DEFAULT_CASE_ID])


def _resolve(filename: str) -> Optional[str]:
    path = os.path.join(SAMPLE_DIR, filename)
    return path if os.path.exists(path) else None


def get_demo_pdf_bytes(case_id: Optional[str]) -> Tuple[str, Optional[bytes]]:
    """Read one bundled demo EOB.

    Returns (filename, None) when the file is missing from the image. The
    caller surfaces that as a 503 — no synthetic stand-in is generated,
    because a document invented at request time would not be the document
    the case metadata describes.
    """
    case = get_case(case_id)
    filename = case["file"]
    path = _resolve(filename)
    if path is None:
        logger.warning("Demo EOB %s is not bundled (looked in %s)", filename, SAMPLE_DIR)
        return filename, None
    with open(path, "rb") as fh:
        return filename, fh.read()
