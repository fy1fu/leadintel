# LeadIntel

Local business acquisition & website-opportunity intelligence platform: discovers local businesses,
evaluates their web presence against industry-specific benchmarks, and ranks them as website-development prospects.

Status: Phase 1 discovery, provisional BOS/LPS scoring and the salesperson interface are built; website analysis (WQS),
CRM and team features are planned. Progress: [docs/DELIVERY_STATUS.md](docs/DELIVERY_STATUS.md). Research, cost
inventory and the long-term roadmap: [docs/PLAN.md](docs/PLAN.md).

Layout (as built):

```
backend/             FastAPI + SQLAlchemy + PostgreSQL/PostGIS; discovery, scoring and the API run in-process (no worker queue)
backend/app/api/static/   the web interface: plain HTML/CSS/JavaScript, MapLibre map (no build step)
packaging/windows/   Friend Edition: per-user Windows installer, launcher, data loading, ZIP builder
docs/                plan, scoring methodology, data-source notes, verification records
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
