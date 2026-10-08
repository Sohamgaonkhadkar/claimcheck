# CLAIMCHECK

**An evidence-first health-insurance settlement review service.** CLAIMCHECK organizes policy wording, a policy schedule, a hospital bill, and a settlement/rejection letter into a reviewed, provenance-aware reconstruction—so people can see what is supported, what is uncertain, and what to ask next.

## 1. The Problem: The Quiet Majority of Lost Money

When an Indian policyholder is discharged from the hospital, they receive a settlement or rejection letter. The amount paid is frequently below expectation, but the letter rarely explains the exact arithmetic or policy clause used to justify deductions.

The scale of this issue is massive. In FY 2023-24, out of ₹1.17 lakh crore registered health claims, **₹15,100 crore (12.9%) was disallowed** (partially cut) and ₹10,937 crore (9.34%) was repudiated completely. The frequently cited "rejection rate" ignores the largest bucket of lost money: the claim that was approved but silently cut down. 

While free escalation machinery exists (Grievance Officers, Bima Bharosa, the Insurance Ombudsman), it is built for people who already know exactly what to allege and can prove it with figures and clauses. Most policyholders lack the diagnosis needed to challenge a deduction.

## 2. The Core Idea: Reconstructing the Claim

A large part of an Indian health-insurance deduction is not a black box. It is a stack of named, individually rule-governed arithmetic steps applied to an itemised bill (e.g., non-payable items removal, room-rent sub-limits, proportionate deductions, and co-pays). Each step has a source that can be pointed at.

```text
Customer PDFs → candidate extraction + evidence → explicit human review
             → trusted structured inputs → deterministic reconstruction
             → findings, uncertainty, and possible next questions
```

Models and parsers propose candidate values. However, **extraction is not truth**. CLAIMCHECK forces explicit human review against the original documents before any analysis begins. After human verification, **deterministic application code** owns the financial arithmetic, reconciliation, and verdict semantics to completely eliminate AI hallucination.

## 3. Product Workflows & Three States

Once the user confirms the evidence, the deterministic calculator evaluates the claim and categorizes each deduction into one of three states:

1. **Supported**: The deduction aligns mathematically and structurally with the uploaded policy terms.
2. **Potentially Inconsistent**: A discrepancy was found (e.g., a 1% room rent limit was applied as a fixed cap).
3. **Undetermined**: The wording is ambiguous or relies on unpublished standards like "Reasonable and Customary" charges, and cannot be mathematically verified.

*CLAIMCHECK does not offer legal advice or declare an insurer "wrong in law". It strictly evaluates mathematical and structural consistency against the uploaded documents.*

## 4. Architecture Overview

- **Frontend**: A React SPA built with Vite. Implements the *Evidence UI* where users review and verify extracted values against PDF highlight spans.
- **Backend API**: A FastAPI service that securely handles uploads, triggers processing, tracks review state, and performs final deterministic financial math.
- **Worker**: A background process orchestrating PDF parsing, data extraction, and verification pipelines.
- **Database**: PostgreSQL storing case metadata, document state, evidence provenance, and review logs.
- **Private Storage**: Local persistent disk storage for user PDFs (never exposed publicly).

## 5. Technology Stack

- **Backend**: Python 3.11, FastAPI, SQLAlchemy, Pydantic, Uvicorn, Celery/Background Tasks
- **Frontend**: React, TypeScript, Vite, Vanilla CSS
- **Database**: PostgreSQL 15
- **Deployment**: Single EC2 Instance, Nginx (Reverse Proxy & Static Files), Systemd Services

## 6. How to Run Locally

1. **Database**: Start a local PostgreSQL instance (e.g. via Docker Compose or natively).
2. **Environment**: Copy `.env.example` to `.env` and configure `DATABASE_URL` and `CLAIMCHECK_STORAGE_ROOT`.
3. **Run API**: 
   ```bash
   python -m uvicorn claimcheck.api.app:app --port 8000 --reload
   ```
4. **Run Worker**:
   ```bash
   claimcheck-worker
   ```
5. **Run Frontend**:
   ```bash
   cd web && npm install && npm run dev
   ```

## 7. Production Deployment Structure

This project is deployed to a single AWS EC2 instance (Amazon Linux 2023) for competition evaluation:
- **Nginx** handles public traffic on ports 80/443, serving the built Vite static assets and reverse-proxying `/api` to FastAPI.
- **FastAPI API Server** runs as a native systemd service on port `127.0.0.1:8000`.
- **Background Worker** runs as a separate systemd service.
- **PostgreSQL** runs natively, bound only to `localhost`.
- **EBS Volume** stores persistent database data and private PDF storage `/var/lib/claimcheck/private-storage`.

## 8. Repository Structure

```
claimcheck/
├── src/claimcheck/          # Backend Python source code
│   ├── api/                 # FastAPI routes and controllers
│   ├── worker/              # Background processing and extraction
│   ├── calc/                # Deterministic financial arithmetic
│   ├── models/              # Database schemas
│   └── pipeline.py          # Analysis and provenance tracking
├── web/                     # Frontend React SPA
│   ├── src/                 # UI components and layouts
│   └── public/              # Static assets and images
├── migrations/              # Alembic database migrations
├── scripts/                 # Setup and deployment scripts
├── .env.example             # Environment configuration template
└── README.md                # This file
```
