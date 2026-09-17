"""
Stub Moss transport — shared by the test suite and `eval_semantic.py --stub`.

It replaces ONLY the network/service layer. It is constructed with, and returns,
the genuine `moss` SDK types, so a drift in those signatures still fails loudly.

Its similarity function is token-overlap (Jaccard), which is nothing like
embedding similarity. Use it to verify that code paths execute and that data
flows through correctly — never to draw a conclusion about retrieval quality.
Any number produced with this stub is a plumbing check, not a result.
"""

from __future__ import annotations

import random
import time
from typing import Dict, List, Optional

from moss import DocumentInfo, MutationOptions, QueryOptions


class StubHit:
    def __init__(self, doc: DocumentInfo, score: float) -> None:
        self.id = doc.id
        self.text = doc.text
        self.metadata = doc.metadata
        self.score = score
        self.payload = None


class StubResult:
    def __init__(self, docs: List[StubHit], query: str, engine_ms: float) -> None:
        self.docs = docs
        self.query = query
        self.index_name = "stub"
        self.model_id = "stub"
        self.time_taken_ms = engine_ms


def tokens(text: str) -> set:
    cleaned = "".join(c.lower() if c.isalnum() else " " for c in text)
    return {t for t in cleaned.split() if len(t) > 2}


class StubMossClient:
    """Mimics the MossClient surface that SemanticMatcher uses. No network."""

    def __init__(self, simulated_engine_ms: float = 0.0) -> None:
        self.docs: Dict[str, DocumentInfo] = {}
        self.created: List[tuple] = []
        self.loaded: List[str] = []
        self.load_should_fail = True   # simulate "index does not exist yet"
        self._engine_ms = simulated_engine_ms

    async def create_index(self, name, docs, model_id=None, *, wait=True):
        assert all(isinstance(d, DocumentInfo) for d in docs), "must pass real DocumentInfo"
        self.created.append((name, model_id, len(docs)))
        for d in docs:
            self.docs[d.id] = d
        self.load_should_fail = False
        return {"ok": True}

    async def load_index(self, name, auto_refresh=False,
                         polling_interval_in_seconds=600, cache_path=None):
        if self.load_should_fail:
            raise RuntimeError(f"index '{name}' not found")
        self.loaded.append(name)
        return name

    async def add_docs(self, name, docs, options: Optional[MutationOptions] = None):
        assert all(isinstance(d, DocumentInfo) for d in docs)
        assert options is None or isinstance(options, MutationOptions)
        for d in docs:
            self.docs[d.id] = d
        return {"added": len(docs)}

    async def query(self, name, query, options: Optional[QueryOptions] = None):
        assert isinstance(options, QueryOptions), "must pass real QueryOptions"
        if self._engine_ms:
            time.sleep(self._engine_ms / 1000.0)
        q = tokens(query)
        scored = [
            StubHit(d, len(q & tokens(d.text)) / len(q | tokens(d.text)) if (q | tokens(d.text)) else 0.0)
            for d in self.docs.values()
        ]
        scored.sort(key=lambda h: h.score, reverse=True)
        top_k = getattr(options, "top_k", 3) or 3
        return StubResult(scored[:top_k], query, self._engine_ms or 0.4)
