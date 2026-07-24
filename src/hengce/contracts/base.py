from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from .enums import QualityStatus


class FactBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    source_id: str
    source_url: HttpUrl
    published_at: datetime | None = None
    effective_at: datetime | None = None
    collected_at: datetime
    version: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    license_policy: str
    quality_status: QualityStatus
    supersedes_id: str | None = None
    valid_from: datetime
