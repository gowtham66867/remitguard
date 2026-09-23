"""
Regenerate the three bundled demo EOBs into `platform/demo_assets/`.

    python make_demo_samples.py

WHY THESE THREE
---------------
Each case exists to prove a different thing, and together they are meant to
be honest about where the system's edge actually is:

  A. regional_reworded  — the pattern library misses it, retrieval catches it.
                          This is the wedge. Regex reports the full payment as
                          received on a document that is short $3,240.

                          Its wording is a genuine paraphrase (token-Jaccard
                          0.29 against the corpus), not a near-copy. An
                          earlier revision used wording sitting at 0.78 from
                          corpus doc r029, which scored 0.884 — impressive,
                          and almost entirely the index recognising itself.

  B. anthem_offset      — the pattern library catches it on its own. Included
                          deliberately: a benchmark where the baseline scores
                          zero is a rigged benchmark. This is the control that
                          shows regex is a real opponent, not a strawman.

  C. paraphrase_gap     — a true paraphrase sharing almost no surface form
                          with the corpus (token-Jaccard 0.15). The lexical
                          fallback retrieves the semantically correct
                          neighbour — r024, "provider owes the plan and the
                          debt is applied here" — and scores it 0.289 against
                          a 0.45 threshold. Right meaning, nowhere near
                          enough confidence. Both paths miss it.

                          This case is bundled to fail. It is the acceptance
                          test for connecting real embeddings: when Moss is
                          live, case C must start flagging $4,150. Until then
                          the demo shows precisely what is missing instead of
                          claiming it already works.

NO VERBATIM CORPUS COPIES. An earlier revision of case C used the exact text
of corpus document r011, which scored 0.9967 — the corpus recognising itself.
That is not evidence of anything. Every phrase below is checked against the
corpus by `tests/test_demo_cases.py::test_no_demo_phrase_is_a_corpus_copy`.

Patient names are synthetic. Samples ship in the container image and are
served to anyone who opens the demo, so no real person's name belongs here
even in a fabricated document.
"""

from __future__ import annotations

import os

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo_assets")

SAMPLES = {
    # ── A. the wedge ─────────────────────────────────────────────────────────
    "regional_reworded_sample.pdf": [
        "Meridian Regional Health Plan",
        "Provider Remittance Advice",
        "",
        "Provider: Behavioral Health Associates",
        "Patient: Sample Patient A (synthetic)",
        "Claim # MRH20260412",
        "DOS 2/03/2026",
        "",
        "Billed Amount: $14,600.00",
        "Total Payment: $9,480.00",
        "",
        "Remittance detail:",
        "  Prior period shortfall netted out of this disbursement      3,240.00",
        "  against the current payment cycle.",
        "",
        "  Contractual adjustment per provider agreement         5,120.00",
        "  Patient responsibility after plan payment               240.00",
        "",
        "Questions: provider.services@meridianregional.example",
    ],
    # ── B. the control ───────────────────────────────────────────────────────
    "anthem_offset_sample.pdf": [
        "Anthem Blue Cross Blue Shield",
        "Explanation of Benefits",
        "",
        "Provider: Behavioral Health Associates",
        "Patient: Sample Patient B (synthetic)",
        "Claim # 2021223CF200498",
        "DOS 3/27/2021",
        "",
        "Billed Amount: $25,000.00",
        "Total Payment: $17,875.09",
        "",
        "OUTSTANDING NEGBAL WITH DIFFER: $18,020.11",
        "This payment has been applied against a prior outstanding balance",
        "on file for this provider. See remittance detail for prior claim history.",
    ],
    # ── C. the honest gap ────────────────────────────────────────────────────
    "paraphrase_gap_sample.pdf": [
        "Cigna Healthcare",
        "Explanation of Benefits",
        "",
        "Provider: Behavioral Health Associates",
        "Patient: Sample Patient C (synthetic)",
        "Claim # CIGNA9988776",
        "DOS 1/15/2026",
        "",
        "Billed Amount: $16,250.00",
        "Total Payment: $12,100.00",
        "",
        "Remittance detail:",
        "  Clearing a legacy debit carried on the provider ledger      4,150.00",
        "  against the current remittance cycle.",
        "",
        "  Contractual adjustment per provider agreement              3,890.00",
        "  Patient responsibility after plan payment                    260.00",
    ],
}


def render(path: str, lines: list) -> None:
    c = canvas.Canvas(path, pagesize=letter)
    text = c.beginText(72, 720)
    text.setFont("Helvetica", 11)
    for line in lines:
        text.textLine(line)
    c.drawText(text)
    c.save()


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    for name, lines in SAMPLES.items():
        path = os.path.join(OUT_DIR, name)
        render(path, lines)
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
