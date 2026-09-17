"""
Dataset integrity tests for the evaluation benchmark.

These deliberately live outside `test_semantic_matcher.py`, which skips wholesale
when `moss` is unavailable. The benchmark's validity does not depend on Moss, so
these must run on every supported Python — including 3.9, where the semantic
layer is switched off. They are what stops the eval set from quietly drifting
into the indexed corpus and reporting memorisation as generalisation.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def test_eval_set_is_disjoint_from_indexed_corpus():
    """
    Guards the whole benchmark: if eval phrases leak into the corpus, the
    numbers measure memorisation. This fails the build rather than quietly
    reporting an inflated score.
    """
    import eval_data

    eval_data.assert_disjoint_from_corpus()
    novel = [r for r in eval_data.overlap_report()
             if r["bank"] in ("novel", "benign")]
    assert novel, "overlap report should cover the generalisation banks"
    assert max(r["max_overlap"] for r in novel) <= eval_data.MAX_CORPUS_OVERLAP


def test_eval_set_is_balanced_and_reproducible():
    import eval_data

    a = eval_data.build_eval_set()
    b = eval_data.build_eval_set()
    assert [l.text for l in a] == [l.text for l in b], "generation must be seeded"
    assert sum(l.is_recoupment for l in a) == sum(not l.is_recoupment for l in a)


def test_regex_baseline_is_non_trivial():
    """
    A benchmark on which regex scores zero would rig the comparison. The
    familiar-wording bank must actually be caught, and benign lines must not be.
    """
    import eval_data
    from agents.recoupment_agent import _detect_flags, _load_compiled_patterns

    compiled = _load_compiled_patterns()
    lines = eval_data.build_eval_set()
    familiar = [l for l in lines if l.category == "familiar_wording"]
    benign = [l for l in lines if not l.is_recoupment]

    caught = sum(1 for l in familiar if _detect_flags(l.text, compiled))
    assert caught / len(familiar) > 0.7, "regex must genuinely catch familiar wording"
    assert not any(_detect_flags(l.text, compiled) for l in benign), \
        "regex must not false-flag benign lines"


