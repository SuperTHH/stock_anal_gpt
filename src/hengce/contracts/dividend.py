from __future__ import annotations

from decimal import Decimal

from pydantic import ConfigDict, Field, model_validator

from hengce.contracts.base import FactBase
from hengce.contracts.enums import ActionStatus


class AnnualDividendRecord(FactBase):
    """Audited fiscal-year dividend evidence without invented action dates."""

    model_config = ConfigDict(extra="forbid")

    ts_code: str
    fiscal_year: int = Field(ge=2000, le=2100)
    has_cash_dividend: bool
    cash_dividend_per_share: Decimal | None = None
    cash_dividend_total: Decimal | None = None
    implementation_status: ActionStatus

    @model_validator(mode="after")
    def validate_dividend_terms(self) -> AnnualDividendRecord:
        values = (self.cash_dividend_per_share, self.cash_dividend_total)
        if self.has_cash_dividend:
            if not any(value is not None for value in values):
                raise ValueError("cash dividend evidence requires an amount")
            if any(value is not None and value <= 0 for value in values):
                raise ValueError("cash dividend amounts must be positive")
            if self.implementation_status is ActionStatus.CANCELLED:
                raise ValueError("cancelled proposal is not a cash dividend record")
        elif any(value is not None for value in values):
            raise ValueError("no-dividend evidence cannot contain cash amounts")
        return self


__all__ = ["AnnualDividendRecord"]
