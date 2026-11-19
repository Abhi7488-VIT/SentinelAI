"""Who is sending: resolved from the ingest API key, never from the payload."""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IngestPrincipal:
    key_id: uuid.UUID
    org_id: uuid.UUID
    service_id: uuid.UUID
    service_name: str  # must equal the OTel resource attribute `service.name`
