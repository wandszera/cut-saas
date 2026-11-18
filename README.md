# 🎬 Cut SaaS — AI-Powered Video Clip Engine

> **Hybrid heuristic + LLM scoring system** that automatically identifies, ranks, and renders the best viral clips from long-form videos — with a self-calibrating feedback loop that learns from editorial decisions.

[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![CI](https://github.com/wandszera/cut-saas/actions/workflows/ci.yml/badge.svg)](https://github.com/wandszera/cut-saas/actions/workflows/ci.yml)
[![LLM](https://img.shields.io/badge/LLM-OpenAI_%7C_Ollama-blueviolet?logo=openai&logoColor=white)](#llm-architecture)

---

## What This Project Demonstrates

This is a **production-grade SaaS platform** built end-to-end as a solo developer. It combines NLP heuristics, LLM-as-judge evaluation, and real-time feedback loops into a unified scoring engine — the kind of system that sits at the intersection of **classical ML engineering and modern LLM integration**.

### Key Engineering Highlights

| Area | What I Built |
|---|---|
| **Hybrid Scoring Engine** | 17-dimension heuristic scorer + LLM reranking with dynamically adjustable weight blending (50/50 → 80/20) |
| **LLM-Guided Heuristics** | LLM pre-analyzes full transcripts to extract topics, viral angles, and promising time ranges — feeding structured signals *into* the heuristic pipeline |
| **Self-Calibrating Feedback Loop** | User approvals/rejections automatically tune scoring multipliers, duration preferences, diversity penalties, and heuristic-vs-LLM trust ratio |
| **Structured Output Parsing** | Enforced JSON responses from Ollama (`format: "json"`) and OpenAI (`response_format: json_object`) with validation and graceful fallback |
| **Circuit Breaker Pattern** | Auto-skip LLM enrichment after 2 consecutive failures; local keyword generation fallback when rate-limited |
| **Durable Pipeline** | 5-step pipeline with pessimistic DB locking, per-step retry, cooperative cancellation, idempotent re-execution, and stale lock recovery |
| **Multi-Tenant SaaS** | Accounts, workspaces, workspace-scoped data isolation, billing (Stripe + Mercado Pago), usage quotas, and signed URLs |

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│                         FastAPI Web Server                          │
│  ┌──────────┐  ┌──────────────┐  ┌─────────┐  ┌─────────────────┐  │
│  │ Auth &   │  │  Job Create  │  │Dashboard│  │   Billing API   │  │
│  │ Sessions │  │  & Monitor   │  │& Detail │  │ Stripe/MP/Mock  │  │
│  └──────────┘  └──────────────┘  └─────────┘  └─────────────────┘  │
└──────────────────────────┬───────────────────────────────────────────┘
                           │ enqueue
                           ▼
┌──────────────────────────────────────────────────────────────────────┐
│                     Background Worker (Polling)                     │
│                                                                     │
│  ┌────────────┐  ┌──────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │ Download   │  │ Extract  │  │ Transcribe   │  │  Analyze &   │  │
│  │ (yt-dlp)   │→ │  Audio   │→ │  (Whisper)   │→ │   Score      │  │
│  └────────────┘  └──────────┘  └──────────────┘  └──────┬───────┘  │
│                                                         │          │
│                  ┌──────────────────────────────────────┐│          │
│                  │        LLM Enrichment (Optional)     ││          │
│                  │  ┌─────────────────────────────────┐ ││          │
│                  │  │ Transcript Insights → Heuristic │ │◄          │
│                  │  │ Candidate Reranking → Hybrid    │ ││          │
│                  │  │ Score Blending (H×0.65 + L×0.35)│ ││          │
│                  │  └─────────────────────────────────┘ ││          │
│                  └──────────────────────────────────────┘│          │
│                                                         ▼          │
│                                              ┌──────────────────┐  │
│                                              │  Render (FFmpeg) │  │
│                                              │  + ASS Subtitles │  │
│                                              └──────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────────┐
│                       Feedback Loop                                 │
│  ┌──────────────┐  ┌────────────────────┐  ┌─────────────────────┐  │
│  │ User Reviews │→ │ Calibration Engine │→ │ Weight Adjustment   │  │
│  │ approve/     │  │ (duration, opening │  │ (heuristic vs LLM   │  │
│  │ reject/fav   │  │  diversity, context)│  │  trust ratio)       │  │
│  └──────────────┘  └────────────────────┘  └─────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
```

---

## LLM Architecture

This is the core of the system and what makes it different from a simple "send text to GPT" approach. The LLM is integrated at **three distinct layers**, each with a specific purpose:

### Layer 1 — Transcript Insights (Pre-Analysis)

Before any candidate is scored, the LLM receives the full transcript (up to 12K chars) and extracts structured editorial intelligence:

```python
# app/services/transcript_insights.py
{
    "main_topics": ["tema 1", "tema 2"],
    "viral_angles": ["ângulo viral"],
    "priority_keywords": ["palavra-chave"],
    "avoid_patterns": ["padrão a evitar"],
    "promising_ranges": [
        {"start_hint_seconds": 30, "end_hint_seconds": 95, "why": "gancho forte"}
    ]
}
```

These insights are **not used for final ranking** — they feed directly into the heuristic scoring engine as additional signal dimensions. This is a **"LLM-guided heuristic"** pattern: the LLM shapes what the deterministic scorer looks for, rather than replacing it.

### Layer 2 — 17-Dimension Heuristic Scoring

Each candidate clip is scored across 17 independent dimensions, each with its own function and tunable weight per niche:

| # | Dimension | What It Measures |
|---|---|---|
| 1 | `hook_score` | Opening strength — questions, power words, weak-start penalties |
| 2 | `opening_strength` | Informative vs. strong openings, contrast creation |
| 3 | `clarity_score` | Word volume fit per mode (short: 35-220 words, long: 350-2500) |
| 4 | `impact_score` | Impact keyword density with capped contribution |
| 5 | `emotion_score` | Emotional keyword density |
| 6 | `closure_score` | Clean endings, substance, weak-end penalties |
| 7 | `continuity_penalty` | Penalizes mid-sentence starts/ends |
| 8 | `format_bonus` | Mode-appropriate formatting (9:16 vs 16:9) |
| 9 | `niche_bonus` | Keyword match against base + learned keywords |
| 10 | `boundary_score` | Pause analysis for clean cut boundaries |
| 11 | `information_density` | Lexical diversity ratio, punctuation density, pacing |
| 12 | `repetition_penalty` | Filler word detection (Portuguese-specific) |
| 13 | `context_dependency` | 20+ dependency patterns ("como eu falei", "isso aqui"…) |
| 14 | `structure_bonus` | Structural markers ("primeiro", "segundo", "passo") |
| 15 | `cta_penalty` | Call-to-action detection and penalization |
| 16 | `transcript_context` | Alignment with LLM-extracted insights from Layer 1 |
| 17 | `feedback_alignment` | Match against historically approved candidate profiles |

After scoring, **diversity reranking** prevents near-duplicate candidates via greedy selection with time overlap + Jaccard text similarity + opening similarity penalties.

### Layer 3 — LLM-as-Judge Reranking

Top-N candidates (default: 12) are sent to the LLM for independent evaluation. The LLM acts as a "judge" that provides its own score, rationale, suggested title, and hook:

```python
# app/services/llm_analysis.py — Hybrid score blending
hybrid_score = (heuristic_score × heuristic_weight) + (llm_score × llm_weight)
# Default: 65% heuristic / 35% LLM
# Dynamically adjusted to 50/50 → 80/20 based on user feedback
```

**The weight split is not static.** The feedback loop (see below) analyzes cases where heuristic and LLM disagree, then adjusts the blend based on which source better predicted the user's approve/reject decisions.

### Resilience Patterns

| Pattern | Implementation |
|---|---|
| **Circuit Breaker** | After 2 consecutive LLM failures, enrichment step auto-skips; pipeline continues with heuristic-only scores |
| **Rate Limit Retry** | 3 attempts with incremental backoff (1.2s, 2.4s, 3.6s) on HTTP 429 |
| **Graceful Degradation** | If LLM fails during niche creation, local keyword extraction runs as fallback |
| **Structured Output Enforcement** | Ollama: `format: "json"` / OpenAI: `response_format: json_object` — both with JSON parse validation |
| **Multi-Provider Abstraction** | Seamless switching between Ollama (local, e.g. Qwen 2.5 7B) and OpenAI via config |

---

## Self-Calibrating Feedback Loop

The system doesn't just score clips — it **learns from editorial decisions** to improve future scoring. This happens through two complementary mechanisms:

### 1. Analysis Calibration (`analysis_calibration.py`)

Builds a calibration profile from reviewed candidates (approved/rejected/favorited) and adjusts:

- **`preferred_short_max_seconds`** — Learns ideal short clip duration from P75 of approved candidates
- **`diversity_penalty_multiplier`** — Increases when rejected candidates share similar openings (duplicate rate ≥ 25%)
- **`informative_opening_multiplier`** — Increases when "informative" openings ("Hoje eu vou falar sobre…") are rejected more than approved
- **`context_penalty_multiplier`** — Increases when context-dependent clips ("Isso que eu falei antes…") are consistently rejected
- **Activation threshold**: Requires ≥ 3 reviews before influencing scores

### 2. Niche Learning (`niche_learning.py`)

Extracts recurring patterns from high-scoring approved candidates:

- **Keyword Learning** — Discovers recurring tokens across ≥ 2 distinct jobs with ≥ 3 occurrences; these learned keywords are fed back into `niche_bonus` scoring
- **Hybrid Weight Tuning** — Analyzes "divergent" candidates (where heuristic and LLM disagree by ≥ 1.2 points) and tracks which source better predicted user approval, then adjusts the heuristic/LLM weight blend accordingly (range: 50/50 to 80/20)
- **Workspace-scoped** — All learning is isolated per workspace, so different content creators develop independent scoring profiles

---

## Pipeline Engineering

### Durable 5-Step Pipeline

```
pending → downloading → extracting_audio → transcribing → analyzing → llm_enrichment → done
```

Each step is tracked as a `JobStep` database record with its own status, attempt count, error history, and timing metadata.

| Feature | Implementation |
|---|---|
| **Pessimistic Locking** | Atomic SQL UPDATE with OR conditions (null lock / stale lock / own lock) prevents race conditions across workers |
| **Worker Identity** | `f"{hostname}:{pid}:{uuid4().hex}"` — enables distributed tracing |
| **Stale Lock Recovery** | Detects abandoned jobs via configurable timeout (default: 1h), resets to pending |
| **Per-Step Retry** | `MAX_STEP_ATTEMPTS = 3` with exhaustion tracking |
| **Cooperative Cancellation** | `_ensure_not_canceled()` polled between steps and within progress callbacks |
| **Idempotent Re-execution** | Each step checks if output already exists and skips — safe restart from any point |
| **Concurrency Control** | Configurable `max_concurrent_pipeline_jobs` with queue position tracking |
| **Progress Heartbeats** | Real-time progress with percentage and human-readable status messages |
| **Self-Healing Queue** | After each job completes/fails, automatically kicks next pending job |

### Incremental Analysis for Long Videos

Videos are split into 900-second chunks with 45-second overlap. Each chunk is independently: segmented → scored → reranked → deduplicated → persisted. Users see first results before the full video is processed.

---

## Tech Stack

| Layer | Technology |
|---|---|
| **API Framework** | FastAPI + Uvicorn |
| **Database** | SQLAlchemy ORM + Alembic migrations (SQLite dev / PostgreSQL prod) |
| **LLM Providers** | OpenAI API, Ollama (local models like Qwen 2.5 7B) |
| **Transcription** | OpenAI Whisper, faster-whisper (CUDA auto-detection, GPU/CPU) |
| **Video Processing** | FFmpeg (clip rendering, audio extraction) |
| **Video Download** | yt-dlp (with cookie/auth support) |
| **Storage** | Local filesystem / Amazon S3 / Cloudflare R2 (Protocol-based abstraction) |
| **Billing** | Stripe, Mercado Pago, Mock (Protocol-based adapter pattern) |
| **Monitoring** | Sentry (error tracking + performance tracing) |
| **Templates** | Jinja2 (server-rendered dashboard with real-time polling) |
| **Testing** | unittest — 244 tests across 30 test files |

---

## Data Model

11 SQLAlchemy models with full relationship mapping:

```
User ──┐
       ├── WorkspaceMember ──── Workspace
       │                            │
       │                    ┌───────┼───────────┬────────────┐
       │                    │       │           │            │
       │                   Job  Subscription  UsageEvent  NicheDefinition
       │                    │
       │          ┌─────────┼──────────┐
       │          │         │          │
       │      Candidate   Clip     JobStep
       │
       └── NicheKeyword (learned keywords per niche)
```

### Key Design Decisions

- **`Candidate` stores 6 sub-scores** individually (`hook_score`, `clarity_score`, `closure_score`, `emotion_score`, `duration_fit_score`, `heuristic_score`) plus `llm_score`, `llm_why`, `llm_title`, `llm_hook` — enabling post-hoc analysis of scoring quality
- **`JobStep` stores JSON details** with heartbeat timestamps, progress messages, and duration metrics — the dashboard reads these for real-time monitoring
- **`NicheDefinition` stores 18 scoring weights as JSON** — fully customizable per niche, per workspace
- **`NicheKeyword` separates `base` vs `learned` sources** — the feedback loop only writes `learned` keywords, preserving original profiles

---

## Project Structure

```
app/
  api/             REST API routes (jobs, billing, files, candidates)
  core/            Configuration (95+ settings with env-specific validation)
  db/              Database connection + session management
  models/          11 SQLAlchemy models with relationships
  schemas/         Pydantic request/response schemas
  services/        41 service modules — pipeline, scoring, LLM, billing, etc.
  templates/       Server-rendered Jinja2 templates (dashboard, job detail, etc.)
  utils/           Media URLs, timecodes, environment helpers
  web/             Web routes, security (CSRF, sessions), template helpers
  worker.py        Standalone background worker process
tests/             244 tests across 30 files
docs/              Technical roadmap
alembic/           Database migrations
scripts/           Utility scripts
```

---

## Getting Started

### Prerequisites

- Python 3.11+
- `ffmpeg` on PATH
- Node.js on PATH (required by yt-dlp)

### Installation

```bash
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
# .venv\Scripts\Activate.ps1     # Windows PowerShell
pip install -r requirements.txt
```

### Configuration

Copy `.env.example` to `.env` and configure:

```env
# Core
ENVIRONMENT=local
DATABASE_URL=sqlite:///./video_cuts.db
SECRET_KEY=dev-secret-change-me

# LLM (choose one)
LLM_PROVIDER=ollama              # Local models via Ollama
LLM_MODEL=qwen2.5:7b
# LLM_PROVIDER=openai            # Or use OpenAI
# OPENAI_API_KEY=sk-...

# Transcription
TRANSCRIPTION_PROVIDER=auto      # auto-detects faster-whisper → openai-whisper
WHISPER_MODEL=base
```

### Running

```bash
# API server
uvicorn app.main:app --reload

# Background worker (separate terminal)
python -m app.worker
```

Open `http://127.0.0.1:8000/` for the web interface.

---

## Testing

```bash
python -m unittest discover -s tests -v
```

**244 tests** covering:

| Test Area | Coverage |
|---|---|
| Scoring engine | All 17 dimensions + diversity reranking |
| Billing | Stripe, Mock, and Mercado Pago adapters + webhook lifecycle |
| Pipeline | Stale lock recovery, retry exhaustion, cancellation |
| Feedback loop | Keyword learning, hybrid weight adjustment |
| Workspace isolation | Cross-workspace data protection |
| Auth & security | Login/logout, sessions, CORS, proxy trust |
| Quotas | Usage limits, billing gating |
| Storage | Local and S3 backends, signed URLs |
| API routes | Job CRUD, candidates, full pipeline integration |
| Web pages | Dashboard rendering, job detail page |

---

## API Reference

<details>
<summary><strong>Infrastructure</strong></summary>

- `GET /health` — Full health check
- `GET /health/live` — Liveness probe
- `GET /health/ready` — Readiness probe

</details>

<details>
<summary><strong>Jobs</strong></summary>

- `POST /jobs/youtube` — Create job from YouTube URL
- `GET /jobs/{id}` — Get job details
- `GET /jobs/{id}/monitor` — Real-time job monitoring
- `POST /jobs/{id}/analyze` — Trigger analysis
- `POST /jobs/{id}/cancel` — Request cancellation
- `GET /jobs/{id}/candidates` — List scored candidates
- `GET /jobs/{id}/clips` — List rendered clips

</details>

<details>
<summary><strong>Rendering</strong></summary>

- `POST /jobs/{id}/render-candidate` — Render specific candidate
- `POST /jobs/{id}/render-approved` — Render all approved candidates
- `POST /jobs/{id}/render-manual` — Render custom time range

</details>

<details>
<summary><strong>Billing</strong></summary>

- `GET /api/billing/status` — Billing overview
- `POST /api/billing/checkout?plan=starter` — Start checkout
- `POST /api/billing/webhook` — Payment webhook
- `POST /api/billing/cancel` — Cancel subscription

</details>

---

## Roadmap

Current status: **6 milestones delivered**, preparing for closed beta.

- [x] Multi-tenant SaaS (accounts, workspaces, data isolation)
- [x] Production database (PostgreSQL + Alembic migrations)
- [x] Durable pipeline (workers, queue, recovery)
- [x] Private storage (S3/R2 + signed URLs + retention)
- [x] Billing integration (Stripe + Mercado Pago)
- [x] Operational dashboard (monitoring, heartbeat, progress)
- [ ] Staging validation with real videos
- [ ] Closed beta (3-5 users)
- [ ] Sentry observability integration
- [ ] Performance optimization (connection pooling, async metadata)

---

## License

This project is proprietary and not licensed for redistribution.

---

<div align="center">

**Built with** Python · FastAPI · SQLAlchemy · Whisper · OpenAI · Ollama · FFmpeg

</div>
