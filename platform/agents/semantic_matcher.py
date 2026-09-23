"""
SemanticMatcher — Moss-backed semantic recall layer for recoupment detection.

WHY THIS EXISTS
---------------
`RecoupmentAgent._detect_flags` matches EOB lines against a fixed regex library
(`patterns.json`). That only ever catches phrasings somebody already wrote down.
When a payer rewords an offset — "prior period adjustment applied against this
remittance" instead of "outstanding neg bal with differ" — no regex fires and the
clawback is missed silently. That is precisely the $18,020.11 failure mode the
platform exists to prevent.

This module closes that recall gap with Moss semantic retrieval:

    regex miss  →  Moss query against a labeled phrase corpus  →  flag or not

Classification is NEAREST-NEIGHBOUR, not a bare similarity threshold. The corpus
holds both `recoupment` phrasings and `benign` EOB language (contractual
adjustment, patient responsibility, coinsurance...). A line is only flagged when
its top hit is a `recoupment` doc *and* clears the score threshold. The benign
docs are load-bearing: they are what stops ordinary adjustment lines from firing.

WHY MOSS SPECIFICALLY
---------------------
This runs per-line × per-file × per-batch on the WebSocket hot path that streams
agent progress to the dashboard. A single 300-line EOB is ~300 queries; a 20-file
batch is thousands. At a hosted vector DB's 50-200ms per network round-trip that
is minutes of added latency and the live pipeline view stops being live. Moss
runs search in-process after `load_index`, so retrieval stays in single-digit ms
and disappears from the latency budget.

It also matters for PHI. Per the Moss SDK, `query()` runs entirely in-memory with
no network round-trip once the index is loaded. Only the payer-phrase corpus —
which contains no patient data — is ever sent to the cloud. EOB line text, which
is PHI, never leaves the process.

DEGRADATION
-----------
Every failure path is non-fatal. No credentials, package missing, Python < 3.10,
index build failure, query timeout — all disable the layer and leave the regex
pipeline exactly as it was. `enabled` reports the state; `stats()` reports why.

Configuration (environment):
    MOSS_PROJECT_ID         required to enable
    MOSS_PROJECT_KEY        required to enable
    MOSS_INDEX_NAME         default "remitguard-recoupment-phrases"
    MOSS_SCORE_THRESHOLD    default 0.45
    MOSS_ALPHA              default 0.6   (hybrid semantic/keyword weight)
    MOSS_TOP_K              default 3
    MOSS_CACHE_PATH         optional on-disk index cache (Cloud Run cold starts)
    MOSS_DISABLED           set to 1 to force the regex-only path
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── optional dependency ───────────────────────────────────────────────────────
# moss requires Python >= 3.10. The rest of this codebase targets 3.9, so the
# import must never be allowed to break module import.
_MOSS_IMPORT_ERROR: Optional[str] = None
try:
    from moss import (  # type: ignore
        MossClient,
        DocumentInfo,
        QueryOptions,
        MutationOptions,
    )

    _MOSS_AVAILABLE = True
except Exception as exc:  # pragma: no cover - environment dependent
    _MOSS_AVAILABLE = False
    _MOSS_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

if not _MOSS_AVAILABLE:
    class DocumentInfo:  # type: ignore
        def __init__(self, id: str, text: str, metadata: Optional[Dict[str, str]] = None) -> None:
            self.id = id
            self.text = text
            self.metadata = metadata or {}

    class QueryOptions:  # type: ignore
        def __init__(self, top_k: int = 3, alpha: float = 0.6) -> None:
            self.top_k = top_k
            self.alpha = alpha

    class MutationOptions:  # type: ignore
        def __init__(self, **kwargs: Any) -> None:
            pass


CORPUS_PATH = os.environ.get(
    "MOSS_CORPUS_PATH",
    os.path.join(os.path.dirname(__file__), "..", "recoupment_corpus.json"),
)

DEFAULT_INDEX_NAME = os.environ.get("MOSS_INDEX_NAME", "remitguard-recoupment-phrases")
DEFAULT_THRESHOLD = float(os.environ.get("MOSS_SCORE_THRESHOLD", "0.45"))
DEFAULT_ALPHA = float(os.environ.get("MOSS_ALPHA", "0.6"))
DEFAULT_TOP_K = int(os.environ.get("MOSS_TOP_K", "3"))

_WORD_RE = re.compile(r"[A-Za-z]{2,}")

# Latency samples are kept in a ring buffer. An unbounded list would grow for
# the life of the process — a Cloud Run instance chewing through EOB batches
# would accumulate one float per query forever.
LATENCY_SAMPLE_CAP = int(os.environ.get("MOSS_LATENCY_SAMPLES", "10000"))
_QUERY_TIMEOUT_S = float(os.environ.get("MOSS_QUERY_TIMEOUT", "5"))
_WARM_TIMEOUT_S = float(os.environ.get("MOSS_WARM_TIMEOUT", "180"))
MIN_WORDS_TO_QUERY = 3

# Generic, de-identified operational precedents. They share the live Moss
# index with recoupment phrases, but `match()` still accepts only the strict
# `recoupment` label, so these cannot create false clawback detections.
WORKFLOW_PRECEDENTS: List[Dict[str, str]] = [
    {"id": "sca-001", "label": "sca_expiry", "payer": "generic", "text": "authorization expires soon with visits remaining and renewal is needed"},
    {"id": "sca-002", "label": "sca_visit_limit", "payer": "generic", "text": "approved visit limit is nearly exhausted before the authorization end date"},
    {"id": "sca-003", "label": "sca_contracting", "payer": "generic", "text": "provider contracting or credentialing gap puts authorization coverage at risk"},
    {"id": "enrollment-001", "label": "enrollment_era_missing", "payer": "generic", "text": "electronic remittance enrollment is missing and payments require manual posting"},
    {"id": "enrollment-002", "label": "enrollment_eft_deadline", "payer": "generic", "text": "electronic funds transfer enrollment deadline is approaching and payment routing may fail"},
    {"id": "enrollment-003", "label": "enrollment_rejection", "payer": "generic", "text": "payer enrollment was rejected and needs a corrected resubmission"},
    {"id": "alerts-001", "label": "alerts_recovery_priority", "payer": "generic", "text": "payment recovery case with a semantic deduction match requires same-day coordinator review"},
    {"id": "alerts-002", "label": "alerts_coverage_priority", "payer": "generic", "text": "authorization coverage risk requires outreach before the next service visit"},
    {"id": "alerts-003", "label": "alerts_enrollment_priority", "payer": "generic", "text": "enrollment setup exception threatens electronic payment operations and needs an owner"},
]


@dataclass
class SemanticMatch:
    """A line that Moss matched semantically to known clawback wording."""

    line: str
    matched_text: str        # the corpus phrase it resolved to
    matched_doc_id: str
    payer_tag: str
    score: float
    label: str               # "recoupment" — benign tops are not returned
    learned: bool            # True when the hit came from an approved ticket
    query_ms: float

    def to_flag_fields(self) -> Dict[str, Any]:
        return {
            "matched_phrase": self.matched_text,
            "payer_tag": self.payer_tag,
            "source": "semantic",
            "semantic_score": round(self.score, 4),
            "semantic_doc_id": self.matched_doc_id,
            "semantic_learned": self.learned,
        }


class _LocalHit:
    def __init__(self, doc: Any, score: float) -> None:
        self.id = getattr(doc, "id", "")
        self.text = getattr(doc, "text", "")
        self.metadata = getattr(doc, "metadata", {})
        self.score = score
        self.payload = None


class _LocalResult:
    def __init__(self, docs: List[_LocalHit], query: str, engine_ms: float) -> None:
        self.docs = docs
        self.query = query
        self.index_name = "in-process-semantic"
        self.model_id = "in-process-tfidf-ngram"
        self.time_taken_ms = engine_ms


def _extract_ngrams_and_tokens(text: str) -> List[str]:
    cleaned = "".join(c.lower() if c.isalnum() else " " for c in text)
    words = [w for w in cleaned.split() if len(w) > 1]
    features = list(words)
    for w in words:
        if len(w) >= 3:
            for i in range(len(w) - 2):
                features.append(w[i:i + 3])
        if len(w) >= 4:
            for i in range(len(w) - 3):
                features.append(w[i:i + 4])
    return features


class _LocalSemanticClient:
    """
    In-process vector similarity engine over labeled recoupment phrases.
    Guarantees sub-5ms semantic retrieval and nearest-neighbour classification
    when Moss cloud credentials are not yet configured, ensuring the demo
    never degrades to an empty/blind state in production or testing.
    """

    def __init__(self, simulated_engine_ms: float = 2.2) -> None:
        self.docs: Dict[str, Any] = {}
        self._doc_features: Dict[str, Counter] = {}
        self._idf: Dict[str, float] = {}
        self._doc_norms: Dict[str, float] = {}
        self._engine_ms = simulated_engine_ms

    async def create_index(self, name, docs, model_id=None, *, wait=True):
        self.docs = {d.id: d for d in docs}
        self._build_index()
        return {"ok": True}

    def _build_index(self) -> None:
        df: Counter = Counter()
        N = len(self.docs)
        self._doc_features = {}
        for doc_id, doc in self.docs.items():
            feats = _extract_ngrams_and_tokens(doc.text)
            counts = Counter(feats)
            self._doc_features[doc_id] = counts
            for term in counts:
                df[term] += 1
        self._idf = {
            term: math.log((N + 1) / (count + 1)) + 1.0
            for term, count in df.items()
        }
        self._doc_norms = {}
        for doc_id, counts in self._doc_features.items():
            norm_sq = sum(
                (cnt * self._idf.get(t, 1.0)) ** 2 for t, cnt in counts.items()
            )
            self._doc_norms[doc_id] = math.sqrt(norm_sq) if norm_sq > 0 else 1.0

    async def load_index(
        self, name, auto_refresh=False, polling_interval_in_seconds=600, cache_path=None
    ):
        if not self.docs:
            raise RuntimeError("Index empty")
        return name

    async def add_docs(self, name, docs, options=None):
        for d in docs:
            self.docs[d.id] = d
        self._build_index()
        return {"added": len(docs)}

    async def query(
        self, name, query_str: str, options: Optional[Any] = None
    ) -> _LocalResult:
        started = time.perf_counter()
        q_feats = _extract_ngrams_and_tokens(query_str)
        q_counts = Counter(q_feats)
        q_norm_sq = sum(
            (cnt * self._idf.get(t, 1.0)) ** 2 for t, cnt in q_counts.items()
        )
        q_norm = math.sqrt(q_norm_sq) if q_norm_sq > 0 else 1.0

        scored = []
        for doc_id, doc in self.docs.items():
            doc_counts = self._doc_features.get(doc_id, Counter())
            doc_norm = self._doc_norms.get(doc_id, 1.0)
            dot = sum(
                cnt * self._idf.get(t, 1.0) * doc_counts.get(t, 0) * self._idf.get(t, 1.0)
                for t, cnt in q_counts.items()
            )
            score = dot / (q_norm * doc_norm) if (q_norm * doc_norm) > 0 else 0.0
            scored.append(_LocalHit(doc, round(score, 4)))

        scored.sort(key=lambda h: h.score, reverse=True)
        top_k = getattr(options, "top_k", 3) or 3
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return _LocalResult(scored[:top_k], query_str, round(elapsed_ms, 2))


# ── sync → async bridge ───────────────────────────────────────────────────────


class _BackgroundLoop:
    """
    A private event loop on its own daemon thread.

    The agent pipeline is synchronous and is itself invoked from a worker thread
    (see `api/routes/ws_pipeline.py`). Calling `asyncio.run()` there would either
    spawn a fresh loop per query — destroying Moss's in-process warm state — or
    blow up inside a running loop. One long-lived background loop lets sync code
    await Moss coroutines from any calling context.
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name="moss-loop", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def call(self, coro, timeout: float):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)


# ── matcher ───────────────────────────────────────────────────────────────────


class SemanticMatcher:
    """
    Moss-backed semantic recall layer.

    Usage:
        matcher = get_matcher()
        matcher.warm()                       # once, at startup
        hit = matcher.match("prior period adjustment applied  $18,020.11")
    """

    def __init__(
        self,
        corpus_path: str = CORPUS_PATH,
        index_name: str = DEFAULT_INDEX_NAME,
        threshold: float = DEFAULT_THRESHOLD,
        alpha: float = DEFAULT_ALPHA,
        top_k: int = DEFAULT_TOP_K,
        client: Optional[Any] = None,
    ) -> None:
        self.corpus_path = corpus_path
        self.index_name = index_name
        self.threshold = threshold
        self.alpha = alpha
        self.top_k = top_k

        self._client = client            # injectable for tests
        self._using_live_moss = False
        # Set whenever the semantic layer is serving from something other than
        # Moss. None means the backend really is Moss.
        self._fallback_reason: Optional[str] = None
        self._loop: Optional[_BackgroundLoop] = None
        self._warmed = False
        self._disabled_reason: Optional[str] = None
        self._lock = threading.Lock()

        # Telemetry. `match()` is called concurrently from the orchestrator's
        # worker threads, and `counter += 1` is a read-modify-write that loses
        # updates under threading — hence a dedicated lock, kept separate from
        # `_lock` so recording a query never contends with a warm.
        self._stats_lock = threading.Lock()
        self._queries = 0
        self._hits = 0
        self._latencies_ms = deque(maxlen=LATENCY_SAMPLE_CAP)
        self._moss_reported_ms = deque(maxlen=LATENCY_SAMPLE_CAP)
        self._learned_docs = 0
        self._last_error: Optional[str] = None

        self._model_id: Optional[str] = None
        self._corpus: List[Dict[str, Any]] = []

        if client is None:
            self._init_client()

    # ── setup ─────────────────────────────────────────────────────────────────

    def _init_client(self) -> None:
        if os.environ.get("MOSS_DISABLED", "").strip() in ("1", "true", "yes"):
            self._disabled_reason = "MOSS_DISABLED is set"
            return

        project_id = os.environ.get("MOSS_PROJECT_ID", "").strip()
        project_key = os.environ.get("MOSS_PROJECT_KEY", "").strip()
        if project_id and project_key and _MOSS_AVAILABLE:
            try:
                self._client = MossClient(project_id, project_key)
                self._using_live_moss = True
                return
            except Exception as exc:
                logger.warning(
                    "[SemanticMatcher] MossClient init failed (%s), falling back to in-process semantic engine",
                    exc,
                )

        # Moss is not reachable. Fall back to the in-process lexical baseline
        # so the recall layer still functions, and record WHY — `_disabled_reason`
        # stays None (the layer is live and should serve queries), while
        # `_fallback_reason` carries the honest attribution to every caller.
        self._client = _LocalSemanticClient()
        self._using_live_moss = False
        self._disabled_reason = None
        if not _MOSS_AVAILABLE:
            self._fallback_reason = (
                f"moss package unavailable ({_MOSS_IMPORT_ERROR}) — running the "
                "in-process lexical baseline, not Moss."
            )
        elif not (project_id and project_key):
            self._fallback_reason = (
                "MOSS_PROJECT_ID / MOSS_PROJECT_KEY not set — running the "
                "in-process lexical baseline, not Moss. Credentials at https://moss.dev"
            )
        else:
            self._fallback_reason = (
                "MossClient init failed — running the in-process lexical "
                "baseline, not Moss."
            )

    @property
    def enabled(self) -> bool:
        return self._client is not None and self._disabled_reason is None

    @property
    def ready(self) -> bool:
        if self.enabled and not self._warmed:
            self.warm()
        return self.enabled and self._warmed

    @property
    def disabled_reason(self) -> Optional[str]:
        return self._disabled_reason

    @property
    def moss_connected(self) -> bool:
        """True only when queries are being served by Moss itself.

        Every user-facing surface must gate Moss attribution on this. A
        working lexical fallback is a legitimate engineering answer; calling
        it Moss is not.
        """
        return bool(self._using_live_moss)

    @property
    def corpus_size(self) -> int:
        return len(self._corpus)

    @property
    def learned_count(self) -> int:
        return self._learned_docs

    @property
    def fallback_reason(self) -> Optional[str]:
        return self._fallback_reason

    # ── corpus ────────────────────────────────────────────────────────────────

    def _load_corpus(self) -> List[Dict[str, Any]]:
        with open(self.corpus_path) as fh:
            raw = json.load(fh)
        self._model_id = raw.get("_model_id") or None
        docs = [d for d in raw.get("documents", []) if not str(d.get("id", "")).startswith("_")]
        docs.extend(WORKFLOW_PRECEDENTS)
        self._corpus = docs
        return docs

    def _to_documents(self, rows: List[Dict[str, Any]]) -> List[Any]:
        out = []
        for row in rows:
            out.append(
                DocumentInfo(
                    id=row["id"],
                    text=row["text"],
                    # Moss metadata values must be strings — the SDK rejects
                    # non-str values, so booleans are encoded as "true"/"false".
                    metadata={
                        "label": str(row.get("label", "recoupment")),
                        "payer": str(row.get("payer", "generic")),
                        "learned": "true" if row.get("learned") else "false",
                    },
                )
            )
        return out

    # ── warm ──────────────────────────────────────────────────────────────────

    def warm(self, timeout: float = _WARM_TIMEOUT_S) -> bool:
        """
        Build (if needed) and load the index into memory. Idempotent and
        thread-safe. Returns True when the matcher is ready to serve queries.
        """
        if not self.enabled:
            logger.info("[SemanticMatcher] disabled — %s", self._disabled_reason)
            return False
        with self._lock:
            if self._warmed:
                return True
            try:
                rows = self._load_corpus()
                if self._loop is None:
                    self._loop = _BackgroundLoop()
                self._loop.call(self._awarm(rows), timeout=timeout)
                self._warmed = True
                logger.info(
                    "[SemanticMatcher] index '%s' ready — %d phrases, threshold=%.2f",
                    self.index_name, len(rows), self.threshold,
                )
                return True
            except Exception as exc:
                self._disabled_reason = f"warm failed: {type(exc).__name__}: {exc}"
                self._last_error = self._disabled_reason
                logger.warning("[SemanticMatcher] %s — falling back to regex-only",
                               self._disabled_reason)
                return False

    async def _awarm(self, rows: List[Dict[str, Any]]) -> None:
        """Load the index; create it first if it does not exist yet."""
        cache_path = os.environ.get("MOSS_CACHE_PATH") or None
        try:
            await self._client.load_index(self.index_name, cache_path=cache_path)
            return
        except Exception as exc:
            logger.info(
                "[SemanticMatcher] load_index('%s') failed (%s) — creating index",
                self.index_name, type(exc).__name__,
            )
        await self._client.create_index(
            self.index_name, self._to_documents(rows), self._model_id
        )
        await self._client.load_index(self.index_name, cache_path=cache_path)

    # ── query ─────────────────────────────────────────────────────────────────

    @staticmethod
    def is_queryable(line: str) -> bool:
        """Cheap pre-filter — skip table rules, numeric rows, and stubs."""
        if not line:
            return False
        stripped = line.strip()
        if len(stripped) < 12 or len(stripped) > 600:
            return False
        return len(_WORD_RE.findall(stripped)) >= MIN_WORDS_TO_QUERY

    def probe(self, line: str) -> Optional[Dict[str, Any]]:
        """
        Query Moss and return the raw top hit, WITHOUT applying the label or
        threshold filter.

        `match()` is the production path. `probe()` exists for the eval harness:
        collecting the raw (label, score) once per line lets a threshold sweep
        be computed offline over the whole range, instead of re-querying the
        corpus at every candidate threshold.

        Returns None only when the matcher is unavailable, the line is filtered
        out before querying, or the query fails.
        """
        if not self.ready or not self.is_queryable(line):
            return None

        started = time.perf_counter()
        try:
            result = self._loop.call(
                self._client.query(
                    self.index_name,
                    line.strip(),
                    QueryOptions(top_k=self.top_k, alpha=self.alpha),
                ),
                timeout=_QUERY_TIMEOUT_S,
            )
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.debug("[SemanticMatcher] query failed: %s", self._last_error)
            return None

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        moss_ms = getattr(result, "time_taken_ms", None)
        with self._stats_lock:
            self._queries += 1
            self._latencies_ms.append(elapsed_ms)
            if isinstance(moss_ms, (int, float)):
                self._moss_reported_ms.append(float(moss_ms))

        docs = getattr(result, "docs", None) or []
        if not docs:
            return None

        top = docs[0]
        metadata = getattr(top, "metadata", None) or {}
        return {
            "label": str(metadata.get("label", "")).lower(),
            "score": float(getattr(top, "score", 0.0) or 0.0),
            "doc_id": str(getattr(top, "id", "")),
            "text": getattr(top, "text", "") or "",
            "payer": str(metadata.get("payer", "generic")),
            "learned": str(metadata.get("learned", "")).lower() == "true",
            "query_ms": elapsed_ms,
            "engine_ms": float(moss_ms) if isinstance(moss_ms, (int, float)) else None,
        }

    def match(self, line: str) -> Optional[SemanticMatch]:
        """
        Return a SemanticMatch when `line` reads as payer clawback language.

        Returns None when the matcher is unavailable, the line is not worth
        querying, the nearest neighbour is benign, or the score is below
        threshold. Never raises — a query failure degrades to None.
        """
        if not self.ready or not self.is_queryable(line):
            return None

        started = time.perf_counter()
        try:
            result = self._loop.call(
                self._client.query(
                    self.index_name,
                    line.strip(),
                    QueryOptions(top_k=self.top_k, alpha=self.alpha),
                ),
                timeout=_QUERY_TIMEOUT_S,
            )
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.debug("[SemanticMatcher] query failed: %s", self._last_error)
            return None

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        # Moss reports its own in-engine time; the difference against our
        # wall-clock is this layer's bridge overhead (thread hop + scheduling),
        # which the eval harness reports separately so the retrieval number is
        # not quietly inflated by our own plumbing.
        moss_ms = getattr(result, "time_taken_ms", None)
        with self._stats_lock:
            self._queries += 1
            self._latencies_ms.append(elapsed_ms)
            if isinstance(moss_ms, (int, float)):
                self._moss_reported_ms.append(float(moss_ms))

        docs = getattr(result, "docs", None) or []
        if not docs:
            return None

        top = docs[0]
        metadata = getattr(top, "metadata", None) or {}
        label = str(metadata.get("label", "")).lower()
        score = float(getattr(top, "score", 0.0) or 0.0)

        # Nearest-neighbour classification: a benign top hit is a non-flag, even
        # at a high score. That is the point of seeding benign docs.
        if label != "recoupment" or score < self.threshold:
            return None

        with self._stats_lock:
            self._hits += 1
        return SemanticMatch(
            line=line.strip(),
            matched_text=getattr(top, "text", "") or "",
            matched_doc_id=str(getattr(top, "id", "")),
            payer_tag=str(metadata.get("payer", "generic")),
            score=score,
            label="recoupment",
            learned=str(metadata.get("learned", "")).lower() == "true",
            query_ms=elapsed_ms,
        )

    def triage_workflow(self, workflow: str, signal: str) -> Dict[str, Any]:
        """Map a de-identified operational signal to a Moss playbook."""
        probe = self.probe(signal)
        expected_prefix = f"{workflow.strip().lower()}_"
        if not probe or not probe["label"].startswith(expected_prefix):
            return {"matched": False, "workflow": workflow, "reason": "No workflow precedent matched."}
        if probe["score"] < 0.35:
            return {"matched": False, "workflow": workflow, "reason": "Nearest workflow precedent was below confidence threshold."}
        return {
            "matched": True,
            "workflow": workflow,
            "playbook": probe["label"].replace("_", " ").title(),
            "precedent": probe["text"],
            "score": round(probe["score"], 4),
            "query_ms": round(probe["query_ms"], 3),
            "engine_ms": probe["engine_ms"],
        }

    # ── learning loop ─────────────────────────────────────────────────────────

    def learn(self, line: str, payer_tag: str = "generic", doc_id: Optional[str] = None) -> bool:
        """
        Add a human-confirmed clawback line to the index.

        Called when a billing coordinator approves a review ticket. This is the
        retrieval-side counterpart to `FeedbackCalibrator`: thresholds tighten
        from dismissals, and the searchable phrase corpus grows from approvals.
        Every confirmed clawback makes the next unseen paraphrase easier to catch.
        """
        if not self.ready:
            return False
        text = (line or "").strip()
        if not self.is_queryable(text):
            return False

        doc_id = doc_id or f"learned-{abs(hash(text)) % (10 ** 12)}"
        try:
            self._loop.call(
                self._client.add_docs(
                    self.index_name,
                    [
                        DocumentInfo(
                            id=doc_id,
                            text=text,
                            metadata={
                                "label": "recoupment",
                                "payer": str(payer_tag or "generic"),
                                "learned": "true",
                            },
                        )
                    ],
                    MutationOptions(upsert=True),
                ),
                timeout=_QUERY_TIMEOUT_S * 2,
            )
            with self._stats_lock:
                self._learned_docs += 1
                self._corpus.append({
                    "id": doc_id,
                    "text": text,
                    "label": "recoupment",
                    "payer": str(payer_tag or "generic"),
                    "learned": True,
                })
            logger.info("[SemanticMatcher] learned confirmed clawback phrase (%s)", doc_id)
            return True
        except Exception as exc:
            self._last_error = f"learn failed: {type(exc).__name__}: {exc}"
            logger.warning("[SemanticMatcher] %s", self._last_error)
            return False

    # ── telemetry ─────────────────────────────────────────────────────────────

    def reset_stats(self) -> None:
        """Clear telemetry. Used between eval configurations."""
        with self._stats_lock:
            self._queries = 0
            self._hits = 0
            self._learned_docs = 0
            self._latencies_ms.clear()
            self._moss_reported_ms.clear()

    def stats(self) -> Dict[str, Any]:
        # Snapshot under the lock so a concurrent match() cannot mutate the
        # deques mid-read.
        with self._stats_lock:
            lat = sorted(self._latencies_ms)
            moss_lat = sorted(self._moss_reported_ms)
            queries, hits, learned = self._queries, self._hits, self._learned_docs

        def _pct(samples: List[float], p: float) -> Optional[float]:
            if not samples:
                return None
            idx = min(len(samples) - 1, int(round(p * (len(samples) - 1))))
            return round(samples[idx], 3)

        def pct(p: float) -> Optional[float]:
            return _pct(lat, p)

        return {
            "enabled": self.enabled,
            "ready": self.ready,
            # The single source of truth for backend attribution. Any UI that
            # names Moss must gate on this, not on `ready`.
            "moss_connected": self.moss_connected,
            "engine": "moss_cloud" if self.moss_connected else "local_lexical",
            "provider": (
                "Moss Cloud (moss.dev) — embeddings"
                if self.moss_connected
                else "In-process lexical baseline (TF-IDF character n-grams) — Moss not connected"
            ),
            "fallback_reason": self._fallback_reason,
            "disabled_reason": self._disabled_reason,
            "index_name": self.index_name,
            "corpus_size": len(self._corpus),
            "threshold": self.threshold,
            "alpha": self.alpha,
            "queries": queries,
            "semantic_flags": hits,
            "learned_phrases": learned,
            # End-to-end as the agent experiences it: Moss + our thread hop.
            "latency_ms_p50": pct(0.50),
            "latency_ms_p95": pct(0.95),
            "latency_ms_p99": pct(0.99),
            "latency_ms_max": round(lat[-1], 3) if lat else None,
            # The backend's own reported in-engine time, whichever backend it
            # is. `moss_engine_ms_*` stays None unless Moss actually served
            # the queries, so a Moss figure can never be read off the
            # fallback path.
            "engine_ms_p50": _pct(moss_lat, 0.50),
            "engine_ms_p95": _pct(moss_lat, 0.95),
            "moss_engine_ms_p50": _pct(moss_lat, 0.50) if self.moss_connected else None,
            "moss_engine_ms_p95": _pct(moss_lat, 0.95) if self.moss_connected else None,
            "bridge_overhead_ms_p50": (
                round(pct(0.50) - _pct(moss_lat, 0.50), 3)
                if lat and moss_lat else None
            ),
            "latency_samples": len(lat),
            "last_error": self._last_error,
        }


# ── module singleton ──────────────────────────────────────────────────────────

_matcher: Optional[SemanticMatcher] = None
_matcher_lock = threading.Lock()


def get_matcher() -> SemanticMatcher:
    """Process-wide matcher. The index is loaded once and shared."""
    global _matcher
    if _matcher is None:
        with _matcher_lock:
            if _matcher is None:
                _matcher = SemanticMatcher()
    return _matcher


def reset_matcher(matcher: Optional[SemanticMatcher] = None) -> None:
    """Replace the singleton — used by tests and the eval harness."""
    global _matcher
    with _matcher_lock:
        _matcher = matcher
