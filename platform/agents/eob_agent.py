"""
EOB Ingestion Agent for behavioral health RCM pipeline.

Uses an iteration loop pattern: tries multiple extraction strategies,
scores confidence after each, breaks early if threshold met, or escalates.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import List, Optional

import pdfplumber


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ExtractionResult:
    filename: str
    text: str
    confidence: float
    strategy_used: str
    iterations: int
    needs_escalation: bool
    escalation_reason: Optional[str]
    extraction_log: List[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class EOBAgent:
    """Extracts text from EOB (Explanation of Benefits) PDFs."""

    MAX_ITERATIONS = 3
    ACCEPT_THRESHOLD = 0.6
    ESCALATE_THRESHOLD = 0.4

    # Keywords that strongly suggest a payer/EOB document
    PAYER_KEYWORDS = [
        "explanation of benefits",
        "eob",
        "claim",
        "deductible",
        "copay",
        "coinsurance",
        "allowed amount",
        "paid amount",
        "member",
        "provider",
        "remittance",
        "adjudication",
        "blue cross",
        "blue shield",
        "aetna",
        "cigna",
        "humana",
        "anthem",
        "united health",
        "optum",
        "medicaid",
        "medicare",
        "tricare",
        "bcbs",
    ]

    def run(self, pdf_bytes: bytes, filename: str) -> ExtractionResult:
        """
        Run up to MAX_ITERATIONS extraction strategies, scoring confidence
        after each. Returns on first strategy meeting ACCEPT_THRESHOLD, or
        escalates after all iterations if confidence stays below ESCALATE_THRESHOLD.
        """
        strategies = [
            ("pdfplumber_standard", self._extract_standard),
            ("pdfplumber_layout",   self._extract_layout),
            ("char_fallback",       self._extract_char_fallback),
        ]

        best_text = ""
        best_confidence = 0.0
        best_strategy = ""
        extraction_log: List[dict] = []

        for iteration, (strategy_name, extract_fn) in enumerate(strategies, start=1):
            text = extract_fn(pdf_bytes)
            confidence = self.score_confidence(text)

            # Determine decision label for logging
            if confidence >= self.ACCEPT_THRESHOLD:
                decision = "ACCEPTED"
            elif iteration == self.MAX_ITERATIONS:
                decision = "ESCALATE"
            else:
                decision = "RETRY"

            log_entry = {
                "iter": iteration,
                "strategy": strategy_name,
                "confidence": round(confidence, 4),
                "decision": decision,
            }
            extraction_log.append(log_entry)

            # Structured stdout log
            print(
                f"[EOBAgent][iter={iteration}][strategy={strategy_name}] "
                f"confidence={confidence:.2f} → {decision}"
            )

            # Track best result in case we never hit threshold
            if confidence > best_confidence:
                best_confidence = confidence
                best_text = text
                best_strategy = strategy_name

            if confidence >= self.ACCEPT_THRESHOLD:
                return ExtractionResult(
                    filename=filename,
                    text=text,
                    confidence=confidence,
                    strategy_used=strategy_name,
                    iterations=iteration,
                    needs_escalation=False,
                    escalation_reason=None,
                    extraction_log=extraction_log,
                )

        # All iterations exhausted — decide whether to escalate
        needs_escalation = best_confidence < self.ESCALATE_THRESHOLD
        escalation_reason: Optional[str] = None
        if needs_escalation:
            escalation_reason = (
                "Cannot reliably extract text - possible scanned PDF requiring OCR"
            )

        return ExtractionResult(
            filename=filename,
            text=best_text,
            confidence=best_confidence,
            strategy_used=best_strategy,
            iterations=self.MAX_ITERATIONS,
            needs_escalation=needs_escalation,
            escalation_reason=escalation_reason,
            extraction_log=extraction_log,
        )

    # ------------------------------------------------------------------
    # Extraction strategies
    # ------------------------------------------------------------------

    def _extract_standard(self, pdf_bytes: bytes) -> str:
        """Strategy 1: pdfplumber default text extraction."""
        parts: List[str] = []
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages:
                    text = page.extract_text()
                    if text:
                        parts.append(text)
        except Exception:
            pass
        return "\n".join(parts)

    def _extract_layout(self, pdf_bytes: bytes) -> str:
        """Strategy 2: pdfplumber with relaxed layout tolerances."""
        parts: List[str] = []
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages:
                    text = page.extract_text(
                        x_tolerance=5,
                        y_tolerance=5,
                        layout=True,
                        x_density=7.25,
                        y_density=13,
                    )
                    if text:
                        parts.append(text)
        except Exception:
            # layout kwarg may not be available in older pdfplumber versions;
            # fall back without layout param
            try:
                with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                    for page in pdf.pages:
                        text = page.extract_text(x_tolerance=5, y_tolerance=5)
                        if text:
                            parts.append(text)
            except Exception:
                pass
        return "\n".join(parts)

    def _extract_char_fallback(self, pdf_bytes: bytes) -> str:
        """Strategy 3: character-level extraction, assembling text from chars."""
        parts: List[str] = []
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages:
                    chars = page.chars
                    if not chars:
                        continue
                    # Sort by vertical then horizontal position
                    chars_sorted = sorted(chars, key=lambda c: (round(c["top"], 1), c["x0"]))
                    line_parts: List[str] = []
                    prev_top: Optional[float] = None
                    line_chars: List[str] = []
                    for ch in chars_sorted:
                        top = round(ch["top"], 1)
                        if prev_top is None:
                            prev_top = top
                        if abs(top - prev_top) > 3:
                            line_parts.append("".join(line_chars))
                            line_chars = []
                            prev_top = top
                        line_chars.append(ch.get("text", ""))
                    if line_chars:
                        line_parts.append("".join(line_chars))
                    parts.append("\n".join(line_parts))
        except Exception:
            pass
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Confidence scoring
    # ------------------------------------------------------------------

    def score_confidence(self, text: str) -> float:
        """
        Score extracted text quality from 0.0 to 1.0.

        Scoring breakdown:
          0.30 — has dollar amounts  ($1,234.56 or $0.00)
          0.20 — has a claim number  (claim #, claim id, claim no, or bare number)
          0.20 — has a date          (MM/DD/YYYY or YYYY-MM-DD variants)
          0.20 — text length > 100 chars
          0.10 — contains payer-domain keywords
        """
        if not text:
            return 0.0

        score = 0.0
        lower = text.lower()

        # Dollar amounts
        if re.search(r"\$\s*[\d,]+\.?\d*", text):
            score += 0.30

        # Claim number (loose match — claim keyword near digits, or ICN/DCN pattern)
        if re.search(
            r"(claim\s*(#|no\.?|number|id)[:\s]*[\w\-]+|\b\d{6,}\b)",
            lower,
        ):
            score += 0.20

        # Date (MM/DD/YYYY, YYYY-MM-DD, DD-Mon-YYYY, etc.)
        if re.search(
            r"\b(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}|\d{4}[/\-]\d{2}[/\-]\d{2})\b",
            text,
        ):
            score += 0.20

        # Text length
        if len(text.strip()) > 100:
            score += 0.20

        # Payer keywords
        if any(kw in lower for kw in self.PAYER_KEYWORDS):
            score += 0.10

        return min(score, 1.0)


# ---------------------------------------------------------------------------
# Quick smoke-test when run directly
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python eob_agent.py <path/to/eob.pdf>")
        sys.exit(1)

    pdf_path = sys.argv[1]
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    agent = EOBAgent()
    result = agent.run(pdf_bytes, filename=pdf_path)

    print("\n--- ExtractionResult ---")
    print(f"filename        : {result.filename}")
    print(f"confidence      : {result.confidence:.4f}")
    print(f"strategy_used   : {result.strategy_used}")
    print(f"iterations      : {result.iterations}")
    print(f"needs_escalation: {result.needs_escalation}")
    print(f"escalation_reason: {result.escalation_reason}")
    print(f"text preview    : {result.text[:200]!r}")
