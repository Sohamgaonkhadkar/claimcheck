# CLAIMCHECK

An evidence-first health-insurance settlement review service that protects policyholders from unexplained deductions by linking deterministic financial arithmetic to explicit human-verified document provenance.

CLAIMCHECK organizes policy wording, a policy schedule, a hospital bill, and a settlement/rejection letter into a reviewed, provenance-aware reconstruction. It explicitly separates AI candidate extraction from mathematical truth: models merely propose inputs, humans explicitly verify them against original source PDFs, and deterministic application code executes the final financial calculation. This completely eliminates AI hallucination from the authoritative financial math.

--------------------------------------------------
## The Problem
--------------------------------------------------

When an Indian policyholder is discharged from the hospital, they receive a settlement or rejection letter. The amount paid is frequently below expectation, but the letter rarely explains the exact arithmetic or policy clause used to justify deductions.

Partial disallowance is the quiet majority of lost money. Deductions can come from multiple independent rules: non-payable items, room-rent limits, proportionate reductions, co-pays, and deductibles. A settlement letter often groups these into opaque buckets. 

To audit the payout, a user must connect a deducted amount to:
- a specific bill line
- a specific policy clause
- a limit in the schedule
- an insurer-paid amount

While escalation mechanisms exist (Grievance Officers, Bima Bharosa, the Insurance Ombudsman), they are built for people who already know exactly what to allege and can prove it with figures and clauses. Most policyholders simply pay the difference.

--------------------------------------------------
## The Core Idea
--------------------------------------------------

CLAIMCHECK's central insight is that **a large part of a settlement review can be represented as an explicit chain of evidence-backed calculations**.

**EXTRACTION ≠ TRUTH**

A parser or Language Model may propose a monetary amount, a policy limit, a deduction, or a boolean condition. But a candidate does not become authoritative simply because a parser found it. CLAIMCHECK explicitly separates candidate extraction from deterministic analysis.

```text
Customer PDFs
    ↓
Document validation
    ↓
Text extraction + candidate fields
    ↓
Evidence spans + provenance
    ↓
Human verification / correction
    ↓
Trusted structured case
    ↓
Deterministic reconstruction
    ↓
Reconciliation
    ↓
Findings + uncertainty + next questions
```

--------------------------------------------------
## Product Workflow
--------------------------------------------------

### 1. Case creation
A Case represents a single hospital admission and settlement event for a single policyholder. It safely scopes documents, jobs, and analysis runs.

### 2. Document upload
Users upload the four primary documents:
- Policy wording
- Policy schedule
- Hospital bill
- Settlement/rejection letter

PDF validation ensures safe ingest: it checks file extensions, structure signatures, page limits, and safe filenames. Files are uniquely hashed (SHA-256) and saved safely into private storage.

### 3. Background processing
PDF extraction is heavy and unpredictable, so it runs asynchronously. Durable processing jobs are dispatched to a background worker. The worker manages leasing and retries, parses pages, extracts candidate fields, and generates highlightable evidence spans.

### 4. Evidence formation
Extracted values are strictly linked to the original document, the page, the exact source text/span, and the extraction method. This provenance prevents untethered numbers from entering the system.

### 5. Human review
The user enters the Evidence UI: **Candidate → Evidence → Human decision**.
The user must explicitly act on each extracted candidate:
- **Confirm**: The value matches the document.
- **Correct**: The value is wrong, and the user selects the right span.
- **Can't verify**: The document lacks the information.

Review corrections are append-only. They never silently overwrite the raw extraction, preserving full auditability.

### 6. Readiness / trust gate
Analysis cannot simply run because candidate fields exist. The Trusted Adapter checks whether all required inputs are present, properly typed, explicitly verified by a human, attached to the correct case, and supported by trusted provenance. Only then does it emit a `TrustedCase`.

### 7. Deterministic reconstruction
Once a case is trusted, **deterministic application code** owns the entire calculation pipeline:
- financial arithmetic
- policy limits
- deductions
- co-pay
- reconciliation
- verdict semantics

--------------------------------------------------
## Why Deterministic Calculation?
--------------------------------------------------

LLMs and parsers can assist with locating and proposing information, but financial settlement arithmetic must be reproducible, inspectable, deterministic, testable, and auditable.

Therefore:
- **Models/parsers**: READ / LOCATE / NORMALIZE / PROPOSE
- **Deterministic application logic**: DECIDE / CALCULATE / RECONCILE / ASSIGN VERDICT

This architectural boundary prevents a language model from inventing financial values, ignoring a deductible, or silently changing arithmetic to reach a pleasing answer.

--------------------------------------------------
## Exact Monetary Arithmetic
--------------------------------------------------

CLAIMCHECK represents all monetary values internally as integer paise.

For example:
- **₹5,00,000** → `50,000,000 paise`
- **1% room-rent limit (₹5,000)** → `500,000 paise`
- **Settlement amount (₹86,638.50)** → `8,663,850 paise`

This guarantees exact monetary representation inside the application. It prevents binary floating-point ambiguity, allows exact reconciliation, and accurately preserves edge cases like `.50` paise. 

--------------------------------------------------
## Policy-Limit Normalization
--------------------------------------------------

A policy schedule often defines limits relatively. For example:
- Sum insured = ₹5,00,000
- Room rent = 1% of sum insured

CLAIMCHECK normalizes these relative rules into absolute bounds. The normalized room limit becomes ₹5,000/day. This normalization converts a percentage-based policy statement into a concrete monetary boundary *before* it enters the deterministic calculator.

--------------------------------------------------
## Claim Reconstruction
--------------------------------------------------

The deterministic calculator transforms the trusted inputs into an explicit sequence of calculation steps:

```text
Gross hospital bill
        ↓
Non-payable deductions
        ↓
Policy-specific limits (e.g. Room Rent)
        ↓
Eligible/admissible amount
        ↓
Co-pay / other supported deductions
        ↓
Reconstructed payable amount
        ↓
Compare with insurer-paid amount
        ↓
Reconciliation
```

Every calculation step generates a traceable identifier and output, so the final result can be mathematically explained back to the user.

--------------------------------------------------
## Reconciliation
--------------------------------------------------

Reconciliation compares the **reconstructed payable amount** against the **amount paid** by the insurer. A difference is not automatically evidence of insurer wrongdoing.

A discrepancy is categorized precisely:
- **Supported difference**: The insurer's deduction matches the policy math.
- **Potentially inconsistent difference**: The insurer deducted more than the math supports.
- **Unexplained difference**: The gap cannot be explained because the settlement letter lacks detail or relies on unpublished standards.

--------------------------------------------------
## Result States
--------------------------------------------------

CLAIMCHECK yields one of three core interpretation states for any deduction:

### 1. SUPPORTED
Evidence and deterministic rules support the deduction/calculation. 
*(Note: This does not mean "legally proven correct"; it means the math aligns structurally with the uploaded text).*

### 2. POTENTIALLY_INCONSISTENT
The available evidence and deterministic calculation indicate a discrepancy or structural inconsistency that deserves attention. 
*(Note: This does not automatically mean illegal, wrongful, or fraudulent).*

### 3. UNDETERMINED
The available documents/evidence are insufficient to reach a deterministic conclusion (e.g. unpublished "Reasonable and Customary" rates). "Undetermined" is a legitimate and important result rather than a system failure.

--------------------------------------------------
## Finding Types
--------------------------------------------------

The deterministic core emits specific typed findings:
- **FINANCIAL**: A monetary deduction or limit (e.g., room rent capped).
- **PROCEDURAL**: A workflow or administrative issue.
- **EVIDENCE_GAP**: A missing rationale in the settlement letter (e.g., deduction without a named cause).

--------------------------------------------------
## Evidence & Provenance Model
--------------------------------------------------

Provenance is the backbone of CLAIMCHECK.

`Document` → `Processing run` → `Page` → `Evidence span` → `Candidate field` → `Review decision` → `Trusted field` → `Analysis run` → `Calculation step`

This strict chain prevents unsupported values from entering the deterministic calculator. Every `Trusted field` must be explicitly scoped to a verified `Evidence span`. The original PDF is retained as the immutable source artifact, ensuring the review always remains linked to the original evidence.

--------------------------------------------------
## Data Model
--------------------------------------------------

The PostgreSQL database maintains durable product state:
- **Case**: The root bounding boundary.
- **Document**: The raw PDF and its hash identity.
- **ProcessingJob / Run**: The async work tracker.
- **PageExtraction / EvidenceSpan**: The coordinates and raw text of parser candidates.
- **ReviewCorrection**: The append-only log of human decisions.
- **InputRevision**: The snapshot of trusted inputs.
- **AnalysisRun**: The deterministic execution log.

--------------------------------------------------
## System Architecture
--------------------------------------------------

```text
                    ┌─────────────────────┐
                    │    React / Vite     │
                    │    Evidence UI      │
                    └──────────┬──────────┘
                               │ HTTPS / API
                               ▼
                    ┌─────────────────────┐
                    │      Nginx          │
                    │ static + reverse    │
                    │      proxy          │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │   FastAPI API       │
                    │ application layer   │
                    └──────┬──────┬───────┘
                           │      │
               ┌───────────┘      └─────────────┐
               ▼                                ▼
       ┌────────────────┐              ┌────────────────┐
       │  PostgreSQL    │              │ Private PDF    │
       │ product state  │              │ storage        │
       └────────────────┘              └────────────────┘
                           ▲
                           │
                    ┌──────┴───────┐
                    │ Python Worker│
                    │ async jobs   │
                    └──────────────┘
```

--------------------------------------------------
## Component Responsibilities
--------------------------------------------------

### Frontend
- Provides the evidence-first review UI.
- Driven entirely by backend API state.
- Strictly performs **no** client-side financial calculations.

### FastAPI
- Handles document uploads and validates file types.
- Orchestrates processing by dispatching to the worker.
- Serves the review and analysis APIs.

### Worker
- Owns durable job processing and leasing behavior.
- Executes heavy PDF text layer extraction.
- Manages state transitions for processing jobs.

### PostgreSQL
- Persists all domain state (Cases, Documents, Jobs, Evidence, Corrections, Analysis Runs).

### Private Storage
- Secures the original uploaded PDFs on disk.

### Deterministic Core (`claimcheck.calc`, `claimcheck.verdict`)
- Owns the schema logic and rule evaluations.
- Executes the calculation graph and reconciliation.
- Emits the final verdict and reporting findings.

--------------------------------------------------
## Security & Trust Boundaries
--------------------------------------------------

- Uploaded PDFs are entirely private; storage is never exposed via public web content.
- Cases and documents are strictly scoped to the owner.
- Safe API errors are returned without leaking internal tracebacks.
- The `TrustedCase` gate ensures unverified documents never execute.

--------------------------------------------------
## API Architecture
--------------------------------------------------

The API boundaries are clean and RESTful:
- `/api/v1/cases`: Case lifecycle and ownership.
- `/api/v1/cases/{id}/documents`: PDF upload, validation, and content fetching.
- `/api/v1/cases/{id}/readiness`: Checks if the case is ready for analysis.
- `/api/v1/cases/{id}/review-queue`: Fetches extracted candidates requiring human verification.
- `/api/v1/cases/{id}/review-corrections`: Accepts append-only human verifications.
- `/api/v1/cases/{id}/analyze`: Triggers the deterministic calculator over the trusted case inputs.

--------------------------------------------------
## Frontend Workflow
--------------------------------------------------

The UI is an evidence-first workflow, **not** a chatbot:
`Landing` → `Documents Upload` → `Processing` → `Human Review (Evidence UI)` → `Analysis Results`

--------------------------------------------------
## Testing & Validation
--------------------------------------------------

The repository contains 423 tests encompassing:
- Deterministic core rules and integer paise preservation.
- Parser and extraction logic.
- Provenance tracking and trust-gate enforcement.
- Upload structure validation.
- End-to-end API vertical slices.

### End-to-End Validation
The system successfully completed a full 4-PDF E2E scenario targeting Indian health policies:
- Sum insured: ₹5,00,000
- Room rent rule: 1% of sum insured
- Normalized room rent limit: ₹5,000/day
- Settlement final payable: ₹86,638.50

In this validation, 4 PDFs were ingested, extracted, manually reviewed, seamlessly reconstructed deterministically, and accurately reconciled to exactly `.50` paise without truncation.

--------------------------------------------------
## ML / AI Approach
--------------------------------------------------

CLAIMCHECK's authoritative financial path is entirely deterministic.

The repository supports research models (e.g. Model A Challenger), but these are isolated proposals trained on controlled synthetic data. They are **not** authoritative and are **not** connected to the production worker's final financial verdicts.

--------------------------------------------------
## Data & Evaluation Limitations
--------------------------------------------------

- Public datasets matching Indian health-insurance claims are severely limited.
- The project does not use generic receipt OCR datasets as claim-ground-truth.
- Extractor capabilities should be evaluated considering the complexity and varying formatting of Indian insurance letters.

--------------------------------------------------
## Why This Architecture?
--------------------------------------------------

| Design choice | Why |
|---|---|
| **Deterministic arithmetic** | Exact, reproducible financial calculations |
| **Integer paise** | Avoid monetary floating-point ambiguity |
| **Evidence spans** | Trace values back to source documents |
| **Explicit human review** | Prevent extracted candidates from becoming truth |
| **Trusted adapter/gate** | Stop incomplete inputs from reaching analysis |
| **PostgreSQL** | Durable product state and auditability |
| **Background worker** | Keep heavy PDF processing out of request path |
| **Private local storage** | Uploaded PDFs are not public assets |
| **Modular monolith** | Strong boundaries without unnecessary distributed-system complexity |

--------------------------------------------------
## Deployment
--------------------------------------------------

**AWS Competition Deployment Target:**
- **Region**: `ap-south-1`
- **Host**: Single EC2 Instance (`t3.small`)
- **OS**: Amazon Linux 2023
- **Services**: Nginx, FastAPI, Python Worker, PostgreSQL (Native)
- **Frontend**: Vite production build served through Nginx
- **Storage**: `/var/lib/claimcheck/private-storage`

**Request Path:**
`Browser` → `Nginx` (Serves React assets / Reverse Proxies API) → `FastAPI` ↔ `PostgreSQL` / `Private Storage` / `Worker`

--------------------------------------------------
## Local Development
--------------------------------------------------

1. **Environment**: Copy `.env.example` to `.env`.
2. **Database**: Run PostgreSQL locally (e.g., via `docker-compose up -d`).
3. **Migrations**: 
   ```bash
   alembic upgrade head
   ```
4. **API**: 
   ```bash
   python -m uvicorn claimcheck.api.app:app --port 8000 --reload
   ```
5. **Worker**:
   ```bash
   claimcheck-worker
   ```
6. **Frontend**:
   ```bash
   cd web
   npm install
   npm run dev
   ```
7. **Frontend Build**:
   ```bash
   cd web
   npm run build
   ```

--------------------------------------------------
## Repository Structure
--------------------------------------------------

```
src/claimcheck/
  api/          # FastAPI routes, auth, schemas, and dependencies
  application/  # Core use-cases, trust gating, and orchestration
  calc/         # Deterministic financial arithmetic and money logic
  cases/        # Golden validation cases
  corpus/       # Hashing and document tracking
  evidence/     # Provenance and span modeling
  explain/      # Reporting definitions
  graph/        # Calculation graph nodes
  ingest/       # Document-specific parsing (Policy, Bill, Settlement)
  persistence/  # SQLAlchemy models, repositories, and local storage
  reconcile/    # Mathematical gap analysis
  rules/        # Indian health policy evaluation logic
  verdict/      # Core finding and state-machine generation
  worker/       # Background Celery/durable processing loop
web/            # React/Vite Frontend
migrations/     # Alembic schemas
scripts/        # E2E and deployment utilities
```

--------------------------------------------------
## Limitations / Non-Goals
--------------------------------------------------

- CLAIMCHECK is **not** legal advice and does not declare insurer liability.
- It does **not** replace insurer adjudication or guarantee recovery of money.
- It does **not** infer facts unsupported by evidence.
- It does **not** treat model output as authoritative financial truth.
- It cannot resolve inherently ambiguous/unpublished policy standards (e.g. "Reasonable and Customary" bounds).

--------------------------------------------------
## Project Status
--------------------------------------------------

- **Frontend**: Completed and polished.
- **Deterministic Backend**: Completed and fully validated.
- **E2E Validation**: 4-PDF test passed seamlessly (including precision paise checks).
- **Deployment**: Configured for single-instance AWS EC2 competition evaluation.

--------------------------------------------------
## Competition Positioning
--------------------------------------------------

CLAIMCHECK is technically differentiated because:
1. It doesn't merely summarize the policy.
2. It completely reconstructs the settlement mathematics.
3. It tightly links all calculations to immutable PDF evidence.
4. It strictly separates LLM extraction from financial truth.
5. It forces explicit human trust/review before any financial analysis.
6. It uses 100% deterministic arithmetic.
7. It explicitly represents uncertainty instead of hallucinating certainty.
