# API

The service is a FastAPI application. Its OpenAPI and interactive docs are enabled only in explicit development mode (`/openapi.json` and `/docs`); production disables those routes. This page summarizes the public product surface, not internal development helpers.

## Base paths and authentication

- Liveness: `GET /healthz` (no database dependency).
- Readiness: `GET /readyz` (requires PostgreSQL and a writable private-storage adapter).
- Product resources: `/api/v1`.
- Case, document, job, review, evidence, and analysis resources are scoped to the authenticated owner resolved by the server-side identity-provider boundary. Clients cannot select an owner by request body or header. Until a production identity provider is injected, production case requests return `401`.
- Unowned or cross-owner resources use safe not-found behavior; responses do not disclose another owner's existence.

## Resource operations

| Method | Path | Behavior |
|---|---|---|
| `POST` | `/api/v1/cases` | Create an owned case (`201`). |
| `GET` | `/api/v1/cases` | List owned cases; bounded pagination and optional status filter. |
| `GET` | `/api/v1/cases/{case_id}` | Get one owned case. |
| `PATCH` | `/api/v1/cases/{case_id}` | Update permitted case metadata. |
| `DELETE` | `/api/v1/cases/{case_id}` | Request case deletion; asynchronous cleanup is tracked as a job (`202`). |
| `POST` | `/api/v1/cases/{case_id}/documents` | Validate and privately store a PDF, then enqueue processing (`202`). |
| `GET` | `/api/v1/cases/{case_id}/documents` | List owned document metadata. |
| `GET` | `/api/v1/cases/{case_id}/documents/{document_id}` | Get document metadata and processing-run summaries. |
| `GET` | `/api/v1/cases/{case_id}/documents/{document_id}/content` | Stream the original PDF only after owner-scoped authorization; private, non-cacheable response. |
| `DELETE` | `/api/v1/cases/{case_id}/documents/{document_id}` | Queue deletion of the source and derived extraction artifacts (`202`). |
| `POST` | `/api/v1/cases/{case_id}/documents/{document_id}/reprocess` | Queue a new immutable processing run (`202`); optional `Idempotency-Key`. |
| `GET` | `/api/v1/cases/{case_id}/documents/{document_id}/fields` | Read fields for a selected or specified processing run. |
| `GET` | `/api/v1/cases/{case_id}/evidence/{evidence_id}` | Read an owner-scoped evidence span. |
| `GET` | `/api/v1/cases/{case_id}/processing-status` | Read case processing status. |
| `GET` | `/api/v1/cases/{case_id}/jobs` | List case jobs; bounded pagination and filters. |
| `GET` | `/api/v1/cases/{case_id}/jobs/{job_id}` | Get an owned job. |
| `GET` | `/api/v1/cases/{case_id}/readiness` | Evaluate the review/trust gate and outstanding requirements. |
| `GET` | `/api/v1/cases/{case_id}/review-queue` | Get explicit human-review items. |
| `POST` | `/api/v1/cases/{case_id}/review-corrections` | Append a provenance-checked human review event (`201`); prior events are not overwritten. |
| `POST` | `/api/v1/cases/{case_id}/document-role-assignments` | Version an explicit document-role/source selection (`201`). |
| `POST` | `/api/v1/cases/{case_id}/analyze` | Analyze only a trusted, ready revision and persist an `AnalysisRun` (`201`). |
| `GET` | `/api/v1/cases/{case_id}/analysis` | List owner-scoped analysis runs. |
| `GET` | `/api/v1/cases/{case_id}/analysis/{analysis_run_id}` | Read one safe, persisted analysis result. |

FastAPI's request and response models remain the source of truth for individual fields and validation rules. Responses include a request ID; the HTTP response also carries `X-Request-ID`. Successful creates that expose a resource location use `Location` where implemented.

## Workflow invariants

1. Upload accepts bounded PDFs only. The source is privately stored and hashed with SHA-256 before the worker processes it.
2. Extraction produces candidates and evidence; it does not make them trusted facts.
3. A human explicitly reviews or corrects load-bearing values and assigns document roles. Corrections are append-only and retain provenance.
4. The readiness gate and `TrustedCaseAdapter` re-check ownership, selected sources, accepted review state, and source-hash integrity before constructing a typed core input.
5. Analysis runs the existing deterministic calculation, reconciliation, rule, and verdict path. Each result is persisted against the `input_revision` used.
6. Reprocessing creates a new immutable processing run. It does not rewrite prior runs or automatically re-analyze a case.
7. API projections do not expose local filesystem paths or opaque storage keys. The explicit authorized PDF-content endpoint returns bytes, not a path.

## Errors and safe behavior

Errors use stable codes and safe details with a request ID. The API normalizes framework validation and unexpected errors; parser/OS exception details are not returned as upload errors. Missing and unowned resources are intentionally indistinguishable. Upload and deletion requests can return `202`; clients should poll processing status or the job resource rather than assume the work is finished.

## Cross-origin use

`CLAIMCHECK_CORS_ORIGINS` is empty by default. Configure exact HTTPS origins only, with HTTP restricted to localhost in development. Wildcards and credentialed CORS are not supported. CORS does not authenticate callers. See [Deployment](DEPLOYMENT.md).
