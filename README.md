# LeadIntel

Local business acquisition & website-opportunity intelligence platform: discovers local businesses,
evaluates their web presence against industry-specific benchmarks, and ranks them as website-development prospects.

Status: planning (free-first architecture, v0.2). See [docs/PLAN.md](docs/PLAN.md) for research, cost inventory, architecture and phased roadmap.

Planned layout:

```
backend/    FastAPI + PostGIS + Procrastinate workers (discovery, crawl, analysis, scoring)
frontend/   React + Vite + TypeScript + Tailwind, interactive map
docs/       plan, scoring methodology, data-source notes
```

## Backend quick start

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest                      # offline test suite
.venv/bin/leadintel net-check                   # live network diagnostics (real requests, no paid APIs)
.venv/bin/uvicorn app.api.main:create_app --factory   # http://localhost:8000/diagnostics/network
```

Network requirements, fallbacks and egress safety: [docs/NETWORK.md](docs/NETWORK.md).
