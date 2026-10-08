"""HTTP identity provider abstraction; the local provider is development-only."""
from __future__ import annotations

from typing import Protocol
from uuid import UUID

from fastapi import Request

from claimcheck.application.identity import OwnerIdentity

# Compatibility-friendly name used by the route signatures.
OwnerPrincipal = OwnerIdentity


class OwnerIdentityProvider(Protocol):
    def resolve(self, request: Request) -> OwnerIdentity: ...


class DevelopmentIdentityProvider:
    """A single server-configured local principal, never derived from a client header/body."""

    auth_mode = "development"

    def __init__(self, owner_id: UUID) -> None:
        self.owner_id = owner_id

    def resolve(self, request: Request) -> OwnerIdentity:
        # Deliberately ignore X-Owner-ID, body fields and arbitrary bearer tokens.
        return OwnerIdentity(
            owner_id=self.owner_id,
            identity_provider="development",
            subject=str(self.owner_id),
            auth_mode="development",
        )
