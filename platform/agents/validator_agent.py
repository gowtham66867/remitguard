"""
ValidatorAgent — cross-validation agent for RecoupmentAgent output.

Pattern: CROSS-VALIDATION
  Independently re-checks a DetectionResult using five distinct checks,
  each using different logic from the original detector. Emits a
  ValidationResult with a recommendation of APPROVE / ESCALATE / REJECT.

Python 3.9 compatible.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Import sibling dataclasses — relative imports work within the agents package.
from .recoupment_agent import DetectionResult
from .eob_agent import ExtractionResult

logger = logging.getLogger(__name__)

EXTRACTION_CONFIDENCE_GATE = 0.6
CONFIDENCE_DOWNGRADE = 0.2
AMOUNT_SANITY_MULTIPLIER = 2.0
EDIT_DISTANCE_THRESHOLD = 2


# ---------------------------------------------------------------------------
# Public result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    passed_checks: List[str]
    failed_checks: List[str]
    warnings: List[str]
    adjusted_flags: List[Dict]
    recommendation: str       # APPROVE | ESCALATE | REJECT
    validation_log: List[Dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Levenshtein edit distance (stdlib only, Python 3.9 compatible)
# ---------------------------------------------------------------------------

def _edit_distance(a: str, b: str) -> int:
    """Compute Levenshtein distance between two strings."""
    if a == b:
        return 0
    len_a, len_b = len(a), len(b)
    if len_a == 0:
        return len_b
    if len_b == 0:
        return len_a
    # two-row DP
    prev = list(range(len_b + 1))
    for i in range(1, len_a + 1):
        curr = [i] + [0] * len_b
        for j in range(1, len_b + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[j] = min(
                curr[j - 1] + 1,       # insert
                prev[j] + 1,           # delete
                prev[j - 1] + cost,    # replace
            )
        prev = curr
    return prev[len_b]


# ---------------------------------------------------------------------------
# ValidatorAgent
# ---------------------------------------------------------------------------

class ValidatorAgent:
    """
    Independent cross-validator for RecoupmentAgent output.

    Usage
    -----
    result = ValidatorAgent().validate(detection_result, extraction_result)
    """

    def validate(
        self,
        detection_result: DetectionResult,
        extraction_result: ExtractionResult,
        db: Optional[Any] = None,
    ) -> ValidationResult:
        passed: List[str] = []
        failed: List[str] = []
        warnings: List[str] = []
        validation_log: List[Dict] = []

        # ------------------------------------------------------------------ #
        # Calibration — load per-payer threshold adjustments from FeedbackStats
        # ------------------------------------------------------------------ #
        calibration_adjustments: Dict[str, float] = {}
        if db is not None:
            try:
                from api.services.feedback_calibrator import FeedbackCalibrator
                calibration_adjustments = FeedbackCalibrator.get_threshold_adjustments(db)
            except Exception as _cal_err:  # never break validation on calibration failure
                logger.warning(
                    "[ValidatorAgent] Calibration load failed: %s — "
                    "proceeding with default thresholds.",
                    _cal_err,
                )
        print(
            f"[ValidatorAgent] Calibration loaded: "
            f"{len(calibration_adjustments)} payer adjustment(s) applied"
        )

        # Deep-copy flags so we can adjust confidences without mutating the input
        flags: List[Dict] = [dict(f) for f in detection_result.flags]

        # Apply per-payer confidence threshold calibration to each flag.
        # Base threshold is 0.5; clamp result to [0.3, 0.9].
        BASE_THRESHOLD = 0.5
        for flag in flags:
            payer_tag = str(flag.get("payer_tag") or "generic").lower()
            adjustment = calibration_adjustments.get(payer_tag, 0.0)
            effective_threshold = max(0.3, min(0.9, BASE_THRESHOLD + adjustment))
            flag["_effective_threshold"] = effective_threshold
            if flag.get("confidence", 1.0) < effective_threshold:
                flag["_needs_calibration_review"] = True

        # ------------------------------------------------------------------ #
        # Check 1 — Amount sanity                                            #
        # Flagged amounts sum should not exceed billed_amount * 2            #
        # ------------------------------------------------------------------ #
        check_name = "amount_sanity"
        flagged_total = sum(
            abs(amt)
            for f in flags
            for amt in f.get("amounts_found") or []
        )
        billed = detection_result.billed_amount
        check1_pass = True
        if billed is not None and billed > 0:
            ceiling = billed * AMOUNT_SANITY_MULTIPLIER
            if flagged_total > ceiling:
                check1_pass = False

        if check1_pass:
            passed.append(check_name)
            status = "PASS"
        else:
            failed.append(check_name)
            status = "FAIL"
            warnings.append(
                f"Flagged amounts total ${flagged_total:.2f} exceeds "
                f"billed_amount * {AMOUNT_SANITY_MULTIPLIER} "
                f"(${billed * AMOUNT_SANITY_MULTIPLIER:.2f}) — "
                "possible parser hallucination."
            )

        log_entry = {
            "check": check_name,
            "status": status,
            "flagged_total": flagged_total,
            "billed_amount": billed,
            "message": f"[ValidatorAgent] check={check_name} → {status}",
        }
        validation_log.append(log_entry)
        logger.info(log_entry["message"])

        # ------------------------------------------------------------------ #
        # Check 2 — Extraction confidence gate                               #
        # Low extraction confidence → downgrade all flag confidences         #
        # ------------------------------------------------------------------ #
        check_name = "extraction_confidence"
        ext_conf = extraction_result.confidence
        if ext_conf < EXTRACTION_CONFIDENCE_GATE:
            warnings.append(
                f"Extraction confidence {ext_conf:.2f} is below gate "
                f"{EXTRACTION_CONFIDENCE_GATE}. Downgrading all flag "
                f"confidences by {CONFIDENCE_DOWNGRADE}."
            )
            for f in flags:
                f["confidence"] = round(
                    max(0.0, f.get("confidence", 0.0) - CONFIDENCE_DOWNGRADE), 3
                )
            # This is a warning, not a hard fail, but we note it
            passed.append(check_name)  # check ran; it's a WARN not a FAIL
            status = "WARN"
        else:
            passed.append(check_name)
            status = "PASS"

        log_entry = {
            "check": check_name,
            "status": status,
            "extraction_confidence": ext_conf,
            "message": (
                f"[ValidatorAgent] check={check_name} → {status} "
                f"(confidence={ext_conf:.2f}"
                + (f", downgrading flags)" if status == "WARN" else ")")
            ),
        }
        validation_log.append(log_entry)
        logger.info(log_entry["message"])

        # ------------------------------------------------------------------ #
        # Check 3 — Claim number present                                     #
        # ------------------------------------------------------------------ #
        check_name = "claim_number_present"
        # ExtractionResult has no claim_number field; inspect the text.
        claim_pattern = re.compile(
            r"(?i:claim)\s*#?\s*[:\-]?\s*([A-Z0-9]{6,}(?=[\s,.\n]|$))"
        )
        has_claim = bool(claim_pattern.search(extraction_result.text or ""))

        if has_claim:
            passed.append(check_name)
            status = "PASS"
        else:
            passed.append(check_name)  # advisory only
            status = "WARN"
            warnings.append(
                "No claim number found in extracted text. "
                "Document may be missing claim identifier."
            )

        log_entry = {
            "check": check_name,
            "status": status,
            "has_claim_number": has_claim,
            "message": f"[ValidatorAgent] check={check_name} → {status}",
        }
        validation_log.append(log_entry)
        logger.info(log_entry["message"])

        # ------------------------------------------------------------------ #
        # Check 4 — Duplicate flag deduplication                            #
        # Two flags matching the same line within edit distance 2 → merge   #
        # ------------------------------------------------------------------ #
        check_name = "duplicate_flag_check"
        deduped_flags: List[Dict] = []
        removed_count = 0

        for flag in flags:
            line_a = flag.get("line", "")
            is_dup = False
            for kept in deduped_flags:
                line_b = kept.get("line", "")
                if _edit_distance(line_a, line_b) <= EDIT_DISTANCE_THRESHOLD:
                    # Keep the higher-confidence copy
                    if flag.get("confidence", 0.0) > kept.get("confidence", 0.0):
                        deduped_flags.remove(kept)
                        deduped_flags.append(flag)
                    is_dup = True
                    removed_count += 1
                    break
            if not is_dup:
                deduped_flags.append(flag)

        flags = deduped_flags

        if removed_count == 0:
            passed.append(check_name)
            status = "PASS"
        else:
            passed.append(check_name)  # dedup is a correction, not a failure
            status = "PASS"
            warnings.append(
                f"Deduplicated {removed_count} near-duplicate flag(s) "
                f"(edit distance ≤ {EDIT_DISTANCE_THRESHOLD})."
            )

        log_entry = {
            "check": check_name,
            "status": status,
            "duplicates_removed": removed_count,
            "flags_remaining": len(flags),
            "message": (
                f"[ValidatorAgent] check={check_name} → {status} "
                f"({removed_count} duplicate(s) removed)"
            ),
        }
        validation_log.append(log_entry)
        logger.info(log_entry["message"])

        # ------------------------------------------------------------------ #
        # Check 5 — Net received sanity                                      #
        # net_received should not be positive AND larger than paid_amount    #
        # ------------------------------------------------------------------ #
        check_name = "net_received_sanity"
        net = detection_result.net_received
        paid = detection_result.paid_amount
        check5_pass = True

        if net is not None and paid is not None:
            if net > 0 and net > paid:
                check5_pass = False

        if check5_pass:
            passed.append(check_name)
            status = "PASS"
        else:
            failed.append(check_name)
            status = "FAIL"
            warnings.append(
                f"net_received (${net:.2f}) is positive and exceeds "
                f"paid_amount (${paid:.2f}) — likely a math error in "
                "recoupment amount computation."
            )

        log_entry = {
            "check": check_name,
            "status": status,
            "net_received": net,
            "paid_amount": paid,
            "message": f"[ValidatorAgent] check={check_name} → {status}",
        }
        validation_log.append(log_entry)
        logger.info(log_entry["message"])

        # ------------------------------------------------------------------ #
        # Recommendation                                                      #
        # ------------------------------------------------------------------ #
        recommendation = self._recommend(
            passed=passed,
            failed=failed,
            warnings=warnings,
            flags=flags,
            detection_result=detection_result,
        )

        log_entry = {
            "check": "recommendation",
            "status": recommendation,
            "message": f"[ValidatorAgent] recommendation={recommendation}",
        }
        validation_log.append(log_entry)
        logger.info(log_entry["message"])

        return ValidationResult(
            passed_checks=passed,
            failed_checks=failed,
            warnings=warnings,
            adjusted_flags=flags,
            recommendation=recommendation,
            validation_log=validation_log,
        )

    # ------------------------------------------------------------------
    # Recommendation logic
    # ------------------------------------------------------------------

    def _recommend(
        self,
        passed: List[str],
        failed: List[str],
        warnings: List[str],
        flags: List[Dict],
        detection_result: DetectionResult,
    ) -> str:
        """
        REJECT   — hard sanity check failed (amount_sanity or net_received)
        ESCALATE — human review needed, or multiple warnings, or low-conf flags
        APPROVE  — all checks pass, no warnings, no human-review triggers
        """
        hard_fails = {"amount_sanity", "net_received_sanity"}
        if any(f in failed for f in hard_fails):
            return "REJECT"

        low_conf = [
            f for f in flags
            if f.get("confidence", 1.0) < f.get("_effective_threshold", 0.5)
        ]
        if detection_result.needs_human_review or low_conf or len(warnings) >= 2:
            return "ESCALATE"

        return "APPROVE"
