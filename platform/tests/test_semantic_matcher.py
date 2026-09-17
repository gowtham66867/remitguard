"""
Integration tests for the Moss semantic recall layer.

WHAT THESE COVER
----------------
The wiring between RemitGuard and the Moss SDK: index build, query dispatch,
nearest-neighbour classification, flag construction, confidence scoring, the
approval learning loop, and graceful degradation.

The `_StubClient` replaces only the Moss *transport*. It is constructed with and
returns the genuine `moss` SDK types (`DocumentInfo`, `QueryOptions`,
`MutationOptions`), so a drift in those signatures fails these tests.

WHAT THESE DO NOT COVER
-----------------------
Retrieval quality. The stub scores by token overlap, not by Moss's embeddings.
These tests prove the integration is correct, not that semantic recall is good —
that is what `eval_semantic.py` measures against the live service.

Run with a Python >= 3.10 interpreter that has `moss` installed:
    python -m pytest tests/test_semantic_matcher.py -v
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

moss = pytest.importorskip("moss", reason="moss requires Python >= 3.10")
from moss import DocumentInfo, MutationOptions, QueryOptions  # noqa: E402

from agents.semantic_matcher import SemanticMatcher  # noqa: E402


from stub_moss import StubMossClient as _StubClient  # noqa: E402


@pytest.fixture
def matcher():
    m = SemanticMatcher(client=_StubClient(), threshold=0.05)
    assert m.warm(timeout=30)
    return m


# ── tests ─────────────────────────────────────────────────────────────────────


def test_warm_creates_then_loads_index(matcher):
    """First warm builds the index from the corpus, then loads it."""
    stub = matcher._client
    assert len(stub.created) == 1
    name, model_id, count = stub.created[0]
    assert model_id == "moss-minilm"      # read from the corpus file
    assert count == 70                    # 40 recoupment + 30 benign
    assert stub.loaded == [name]
    assert matcher.ready


def test_warm_is_idempotent(matcher):
    matcher.warm()
    matcher.warm()
    assert len(matcher._client.created) == 1


def test_recoupment_line_matches(matcher):
    hit = matcher.match("Amount recouped from this payment          940.55")
    assert hit is not None
    assert hit.label == "recoupment"
    assert hit.score >= matcher.threshold
    assert hit.query_ms > 0


def test_benign_nearest_neighbour_is_not_flagged(matcher):
    """
    The load-bearing case: a benign line whose top hit is a benign doc returns
    None even though the similarity score is high. Without the benign half of
    the corpus this line would be flagged.
    """
    hit = matcher.match("Contractual adjustment per provider agreement   1,240.00")
    assert hit is None


def test_flag_fields_are_well_formed(matcher):
    hit = matcher.match("Offset applied to prior outstanding balance    500.00")
    assert hit is not None
    fields = hit.to_flag_fields()
    assert fields["source"] == "semantic"
    assert fields["payer_tag"]
    assert 0.0 <= fields["semantic_score"] <= 1.0
    assert fields["semantic_doc_id"]
    assert fields["semantic_learned"] is False


def test_short_lines_are_not_queried(matcher):
    before = matcher.stats()["queries"]
    assert matcher.match("$12.00") is None
    assert matcher.match("") is None
    assert matcher.stats()["queries"] == before, "pre-filter should skip the query"


def test_learn_adds_confirmed_phrase_and_marks_it(matcher):
    novel = "Vendor chargeback netted against this disbursement per audit"
    assert matcher.learn(novel, payer_tag="anthem") is True
    assert matcher.stats()["learned_phrases"] == 1

    hit = matcher.match(novel)
    assert hit is not None
    assert hit.learned is True
    assert hit.payer_tag == "anthem"
    assert hit.to_flag_fields()["semantic_learned"] is True


def test_stats_reports_latency_percentiles(matcher):
    for _ in range(5):
        matcher.match("Amount recouped from this payment    940.55")
    stats = matcher.stats()
    assert stats["ready"] is True
    assert stats["queries"] >= 5
    assert stats["latency_ms_p50"] is not None
    assert stats["latency_ms_p95"] is not None
    assert stats["corpus_size"] == 70


def test_query_failure_degrades_to_none(matcher):
    async def boom(*a, **k):
        raise RuntimeError("moss service unreachable")

    matcher._client.query = boom
    assert matcher.match("Amount recouped from this payment    940.55") is None
    assert "unreachable" in matcher.stats()["last_error"]


def test_disabled_without_credentials(monkeypatch):
    for var in ("MOSS_PROJECT_ID", "MOSS_PROJECT_KEY"):
        monkeypatch.delenv(var, raising=False)
    m = SemanticMatcher()
    assert m.enabled is False
    assert m.ready is False
    assert m.match("Amount recouped from this payment  940.55") is None
    assert m.warm() is False
    assert "MOSS_PROJECT_ID" in m.disabled_reason


def test_force_disabled_env(monkeypatch):
    monkeypatch.setenv("MOSS_PROJECT_ID", "x")
    monkeypatch.setenv("MOSS_PROJECT_KEY", "y")
    monkeypatch.setenv("MOSS_DISABLED", "1")
    m = SemanticMatcher()
    assert m.enabled is False
    assert "MOSS_DISABLED" in m.disabled_reason


# ── agent-level integration ───────────────────────────────────────────────────


def test_agent_adds_semantic_flag_regex_misses(matcher):
    from agents.recoupment_agent import RecoupmentAgent

    line = "Payment reduced to satisfy an earlier excess disbursement   1,204.00"

    baseline = RecoupmentAgent(use_semantic=False).run(line, "t.pdf")
    assert baseline.flags == [], "regex should miss this rewording"

    result = RecoupmentAgent(matcher=matcher).run(line, "t.pdf")
    assert len(result.flags) == 1
    flag = result.flags[0]
    assert flag["source"] == "semantic"
    assert flag["amounts_found"] == [1204.00]
    assert flag["confidence"] > 0


def test_semantic_flag_reduces_net_received(matcher):
    """A semantically detected clawback must move net_received, not just log."""
    from agents.recoupment_agent import RecoupmentAgent

    text = (
        "Total amount paid to provider                 5,000.00\n"
        "Payment reduced to satisfy an earlier excess disbursement   1,204.00\n"
    )
    result = RecoupmentAgent(matcher=matcher).run(text, "t.pdf")
    assert result.paid_amount == 5000.00
    assert result.net_received == 3796.00


def test_regex_verdict_wins_over_semantic(matcher):
    """A line regex already claimed is never re-queried or double-flagged."""
    from agents.recoupment_agent import RecoupmentAgent

    line = "OUTSTANDING NEG BAL WITH DIFFER    18,020.11"
    result = RecoupmentAgent(matcher=matcher).run(line, "t.pdf")
    assert len(result.flags) == 1
    assert result.flags[0]["source"] == "pattern"


def test_learned_phrase_scores_higher_than_plain_semantic(matcher):
    """The +0.05 human-confirmed bonus must actually reach the flag."""
    from agents.recoupment_agent import _flag_confidence

    base = {"source": "semantic", "amounts_found": [900.0], "line": "x"}
    learned = dict(base, semantic_learned=True)
    assert _flag_confidence(learned, None, {}) > _flag_confidence(base, None, {})


def test_money_gate_suppresses_amountless_lines(matcher):
    """
    Lines without a dollar figure are never sent to Moss. A clawback the
    practice can act on always states an amount; prose and footers do not.
    This is both a precision guard and a query-volume reduction.
    """
    from agents.recoupment_agent import _detect_flags, _load_compiled_patterns

    compiled = _load_compiled_patterns()
    text = (
        "Settlement of an outstanding accounts receivable      3,240.00\n"
        "balance against this payment cycle.\n"
        "Questions: provider.services@meridianregional.example\n"
    )
    before = matcher.stats()["queries"]
    flags = _detect_flags(text, compiled, matcher=matcher)

    assert len(flags) == 1, "only the money-bearing line should flag"
    assert flags[0]["amounts_found"] == [3240.00]
    assert matcher.stats()["queries"] - before == 1, "amountless lines must not be queried"

    # Opting out restores querying of every regex-missed line.
    before = matcher.stats()["queries"]
    _detect_flags(text, compiled, matcher=matcher, semantic_requires_amount=False)
    assert matcher.stats()["queries"] - before == 3


# ── concurrency ───────────────────────────────────────────────────────────────


def test_concurrent_match_keeps_telemetry_exact(matcher):
    """
    `match()` is called from the orchestrator's worker threads. `counter += 1`
    is a read-modify-write, so without a lock these counters silently lose
    updates under load. Eight threads is enough to expose it reliably.
    """
    import threading

    line = "Amount recouped from this payment    940.55"
    per_thread, threads = 40, 8
    errors = []

    def worker():
        try:
            for _ in range(per_thread):
                matcher.match(line)
        except Exception as exc:                    # pragma: no cover
            errors.append(exc)

    workers = [threading.Thread(target=worker) for _ in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()

    assert not errors, f"concurrent match raised: {errors[:3]}"
    stats = matcher.stats()
    assert stats["queries"] == threads * per_thread, "lost counter updates under concurrency"
    assert stats["semantic_flags"] == threads * per_thread
    assert stats["latency_ms_p50"] is not None


def test_latency_samples_are_bounded(monkeypatch):
    """
    An unbounded latency list grows for the life of the process. A long-running
    Cloud Run instance would accumulate one float per query forever.
    """
    from agents import semantic_matcher as sm
    from stub_moss import StubMossClient

    monkeypatch.setattr(sm, "LATENCY_SAMPLE_CAP", 50)
    m = sm.SemanticMatcher(client=StubMossClient(), threshold=0.05)
    # the deque is sized at construction, so rebuild it under the patched cap
    m._latencies_ms = sm.deque(maxlen=50)
    assert m.warm(timeout=30)

    for _ in range(200):
        m.match("Amount recouped from this payment    940.55")

    assert len(m._latencies_ms) == 50, "latency samples must be a bounded ring buffer"
    assert m.stats()["queries"] == 200, "the counter itself must keep counting"


def test_reset_stats_clears_telemetry(matcher):
    matcher.match("Amount recouped from this payment    940.55")
    assert matcher.stats()["queries"] > 0
    matcher.reset_stats()
    s = matcher.stats()
    assert s["queries"] == 0 and s["semantic_flags"] == 0
    assert s["latency_ms_p50"] is None


# ── probe (eval surface) ──────────────────────────────────────────────────────


def test_probe_returns_raw_hit_below_threshold(matcher):
    """
    probe() must ignore the threshold and label filter — the offline sweep
    depends on seeing hits that match() would reject.
    """
    matcher.threshold = 0.99
    assert matcher.match("Amount recouped from this payment  940.55") is None

    raw = matcher.probe("Amount recouped from this payment  940.55")
    assert raw is not None
    assert raw["label"] in ("recoupment", "benign")
    assert 0.0 <= raw["score"] <= 1.0
    assert raw["query_ms"] > 0


def test_probe_respects_the_prefilter(matcher):
    before = matcher.stats()["queries"]
    assert matcher.probe("$12.00") is None
    assert matcher.stats()["queries"] == before


# ── invariant ─────────────────────────────────────────────────────────────────


def test_semantic_layer_never_removes_a_regex_detection(matcher):
    """
    Safety property: adding Moss may only ever ADD flags. If enabling it could
    drop a detection the regex library already made, the layer would be a
    liability rather than an improvement. Checked across the whole eval set.
    """
    import eval_data
    from agents.recoupment_agent import RecoupmentAgent

    baseline = RecoupmentAgent(use_semantic=False)
    augmented = RecoupmentAgent(matcher=matcher)

    for line in eval_data.build_eval_set():
        if baseline.run(line.text, "t.pdf").flags:
            assert augmented.run(line.text, "t.pdf").flags, (
                f"semantic layer dropped a regex detection: {line.text!r}")
