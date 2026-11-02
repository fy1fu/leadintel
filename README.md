# LeadIntel

Local business acquisition & website-opportunity intelligence platform: discovers local businesses,
evaluates their web presence against industry-specific benchmarks, and ranks them as website-development prospects.

Status: planning. See [docs/PLAN.md](docs/PLAN.md) for research, architecture, scoring methodology and phased roadmap.

Planned layout:

```
backend/    FastAPI + PostGIS + Procrastinate workers (discovery, crawl, analysis, scoring)
frontend/   React + Vite + TypeScript + Tailwind, interactive map
docs/       plan, scoring methodology, data-source notes
```
