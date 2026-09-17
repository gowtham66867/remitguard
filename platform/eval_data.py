"""
Evaluation dataset for recoupment detection.

DESIGN
------
The weakness of a hand-written eval set is that the person writing it and the
person writing the indexed corpus share a vocabulary, so the benchmark measures
recall of their own phrasing rather than of real payer language. Two mitigations
are built in here:

1. The eval phrases are written in a deliberately different register from
   `recoupment_corpus.json` — industry shorthand ("A/R balance netted",
   "chargeback posted", "SIU findings") rather than the corpus's fuller prose.

2. `assert_disjoint_from_corpus()` enforces the separation mechanically. It
   fails if any eval phrase is an exact match OR exceeds a token-overlap
   ceiling against any indexed phrase, so the split cannot silently rot as
   either file is edited.

Lines are then rendered through formatters modelled on how offsets actually
appear in extracted EOB text: ALL CAPS, truncated words, parenthesised
negatives, wide whitespace columns, claim/DOS prefixes. The real Anthem string
that motivated this project — "OUTSTANDING NEG BAL WITH DIFFER" — is caps plus
abbreviation plus a trailing column, which is exactly what defeats naive
matching.

Everything is seeded, so a run is reproducible.
"""

from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

CORPUS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "recoupment_corpus.json")

# Maximum token overlap (Jaccard) an eval phrase may share with any indexed
# phrase before it counts as leakage rather than generalisation.
MAX_CORPUS_OVERLAP = 0.60


# ── phrase banks ──────────────────────────────────────────────────────────────
# (phrase, payer, category)

RECOUPMENT_PHRASES: List[Tuple[str, str, str]] = [
    ("chargeback posted to provider account",                "generic",  "debt_collection"),
    ("A/R balance netted against current cycle",             "generic",  "ar_netting"),
    ("funds pulled back per retro review",                   "aetna",    "audit_recovery"),
    ("claim voided and monies returned to plan",             "generic",  "reversal"),
    ("prior cycle credit applied here",                      "anthem",   "ar_netting"),
    ("amount held for open refund case",                     "cigna",    "debt_collection"),
    ("we are reclaiming an earlier disbursement",            "generic",  "advance_recovery"),
    ("provider debt applied this voucher",                   "generic",  "debt_collection"),
    ("retro adjustment reduces amount payable",              "anthem",   "offset_explicit"),
    ("SIU findings result in funds returned",                "generic",  "audit_recovery"),
    ("payment rescinded for non covered period",             "generic",  "reversal"),
    ("carryover deficit deducted",                           "anthem",   "ar_netting"),
    ("overpaid earlier claim now corrected downward",        "united_healthcare", "audit_recovery"),
    ("sum retained against prior disbursement error",        "generic",  "advance_recovery"),
    ("your account is in arrears to the plan and reduced",   "generic",  "debt_collection"),
    ("recovery unit has applied a deduction",                "medicaid", "audit_recovery"),
    ("funds clawed from this remittance",                    "generic",  "offset_explicit"),
    ("negative adjustment from prior determination",         "anthem",   "offset_explicit"),
    ("we withheld payment to cure an earlier excess",        "generic",  "advance_recovery"),
    ("claim rescored and payment pulled",                    "generic",  "reversal"),
    ("amount charged back per contract audit",               "cigna",    "audit_recovery"),
    ("prior payment nullified and recovered",                "generic",  "reversal"),
    ("open receivable satisfied from this check",            "generic",  "ar_netting"),
    ("deduction applied to close a refund request",          "united_healthcare", "debt_collection"),
    ("reduction for monies previously issued in excess",     "medicaid", "advance_recovery"),
]

# Offsets worded the way the regex library already expects. A benchmark made
# only of rewordings would rig the baseline to zero and make any semantic layer
# look good; in production a real share of offsets do use familiar language.
# Keeping these in means the reported delta is the honest marginal gain, and the
# per-category breakdown shows exactly where each mechanism earns its keep.
FAMILIAR_RECOUPMENT_PHRASES: List[Tuple[str, str, str]] = [
    ("offset taken this cycle",                              "generic",  "familiar_wording"),
    ("recoupment processed against claim",                   "medicaid", "familiar_wording"),
    ("prior overpayment identified on account",              "aetna",    "familiar_wording"),
    ("balance forward from last remittance",                 "generic",  "familiar_wording"),
    ("amount withheld this period",                          "generic",  "familiar_wording"),
    ("takeback of funds issued earlier",                     "generic",  "familiar_wording"),
    ("deducted from this payment per plan",                  "generic",  "familiar_wording"),
    ("net balance due to the plan",                          "generic",  "familiar_wording"),
    ("previously overpaid on this member",                   "generic",  "familiar_wording"),
    ("adjustment for prior claim correction",                "generic",  "familiar_wording"),
    ("outstanding neg bal with differ",                      "anthem",   "familiar_wording"),
    ("overpayment recovery on prior claim",                  "aetna",    "familiar_wording"),
    ("negative balance applied to remit",                    "united_healthcare", "familiar_wording"),
]

BENIGN_PHRASES: List[Tuple[str, str, str]] = [
    ("member cost share applied",                            "generic", "patient_resp"),
    ("plan discount per network contract",                   "generic", "contractual"),
    ("amount applied to annual deductible",                  "generic", "patient_resp"),
    ("not medically necessary per review",                   "generic", "denial"),
    ("prior authorization not obtained",                     "generic", "denial"),
    ("benefit maximum reached for this period",              "generic", "denial"),
    ("eligible amount after network reduction",              "generic", "contractual"),
    ("secondary payer responsibility",                       "generic", "informational"),
    ("supplemental payment for quality bonus",               "generic", "favorable_payment"),
    ("retroactive rate increase paid to provider",           "generic", "favorable_payment"),
    ("late payment interest issued",                         "generic", "favorable_payment"),
    ("please retain for your records",                       "generic", "informational"),
    ("claim accepted and scheduled for payment",             "generic", "informational"),
    ("member has other insurance primary",                   "generic", "informational"),
    ("service included in global period",                    "generic", "denial"),
    ("modifier required for this procedure",                 "generic", "denial"),
    ("rendering provider NPI on file",                       "generic", "informational"),
    ("check enclosed for the amount shown",                  "generic", "informational"),
    ("network savings applied to this claim",                "generic", "contractual"),
    ("patient deductible met for the year",                  "generic", "patient_resp"),
    ("coinsurance portion billed to member",                 "generic", "patient_resp"),
    ("adjusted to fee schedule allowance",                   "generic", "contractual"),
    ("additional reimbursement after reconsideration",       "generic", "favorable_payment"),
    ("amount payable under member benefits",                 "generic", "informational"),
    ("claim under review no action needed",                  "generic", "informational"),
    ("copay collected at time of service",                   "generic", "patient_resp"),
    ("out of pocket maximum not yet met",                    "generic", "patient_resp"),
    ("allowable charge under the fee schedule",              "generic", "contractual"),
    ("provider discount honoured on this claim",             "generic", "contractual"),
    ("duplicate of a previously submitted claim",            "generic", "denial"),
    ("procedure code invalid for date of service",           "generic", "denial"),
    ("member not eligible on the service date",              "generic", "denial"),
    ("payment issued under the capitation arrangement",      "generic", "informational"),
    ("remittance covers the claims listed below",            "generic", "informational"),
    ("incentive payment for care quality measures",          "generic", "favorable_payment"),
    ("reprocessed in your favour additional amount due",     "generic", "favorable_payment"),
    ("appeal upheld and balance released to provider",       "generic", "favorable_payment"),
    ("electronic funds transfer to account on file",         "generic", "informational"),
]


# ── EOB formatting ────────────────────────────────────────────────────────────

_ABBREV = {
    "balance": "BAL", "negative": "NEG", "adjustment": "ADJ", "amount": "AMT",
    "account": "ACCT", "receivable": "RECV", "payment": "PMT", "previously": "PREV",
    "prior": "PR", "outstanding": "OUTSTD", "provider": "PROV", "recovery": "RECOV",
    "difference": "DIFFER", "reduction": "REDUC", "deduction": "DEDUC",
}


def _abbreviate(text: str) -> str:
    out = []
    for word in text.split():
        out.append(_ABBREV.get(word.lower().strip(".,"), word))
    return " ".join(out)


def _fmt_plain(phrase: str, amt: str) -> str:
    return f"{phrase}   {amt}"


def _fmt_caps(phrase: str, amt: str) -> str:
    return f"{phrase.upper()}   {amt}"


def _fmt_dollar(phrase: str, amt: str) -> str:
    return f"{phrase}   ${amt}"


def _fmt_parens(phrase: str, amt: str) -> str:
    return f"{phrase}   ({amt})"


def _fmt_column(phrase: str, amt: str) -> str:
    pad = max(4, 58 - len(phrase))
    return f"{phrase}{' ' * pad}{amt}"


def _fmt_abbrev_caps(phrase: str, amt: str) -> str:
    return f"{_abbreviate(phrase).upper()}   {amt}"


def _fmt_claim_prefix(phrase: str, amt: str, rng: random.Random) -> str:
    claim = f"CLM{rng.randint(10_000_000, 99_999_999)}"
    return f"{claim}  {phrase}   {amt}"


FORMATTERS = [
    ("plain", _fmt_plain),
    ("caps", _fmt_caps),
    ("dollar", _fmt_dollar),
    ("parens", _fmt_parens),
    ("column", _fmt_column),
    ("abbrev_caps", _fmt_abbrev_caps),
]


@dataclass
class EvalLine:
    text: str
    is_recoupment: bool
    category: str
    payer: str
    style: str
    amount: float
    phrase: str


def _amount(rng: random.Random) -> Tuple[float, str]:
    """Realistic EOB amounts, weighted toward the sizes that matter."""
    bucket = rng.random()
    if bucket < 0.25:
        value = round(rng.uniform(10, 500), 2)
    elif bucket < 0.75:
        value = round(rng.uniform(500, 10_000), 2)
    else:
        value = round(rng.uniform(10_000, 40_000), 2)
    return value, f"{value:,.2f}"


def build_eval_set(seed: int = 20260917, per_phrase: int = 4) -> List[EvalLine]:
    """
    Render every phrase through `per_phrase` distinct formatting styles.

    38 recoupment (25 reworded + 13 in familiar library wording) and 38 benign
    phrases x 4 styles, with the style assignment rotated per phrase so no
    phrase is over-represented in any one rendering.
    """
    rng = random.Random(seed)
    lines: List[EvalLine] = []

    for bank, is_recoup in ((RECOUPMENT_PHRASES, True),
                            (FAMILIAR_RECOUPMENT_PHRASES, True),
                            (BENIGN_PHRASES, False)):
        for idx, (phrase, payer, category) in enumerate(bank):
            styles = FORMATTERS[idx % len(FORMATTERS):] + FORMATTERS[:idx % len(FORMATTERS)]
            for style_name, fn in styles[:per_phrase]:
                value, rendered_amt = _amount(rng)
                if style_name == "plain" and rng.random() < 0.3:
                    text = _fmt_claim_prefix(phrase, rendered_amt, rng)
                    style_name = "claim_prefix"
                else:
                    text = fn(phrase, rendered_amt)
                lines.append(EvalLine(
                    text=text, is_recoupment=is_recoup, category=category,
                    payer=payer, style=style_name, amount=value, phrase=phrase,
                ))

    rng.shuffle(lines)
    return lines


# ── document-level set ────────────────────────────────────────────────────────

@dataclass
class EvalDocument:
    name: str
    text: str
    contains_recoupment: bool
    split_across_lines: bool


def build_document_set(seed: int = 20260917, n_docs: int = 40) -> List[EvalDocument]:
    """
    Assemble multi-line EOB documents.

    A quarter of the positive documents split the offset across two lines —
    wording on one, amount on the next — which is common in extracted PDF text
    and is the case the money-gate pre-filter is least equipped for. Measuring
    it is the point: the harness should surface this system's own weak spot.
    """
    rng = random.Random(seed + 1)
    header = [
        "Meridian Regional Health Plan",
        "Provider Remittance Advice",
        "Provider: Behavioral Health Associates",
    ]
    docs: List[EvalDocument] = []

    for i in range(n_docs):
        positive = i % 2 == 0
        split = positive and (i % 8 == 0)
        body = list(header)
        body.append(f"Claim # MRH{rng.randint(10_000_000, 99_999_999)}")

        for _ in range(rng.randint(2, 4)):
            phrase, _payer, _cat = rng.choice(BENIGN_PHRASES)
            _v, amt = _amount(rng)
            body.append(f"  {phrase}   {amt}")

        if positive:
            phrase, _payer, _cat = rng.choice(
                RECOUPMENT_PHRASES + FAMILIAR_RECOUPMENT_PHRASES)
            _v, amt = _amount(rng)
            if split:
                body.append(f"  {phrase}")
                body.append(f"      {amt}")
            else:
                body.append(f"  {phrase}   {amt}")

        body.append("  Total amount paid to provider   9,480.00")
        rng.shuffle(body[4:-1])
        docs.append(EvalDocument(
            name=f"doc_{i:03d}.pdf",
            text="\n".join(body),
            contains_recoupment=positive,
            split_across_lines=split,
        ))
    return docs


# ── leakage guard ─────────────────────────────────────────────────────────────

def _tokens(text: str) -> set:
    cleaned = "".join(c.lower() if c.isalnum() else " " for c in text)
    return {t for t in cleaned.split() if len(t) > 2}


def corpus_phrases(path: str = CORPUS_PATH) -> List[str]:
    with open(path) as fh:
        raw = json.load(fh)
    return [d["text"] for d in raw.get("documents", [])]


def overlap_report(path: str = CORPUS_PATH) -> List[Dict]:
    """Worst-case token overlap between each eval phrase and the indexed corpus."""
    corpus = [(t, _tokens(t)) for t in corpus_phrases(path)]
    report = []
    banks = ([(p, "novel") for p, _, _ in RECOUPMENT_PHRASES]
             + [(p, "familiar") for p, _, _ in FAMILIAR_RECOUPMENT_PHRASES]
             + [(p, "benign") for p, _, _ in BENIGN_PHRASES])
    for phrase, bank in banks:
        pt = _tokens(phrase)
        worst_text, worst = "", 0.0
        for ctext, ct in corpus:
            j = len(pt & ct) / len(pt | ct) if (pt | ct) else 0.0
            if j > worst:
                worst, worst_text = j, ctext
        report.append({"phrase": phrase, "bank": bank,
                       "max_overlap": round(worst, 3),
                       "nearest_corpus_phrase": worst_text})
    return report


def assert_disjoint_from_corpus(path: str = CORPUS_PATH,
                                ceiling: float = MAX_CORPUS_OVERLAP) -> None:
    """
    Fail if the eval set has leaked into the indexed corpus.

    Exact duplicates make the benchmark meaningless; near-duplicates make it
    quietly optimistic. Both are caught here so the split cannot rot silently.
    """
    exact = set(corpus_phrases(path))
    violations = []
    for row in overlap_report(path):
        # The familiar-wording bank is *meant* to resemble known phrasing — it
        # exists to give the regex baseline something real to catch. Only the
        # novel and benign banks carry the generalisation claim, so only they
        # are held to the overlap ceiling.
        if row["bank"] == "familiar":
            continue
        if row["phrase"] in exact:
            violations.append(f"EXACT DUPLICATE: {row['phrase']!r}")
        elif row["max_overlap"] > ceiling:
            violations.append(
                f"overlap {row['max_overlap']:.2f} > {ceiling:.2f}: "
                f"{row['phrase']!r} vs corpus {row['nearest_corpus_phrase']!r}"
            )
    if violations:
        raise AssertionError(
            "Eval set overlaps the indexed corpus — results would measure "
            "memorisation, not generalisation:\n  " + "\n  ".join(violations)
        )
