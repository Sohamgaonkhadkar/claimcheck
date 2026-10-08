"""Local/development-only routes for deterministic fixture demonstrations."""
from __future__ import annotations

from fastapi import APIRouter, Request

from claimcheck.api.schemas import GoldenDemoResponse
from claimcheck.application.golden_demo import GoldenDemoService

router = APIRouter(prefix="/api/v1/dev", tags=["development demo"])


@router.get(
    "/golden-001",
    response_model=GoldenDemoResponse,
    summary="Run the synthetic GOLDEN-001 demonstration",
)
def golden_001(request: Request) -> GoldenDemoResponse:
    service: GoldenDemoService = request.app.state.golden_demo_service
    return GoldenDemoResponse.model_validate(service.run())
