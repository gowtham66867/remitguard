"""
RemitGuard Multi-Agent Pipeline — live demo runner.

Runs the full Orchestrator pipeline against one or more real EOB samples,
printing the full agent trace to stdout so every iteration loop,
self-validation pass, escalation decision, and human-review ticket is visible.

Usage:
    python run_pipeline.py                          # uses built-in samples
    python run_pipeline.py path/to/eob.pdf          # single file
    python run_pipeline.py *.pdf --ledger l.csv     # batch with ledger

Then to action the human review queue:
    python run_pipeline.py --review                 # show pending tickets
    python run_pipeline.py --approve 1 "Verified"  # approve ticket #1
    python run_pipeline.py --dismiss 1 "False pos" # dismiss ticket #1
"""

import sys
import os
import argparse
import csv
import io

# Make sure we can import from the platform package
sys.path.insert(0, os.path.dirname(__file__))

from api.models.base import Base, engine, SessionLocal
from api.models import (Facility, RecoupmentResult, RecoupmentFlag,
                        SCA, ERAEnrollment, EFTEnrollment)

# Ensure all tables exist (including HumanReview added by escalation agent)
try:
    from api.models.human_review import HumanReview
    Base.metadata.create_all(bind=engine)
except ImportError:
    Base.metadata.create_all(bind=engine)


_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_CYAN   = "\033[96m"
_GREEN  = "\033[92m"
_YELLOW = "\033[93m"
_RED    = "\033[91m"
_PURPLE = "\033[95m"
_GREY   = "\033[90m"


def load_ledger(path: str) -> dict:
    ledger = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            claim = row.get("claim_number", "").strip()
            try:
                ledger[claim] = float(row.get("expected_amount", "0"))
            except ValueError:
                continue
    return ledger


def run_pipeline(pdf_paths: list, ledger_path: str = None):
    db = SessionLocal()
    try:
        from agents.orchestrator import Orchestrator
        orch = Orchestrator(db=db)

        ledger = load_ledger(ledger_path) if ledger_path else None

        files = []
        for path in pdf_paths:
            with open(path, "rb") as f:
                files.append((os.path.basename(path), f.read()))

        if len(files) == 1:
            result = orch.run(files[0][1], files[0][0], ledger=ledger)
            print_result(result)
        else:
            batch = orch.run_batch(files, ledger=ledger)
            print_batch_summary(batch["summary"])
    finally:
        db.close()


def print_result(result):
    print(f"\n{'═'*60}")
    colour = _GREEN if result.status == "APPROVED" else (_YELLOW if result.status == "ESCALATED" else _RED)
    print(f"{colour}{_BOLD}RESULT: {result.status}{_RESET}  —  {result.filename}")
    print(f"{'═'*60}")
    print(f"  Extraction confidence : {result.extraction_confidence:.2f}")
    print(f"  Billed                : ${result.billed_amount:,.2f}" if result.billed_amount else "  Billed: N/A")
    print(f"  Paid (EOB line item)  : ${result.paid_amount:,.2f}" if result.paid_amount else "  Paid: N/A")
    if result.flagged:
        print(f"  {_RED}{_BOLD}Net actually received : ${result.net_received:,.2f}{_RESET}")
        print(f"\n  {_RED}⚠  RECOUPMENT FLAGS:{_RESET}")
        for i, flag in enumerate(result.flags, 1):
            conf = flag.get("confidence", "?")
            conf_str = f"{conf:.2f}" if isinstance(conf, float) else str(conf)
            print(f"    [{i}] [{flag.get('payer_tag','?')}] conf={conf_str} | {flag.get('line','')[:80]}")
    else:
        print(f"  {_GREEN}✓ No recoupment detected{_RESET}")

    if result.escalation_tickets:
        print(f"\n  {_PURPLE}⚑  Escalation tickets created: {result.escalation_tickets}{_RESET}")
        print(f"  Run with --review to see pending human review queue")
    print(f"  Pipeline time: {result.elapsed_ms}ms")
    print(f"{'═'*60}\n")


def print_batch_summary(summary: dict):
    print(f"\n{'═'*60}")
    print(f"{_BOLD}BATCH SUMMARY{_RESET}")
    print(f"{'═'*60}")
    print(f"  Total files     : {summary['total_files']}")
    print(f"  Approved        : {_GREEN}{summary['approved']}{_RESET}")
    print(f"  Escalated       : {_YELLOW}{summary['escalated']}{_RESET}")
    print(f"  Rejected        : {_RED}{summary['rejected']}{_RESET}")
    print(f"  Flagged files   : {summary['flagged_files']}")
    print(f"  {_RED}{_BOLD}Total amount at risk: ${summary['total_amount_at_risk']:,.2f}{_RESET}")
    print(f"  Tickets created : {summary['escalation_tickets']}")
    print(f"{'═'*60}\n")


def show_review_queue():
    db = SessionLocal()
    try:
        from api.models.human_review import HumanReview
        tickets = db.query(HumanReview).filter(HumanReview.status == "pending").all()
        if not tickets:
            print(f"{_GREEN}No pending review tickets.{_RESET}")
            return
        print(f"\n{_YELLOW}{_BOLD}PENDING HUMAN REVIEW ({len(tickets)} tickets){_RESET}")
        print(f"{'─'*60}")
        for t in tickets:
            print(f"  #{t.id}  [{t.ticket_type}]  {t.source_agent} → {t.reason[:70]}")
            print(f"       Created: {t.created_at}  Status: {t.status}")
        print(f"{'─'*60}")
        print(f"  Approve: python run_pipeline.py --approve <id> \"notes\"")
        print(f"  Dismiss: python run_pipeline.py --dismiss <id> \"notes\"\n")
    finally:
        db.close()


def approve_ticket(ticket_id: int, notes: str = ""):
    db = SessionLocal()
    try:
        from agents.escalation_agent import EscalationAgent
        ticket = EscalationAgent().approve(db, ticket_id, reviewer_notes=notes)
        db.commit()
        print(f"{_GREEN}✓ Ticket #{ticket_id} approved.{_RESET} Notes: {notes}")
    finally:
        db.close()


def dismiss_ticket(ticket_id: int, notes: str = ""):
    db = SessionLocal()
    try:
        from agents.escalation_agent import EscalationAgent
        ticket = EscalationAgent().dismiss(db, ticket_id, reviewer_notes=notes)
        db.commit()
        print(f"{_YELLOW}✗ Ticket #{ticket_id} dismissed.{_RESET} Notes: {notes}")
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="RemitGuard multi-agent pipeline runner")
    parser.add_argument("pdfs", nargs="*", help="PDF file path(s) to analyze")
    parser.add_argument("--ledger", help="Claims ledger CSV path")
    parser.add_argument("--review", action="store_true", help="Show pending review queue")
    parser.add_argument("--approve", type=int, metavar="ID", help="Approve ticket by ID")
    parser.add_argument("--dismiss", type=int, metavar="ID", help="Dismiss ticket by ID")
    parser.add_argument("--notes", default="", help="Reviewer notes for approve/dismiss")
    args = parser.parse_args()

    if args.review:
        show_review_queue()
        return

    if args.approve:
        approve_ticket(args.approve, args.notes)
        return

    if args.dismiss:
        dismiss_ticket(args.dismiss, args.notes)
        return

    # Default: run against built-in samples if no files given
    if not args.pdfs:
        samples_dir = os.path.join(os.path.dirname(__file__), "..", "samples")
        sample_files = [
            os.path.join(samples_dir, "anthem_haddad_sample.pdf"),
            os.path.join(samples_dir, "cigna_unknown_phrasing_sample.pdf"),
        ]
        args.pdfs = [f for f in sample_files if os.path.exists(f)]
        if not args.pdfs:
            print("No PDF files found. Pass a path or add samples to ../samples/")
            sys.exit(1)
        args.ledger = args.ledger or os.path.join(samples_dir, "sample_ledger.csv")

    run_pipeline(args.pdfs, ledger_path=args.ledger)


if __name__ == "__main__":
    main()
