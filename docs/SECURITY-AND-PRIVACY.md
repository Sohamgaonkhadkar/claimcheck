# Security and privacy

CLAIMCHECK handles health-insurance paperwork that may contain sensitive personal and health information. The repository is a prototype; the controls below describe implemented boundaries, not a security certification or a claim of regulatory compliance.

## Identity and tenant isolation

- Case resources are owner-scoped using the principal injected by a server-side identity-provider boundary. Clients cannot choose the owner in a body or header.
- Unowned and cross-owner resources return the same safe not-found behavior. Document bytes, fields, evidence, jobs, review state, and analysis runs follow the same ownership boundary.
- A fixed development identity exists only when `CLAIMCHECK_ENV=development`. A production identity provider is **not included**. Do not serve real users until one is integrated, reviewed, and verified.

## Documents and private storage

- The current adapter is `PrivateLocalFileStorage`. It stores original uploads and derived files outside the web root, uses opaque keys internally, restricts the storage root and file permissions, and rejects unsafe path/symlink traversal.
- The API never returns filesystem paths or opaque storage keys. The authorized document-content route streams PDF bytes only after owner checks and sends `Cache-Control: private, no-store` and `X-Content-Type-Options: nosniff`.
- Uploads are bounded by byte and page limits, accepted as PDFs only, and validated before persistence. Password-protected, malformed, unsupported, oversized, or over-page-limit files are rejected with stable safe errors.
- The original upload and processing artifacts carry SHA-256 provenance. Before a case becomes trusted, the adapter rechecks that the selected source still exists and its hash matches. A failed verification blocks trust; hashes are integrity checks, not encryption.
- The current filesystem adapter is for private local operation. Production hosting needs encrypted private storage, controlled access, backups, retention, and deletion behavior appropriate to its environment. A future private S3/object-storage adapter can sit behind the existing storage port; no S3 client or migration is implemented here. API and worker must use the same storage service/root.

## Review, provenance, and analysis

- Extracted values are candidates, not truth. Material review decisions record the selected document/run, evidence, reviewer action, verification method, and reason where required.
- Corrections and role assignments are append-only events; changing a value does not overwrite prior history. Processing runs are versioned. Analysis results record the `input_revision` used and remain readable as historical results.
- Analysis is blocked until the explicit trust/readiness path accepts its inputs. This does not guarantee the result is complete or correct; users must check supporting documents and obtain qualified advice where needed.
- The application does not automatically train on customer uploads, feed them into Model A, or add them to a dataset. Any future training use requires a separate controlled process with appropriate consent/legal basis, de-identification, access controls, and validation.

## Deletion and retention

- Document deletion is a durable `DOCUMENT_DELETE` job. The API returns `202`; the worker removes the original and run-scoped extracted artifacts and records completion. Operators must monitor failed/retried jobs and storage lifecycle behavior rather than treating request acceptance as deletion completion.
- Case deletion is also tracked through durable cleanup work. Deployment-specific backups, replicas, snapshots, and provider retention are not erased by this application code; define and test those retention procedures separately.
- Keep the API and worker restricted to private networks where possible, protect `DATABASE_URL` and identity-provider secrets using a secret manager, and do not commit `.env`, runtime databases, uploads, or generated artifacts.

## Deployment gates and non-claims

Before production use, integrate and test a real identity provider, PostgreSQL permissions and backups, encrypted private storage, HTTPS termination, secret rotation, monitoring/alerting, retention/deletion across backups, incident response, and browser/API security testing. CORS only controls browser origin access; it is not authentication. No legal, regulatory, privacy, or security compliance certification is claimed.

See [Deployment](DEPLOYMENT.md) for the preparation checklist and [API](API.md) for resource boundaries.
