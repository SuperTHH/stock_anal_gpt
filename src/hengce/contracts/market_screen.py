from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import ConfigDict, model_validator

from hengce.contracts.base import FactBase


class ImplementedDividend(FactBase):
    """One exchange-confirmed implemented cash-dividend event."""

    model_config = ConfigDict(extra="forbid")

    ts_code: str
    record_date: date
    ex_date: date
    cash_dividend_per_share: Decimal

    @model_validator(mode="after")
    def validate_terms(self) -> ImplementedDividend:
        if self.cash_dividend_per_share <= 0:
            raise ValueError("implemented dividend must be positive")
        if self.record_date > self.ex_date:
            raise ValueError("record date must not follow ex date")
        return self


__all__ = ["ImplementedDividend"]
