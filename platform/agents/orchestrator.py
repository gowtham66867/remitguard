"""
RemitGuard Multi-Agent Orchestrator

Coordinates the full EOB analysis pipeline:

  EOBAgent (iterative extraction)
      ↓
  RecoupmentAgent (detection + self-validation loop)
      ↓
  ValidatorAgent (cross-validation)
      ↓
  EscalationAgent (human-in-the-loop gate)
      ↓
  Persist / Return result

Each agent emits structured logs. The orchestrator captures and re-emits
them with a pipeline prefix so the full trace is visible in one stream.

Usage:
    result = Orchestrator(db).run(pdf_bytes, filename, ledger=None)
    # or run multiple files:
    results = Orchestrator(db).run_batch(files, ledger=None)
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

# ── colour codes for terminal output ──────────────────────────────────────────
_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_CYAN   = "\033[96m"
_GREEN  = "\033[92m"
_YELLOW = "\033[93m"
_RED    = "\033[91m"
_GREY   = "\033[90m"
_PURPLE = "\033[95m"


def _tag(agent: str, colour: str = _CYAN) -> str:
    return f"{colour}{_BOLD}[{agent}]{_RESET}"


def _log(agent: str, msg: str, level: str = "info") -> None:
    colour = {
        "info":    _CYAN,
        "success": _GREEN,
        "warn":    _YELLOW,
        "error":   _RED,
        "escalate":_PURPLE,
    }.get(level, _CYAN)
    print(f"{_tag(agent, colour)} {msg}")


@dataclass
class PipelineResult:
    filename: str
    status: str          # APPROVED | ESCALATED | REJECTED | ERROR
    flagged: bool
    flags: List[Dict]
    billed_amount: Optional[float]
    paid_amount: Optional[float]
    net_received: Optional[float]
    extraction_confidence: float
    validation_recommendation: str
    escalation_tickets: List[int]   # ticket IDs created during this run
    pipeline_log: List[Dict]        # full structured trace
    elapsed_ms: int


class Orchestrator:
    """
    Coordinates EOBAgent → RecoupmentAgent → ValidatorAgent → EscalationAgent.
    Requires a SQLAlchemy session for persistence.
    """

    def __init__(self, db=None):
        self.db = db
        self._pipeline_log: List[Dict] = []

    # ── public interface ───────────────────────────────────────────────────────

    def run(
        self,
        pdf_bytes: bytes,
        filename: str,
        ledger: Optional[Dict] = None,
        facility_id: Optional[int] = None,
        emit=None,  # Optional[Callable[[dict], None]]
    ) -> PipelineResult:
        start = time.time()
        self._pipeline_log = []
        tickets: List[int] = []

        def _emit(event: dict) -> None:
            if emit is not None:
                try:
                    emit(event)
                except Exception:
                    pass

        print()
        print(f"{_GREY}{'─'*60}{_RESET}")
        _log("Orchestrator", f"Starting pipeline for {_BOLD}{filename}{_RESET}")
        print(f"{_GREY}{'─'*60}{_RESET}")

        # ── STAGE 1: EOB Ingestion (iterative extraction) ─────────────────────
        _log("Orchestrator", "Stage 1 → EOBAgent (iterative extraction)")
        _emit({"type": "phase_start", "phase": "extraction", "file": filename, "agent": "EOBAgent"})
        try:
            from agents.eob_agent import EOBAgent
            extraction = EOBAgent().run(pdf_bytes, filename)
            self._record("EOBAgent", "extraction_complete", {
                "confidence": extraction.confidence,
                "strategy":   extraction.strategy_used,
                "iterations": extraction.iterations,
                "needs_escalation": extraction.needs_escalation,
            })
            _emit({"type": "phase_complete", "phase": "extraction", "file": filename,
                   "agent": "EOBAgent", "confidence": extraction.confidence,
                   "strategy": extraction.strategy_used, "iterations": extraction.iterations})
        except Exception as e:
            _log("Orchestrator", f"EOBAgent failed: {e}", "error")
            return self._error_result(filename, str(e), start)

        # ── ESCALATE: unreadable PDF ──────────────────────────────────────────
        if extraction.needs_escalation:
            _log("Orchestrator", "Extraction below threshold → escalating", "escalate")
            tid = self._escalate(
                reason=extraction.escalation_reason or "Low confidence extraction",
                context={"filename": filename, "confidence": extraction.confidence,
                         "log": extraction.extraction_log},
                escalation_type="low_confidence_extraction",
                source_agent="EOBAgent",
            )
            tickets.append(tid)
            early_result = PipelineResult(
                filename=filename, status="ESCALATED", flagged=False, flags=[],
                billed_amount=None, paid_amount=None, net_received=None,
                extraction_confidence=extraction.confidence,
                validation_recommendation="ESCALATE",
                escalation_tickets=tickets,
                pipeline_log=self._pipeline_log,
                elapsed_ms=int((time.time() - start) * 1000),
            )
            _emit({"type": "file_complete", "file": filename, "status": early_result.status,
                   "flagged": early_result.flagged, "net_received": early_result.net_received,
                   "paid_amount": early_result.paid_amount,
                   "escalation_tickets": early_result.escalation_tickets})
            return early_result

        # ── STAGE 2: Recoupment Detection (self-validation loop) ──────────────
        _log("Orchestrator", "Stage 2 → RecoupmentAgent (detect + self-validate)")
        _emit({"type": "phase_start", "phase": "detection", "file": filename, "agent": "RecoupmentAgent"})
        try:
            from agents.recoupment_agent import RecoupmentAgent
            detection = RecoupmentAgent().run(extraction.text, filename, ledger=ledger)
            self._record("RecoupmentAgent", "detection_complete", {
                "flags_found": len(detection.flags),
                "needs_human_review": detection.needs_human_review,
                "paid_amount": detection.paid_amount,
                "net_received": detection.net_received,
            })
            _emit({"type": "phase_complete", "phase": "detection", "file": filename,
                   "agent": "RecoupmentAgent", "flags_found": len(detection.flags),
                   "needs_human_review": detection.needs_human_review})
        except Exception as e:
            _log("Orchestrator", f"RecoupmentAgent failed: {e}", "error")
            return self._error_result(filename, str(e), start)

        # ── STAGE 3: Cross-Validation ─────────────────────────────────────────
        _log("Orchestrator", "Stage 3 → ValidatorAgent (cross-validation)")
        _emit({"type": "phase_start", "phase": "validation", "file": filename, "agent": "ValidatorAgent"})
        try:
            from agents.validator_agent import ValidatorAgent
            validation = ValidatorAgent().validate(detection, extraction)
            self._record("ValidatorAgent", "validation_complete", {
                "passed": validation.passed_checks,
                "failed": validation.failed_checks,
                "warnings": validation.warnings,
                "recommendation": validation.recommendation,
            })
            _emit({"type": "phase_complete", "phase": "validation", "file": filename,
                   "agent": "ValidatorAgent", "recommendation": validation.recommendation,
                   "passed_checks": validation.passed_checks, "warnings": validation.warnings})
        except Exception as e:
            _log("Orchestrator", f"ValidatorAgent failed: {e}", "error")
            return self._error_result(filename, str(e), start)

        # ── STAGE 4: Escalation Gate ──────────────────────────────────────────
        _log("Orchestrator", "Stage 4 → EscalationAgent (human-in-the-loop gate)")
        _emit({"type": "phase_start", "phase": "escalation_gate", "file": filename, "agent": "EscalationAgent"})

        final_flags = validation.adjusted_flags or detection.flags

        # Rule A: validator says escalate
        if validation.recommendation == "ESCALATE":
            _log("Orchestrator", "Validator recommends ESCALATE", "escalate")
            tid = self._escalate(
                reason=f"Validator flagged issues: {'; '.join(validation.failed_checks + validation.warnings)}",
                context={"filename": filename, "flags": final_flags,
                         "validation_log": validation.validation_log},
                escalation_type="ambiguous_flag",
                source_agent="ValidatorAgent",
            )
            tickets.append(tid)

        # Rule B: agent itself requested human review (low-confidence flag)
        if detection.needs_human_review:
            _log("Orchestrator", "RecoupmentAgent requested human review", "escalate")
            tid = self._escalate(
                reason=detection.review_reason or "Low confidence flag",
                context={"filename": filename, "flags": final_flags,
                         "detection_log": detection.detection_log},
                escalation_type="ambiguous_flag",
                source_agent="RecoupmentAgent",
            )
            tickets.append(tid)

        # Rule C: large amount auto-escalate (any flag > $10k)
        for flag in final_flags:
            for amt in flag.get("amounts_found", []):
                if abs(amt) > 10_000:
                    _log("Orchestrator",
                         f"Large amount ${abs(amt):,.2f} → auto-escalate", "escalate")
                    tid = self._escalate(
                        reason=f"Flag amount ${abs(amt):,.2f} exceeds $10K auto-escalation threshold",
                        context={"filename": filename, "flag": flag, "amount": amt},
                        escalation_type="large_amount",
                        source_agent="Orchestrator",
                    )
                    tickets.append(tid)
                    break

        # Rule D: new payer pattern (generic tag + amount > $5k) → pattern library
        for flag in final_flags:
            if flag.get("payer_tag") == "generic":
                for amt in flag.get("amounts_found", []):
                    if abs(amt) > 5_000:
                        _log("Orchestrator",
                             "New generic payer pattern with large amount → flag for library update",
                             "warn")
                        tid = self._escalate(
                            reason="Unrecognised payer pattern matched generically on large amount — add to patterns.json",
                            context={"filename": filename, "flag": flag},
                            escalation_type="new_payer_pattern",
                            source_agent="Orchestrator",
                        )
                        tickets.append(tid)
                        break

        # ── Determine final status ─────────────────────────────────────────────
        if validation.recommendation == "REJECT":
            status = "REJECTED"
        elif tickets:
            status = "ESCALATED"
        else:
            status = "APPROVED"

        # ── Persist to DB ──────────────────────────────────────────────────────
        if self.db and status == "APPROVED":
            try:
                from api.services.recoupment_service import RecoupmentService
                result_dict = {
                    "filename": filename,
                    "claim_numbers": [],
                    "dates_of_service": [],
                    "billed_amount": detection.billed_amount,
                    "paid_amount": detection.paid_amount,
                    "net_received": detection.net_received,
                    "flagged": bool(final_flags),
                    "flags": final_flags,
                    "extraction_warning": None,
                }
                svc = RecoupmentService()
                svc.save_result(self.db, facility_id, result_dict)
                self.db.commit()
                _log("Orchestrator", "Result persisted to DB", "success")
            except Exception as e:
                _log("Orchestrator", f"DB persist failed: {e}", "warn")

        elapsed = int((time.time() - start) * 1000)

        print(f"{_GREY}{'─'*60}{_RESET}")
        colour = _GREEN if status == "APPROVED" else (_YELLOW if status == "ESCALATED" else _RED)
        print(f"{_tag('Orchestrator', colour)} Pipeline complete: {_BOLD}{status}{_RESET} "
              f"| flags={len(final_flags)} | tickets={len(tickets)} | {elapsed}ms")
        print(f"{_GREY}{'─'*60}{_RESET}\n")

        result = PipelineResult(
            filename=filename,
            status=status,
            flagged=bool(final_flags),
            flags=final_flags,
            billed_amount=detection.billed_amount,
            paid_amount=detection.paid_amount,
            net_received=detection.net_received,
            extraction_confidence=extraction.confidence,
            validation_recommendation=validation.recommendation,
            escalation_tickets=list(set(tickets)),
            pipeline_log=self._pipeline_log,
            elapsed_ms=elapsed,
        )
        _emit({"type": "file_complete", "file": filename, "status": result.status,
               "flagged": result.flagged, "net_received": result.net_received,
               "paid_amount": result.paid_amount, "escalation_tickets": result.escalation_tickets})
        return result

    def run_batch(
        self,
        files: List[tuple],  # list of (filename, bytes)
        ledger: Optional[Dict] = None,
        facility_id: Optional[int] = None,
        emit=None,  # Optional[Callable[[dict], None]]
    ) -> Dict:
        _log("Orchestrator", f"Batch pipeline: {len(files)} file(s)", "info")
        results = []
        for filename, pdf_bytes in files:
            r = self.run(pdf_bytes, filename, ledger=ledger, facility_id=facility_id, emit=emit)
            results.append(r)

        total_flagged  = sum(1 for r in results if r.flagged)
        total_escalated = sum(1 for r in results if r.status == "ESCALATED")
        total_amount   = sum(
            abs(a) for r in results
            for f in r.flags
            for a in f.get("amounts_found", [])
        )
        total_tickets  = sum(len(r.escalation_tickets) for r in results)

        summary = {
            "total_files":     len(results),
            "approved":        sum(1 for r in results if r.status == "APPROVED"),
            "escalated":       total_escalated,
            "rejected":        sum(1 for r in results if r.status == "REJECTED"),
            "flagged_files":   total_flagged,
            "total_amount_at_risk": total_amount,
            "escalation_tickets": total_tickets,
        }

        _log("Orchestrator",
             f"Batch done — approved={summary['approved']} escalated={summary['escalated']} "
             f"amount_at_risk=${total_amount:,.2f}",
             "success")

        return {"results": [_safe_dict(r) for r in results], "summary": summary}

    # ── helpers ────────────────────────────────────────────────────────────────

    def _escalate(self, reason: str, context: Dict, escalation_type: str,
                  source_agent: str) -> int:
        try:
            from agents.escalation_agent import EscalationAgent
            ticket = EscalationAgent().escalate(
                db=self.db,
                reason=reason,
                context_dict=context,
                escalation_type=escalation_type,
                source_agent=source_agent,
            )
            self._record("EscalationAgent", "ticket_created", {
                "ticket_id": ticket.id if ticket else None,
                "type": escalation_type,
                "reason": reason,
            })
            return ticket.id if ticket else -1
        except Exception as e:
            _log("Orchestrator", f"EscalationAgent failed: {e}", "warn")
            return -1

    def _record(self, agent: str, event: str, data: Dict) -> None:
        self._pipeline_log.append({"agent": agent, "event": event, **data})

    def _error_result(self, filename: str, error: str, start: float) -> PipelineResult:
        return PipelineResult(
            filename=filename, status="ERROR", flagged=False, flags=[],
            billed_amount=None, paid_amount=None, net_received=None,
            extraction_confidence=0.0, validation_recommendation="REJECT",
            escalation_tickets=[], pipeline_log=self._pipeline_log,
            elapsed_ms=int((time.time() - start) * 1000),
        )


def _safe_dict(obj: Any) -> Any:
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _safe_dict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, list):
        return [_safe_dict(i) for i in obj]
    return obj
