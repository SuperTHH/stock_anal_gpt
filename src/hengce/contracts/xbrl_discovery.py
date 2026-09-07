from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class ExchangeXbrlDiscoveryScan(BaseModel):
    """Point-in-time result of probing an exchange's public XBRL listing."""

    model_config = ConfigDict(extra="forbid")

    scan_id: str = Field(min_length=1)
    source_id: Literal["sse", "szse"]
    market_date: date
    listing_url: HttpUrl
    status: Literal["AVAILABLE", "UNAVAILABLE", "FAILED"]
    instance_count: int = Field(ge=0)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    scanned_at: datetime
    reason_code: str | None = None

    @field_validator("scanned_at")
    @classmethod
    def scanned_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scan time must include a timezone")
        return value

    @model_validator(mode="after")
    def status_must_match_result(self) -> "ExchangeXbrlDiscoveryScan":
        if self.status == "AVAILABLE" and self.instance_count == 0:
            raise ValueError("available scan must contain an instance")
        if self.status != "AVAILABLE" and self.instance_count != 0:
            raise ValueError("unavailable or failed scan cannot contain instances")
        if self.status != "AVAILABLE" and not self.reason_code:
            raise ValueError("unavailable or failed scan requires a reason")
        return self


__all__ = ["ExchangeXbrlDiscoveryScan"]
