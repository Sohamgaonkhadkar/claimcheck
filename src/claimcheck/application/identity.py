"""Transport-neutral owner identity passed from an authentication adapter to services."""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class OwnerIdentity:
    owner_id: UUID
    identity_provider: str
    subject: str
    auth_mode: str
