"""
ComparisonService — run one EOB down both detection paths and report the delta.

WHY THIS EXISTS
---------------
The platform's claim is specific: a regex library only catches phrasings
somebody already wrote down, and Moss semantic recall closes that gap. Stating
that in a README is an assertion. This module turns it into something a
reviewer can watch happen on one document:

    same EOB  →  regex-only verdict   (POST $9,480, zero flags)
              →  regex + Moss verdict (HOLD, $3,240 at risk, evidence attached)

Both passes share a single PDF extraction, so the only variable between them is
the detection layer. The difference in `net_received` is the money the regex
path would have let through.

THE RETRIEVAL TRACE
-------------------
`match()` is production's decision path and returns None for anything it does
not flag — which makes a rejection invisible. For the comparison view every
regex-missed money line goes through `probe()` instead, which returns the raw
top hit without applying the label/threshold filter. This module then applies
*the same rule match() applies*:

    flag  ⟺  top hit is labelled "recoupment"  AND  score >= threshold

so the verdict is identical to production while the rejected lines stay
visible. That matters more than the flags do. An EOB's benign adjustment lines
(contractual adjustment, patient responsibility) are queried and turned down,
and showing that is the only honest way to present recall — a layer that
flagged every money line would score 100% recall and be useless.

One query per line, same as production. No double-querying.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

from .sample_files import get_case, get_demo_pdf_bytes
from .recoupment_service import (
    CLAIM_NUM_RE,
    DOS_RE,
    MONEY_RE,
    _EOBResult,
    _Flag,
    _extract_text_from_bytes,
    _find_paid_and_billed,
    _find_recoupment_flags,
    _parse_money,
)

logger = logging.getLogger(__name__)

def _percentile(samples: List[float], p: float) -> Optional[float]:
    if not samples:
        return None
    ordered = sorted(samples)
    idx = min(len(ordered) - 1, int(round(p * (len(ordered) - 1))))
    return round(ordered[idx], 3)


def _verdict(result: _EOBResult) -> Dict[str, Any]:
    """Collapse an _EOBResult into the numbers a coordinator acts on."""
    case = result.recovery_case
    return {
        "flagged": bool(result.flags),
        "flag_count": len(result.flags),
        "recovery_at_risk": result.recoupment_amount,
        "net_received": result.net_received,
        "decision": case["decision"],
        "recommended_action": case["recommended_action"],
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
            for f in result.flags
        ],
    }


def _semantic_pass(
    text: str,
    regex_flags: List[_Flag],
    matcher,
) -> Dict[str, Any]:
    """Query every regex-missed money line and record what came back.

    Returns the flags Moss added plus a full trace — including the lines it
    was asked about and declined to flag, which is where the precision story
    lives.
    """
    already_flagged = {f.line.strip() for f in regex_flags}
    threshold = matcher.threshold

    trace: List[Dict[str, Any]] = []
    new_flags: List[_Flag] = []
    query_ms: List[float] = []
    engine_ms: List[float] = []
    lines_scanned = 0
    lines_queried = 0

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        lines_scanned += 1
        if line in already_flagged:
            continue

        amounts = [_parse_money(a) for a in MONEY_RE.findall(raw)]
        # Same money gate production uses: a clawback a practice can act on
        # always states an amount, and the gate keeps query volume bounded.
        if not amounts:
            continue
        if not matcher.is_queryable(raw):
            continue

        hit = matcher.probe(raw)
        lines_queried += 1
        if hit is None:
            trace.append({
                "line": line,
                "amount": amounts[-1],
                "decision": "PASS",
                "reason": "no retrieval result",
            })
            continue

        query_ms.append(hit["query_ms"])
        if hit.get("engine_ms") is not None:
            engine_ms.append(hit["engine_ms"])

        # The production rule, applied verbatim (see SemanticMatcher.match).
        is_flag = hit["label"] == "recoupment" and hit["score"] >= threshold
        trace.append({
            "line": line,
            "amount": amounts[-1],
            "decision": "FLAG" if is_flag else "PASS",
            "top_label": hit["label"],
            "score": round(hit["score"], 4),
            "matched_text": hit["text"],
            "matched_doc_id": hit["doc_id"],
            "payer_tag": hit["payer"],
            "learned": hit["learned"],
            "query_ms": round(hit["query_ms"], 3),
            "engine_ms": round(hit["engine_ms"], 3) if hit.get("engine_ms") is not None else None,
            "reason": (
                None if is_flag
                else f"nearest neighbour is labelled '{hit['label']}'"
                if hit["label"] != "recoupment"
                else f"score {hit['score']:.3f} below threshold {threshold:.2f}"
            ),
        })

        if is_flag:
            new_flags.append(_Flag(
                line=line,
                matched_phrase=hit["text"],
                payer_tag=hit["payer"],
                amounts_found=amounts,
                source="semantic",
                semantic_score=hit["score"],
                semantic_doc_id=hit["doc_id"],
                semantic_learned=hit["learned"],
            ))

    p50 = _percentile(query_ms, 0.50)
    engine_p50 = _percentile(engine_ms, 0.50)
    return {
        "flags": new_flags,
        "retrieval": {
            "lines_scanned": lines_scanned,
            "lines_queried": lines_queried,
            "flagged": sum(1 for t in trace if t["decision"] == "FLAG"),
            "declined": sum(1 for t in trace if t["decision"] == "PASS"),
            "threshold": threshold,
            "alpha": matcher.alpha,
            "top_k": matcher.top_k,
            "corpus_size": len(getattr(matcher, "_corpus", []) or []),
            "query_ms_p50": p50,
            "query_ms_p95": _percentile(query_ms, 0.95),
            "query_ms_max": round(max(query_ms), 3) if query_ms else None,
            "engine_ms_p50": engine_p50,
            # Wall-clock minus Moss's own reported time: this integration's
            # thread hop. Reported separately so the retrieval number is not
            # quietly inflated by our plumbing, nor flattered by hiding it.
            "bridge_overhead_ms_p50": (
                round(p50 - engine_p50, 3)
                if p50 is not None and engine_p50 is not None else None
            ),
            "under_10ms": bool(p50 is not None and p50 < 10.0),
            "trace": trace,
        },
    }


def run_comparison(pdf_bytes: bytes, filename: str, compiled: Dict[str, Any]) -> Dict[str, Any]:
    """Analyse one EOB twice — regex-only, then regex + Moss — and diff them.

    The PDF is extracted once and both passes read the same text, so the only
    thing that differs between the two verdicts is the detection layer.
    """
    try:
        text = _extract_text_from_bytes(pdf_bytes)
    except Exception as exc:
        logger.exception("PDF extraction failed for %s", filename)
        return {"error": f"Could not read PDF: {exc}", "filename": filename}

    billed, paid = _find_paid_and_billed(text)
    claim_numbers = CLAIM_NUM_RE.findall(text)
    dates = DOS_RE.findall(text)

    def _result(flags: List[_Flag]) -> _EOBResult:
        return _EOBResult(
            source_file=filename,
            full_text=text,
            billed_amount=billed,
            paid_amount=paid,
            claim_numbers=claim_numbers,
            dates_of_service=dates,
            flags=flags,
        )

    # ── pass 1: regex only ────────────────────────────────────────────────
    t0 = time.perf_counter()
    regex_flags = _find_recoupment_flags(text, compiled, matcher=None)
    regex_ms = (time.perf_counter() - t0) * 1000.0
    baseline = _verdict(_result(regex_flags))
    baseline["mode"] = "regex_only"
    baseline["elapsed_ms"] = round(regex_ms, 3)

    # ── pass 2: regex + Moss ──────────────────────────────────────────────
    matcher = None
    try:
        from agents.semantic_matcher import get_matcher
        matcher = get_matcher()
    except Exception as exc:  # pragma: no cover - optional integration
        logger.info("Semantic recall unavailable: %s", exc)

    moss_state = {
        "enabled": bool(matcher and matcher.enabled),
        "ready": bool(matcher and matcher.ready),
        "disabled_reason": matcher.disabled_reason if matcher else "semantic layer not importable",
        "index_name": getattr(matcher, "index_name", None),
    }

    if not (matcher and matcher.ready):
        # Honest degradation. No synthetic semantic column is invented — the
        # UI renders the reason and the one-line fix instead.
        return {
            "filename": filename,
            "claim_number": ", ".join(claim_numbers) or None,
            "date_of_service": ", ".join(dates) or None,
            "billed_amount": billed,
            "paid_amount": paid,
            "baseline": baseline,
            "semantic": {
                "mode": "unavailable",
                "available": False,
                "disabled_reason": moss_state["disabled_reason"],
            },
            "delta": {
                "available": False,
                "diverged": False,
                "cash_recovered": 0.0,
                "flags_gained": 0,
                "verdict_changed": False,
            },
            "moss": moss_state,
        }

    t1 = time.perf_counter()
    sem = _semantic_pass(text, regex_flags, matcher)
    semantic_ms = (time.perf_counter() - t1) * 1000.0

    combined = _verdict(_result(regex_flags + sem["flags"]))
    combined["mode"] = "regex_plus_moss"
    combined["available"] = True
    combined["elapsed_ms"] = round(regex_ms + semantic_ms, 3)
    combined["semantic_only_ms"] = round(semantic_ms, 3)

    missed = [
        {
            "line": f.line,
            "amount": max((abs(a) for a in f.amounts_found), default=0.0),
            "score": round(f.semantic_score or 0.0, 4),
            "matched_text": f.matched_phrase,
            "matched_doc_id": f.semantic_doc_id,
            "payer_tag": f.payer_tag,
            "learned": f.semantic_learned,
        }
        for f in sem["flags"]
    ]

    return {
        "filename": filename,
        "claim_number": ", ".join(claim_numbers) or None,
        "date_of_service": ", ".join(dates) or None,
        "billed_amount": billed,
        "paid_amount": paid,
        "baseline": baseline,
        "semantic": combined,
        "delta": {
            "available": True,
            # True when the two paths reached materially different verdicts —
            # the thing the split-screen exists to show.
            "diverged": (
                combined["decision"] != baseline["decision"]
                or combined["flag_count"] != baseline["flag_count"]
            ),
            # The money the regex path would have posted as clean.
            "cash_recovered": round(
                (combined["recovery_at_risk"] or 0.0) - (baseline["recovery_at_risk"] or 0.0), 2
            ),
            "flags_gained": combined["flag_count"] - baseline["flag_count"],
            "verdict_changed": combined["decision"] != baseline["decision"],
            "diverged": (combined["decision"] != baseline["decision"]) or bool(missed),
            "missed_by_regex": missed,
            "latency_cost_ms": round(semantic_ms, 3),
        },
        "retrieval": sem["retrieval"],
        "moss": moss_state,
    }


def run_demo_case(case_id: Optional[str], compiled: Dict[str, Any]) -> Dict[str, Any]:
    """Run one catalog case and attach its metadata to the measured result.

    `expected` travels with the response next to what was actually measured,
    so a reviewer can see the catalog agreeing with the document rather than
    having to trust it. `tests/test_demo_cases.py` enforces the same thing at
    build time.
    """
    case = get_case(case_id)
    filename, pdf_bytes = get_demo_pdf_bytes(case["id"])
    if pdf_bytes is None:
        return {"error": f"Demo EOB '{filename}' is not bundled in this deployment."}

    out = run_comparison(pdf_bytes, filename, compiled)
    out["case"] = {k: v for k, v in case.items() if k != "expected"}
    expected = case["expected"]
    out["expectation_check"] = {
        "expected": expected,
        "measured": {
            "paid_amount": out.get("paid_amount"),
            "baseline_decision": out["baseline"]["decision"],
            "baseline_flag_count": out["baseline"]["flag_count"],
            "cash_at_risk": (out.get("semantic") or {}).get("recovery_at_risk"),
        },
    }
    out["expectation_check"]["matches"] = all(
        out["expectation_check"]["measured"].get(k) == v
        for k, v in expected.items()
        # cash_at_risk legitimately changes the moment Moss is connected —
        # that is the point of the gap case — so it is reported, not asserted.
        if k != "cash_at_risk"
    )
    return out
