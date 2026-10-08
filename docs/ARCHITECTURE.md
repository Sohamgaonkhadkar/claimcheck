# Architecture

CLAIMCHECK is an evidence-first health-insurance settlement review service. The frontend, API, worker, PostgreSQL persistence, and deterministic trust core are implemented; the product architecture is frozen for runtime verification.

## Runtime shape

```text
React + TypeScript (web/)
        │ same-origin /api requests
        ▼
FastAPI API (src/claimcheck/api/)
        ▼
Application workflows (src/claimcheck/application/)
        ├── PostgreSQL metadata + durable document jobs
        ├── private storage adapter (currently local filesystem)
        └── claimcheck-worker (separate process)
                    │
             PDF extraction + evidence
                    │
   explicit roles + append-only human review
                    │
             readiness / trust gates
                    ▼
          TrustedCaseAdapter
                    ▼
          StructuredCase
                    ▼
      deterministic pipeline
  graph → rules/calculation → reconciliation → verdict
                    ▼
      persisted AnalysisRun + API result
```

## Responsibilities and boundaries

| Component | Responsibility |
|---|---|
| **Frontend** | Uploads PDFs, shows API-backed processing/review/results, and displays source context. It does not calculate claim amounts. |
| **API** | Owner-scoped HTTP resources, validation, workflow orchestration. It does not implement a second calculation engine. |
| **Application layer** | Case/document workflows, review/readiness, `TrustedCaseAdapter`, `input_revision`, and analysis persistence. |
| **Worker** | Claims durable PostgreSQL `DOCUMENT_PROCESS` and `DOCUMENT_DELETE` jobs using leases/retries; it does not execute the current synchronous analysis endpoint. |
| **Persistence** | PostgreSQL-only SQLAlchemy models and owner-scoped repositories. SQLite is rejected. |
| **Storage** | `PrivateLocalFileStorage` keeps source and run-scoped derived files outside the web root. A private object-storage adapter is a future deployment substitution, not implemented here. |
| **TrustedCaseAdapter** | Revalidates source hashes, current role assignments, effective reviewed values, and required evidence before creating the typed core input. It does not analyze the claim. |
| **Deterministic core** | `graph/`, `calc/`, `rules/`, `reconcile/`, `verdict/`, and `pipeline.py` own typed calculations, reconciliation, rule gates, and verdict semantics. |

## Trust and result lifecycle

```text
PDF → candidate extraction → explicit human review
    → readiness → TrustedCaseAdapter → StructuredCase
    → deterministic analysis → persisted revision-pinned result
```

Candidate extraction is not automatically truth. Missing, unreadable, conflicting, and unverified values remain distinct. Human corrections are append-only. Each `AnalysisRun` records the `input_revision` it used; changing inputs does not rewrite a prior result. The result projection avoids raw PDF bytes, storage paths, storage keys, and source quotes.

## Persistence and migrations

PostgreSQL is required for cases, documents, durable jobs, processing runs, review events, input revisions, analysis runs, and audit metadata. The checked-in Alembic chain has one head: **`b31f5c2a9e70`**. Production startup does not call `metadata.create_all`; apply Alembic migrations explicitly before serving traffic.

## Authentication and origins

The API resolves an owner through an injected identity-provider boundary. The repository includes only a fixed development identity when `CLAIMCHECK_ENV=development`; production mode does not include a provider by default and returns `401` for case resources until one is injected. Do not use the development identity for real users.

CORS is opt-in through `CLAIMCHECK_CORS_ORIGINS`: exact HTTPS origins only, with HTTP limited to localhost in development. No wildcard or credentialed cross-origin access is enabled. Same-origin hosting or the local Vite proxy needs no CORS configuration.

## AI / ML boundary

The production monetary and verdict path is deterministic; Model A is a disconnected research-only proposal adapter over controlled synthetic data. It does not run in the production worker and cannot replace an accepted deterministic result. Customer uploads are not automatically fed into training.

## Deployment constraint

The architecture is not being redesigned. The remaining technical gate is disposable-PostgreSQL integration/runtime validation, followed by browser E2E and deployment work. See [Deployment](DEPLOYMENT.md) and [Runbook](RUNBOOK.md).
