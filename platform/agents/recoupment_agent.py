"""
RecoupmentAgent — self-validating EOB recoupment detector.

Pattern: SELF-VALIDATION LOOP
  Phase 1: Detect flags via pattern matching + ledger reconciliation.
  Phase 2: Score each flag with a confidence rubric; re-examine low-confidence
           flags in a ±3-line window; loop up to 2 iterations.

Python 3.9 compatible.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Reuse helpers from the service layer (no I/O — pure string logic)
# ---------------------------------------------------------------------------

import os as _os
PATTERNS_PATH = _os.environ.get(
    "PATTERNS_PATH",
    _os.path.join(_os.path.dirname(__file__), "..", "patterns.json"),
)

MONEY_RE = re.compile(r"\$?\(?-?\s?[\d,]+\.\d{2}\)?")
CLAIM_NUM_RE = re.compile(r"(?i:claim)\s*#?\s*[:\-]?\s*([A-Z0-9]{6,}(?=[\s,.\n]|$))")
DOS_RE = re.compile(r"\bDOS\b\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{2,4})", re.IGNORECASE)

CONFIDENCE_THRESHOLD = 0.5
MAX_VALIDATE_ITERATIONS = 2
CONTEXT_WINDOW = 3  # lines on each side for re-examination

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclass returned by RecoupmentAgent.run()
# ---------------------------------------------------------------------------

@dataclass
class DetectionResult:
    filename: str
    flags: List[Dict]  # each: line, matched_phrase, payer_tag, amounts_found, source, confidence, validated
    paid_amount: Optional[float]
    billed_amount: Optional[float]
    net_received: Optional[float]
    needs_human_review: bool
    review_reason: Optional[str]
    detection_log: List[Dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_compiled_patterns(path: str = PATTERNS_PATH) -> Dict[str, re.Pattern]:
    with open(path) as fh:
        raw: Dict = json.load(fh)
    compiled = {}
    for tag, phrases in raw.items():
        if tag.startswith("_"):
            continue
        compiled[tag] = re.compile("|".join(phrases), re.IGNORECASE)
    return compiled


def _parse_money(token: str) -> float:
    neg = token.strip().startswith("(") or token.strip().startswith("-")
    cleaned = re.sub(r"[^\d.]", "", token)
    val = float(cleaned) if cleaned else 0.0
    return -val if neg else val


def _extract_amounts(line: str) -> List[float]:
    return [_parse_money(t) for t in MONEY_RE.findall(line)]


def _find_paid_and_billed(text: str):
    billed: Optional[float] = None
    paid: Optional[float] = None
    for line in text.splitlines():
        low = line.lower()
        if billed is None and ("billed" in low or "total charge" in low):
            amounts = MONEY_RE.findall(line)
            if amounts:
                billed = _parse_money(amounts[-1])
        if paid is None and ("paid" in low or "amount paid" in low or "total payment" in low):
            amounts = MONEY_RE.findall(line)
            if amounts:
                paid = _parse_money(amounts[-1])
    return billed, paid


def _detect_flags(text: str, compiled: Dict[str, re.Pattern]) -> List[Dict]:
    """Phase 1 detection: scan every line for pattern matches."""
    flags: List[Dict] = []
    for line in text.splitlines():
        for tag, regex in compiled.items():
            match = regex.search(line)
            if match:
                amounts = _extract_amounts(line)
                flags.append({
                    "line": line.strip(),
                    "matched_phrase": match.group(0),
                    "payer_tag": tag,
                    "amounts_found": amounts,
                    "source": "pattern",
                    "confidence": 0.0,
                    "validated": False,
                })
                break  # one flag per line
    return flags


def _reconcile_with_ledger(
    flags: List[Dict],
    claim_numbers: List[str],
    paid_amount: Optional[float],
    ledger: Dict[str, float],
) -> None:
    """Append ledger-mismatch flags in-place."""
    if paid_amount is None:
        return
    for claim in claim_numbers:
        expected = ledger.get(claim)
        if expected is None:
            continue
        diff = round(expected - paid_amount, 2)
        if abs(diff) > 0.01:
            flags.append({
                "line": (
                    f"Ledger expected ${expected:.2f} for claim {claim}, "
                    f"EOB shows ${paid_amount:.2f} paid"
                ),
                "matched_phrase": "ledger_mismatch",
                "payer_tag": "ledger",
                "amounts_found": [diff],
                "source": "ledger_mismatch",
                "confidence": 0.0,
                "validated": False,
            })


# ---------------------------------------------------------------------------
# Confidence rubric
# ---------------------------------------------------------------------------

_KNOWN_PAYER_NAMES = {
    "anthem", "aetna", "united", "uhc", "cigna", "medicaid", "humana",
    "bcbs", "blue cross", "blue shield", "molina", "centene", "wellcare",
}

_PAYER_PATTERNS_COMPILED = _load_compiled_patterns.__doc__  # dummy sentinel


def _flag_confidence(
    flag: Dict,
    paid_amount: Optional[float],
    compiled: Dict[str, re.Pattern],
) -> float:
    """
    Score a single flag against the confidence rubric.

    Rubric:
      +0.4  Has a dollar amount on the same line
      +0.2  Amount > $100
      +0.2  Payer pattern match (non-ledger source)
      +0.1  Line contains a known payer name
      +0.1  Any amount > paid_amount (full clawback scenario)
    """
    score = 0.0
    amounts = flag.get("amounts_found") or []
    line_lower = flag.get("line", "").lower()

    # +0.4 — dollar amount present on line
    if amounts:
        score += 0.4

    # +0.2 — any amount > $100
    if any(abs(a) > 100 for a in amounts):
        score += 0.2

    # +0.2 — matched via payer pattern (not ledger)
    if flag.get("source") == "pattern":
        score += 0.2

    # +0.1 — line contains a known payer name
    if any(name in line_lower for name in _KNOWN_PAYER_NAMES):
        score += 0.1

    # +0.1 — full clawback: any flagged amount exceeds paid_amount
    if paid_amount is not None and any(abs(a) > paid_amount for a in amounts):
        score += 0.1

    return round(min(score, 1.0), 3)


# ---------------------------------------------------------------------------
# Corroboration search (±3-line window)
# ---------------------------------------------------------------------------

def _find_corroborating_lines(
    flag: Dict,
    all_lines: List[str],
    window: int = CONTEXT_WINDOW,
) -> List[str]:
    """Return up to `window` lines on either side of the flag's line."""
    target = flag.get("line", "").strip()
    for idx, line in enumerate(all_lines):
        if line.strip() == target:
            start = max(0, idx - window)
            end = min(len(all_lines), idx + window + 1)
            context = all_lines[start:end]
            return [l.strip() for l in context if l.strip() != target]
    return []


def _boost_from_context(flag: Dict, context_lines: List[str]) -> float:
    """
    Boost confidence from corroborating nearby lines.
    Adds up to +0.2 if the context contains amounts or payer names.
    """
    boost = 0.0
    combined = " ".join(context_lines).lower()
    if MONEY_RE.search(combined):
        boost += 0.1
    if any(name in combined for name in _KNOWN_PAYER_NAMES):
        boost += 0.1
    return boost


# ---------------------------------------------------------------------------
# RecoupmentAgent
# ---------------------------------------------------------------------------

class RecoupmentAgent:
    """
    Multi-phase recoupment detection agent with self-validation loop.

    Usage
    -----
    result = RecoupmentAgent().run(text, "eob_2024.pdf", ledger={"CLM001": 450.00})
    """

    def __init__(self, patterns_path: str = PATTERNS_PATH) -> None:
        self._compiled = _load_compiled_patterns(patterns_path)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(
        self,
        text: str,
        filename: str,
        ledger: Optional[Dict[str, float]] = None,
    ) -> DetectionResult:
        detection_log: List[Dict] = []
        all_lines = text.splitlines()

        # ---- Phase 1: Detect ------------------------------------------------
        flags = _detect_flags(text, self._compiled)
        billed_amount, paid_amount = _find_paid_and_billed(text)
        claim_numbers = CLAIM_NUM_RE.findall(text)

        if ledger:
            _reconcile_with_ledger(flags, claim_numbers, paid_amount, ledger)

        detection_log.append({
            "phase": "detect",
            "message": f"[RecoupmentAgent][phase=detect] found {len(flags)} flags",
            "flag_count": len(flags),
        })
        logger.info("[RecoupmentAgent][phase=detect] found %d flags", len(flags))

        # ---- Phase 2: Self-validate loop ------------------------------------
        for iteration in range(1, MAX_VALIDATE_ITERATIONS + 1):
            reexamine_next: List[int] = []

            for i, flag in enumerate(flags):
                conf = _flag_confidence(flag, paid_amount, self._compiled)
                flag["confidence"] = conf

                if conf >= CONFIDENCE_THRESHOLD:
                    flag["validated"] = True
                    status = "CONFIRMED"
                else:
                    status = "RE-EXAMINING" if iteration < MAX_VALIDATE_ITERATIONS else "ESCALATE"

                log_entry = {
                    "phase": "validate",
                    "iteration": iteration,
                    "flag_index": i,
                    "confidence": conf,
                    "status": status,
                    "message": (
                        f"[RecoupmentAgent][phase=validate][iter={iteration}] "
                        f"flag#{i+1} confidence={conf:.2f} → {status}"
                    ),
                }
                detection_log.append(log_entry)
                logger.info(log_entry["message"])

                if conf < CONFIDENCE_THRESHOLD:
                    reexamine_next.append(i)

            # Re-examine low-confidence flags using context window
            if reexamine_next and iteration < MAX_VALIDATE_ITERATIONS:
                for i in reexamine_next:
                    flag = flags[i]
                    context = _find_corroborating_lines(flag, all_lines)
                    boost = _boost_from_context(flag, context)
                    if boost > 0:
                        flag["confidence"] = round(
                            min(flag["confidence"] + boost, 1.0), 3
                        )
                        detection_log.append({
                            "phase": "validate",
                            "iteration": iteration,
                            "flag_index": i,
                            "action": "context_boost",
                            "boost": boost,
                            "new_confidence": flag["confidence"],
                            "message": (
                                f"[RecoupmentAgent][phase=validate][iter={iteration}] "
                                f"flag#{i+1} context boost +{boost:.2f} → "
                                f"confidence={flag['confidence']:.2f}"
                            ),
                        })
                        logger.info(detection_log[-1]["message"])

        # After all iterations: mark remaining low-confidence flags as not validated
        for flag in flags:
            if flag["confidence"] < CONFIDENCE_THRESHOLD:
                flag["validated"] = False

        # ---- Build result ---------------------------------------------------
        low_conf_flags = [f for f in flags if f["confidence"] < CONFIDENCE_THRESHOLD]
        needs_review = bool(low_conf_flags)
        review_reason: Optional[str] = None
        if needs_review:
            phrases = [f.get("matched_phrase", "") for f in low_conf_flags]
            review_reason = (
                f"{len(low_conf_flags)} flag(s) below confidence threshold "
                f"after {MAX_VALIDATE_ITERATIONS} iterations: "
                + ", ".join(phrases)
            )

        net_received: Optional[float] = None
        if paid_amount is not None:
            clawed_back = sum(
                amt
                for f in flags
                for amt in f.get("amounts_found", [])
                if f.get("source") == "pattern"
            )
            net_received = round(paid_amount - clawed_back, 2)

        return DetectionResult(
            filename=filename,
            flags=flags,
            paid_amount=paid_amount,
            billed_amount=billed_amount,
            net_received=net_received,
            needs_human_review=needs_review,
            review_reason=review_reason,
            detection_log=detection_log,
        )
