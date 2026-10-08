# CLAIMCHECK

**An evidence-first health-insurance settlement review service.** CLAIMCHECK organizes policy wording, a policy schedule, a hospital bill, and a settlement/rejection letter into a reviewed, provenance-aware reconstruction—so people can see what is supported, what is uncertain, and what to ask next.

## What CLAIMCHECK Does

A settlement letter may state what was paid without making it easy to trace **why** deductions were made, **which policy terms** support them, or **what to ask the insurer next**. CLAIMCHECK reconstructs a claim using the documents supplied by the customer, giving clarity and actionable next steps.

## Key Product Workflow

1. **Upload Documents**: Provide the Policy Wording, Policy Schedule, Hospital Bill, and Settlement Letter PDFs.
2. **Analysis & Checking**: The backend extracts candidate information from the PDFs.
3. **Human Review (Trust Gate)**: The customer explicitly reviews the extracted values against the original source documents via a side-by-side Evidence UI.
4. **Deterministic Reconstruction**: Once fields are verified, a deterministic calculator handles all financial math, limits, and deductions to prevent AI hallucinations.
5. **Results Generation**: Presents clear findings, outstanding uncertainties, and actionable questions to ask the insurer.

## Architecture Overview

- **Frontend**: A React SPA that handles uploads, the Evidence UI for human review, and results presentation.
- **Backend API**: A FastAPI service that securely handles uploads, triggers processing, tracks review state, and performs final deterministic financial math.
- **Worker**: A background process orchestrating PDF parsing, data extraction, and verification pipelines.
- **Database**: PostgreSQL storing case metadata, document state, evidence provenance, and review logs.
- **Private Storage**: Local persistent disk storage for user PDFs (never exposed publicly).

## Technology Stack

- **Backend**: Python 3.11, FastAPI, SQLAlchemy, Pydantic, Uvicorn, Celery/Background Tasks
- **Frontend**: React, TypeScript, Vite, Vanilla CSS
- **Database**: PostgreSQL 15
- **Deployment**: Single EC2 Instance, Nginx (Reverse Proxy & Static Files), Systemd Services

## How to Run Locally

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

## Production Deployment Structure

This project is deployed to a single AWS EC2 instance (Amazon Linux 2023) for competition evaluation:
- **Nginx** handles public traffic on ports 80/443, serving the built Vite static assets and reverse-proxying `/api` to FastAPI.
- **FastAPI API Server** runs as a native systemd service on port `127.0.0.1:8000`.
- **Background Worker** runs as a separate systemd service.
- **PostgreSQL** runs natively, bound only to `localhost`.
- **EBS Volume** stores persistent database data and private PDF storage `/var/lib/claimcheck/private-storage`.

## Important Limitations / Disclaimer

CLAIMCHECK is designed for transparency and is not legal or financial advice. The models propose candidate values, but all final financial math and deductions are deterministic and rely on human verification through the Evidence UI. Only 4 specific document types are supported for extraction and review.

## Repository Structure

```
claimcheck/
├── src/claimcheck/          # Backend Python source code
│   ├── api/                 # FastAPI routes and controllers
│   ├── worker/              # Background processing and extraction
│   ├── calculator/          # Deterministic financial arithmetic
│   ├── models/              # Database schemas
│   └── pipeline/            # Analysis and provenance tracking
├── web/                     # Frontend React SPA
│   ├── src/                 # UI components and layouts
│   └── public/              # Static assets and images
├── migrations/              # Alembic database migrations
├── scripts/                 # Setup and deployment scripts
├── .env.example             # Environment configuration template
└── README.md                # This file
```
