# Publish manifest

**Purpose:** define the exact curated public push set and the workspace-only material that must stay out of a GitHub push. This is a staging policy, not evidence that a Git commit or push occurred. The current Arena snapshot has no `.git/` directory or configured remote; no repository has been pushed.

Do not use a blanket `*.py` or `src/` ignore. Product source, migrations, tests, frontend source, and source-only developer utilities remain publishable. The exclusions below target secrets, raw/generated/private data, runtime outputs, and historical/internal documentation.

## Push to the public repository

### Root files

- `README.md`
- `.env.example` (sanitized placeholders only)
- `.gitignore`
- `.gitattributes`
- `.dockerignore`
- `SECURITY.md`
- `pyproject.toml`
- `alembic.ini`
- `compose.yaml` (local-only PostgreSQL service; do not expose its development defaults)
- `Dockerfile.api`
- `Dockerfile.worker`

No `LICENSE` file exists. Do not create or infer one from dependencies, source materials, or dataset terms.

### Curated public documentation

Only these Markdown documents under `docs/` are public:

- `docs/ARCHITECTURE.md`
- `docs/RUNBOOK.md`
- `docs/DEPLOYMENT.md`
- `docs/SECURITY-AND-PRIVACY.md`
- `docs/API.md`
- `docs/PUBLISH-MANIFEST.md`

### Product and test code

- `src/` — all application/API/worker/persistence/ingestion/evidence/core source.
- `migrations/` — Alembic environment, template, and migration revisions.
- `tests/` — Python test source. Data-backed research tests may require workspace-only assets and are not represented as a clean-install public acceptance suite; see the Runbook.
- `web/.gitignore`, `web/index.html`, `web/package.json`, `web/package-lock.json`, `web/tsconfig.json`, `web/vite.config.ts`, and `web/src/` — source and reproducible frontend manifests only.

### Source-only utilities

- `scripts/*.py` — Python source; no downloaded payloads, credentials, private caches, or generated outputs.
- `eval/*.py` — evaluation source only.
- `bench/**/*.py` — benchmark/tool source only.
- `bench/ml/model_a/config.json` and `bench/ml/model_a/requirements.lock` — source-side Model A research configuration only; research code is not part of production money/verdict decisions.

Within `bench/`, the following are **not** in the push set: top-level research Markdown, `cases/`, `dataset/`, `results/`, and `reports/`. Their generators/source modules remain distinct and publishable.

## Workspace-only exclusions and reasons

| Path or pattern | Reason it must not be pushed |
|---|---|
| `.env`, `.env.*` except `.env.example` | Local credentials and deployment secrets. |
| `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.der` | Private keys/certificates. |
| `*.db`, `*.db-*`, `*.sqlite`, `*.sqlite3`, WAL/SHM files, `/runtime/`, `/storage/`, `/uploads/`, `/private-storage/` | Runtime databases, workspace dataset-index catalogs, local state, uploaded source PDFs, and private artifacts. |
| `/data/` | Dataset/corpus snapshots, acquired source documents, annotations, and research provenance material not curated for public release. |
| `/cases/` | Local/demo/Golden fixtures and generated case inputs; not needed for the public product path. |
| `/review/` | Annotation/review page images and related private or unreviewed source material. |
| `/models/` | Model weights/checkpoints and generated artifacts not required by the app. |
| `/bench/cases/`, `/bench/dataset/` | Synthetic/derived benchmark cases and evaluation datasets. |
| `/bench/results/`, `/bench/reports/`, `/bench/*.md`, `/eval/results/` | Generated result bundles and historical research/evaluation reports. |
| `/Makefile` | Legacy internal gold-annotation/research workflow targets that assume workspace-only data; public commands are in the Runbook. |
| `/archive/` | Workspace preservation/archive area. Historical files are retained, not deleted. |
| `/ARENA-HANDOFF*`, `/MODEL-ARTIFACT-MANIFEST.json` | Internal handoff/annotation/model metadata and compatibility symlinks. |
| `/docs/internal/`, `/docs/history/`, `/docs/handoff/`, `/docs/research/`, `/docs/qa/`, `/docs/ml/`, `/docs/archive/` | Internal curation index, historical reports, Antigravity handoffs, research, QA, and model material. |
| `/docs/architecture/`, `/docs/data/`, `/docs/datasets/`, `/docs/decisions/`, and the old root-level docs symlinks | Compatibility aliases into internal history/research. They are not curated public docs. |
| `/docs/ANTIGRAVITY-ANNOTATION-MANIFEST.json` | Workspace-only annotation and source metadata. |
| `web/node_modules/`, `web/dist/`, `web/.vite/`, any `node_modules/`, Python virtualenvs, bytecode, test/tool caches | Dependency installs and generated build/cache output. |
| `*.pdf`, `*.parquet`, `*.jsonl`, `*.jsonl.zst`, temporary/cropped images | Raw documents, dataset shards, generated evaluation material, or temporary image artifacts. |

The current compatibility symlinks keep old filesystem paths resolving from the workspace; they do not make the target documents public. Relative Markdown links in preserved files were audited from their physical destination paths. A viewer that resolves a file opened through an old symlink using the alias path as its relative-link base may show different link behavior; use the physical archive path for browsing historical documents. The three frozen documents `docs/architecture/DATA-MODEL.md`, `docs/architecture/ML-TRAINING-ARCHITECTURE.md`, and `docs/datasets/BILL-HEAD-ONTOLOGY-v0.1.md` must remain regular files at their manifest-locked legacy paths in the workspace; curated public documentation does not supersede them.

## Ignore-file policy

`.gitignore` enforces the principal exclusions above while leaving product/source files, tests, migrations, lockfiles, and curated docs eligible for version control. Review every new binary, data file, symlink, or build output against this manifest before staging. Do not `git add -f` an excluded path without separate explicit approval.

## Release checklist

- [ ] Confirm the root `.env` is absent and `.env.example` contains placeholders only.
- [ ] Confirm no raw/customer PDFs, datasets, model weights, runtime DBs, review images, or generated outputs are staged.
- [ ] Confirm only the six public `docs/*.md` guides are included.
- [ ] Confirm the source-only utility code is not bundled with data/results.
- [ ] Run the focused checks from [Runbook](RUNBOOK.md) and record exact outcomes; PostgreSQL/browser/AWS status must remain explicit.
- [ ] Create a Git repository/remote and push only after an authorized maintainer reviews the staged file list; no push is performed by this Arena task.
