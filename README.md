# RemitGuard — Behavioral Health RCM Platform

> **A purpose-built operating system for behavioral health billing teams — replacing ad-hoc coordination tools with structured workflows.**

![Architecture](docs/architecture.svg)

A multi-agent AI platform that catches hidden payer clawbacks, tracks Single Case Agreement lifecycles, and manages ERA/EFT enrollment status — built for behavioral health practices that can't afford to miss a $18,000 offset buried in an EOB.

**Live demo:** _not currently deployed_ — `cd platform && bash deploy.sh`
**API docs:** `/docs` on the deployed service

---

## The problem

Payers like Anthem hide recoupments as line-item offsets inside EOBs. A billing coordinator processes the EOB, the check arrives, everything looks fine — then 3 months later a collection letter arrives for money the payer already took back. The Haddad case that inspired this: **$18,020.11**, missed for 90 days, hidden in 4 lines on page 3.

Beyond recoupments, behavioral health practices lose money three other ways this platform addresses:
- **Expired SCAs** — billing under a dead Single Case Agreement means retroactive denials
- **Lapsed ERA enrollment** — paper EOBs mean manual reconciliation and 30+ day delays
- **Missed EFT re-enrollment** — bank switches cause weeks of paper check processing

---

## What's built

### Multi-agent pipeline

```
PDF Upload
    ↓
EOBAgent          — 3-strategy iterative extraction (pdfplumber standard → layout → char fallback)
    ↓                 confidence scoring 0–1, escalates if < 0.4
RecoupmentAgent   — pattern detection against 6 payer phrase libraries + ledger reconciliation
    ↓                 self-validation loop: re-examines ±3 line window, max 2 iterations
ValidatorAgent    — 5 sanity checks, Levenshtein duplicate detection
    ↓                 thresholds dynamically adjusted per payer via feedback calibration
EscalationAgent   — 4 auto-escalation rules, writes HumanReview tickets
    ↓
Result: APPROVED / ESCALATED / REJECTED
```

### API surface

| Route | What it does |
|-------|-------------|
| `POST /api/recoupment/analyze` | Analyze a single EOB PDF |
| `POST /api/recoupment/batch` | Batch analyze multiple EOBs |
| `GET  /api/recoupment/history` | Past results |
| `WS   /ws/pipeline` | Real-time streaming — phase events per file as agents run |
| `GET  /api/sca/` | All SCAs with computed status |
| `POST /api/sca/create` | Create a new SCA |
| `GET  /api/sca/alerts` | SCAs expiring or exhausted |
| `GET  /api/enrollment/era` | ERA enrollment status per payer |
| `GET  /api/enrollment/eft` | EFT enrollment status per payer |
| `GET  /api/review/pending` | Human review queue |
| `POST /api/review/{id}/approve` | Approve a ticket (triggers calibration) |
| `POST /api/review/{id}/dismiss` | Dismiss a ticket (triggers calibration) |
| `GET  /api/review/calibration` | Moat dashboard — per-payer FPR and threshold adjustments |

### The moat: feedback calibration loop

Every human approve/dismiss decision upserts `feedback_stats`:

```
Billing coordinator dismisses a false positive for "generic" payer
    → FeedbackCalibrator.record_outcome()
        → false_positive_rate for "generic" rises to 0.73
            → confidence_adjustment = +0.20

Next ValidatorAgent run for a "generic" payer flag:
    → effective_threshold = clamp(0.3, 0.9, 0.5 + 0.20) = 0.70
        → fewer false positives escalated
```

After 100 tickets, false positive rate drops measurably per payer. That calibration table is the data moat — competitors can copy the code, not the decisions.

---

## Project structure

```
remitguard/
├── patterns.json                    # payer-specific recoupment phrase library
├── detector.py                      # standalone CLI tool (original prototype)
├── app.py                           # Flask demo (original prototype)
│
└── platform/                        # production FastAPI platform
    ├── main.py                      # FastAPI app, route registration
    ├── requirements.txt
    ├── Dockerfile
    ├── deploy.sh                    # one-command Cloud Run deploy
    │
    ├── agents/
    │   ├── orchestrator.py          # 4-stage coordinator, emit() streaming callbacks
    │   ├── eob_agent.py             # iterative PDF extraction
    │   ├── recoupment_agent.py      # detection + self-validation loop
    │   ├── validator_agent.py       # cross-validation, calibration-adjusted thresholds
    │   └── escalation_agent.py     # human-in-the-loop gate, feedback recording
    │
    ├── api/
    │   ├── models/
    │   │   ├── base.py              # SQLAlchemy setup, SQLite
    │   │   ├── recoupment.py        # RecoupmentResult, RecoupmentFlag
    │   │   ├── sca.py               # SCA with status/warning computed properties
    │   │   ├── enrollment.py        # ERAEnrollment, EFTEnrollment
    │   │   ├── human_review.py      # HumanReview ticket queue
    │   │   └── feedback_stats.py    # per-payer FPR tracking (the moat)
    │   │
    │   ├── routes/
    │   │   ├── recoupment.py        # /api/recoupment/*
    │   │   ├── sca.py               # /api/sca/*
    │   │   ├── enrollment.py        # /api/enrollment/*
    │   │   ├── review.py            # /api/review/*
    │   │   └── ws_pipeline.py       # /ws/pipeline (WebSocket)
    │   │
    │   └── services/
    │       ├── recoupment_service.py
    │       ├── sca_service.py
    │       ├── enrollment_service.py
    │       └── feedback_calibrator.py   # threshold adjustment computation
    │
    ├── frontend/
    │   └── index.html               # single-page dashboard (no build step)
    │
    └── run_pipeline.py              # CLI runner for local testing
```

---

## Running locally

**Prerequisites:** Python 3.9+, pip

```bash
cd platform
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000 — the full dashboard loads.

**Run the multi-agent pipeline from the CLI:**

```bash
cd platform
python run_pipeline.py path/to/eob.pdf
python run_pipeline.py path/to/eob.pdf --ledger claims_ledger.csv

# batch
python run_pipeline.py eob1.pdf eob2.pdf eob3.pdf

# human review queue
python run_pipeline.py --review
python run_pipeline.py --approve 3 "Verified clawback, amount matches ledger"
python run_pipeline.py --dismiss 4 "False positive — line item is a credit"
```

**Test WebSocket streaming:**

```bash
pip install websockets
python test_ws.py   # requires server running on :8000
```

---

## Deploying to Google Cloud Run

Requires `gcloud` CLI authenticated to a project with Cloud Run and Cloud Build APIs enabled.

```bash
cd platform
bash deploy.sh
```

This builds the image via Cloud Build (no local Docker needed), deploys to `us-central1`, and prints the live URL. Takes ~90 seconds.

To change the project or region, edit the variables at the top of `deploy.sh`:

```bash
PROJECT_ID="your-gcp-project"
REGION="us-central1"
SERVICE_NAME="remitguard"
```

> **Note on persistence:** Cloud Run uses SQLite on ephemeral disk. Data resets on container restarts. For a persistent production deployment, set `DATABASE_URL` to a Cloud SQL (Postgres) connection string.

---

## Adding payer patterns

`patterns.json` is the phrase library. Each key is a payer tag; each value is a list of regex patterns.

```json
{
  "anthem": [
    "outstanding\\s+neg\\s*bal\\s+with\\s+differ",
    "offset\\s+applied"
  ],
  "cigna": [
    "recovery\\s+amount",
    "prior\\s+overpayment"
  ],
  "generic": [
    "recoup",
    "clawback",
    "offset"
  ]
}
```

When the system escalates a ticket with `ticket_type = "new_payer_pattern"`, that's a signal to add the matched phrase to this file. The moat grows with every new payer encounter.

---

## Origin

Built from operational data and real failure modes observed running a behavioral health billing team. Every feature traces to a specific recurring problem:

- Recoupment detector → Anthem/Haddad $18,020.11 offset, missed 90 days
- SCA tracker → Cigna SCA expiry not noticed, 8 claims denied retroactively  
- ERA/EFT tracker → bank switch caused 60-day payment delay across 12 payers
- Human review queue → billing coordinator needed to verify before acting on AI flags
- Feedback calibration → after 3 false positives on generic patterns, thresholds tightened

---

## Stack

- **Backend:** FastAPI, SQLAlchemy, SQLite (swap to Postgres for production)
- **PDF parsing:** pdfplumber (3 strategies: standard, layout, character-level)
- **AI agents:** Pure Python — no LLM API calls, all rule-based with confidence scoring
- **WebSocket:** FastAPI native WebSocket + asyncio.Queue for sync→async bridge
- **Frontend:** Vanilla HTML/JS/CSS, no build step, no framework
- **Deploy:** Google Cloud Run via Cloud Build
