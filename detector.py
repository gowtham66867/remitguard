"""
Recoupment/offset detector for behavioral-health EOBs.

Parses a PDF EOB, extracts the claim amount paid vs. billed, and flags
hidden recoupment/offset language (e.g. payers netting a current payment
against an old, unrelated debt) that would otherwise only surface by a
human manually reading the EOB footer line-by-line.

Also supports reconciling against a claims ledger (CSV of claim_number,
expected_amount) to catch recoupments even when the payer's phrasing
isn't yet in our pattern library — the ledger mismatch itself is the
tell.

Usage:
    python detector.py path/to/eob.pdf
    python detector.py path/to/eob.pdf --ledger ledger.csv
    python detector.py --batch path/to/folder/
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pdfplumber

PATTERNS_PATH = os.path.join(os.path.dirname(__file__), "patterns.json")

MONEY_RE = re.compile(r"\$?\(?-?\s?[\d,]+\.\d{2}\)?")
CLAIM_NUM_RE = re.compile(r"(?i:claim)\s*#?\s*[:\-]?\s*([A-Z0-9]{6,}(?=[\s,.\n]|$))")
DOS_RE = re.compile(r"\bDOS\b\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{2,4})", re.IGNORECASE)


def load_patterns(path: str = PATTERNS_PATH) -> Dict[str, List[str]]:
    with open(path) as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def compile_patterns(patterns: Dict[str, List[str]]) -> Dict[str, re.Pattern]:
    """One compiled regex per payer tag (plus 'generic'), so a flag can be
    attributed to which payer-pattern caught it."""
    return {
        tag: re.compile("|".join(phrases), re.IGNORECASE)
        for tag, phrases in patterns.items()
    }


@dataclass
class RecoupmentFlag:
    line: str
    matched_phrase: str
    payer_tag: str
    amounts_found: list = field(default_factory=list)
    source: str = "pattern"  # "pattern" or "ledger_mismatch"


@dataclass
class EOBResult:
    source_file: str
    full_text: str
    billed_amount: Optional[float] = None
    paid_amount: Optional[float] = None
    claim_numbers: list = field(default_factory=list)
    dates_of_service: list = field(default_factory=list)
    flags: list = field(default_factory=list)
    extraction_warning: Optional[str] = None

    @property
    def net_received(self) -> Optional[float]:
        if self.paid_amount is None:
            return None
        if not self.flags:
            return self.paid_amount
        clawed_back = sum(
            amt for f in self.flags for amt in f.amounts_found if f.source == "pattern"
        )
        return round(self.paid_amount - clawed_back, 2)


def _parse_money(token: str) -> float:
    neg = token.strip().startswith("(") or token.strip().startswith("-")
    cleaned = re.sub(r"[^\d.]", "", token)
    val = float(cleaned) if cleaned else 0.0
    return -val if neg else val


def extract_text(pdf_path: str) -> str:
    chunks = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            chunks.append(text)
    return "\n".join(chunks)


def find_paid_and_billed(text: str) -> Tuple[Optional[float], Optional[float]]:
    billed = None
    paid = None
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


def find_recoupment_flags(text: str, compiled: Dict[str, re.Pattern]) -> List[RecoupmentFlag]:
    flags = []
    for line in text.splitlines():
        for tag, regex in compiled.items():
            match = regex.search(line)
            if match:
                amounts = [_parse_money(a) for a in MONEY_RE.findall(line)]
                flags.append(
                    RecoupmentFlag(
                        line=line.strip(),
                        matched_phrase=match.group(0),
                        payer_tag=tag,
                        amounts_found=amounts,
                        source="pattern",
                    )
                )
                break  # one flag per line is enough
    return flags


def load_ledger(path: str) -> Dict[str, float]:
    """CSV with columns: claim_number, expected_amount"""
    ledger = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            claim = row.get("claim_number", "").strip()
            try:
                ledger[claim] = float(row.get("expected_amount", "0"))
            except ValueError:
                continue
    return ledger


def reconcile_with_ledger(result: EOBResult, ledger: Dict[str, float]) -> None:
    """Flags a mismatch between what the ledger expected and what was
    actually paid, even if no recognized recoupment phrase was found —
    this is the catch-all for payer wording we haven't seen yet."""
    if result.paid_amount is None:
        return
    for claim in result.claim_numbers:
        expected = ledger.get(claim)
        if expected is None:
            continue
        diff = round(expected - result.paid_amount, 2)
        if abs(diff) > 0.01:
            result.flags.append(
                RecoupmentFlag(
                    line=f"Ledger expected ${expected:.2f} for claim {claim}, EOB shows ${result.paid_amount:.2f} paid",
                    matched_phrase="ledger_mismatch",
                    payer_tag="ledger",
                    amounts_found=[diff],
                    source="ledger_mismatch",
                )
            )


def analyze(pdf_path: str, ledger: Optional[Dict[str, float]] = None) -> EOBResult:
    patterns = load_patterns()
    compiled = compile_patterns(patterns)

    text = extract_text(pdf_path)
    warning = None
    if not text.strip():
        warning = (
            "No extractable text found — this PDF is likely scanned. "
            "OCR is not yet configured (requires tesseract); flag for manual review."
        )

    billed, paid = find_paid_and_billed(text)
    flags = find_recoupment_flags(text, compiled)
    claim_numbers = CLAIM_NUM_RE.findall(text)
    dates = DOS_RE.findall(text)

    result = EOBResult(
        source_file=pdf_path,
        full_text=text,
        billed_amount=billed,
        paid_amount=paid,
        claim_numbers=claim_numbers,
        dates_of_service=dates,
        flags=flags,
        extraction_warning=warning,
    )

    if ledger:
        reconcile_with_ledger(result, ledger)

    return result


def batch_analyze(folder: str, ledger: Optional[Dict[str, float]] = None) -> List[EOBResult]:
    results = []
    for fname in sorted(os.listdir(folder)):
        if fname.lower().endswith(".pdf"):
            path = os.path.join(folder, fname)
            try:
                results.append(analyze(path, ledger=ledger))
            except Exception as e:
                r = EOBResult(source_file=path, full_text="", extraction_warning=f"Failed to parse: {e}")
                results.append(r)
    return results


def render_report(result: EOBResult) -> str:
    lines = [f"EOB Analysis: {result.source_file}", "=" * 60]
    if result.extraction_warning:
        lines.append(f"⚠️  {result.extraction_warning}")
    if result.claim_numbers:
        lines.append(f"Claim #(s): {', '.join(result.claim_numbers)}")
    if result.dates_of_service:
        lines.append(f"Date(s) of service: {', '.join(result.dates_of_service)}")
    lines.append(f"Billed amount:  {result.billed_amount}")
    lines.append(f"Paid (line item): {result.paid_amount}")

    if result.flags:
        lines.append("")
        lines.append("⚠️  RECOUPMENT/OFFSET DETECTED")
        for f in result.flags:
            lines.append(f"  - [{f.payer_tag}] matched: '{f.matched_phrase}' (source: {f.source})")
            lines.append(f"    line: {f.line}")
            if f.amounts_found:
                lines.append(f"    amounts on line: {f.amounts_found}")
        lines.append("")
        lines.append(f"Net cash actually received (est.): {result.net_received}")
        if any(f.source == "pattern" for f in result.flags):
            lines.append(
                "NOTE: the EOB shows a payment, but a recoupment/offset is "
                "clawing back funds against a separate balance. Verify before "
                "booking this as revenue."
            )
        else:
            lines.append(
                "NOTE: the amount paid does not match what the ledger expected "
                "for this claim. Could be a recoupment, an underpayment, or a "
                "pricing dispute — verify before booking this as revenue."
            )
    else:
        lines.append("")
        lines.append("No recoupment/offset language detected.")

    return "\n".join(lines)


def render_batch_summary(results: List[EOBResult]) -> str:
    total_paid = sum(r.paid_amount for r in results if r.paid_amount)
    total_flagged = sum(
        amt for r in results for f in r.flags for amt in f.amounts_found
    )
    flagged_files = [r for r in results if r.flags]

    lines = [
        f"Batch summary: {len(results)} EOBs processed",
        "=" * 60,
        f"Total line-item paid across batch: ${total_paid:,.2f}",
        f"Total hidden recoupment/offset detected: ${total_flagged:,.2f}",
        f"Files with at least one flag: {len(flagged_files)} / {len(results)}",
        "",
    ]
    for r in flagged_files:
        lines.append(f"  - {os.path.basename(r.source_file)}: net received ${r.net_received:,.2f} (vs. ${r.paid_amount:,.2f} shown paid)")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf_path", nargs="?", help="Path to EOB PDF")
    parser.add_argument("--batch", help="Path to folder of EOB PDFs to process together")
    parser.add_argument("--ledger", help="Path to claims ledger CSV (claim_number, expected_amount)")
    parser.add_argument("--raw", action="store_true", help="Also print extracted raw text")
    args = parser.parse_args()

    ledger = load_ledger(args.ledger) if args.ledger else None

    if args.batch:
        results = batch_analyze(args.batch, ledger=ledger)
        for r in results:
            print(render_report(r))
            print()
        print(render_batch_summary(results))
        return

    if not args.pdf_path:
        parser.error("provide a pdf_path or --batch folder")

    result = analyze(args.pdf_path, ledger=ledger)
    print(render_report(result))
    if args.raw:
        print("\n--- RAW TEXT ---\n")
        print(result.full_text)


if __name__ == "__main__":
    sys.exit(main())
