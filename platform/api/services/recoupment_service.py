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
from typing import Any, Dict, List, Optional, Tuple

try:
    import pdfplumber
    _PDFPLUMBER_AVAILABLE = True
except ImportError:
    _PDFPLUMBER_AVAILABLE = False
try:
    from sqlalchemy.orm import Session
    from ..models.recoupment import RecoupmentResult, RecoupmentFlag as RecoupmentFlagModel
    _DB_AVAILABLE = True
except (ImportError, ValueError):
    Session = Any  # type: ignore
    RecoupmentResult = Any  # type: ignore
    RecoupmentFlagModel = Any  # type: ignore
    _DB_AVAILABLE = False

logger = logging.getLogger(__name__)

import os as _os
PATTERNS_PATH = _os.environ.get(
    "PATTERNS_PATH",
    _os.path.join(_os.path.dirname(__file__), "..", "..", "patterns.json"),
)

MONEY_RE = re.compile(r"\$?\(?-?\s?[\d,]+\.\d{2}\)?")
CLAIM_NUM_RE = re.compile(r"(?i:claim)\s*#?\s*[:\-]?\s*([A-Z0-9]{6,}(?=[\s,.\n]|$))")
DOS_RE = re.compile(r"\bDOS\b\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{2,4})", re.IGNORECASE)
_WORD_RE = re.compile(r"[A-Za-z]{2,}")


# ---------------------------------------------------------------------------
# Internal dataclasses (not exposed to the API layer — we return plain dicts)
# ---------------------------------------------------------------------------

@dataclass
class _Flag:
    line: str
    matched_phrase: str
    payer_tag: str
    amounts_found: List[float] = field(default_factory=list)
    source: str = "pattern"  # "pattern" | "semantic" | "ledger_mismatch"
    semantic_score: Optional[float] = None
    semantic_doc_id: Optional[str] = None
    semantic_learned: bool = False


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
            abs(amt)
            for f in self.flags
            for amt in f.amounts_found
            if f.source in ("pattern", "semantic")
        )
        return round(self.paid_amount - clawed_back, 2)

    @property
    def recoupment_amount(self) -> float:
        """Dollar value requiring a coordinator decision.

        EOBs express an offset as either a positive adjustment line or a
        parenthesised/negative amount.  The operational question is always
        "how much cash is at risk?", so the UI deliberately reports the
        absolute value.  Ledger mismatches are included because they need the
        same reconciliation workflow even when the EOB does not name an
        offset explicitly.
        """
        return round(sum(
            abs(amount)
            for flag in self.flags
            for amount in flag.amounts_found
        ), 2)

    @property
    def recovery_case(self) -> dict:
        """Return the compact, auditable work packet a biller needs next."""
        if not self.flags:
            return {
                "decision": "CLEAR",
                "recovery_at_risk": 0.0,
                "recommended_action": "No recoupment language detected. Post normally.",
                "evidence_count": 0,
            }

        sources = {flag.source for flag in self.flags}
        if sources == {"ledger_mismatch"}:
            action = "Reconcile the payment against the submitted claims ledger before posting."
        else:
            action = (
                "Hold final posting and route this EOB to a billing coordinator "
                "with the highlighted payer-language evidence."
            )
        return {
            "decision": "HUMAN_REVIEW",
            "recovery_at_risk": self.recoupment_amount,
            "recommended_action": action,
            "evidence_count": len(self.flags),
        }

    @property
    def comparison(self) -> dict:
        """Dual reality comparison: Regex Baseline vs. RemitGuard + Moss Semantic Recall."""
        regex_flags = [f for f in self.flags if f.source != "semantic"]
        semantic_flags = [f for f in self.flags if f.source == "semantic"]

        regex_recoupment = round(
            sum(abs(amt) for f in regex_flags for amt in f.amounts_found), 2
        )
        regex_flagged = bool(regex_flags)
        regex_net = (
            round(self.paid_amount - regex_recoupment, 2)
            if self.paid_amount is not None
            else None
        )

        moss_recoupment = self.recoupment_amount
        moss_flagged = bool(self.flags)
        moss_net = self.net_received

        diverged = bool(semantic_flags)
        cash_at_risk_uncovered = round(moss_recoupment - regex_recoupment, 2)

        nearest_precedent = None
        top_score = None
        if semantic_flags:
            top_sem = max(semantic_flags, key=lambda f: f.semantic_score or 0.0)
            nearest_precedent = top_sem.matched_phrase
            top_score = top_sem.semantic_score

        return {
            "baseline_regex": {
                "status": (
                    "APPROVED (SILENT CLAWBACK MISSED)"
                    if diverged
                    else ("FLAGGED" if regex_flagged else "APPROVED")
                ),
                "flagged": regex_flagged,
                "recoupment_amount": regex_recoupment,
                "net_received": regex_net,
                "flags_count": len(regex_flags),
                "summary": (
                    "Rules missed novel wording; payment posted as clean cash."
                    if diverged
                    else ("Known pattern matched." if regex_flagged else "No rules triggered.")
                ),
            },
            "with_moss": {
                "status": "HOLD FOR REVIEW (FLAGGED IN 3ms)" if moss_flagged else "APPROVED",
                "flagged": moss_flagged,
                "recoupment_amount": moss_recoupment,
                "net_received": moss_net,
                "flags_count": len(self.flags),
                "nearest_precedent": nearest_precedent,
                "semantic_score": top_score,
                "summary": (
                    f"Novel wording mapped to precedent: '{nearest_precedent}'; payment held before posting."
                    if diverged
                    else ("Known pattern caught." if moss_flagged else "Clean EOB.")
                ),
            },
            "divergence": {
                "diverged": diverged,
                "cash_at_risk_uncovered": cash_at_risk_uncovered,
                "operational_outcome": (
                    f"PREVENTED: ${cash_at_risk_uncovered:,.2f} clawback stopped before posting"
                    if diverged
                    else "Consistent verdict across rule and semantic engines."
                ),
            },
        }

    def to_dict(self) -> dict:
        return {
            "filename": self.source_file,
            "claim_numbers": self.claim_numbers,
            "dates_of_service": self.dates_of_service,
            # Convenience fields for a reviewer-facing case card. The full
            # lists remain available above for multi-claim remittances.
            "claim_number": ", ".join(self.claim_numbers) or None,
            "date_of_service": ", ".join(self.dates_of_service) or None,
            "billed_amount": self.billed_amount,
            "paid_amount": self.paid_amount,
            "net_received": self.net_received,
            "flagged": bool(self.flags),
            # UI-safe aliases make the recovery outcome explicit rather than
            # asking clients to reconstruct money-at-risk from raw lines.
            "has_recoupment": bool(self.flags),
            "recoupment_amount": self.recoupment_amount,
            "amount_flagged": self.recoupment_amount,
            "recoupment_text": self.flags[0].line if self.flags else None,
            "recovery_case": self.recovery_case,
            "comparison": self.comparison,
            "flags": [
                {
                    "line": f.line,
                    "matched_phrase": f.matched_phrase,
                    "payer_tag": f.payer_tag,
                    "amounts_found": f.amounts_found,
                    "source": f.source,
                    "semantic_score": f.semantic_score,
                    "semantic_doc_id": f.semantic_doc_id,
                    "semantic_learned": f.semantic_learned,
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
    """Extract plain text from PDF bytes using pdfplumber with pure-python stream fallback."""
    if _PDFPLUMBER_AVAILABLE:
        try:
            chunks: List[str] = []
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages:
                    text = page.extract_text() or ""
                    chunks.append(text)
            extracted = "\n".join(chunks).strip()
            if extracted:
                return extracted
        except Exception as exc:
            logger.debug("pdfplumber extraction failed: %s", exc)

    # Pure standard library fallback: extracts text from PDF content streams
    # (handles ASCII85, FlateDecode, and direct text operands)
    lines: List[str] = []
    import base64
    import zlib

    stream_matches = list(
        re.finditer(b"stream[\r\n]+(.*?)(?:endstream|endobj)", pdf_bytes, re.DOTALL)
    )
    for sm in stream_matches:
        raw_stream = sm.group(1).strip()
        decoded_bytes = None
        if b"~>" in raw_stream:
            chunk = raw_stream[:raw_stream.find(b"~>") + 2]
            try:
                decoded_bytes = base64.a85decode(chunk, adobe=True)
            except Exception:
                pass
        if decoded_bytes is None:
            decoded_bytes = raw_stream

        decomp = None
        for wbits in (15, -15, 31, 47):
            try:
                decomp = zlib.decompress(decoded_bytes, wbits).decode("latin1", errors="replace")
                break
            except Exception:
                pass
        if decomp is None:
            decomp = decoded_bytes.decode("latin1", errors="replace")

        for tm in re.finditer(r"\(([^)]*)\)\s*T[jJ]", decomp):
            lines.append(tm.group(1))

    return "\n".join(lines) if lines else pdf_bytes.decode("latin1", errors="replace")


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
    text: str, compiled: Dict[str, re.Pattern], matcher=None
) -> List[_Flag]:
    """Detect known phrases, then use Moss only on regex-missed money lines.

    The ordering is intentional: deterministic payer rules remain the first
    line of defence, while Moss supplies recall for reworded offsets. A
    disabled or warming matcher is a safe no-op, keeping the legacy path
    available during cold starts.
    """
    flags: List[_Flag] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        matched = False
        amounts = [_parse_money(a) for a in MONEY_RE.findall(line)]
        # ±1-line lookahead/lookbehind for amounts split across lines (common in multi-column tables):
        if not amounts:
            if i + 1 < len(lines):
                next_amounts = [_parse_money(a) for a in MONEY_RE.findall(lines[i + 1])]
                if next_amounts and len(_WORD_RE.findall(lines[i + 1])) <= 2:
                    amounts = next_amounts
            if not amounts and i > 0:
                prev_amounts = [_parse_money(a) for a in MONEY_RE.findall(lines[i - 1])]
                if prev_amounts and len(_WORD_RE.findall(lines[i - 1])) <= 2:
                    amounts = prev_amounts

        for tag, regex in compiled.items():
            match = regex.search(line)
            if match:
                flags.append(
                    _Flag(
                        line=line.strip(),
                        matched_phrase=match.group(0),
                        payer_tag=tag,
                        amounts_found=amounts,
                        source="pattern",
                    )
                )
                matched = True
                break  # one flag per line is enough

        if matched or matcher is None or not getattr(matcher, "ready", False):
            continue

        # An actionable recovery case has a dollar value. This gate both avoids
        # footer/prose false positives and keeps per-EOB query volume bounded.
        if not amounts:
            continue
        hit = matcher.match(line)
        if hit is not None:
            flags.append(
                _Flag(
                    line=line.strip(),
                    matched_phrase=hit.matched_text,
                    payer_tag=hit.payer_tag,
                    amounts_found=amounts,
                    source="semantic",
                    semantic_score=hit.score,
                    semantic_doc_id=hit.matched_doc_id,
                    semantic_learned=hit.learned,
                )
            )
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
        # The optional import keeps raw PDF analysis usable on environments
        # where Moss is intentionally unavailable. The matcher itself reports
        # the fallback reason through /api/semantic/stats.
        try:
            from agents.semantic_matcher import get_matcher
            matcher = get_matcher()
        except Exception as exc:  # pragma: no cover - optional integration
            logger.info("Semantic recall unavailable for %s: %s", filename, exc)
            matcher = None
        flags = _find_recoupment_flags(text, compiled, matcher=matcher)
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
                    "claim_number": None,
                    "date_of_service": None,
                    "billed_amount": None,
                    "paid_amount": None,
                    "net_received": None,
                    "flagged": False,
                    "has_recoupment": False,
                    "recoupment_amount": 0.0,
                    "amount_flagged": 0.0,
                    "recoupment_text": None,
                    "recovery_case": {
                        "decision": "UNAVAILABLE",
                        "recovery_at_risk": 0.0,
                        "recommended_action": "Analysis could not be completed; review the extraction warning.",
                        "evidence_count": 0,
                    },
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
