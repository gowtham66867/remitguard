"""
RecoupmentService — wraps the EOB detection logic for use inside the FastAPI platform.

Migrated from detector.py; operates on bytes (no filesystem reads) so it works
cleanly in a web context where files arrive as multipart uploads.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pdfplumber
from sqlalchemy.orm import Session

from ..models.recoupment import RecoupmentResult, RecoupmentFlag as RecoupmentFlagModel

logger = logging.getLogger(__name__)

import os as _os
PATTERNS_PATH = _os.environ.get(
    "PATTERNS_PATH",
    _os.path.join(_os.path.dirname(__file__), "..", "..", "patterns.json"),
)

MONEY_RE = re.compile(r"\$?\(?-?\s?[\d,]+\.\d{2}\)?")
CLAIM_NUM_RE = re.compile(r"(?i:claim)\s*#?\s*[:\-]?\s*([A-Z0-9]{6,}(?=[\s,.\n]|$))")
DOS_RE = re.compile(r"\bDOS\b\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{2,4})", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Internal dataclasses (not exposed to the API layer — we return plain dicts)
# ---------------------------------------------------------------------------

@dataclass
class _Flag:
    line: str
    matched_phrase: str
    payer_tag: str
    amounts_found: List[float] = field(default_factory=list)
    source: str = "pattern"  # "pattern" | "ledger_mismatch"


@dataclass
class _EOBResult:
    source_file: str
    full_text: str
    billed_amount: Optional[float] = None
    paid_amount: Optional[float] = None
    claim_numbers: List[str] = field(default_factory=list)
    dates_of_service: List[str] = field(default_factory=list)
    flags: List[_Flag] = field(default_factory=list)
    extraction_warning: Optional[str] = None

    @property
    def net_received(self) -> Optional[float]:
        if self.paid_amount is None:
            return None
        if not self.flags:
            return self.paid_amount
        clawed_back = sum(
            amt
            for f in self.flags
            for amt in f.amounts_found
            if f.source == "pattern"
        )
        return round(self.paid_amount - clawed_back, 2)

    def to_dict(self) -> dict:
        return {
            "filename": self.source_file,
            "claim_numbers": self.claim_numbers,
            "dates_of_service": self.dates_of_service,
            "billed_amount": self.billed_amount,
            "paid_amount": self.paid_amount,
            "net_received": self.net_received,
            "flagged": bool(self.flags),
            "flags": [
                {
                    "line": f.line,
                    "matched_phrase": f.matched_phrase,
                    "payer_tag": f.payer_tag,
                    "amounts_found": f.amounts_found,
                    "source": f.source,
                }
                for f in self.flags
            ],
            "extraction_warning": self.extraction_warning,
        }


# ---------------------------------------------------------------------------
# Helpers (module-level so they can be unit-tested independently)
# ---------------------------------------------------------------------------

def _parse_money(token: str) -> float:
    neg = token.strip().startswith("(") or token.strip().startswith("-")
    cleaned = re.sub(r"[^\d.]", "", token)
    val = float(cleaned) if cleaned else 0.0
    return -val if neg else val


def _extract_text_from_bytes(pdf_bytes: bytes) -> str:
    """Extract plain text from PDF bytes using pdfplumber."""
    chunks: List[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            chunks.append(text)
    return "\n".join(chunks)


def _find_paid_and_billed(text: str) -> Tuple[Optional[float], Optional[float]]:
    billed: Optional[float] = None
    paid: Optional[float] = None
    for line in text.splitlines():
        low = line.lower()
        if billed is None and ("billed" in low or "total charge" in low):
            amounts = MONEY_RE.findall(line)
            if amounts:
                billed = _parse_money(amounts[-1])
        if paid is None and (
            "paid" in low or "amount paid" in low or "total payment" in low
        ):
            amounts = MONEY_RE.findall(line)
            if amounts:
                paid = _parse_money(amounts[-1])
    return billed, paid


def _find_recoupment_flags(
    text: str, compiled: Dict[str, re.Pattern]
) -> List[_Flag]:
    flags: List[_Flag] = []
    for line in text.splitlines():
        for tag, regex in compiled.items():
            match = regex.search(line)
            if match:
                amounts = [_parse_money(a) for a in MONEY_RE.findall(line)]
                flags.append(
                    _Flag(
                        line=line.strip(),
                        matched_phrase=match.group(0),
                        payer_tag=tag,
                        amounts_found=amounts,
                        source="pattern",
                    )
                )
                break  # one flag per line is enough
    return flags


def _reconcile_with_ledger(
    result: _EOBResult, ledger: Dict[str, float]
) -> None:
    if result.paid_amount is None:
        return
    for claim in result.claim_numbers:
        expected = ledger.get(claim)
        if expected is None:
            continue
        diff = round(expected - result.paid_amount, 2)
        if abs(diff) > 0.01:
            result.flags.append(
                _Flag(
                    line=(
                        f"Ledger expected ${expected:.2f} for claim {claim}, "
                        f"EOB shows ${result.paid_amount:.2f} paid"
                    ),
                    matched_phrase="ledger_mismatch",
                    payer_tag="ledger",
                    amounts_found=[diff],
                    source="ledger_mismatch",
                )
            )


def _parse_ledger_csv(csv_bytes: bytes) -> Dict[str, float]:
    """Parse ledger CSV (claim_number, expected_amount) from raw bytes."""
    ledger: Dict[str, float] = {}
    text = csv_bytes.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        claim = row.get("claim_number", "").strip()
        if not claim:
            continue
        try:
            ledger[claim] = float(row.get("expected_amount", "0"))
        except ValueError:
            continue
    return ledger


# ---------------------------------------------------------------------------
# Service class
# ---------------------------------------------------------------------------

class RecoupmentService:
    """
    Stateless analysis service.  Instantiate once per request (or as a
    singleton) — patterns are loaded lazily on first use.
    """

    def __init__(self, patterns_path: str = PATTERNS_PATH) -> None:
        self._patterns_path = patterns_path
        self._compiled: Optional[Dict[str, re.Pattern]] = None

    # ------------------------------------------------------------------
    # Pattern management
    # ------------------------------------------------------------------

    def load_patterns(self) -> Dict[str, List[str]]:
        """Load raw patterns dict from JSON file."""
        with open(self._patterns_path) as f:
            raw = json.load(f)
        return {k: v for k, v in raw.items() if not k.startswith("_")}

    def _get_compiled(self) -> Dict[str, re.Pattern]:
        if self._compiled is None:
            patterns = self.load_patterns()
            self._compiled = {
                tag: re.compile("|".join(phrases), re.IGNORECASE)
                for tag, phrases in patterns.items()
            }
        return self._compiled

    # ------------------------------------------------------------------
    # Core analysis
    # ------------------------------------------------------------------

    def _analyze_internal(
        self,
        pdf_bytes: bytes,
        filename: str,
        ledger: Optional[Dict[str, float]] = None,
    ) -> _EOBResult:
        compiled = self._get_compiled()

        try:
            text = _extract_text_from_bytes(pdf_bytes)
        except Exception as exc:
            logger.exception("PDF extraction failed for %s", filename)
            return _EOBResult(
                source_file=filename,
                full_text="",
                extraction_warning=f"Failed to parse PDF: {exc}",
            )

        warning: Optional[str] = None
        if not text.strip():
            warning = (
                "No extractable text found — this PDF is likely scanned. "
                "OCR is not yet configured (requires tesseract); flag for manual review."
            )

        billed, paid = _find_paid_and_billed(text)
        flags = _find_recoupment_flags(text, compiled)
        claim_numbers = CLAIM_NUM_RE.findall(text)
        dates = DOS_RE.findall(text)

        result = _EOBResult(
            source_file=filename,
            full_text=text,
            billed_amount=billed,
            paid_amount=paid,
            claim_numbers=claim_numbers,
            dates_of_service=dates,
            flags=flags,
            extraction_warning=warning,
        )

        if ledger:
            _reconcile_with_ledger(result, ledger)

        return result

    # ------------------------------------------------------------------
    # Public API — returns plain dicts (JSON-serialisable)
    # ------------------------------------------------------------------

    def analyze_pdf(
        self,
        pdf_bytes: bytes,
        filename: str,
        ledger: Optional[Dict[str, float]] = None,
    ) -> dict:
        """
        Analyse a single PDF EOB.

        Parameters
        ----------
        pdf_bytes:  raw bytes of the PDF file
        filename:   original filename (used for display / DB storage)
        ledger:     optional dict of {claim_number: expected_amount} for
                    reconciliation cross-checks

        Returns
        -------
        dict with keys: filename, claim_numbers, dates_of_service,
        billed_amount, paid_amount, net_received, flagged, flags,
        extraction_warning
        """
        result = self._analyze_internal(pdf_bytes, filename, ledger)
        return result.to_dict()

    def batch_analyze(
        self,
        files: List[Tuple[str, bytes]],
        ledger: Optional[Dict[str, float]] = None,
    ) -> Tuple[List[dict], dict]:
        """
        Analyse multiple PDF EOBs.

        Parameters
        ----------
        files:   list of (filename, pdf_bytes) tuples
        ledger:  optional shared ledger dict

        Returns
        -------
        (results, summary) where results is a list of per-file dicts and
        summary is {total_files, total_paid, total_flagged, flagged_count}
        """
        results: List[dict] = []
        for filename, pdf_bytes in files:
            try:
                r = self.analyze_pdf(pdf_bytes, filename, ledger)
            except Exception as exc:
                logger.exception("Unexpected error analysing %s", filename)
                r = {
                    "filename": filename,
                    "claim_numbers": [],
                    "dates_of_service": [],
                    "billed_amount": None,
                    "paid_amount": None,
                    "net_received": None,
                    "flagged": False,
                    "flags": [],
                    "extraction_warning": f"Unexpected error: {exc}",
                }
            results.append(r)

        total_paid = sum(r["paid_amount"] or 0.0 for r in results)
        flagged_results = [r for r in results if r["flagged"]]
        total_flagged = sum(
            amt
            for r in flagged_results
            for f in r["flags"]
            for amt in f["amounts_found"]
        )

        summary = {
            "total_files": len(results),
            "total_paid": round(total_paid, 2),
            "total_flagged": round(total_flagged, 2),
            "flagged_count": len(flagged_results),
        }
        return results, summary

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save_result(
        self,
        db: Session,
        facility_id: Optional[int],
        result_dict: dict,
    ) -> RecoupmentResult:
        """
        Persist a single analysis result (and its flags) to the database.

        Parameters
        ----------
        db:           SQLAlchemy session (injected via get_db dependency)
        facility_id:  optional FK to facilities table
        result_dict:  dict as returned by analyze_pdf()

        Returns
        -------
        The newly created RecoupmentResult ORM object (flushed, not committed —
        the caller is responsible for commit/rollback).
        """
        # Store the first claim number and date for the denormalised columns;
        # the full lists live in the flags rows.
        claim_numbers: List[str] = result_dict.get("claim_numbers") or []
        dates: List[str] = result_dict.get("dates_of_service") or []

        db_result = RecoupmentResult(
            facility_id=facility_id,
            filename=result_dict.get("filename"),
            claim_number=", ".join(claim_numbers) if claim_numbers else None,
            date_of_service=", ".join(dates) if dates else None,
            billed_amount=result_dict.get("billed_amount"),
            paid_amount=result_dict.get("paid_amount"),
            net_received=result_dict.get("net_received"),
            flagged=result_dict.get("flagged", False),
            extraction_warning=result_dict.get("extraction_warning"),
        )
        db.add(db_result)
        db.flush()  # get the auto-generated id before inserting flags

        for flag_dict in result_dict.get("flags") or []:
            db_flag = RecoupmentFlagModel(
                result_id=db_result.id,
                line=flag_dict.get("line"),
                matched_phrase=flag_dict.get("matched_phrase"),
                payer_tag=flag_dict.get("payer_tag"),
                amounts_found=json.dumps(flag_dict.get("amounts_found") or []),
                source=flag_dict.get("source"),
            )
            db.add(db_flag)

        return db_result
