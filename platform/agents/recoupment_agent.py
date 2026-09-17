"""
RecoupmentAgent — self-validating EOB recoupment detector.

Pattern: SELF-VALIDATION LOOP
  Phase 1: Detect flags via pattern matching + Moss semantic recall + ledger
           reconciliation.
  Phase 2: Score each flag with a confidence rubric; re-examine low-confidence
           flags in a ±3-line window; loop up to 2 iterations.

Detection runs two complementary passes per line:

  1. Regex against `patterns.json` — exact, zero-cost, but only catches phrasings
     already in the library.
  2. Moss semantic retrieval (`agents/semantic_matcher.py`) on the lines regex
     missed — catches reworded offsets the library has never seen, which is the
     failure mode that lets a clawback sit undetected for 90 days.

Pass 2 is optional. With no Moss credentials the agent behaves exactly as it did
before: regex-only, same flags, same confidences.

Python 3.9 compatible (the Moss layer self-disables below Python 3.10).
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


def _detect_flags(
    text: str,
    compiled: Dict[str, re.Pattern],
    matcher=None,
    semantic_requires_amount: bool = True,
) -> List[Dict]:
    """
    Phase 1 detection: scan every line for pattern matches, then fall back to
    Moss semantic retrieval on the lines no regex claimed.

    The semantic pass only ever sees lines regex already rejected, so it can add
    recall but can never change an existing regex verdict. `matcher=None`
    reproduces the original regex-only behaviour exactly.

    `semantic_requires_amount` restricts the semantic pass to lines carrying a
    dollar figure. A clawback the practice can act on always states an amount,
    while prose, addresses and footers do not — so the gate removes a whole
    class of unactionable near-miss flags and cuts query volume (and therefore
    latency) on a typical EOB at the same time. Regex matching is unaffected.
    """
    flags: List[Dict] = []
    for line in text.splitlines():
        matched = False
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
                matched = True
                break  # one flag per line

        if matched or matcher is None:
            continue

        # ── semantic recall pass ──────────────────────────────────────────────
        amounts = _extract_amounts(line)
        if semantic_requires_amount and not amounts:
            continue

        hit = matcher.match(line)
        if hit is not None:
            flag = {
                "line": line.strip(),
                "amounts_found": amounts,
                "confidence": 0.0,
                "validated": False,
            }
            flag.update(hit.to_flag_fields())
            flags.append(flag)

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
      +0.4   Has a dollar amount on the same line
      +0.2   Amount > $100
      +0.2   Payer regex pattern match (non-ledger source)
      +0.15  Moss semantic match — weighted below an exact regex hit, since a
             semantic neighbour is weaker evidence than a known payer phrase
      +0.05  ...and the semantic hit resolved to a human-confirmed phrase
      +0.1   Line contains a known payer name
      +0.1   Any amount > paid_amount (full clawback scenario)
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
    elif flag.get("source") == "semantic":
        # Semantic recall is real evidence but weaker than an exact phrase hit.
        score += 0.15
        if flag.get("semantic_learned"):
            # This phrasing was confirmed by a billing coordinator on a previous
            # EOB — strongest signal the semantic layer can offer.
            score += 0.05

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

    def __init__(
        self,
        patterns_path: str = PATTERNS_PATH,
        matcher=None,
        use_semantic: bool = True,
    ) -> None:
        self._compiled = _load_compiled_patterns(patterns_path)

        # Moss semantic layer. Resolved lazily via the process-wide singleton so
        # the index is loaded once and shared; `use_semantic=False` pins the
        # agent to regex-only, which the eval harness uses as its baseline.
        if not use_semantic:
            self._matcher = None
        elif matcher is not None:
            self._matcher = matcher
        else:
            try:
                from agents.semantic_matcher import get_matcher
                self._matcher = get_matcher()
            except Exception:  # never let the optional layer break detection
                self._matcher = None

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
        active_matcher = self._matcher if (self._matcher and self._matcher.ready) else None
        flags = _detect_flags(text, self._compiled, matcher=active_matcher)
        billed_amount, paid_amount = _find_paid_and_billed(text)
        claim_numbers = CLAIM_NUM_RE.findall(text)

        if ledger:
            _reconcile_with_ledger(flags, claim_numbers, paid_amount, ledger)

        semantic_count = sum(1 for f in flags if f.get("source") == "semantic")
        detection_log.append({
            "phase": "detect",
            "message": (
                f"[RecoupmentAgent][phase=detect] found {len(flags)} flags "
                f"({semantic_count} via Moss semantic recall)"
            ),
            "flag_count": len(flags),
            "semantic_flag_count": semantic_count,
            "semantic_enabled": active_matcher is not None,
        })
        logger.info(
            "[RecoupmentAgent][phase=detect] found %d flags (%d semantic)",
            len(flags), semantic_count,
        )

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
                if f.get("source") in ("pattern", "semantic")
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
