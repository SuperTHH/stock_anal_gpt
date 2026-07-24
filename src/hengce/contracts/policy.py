from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, HttpUrl, model_validator

from .enums import ReviewStatus


class SourcePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    source_name: str
    allowed_domains: list[str]
    allowed_schemes: list[Literal["http", "https"]]
    allowed_purposes: list[str]
    fetch_frequency: str
    full_text_rule: str
    attachment_rule: str
    rate_limit_per_minute: int
    robots_policy: str
    terms_url: HttpUrl
    terms_reviewed_at: datetime | None = None
    review_status: ReviewStatus
    connection_status: str
    enabled: bool

    @model_validator(mode="after")
    def approved_when_enabled(self) -> "SourcePolicy":
        if self.enabled and self.review_status is not ReviewStatus.APPROVED:
            raise ValueError("enabled source policy must be approved")
        return self
