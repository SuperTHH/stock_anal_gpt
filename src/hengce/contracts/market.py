from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, model_validator

from .base import FactBase


class SecurityMaster(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ts_code: str
    symbol: str
    name: str
    exchange: str
    board: str
    currency: str = "CNY"
    list_date: date
    delist_date: date | None = None
    industry_l1: str | None = None
    security_type: str = "A_SHARE"
    is_in_scope: bool


class TradingStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ts_code: str
    trade_date: date
    is_trading: bool
    is_suspended: bool
    st_status: str | None = None
    delisting_risk: bool = False
    special_treatment_reason: str | None = None


class MarketBar(FactBase):
    ts_code: str
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    pre_close: Decimal
    volume: Decimal
    amount: Decimal
    currency: str = "CNY"

    @model_validator(mode="after")
    def valid_range(self) -> "MarketBar":
        if min(self.open, self.high, self.low, self.close, self.pre_close) < 0:
            raise ValueError("prices must be non-negative")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high is below another price")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low is above another price")
        if self.volume < 0 or self.amount < 0:
            raise ValueError("volume and amount must be non-negative")
        return self
