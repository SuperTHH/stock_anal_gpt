from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator, model_validator

from .base import FactBase
from .enums import ActionStatus, ActionType


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


class CorporateAction(FactBase):
    ts_code: str
    action_type: ActionType
    record_date: date
    ex_date: date
    pay_date: date | None = None
    cash_dividend_per_share: Decimal | None = None
    cash_dividend_total: Decimal | None = None
    fiscal_year: int | None = None
    stock_dividend_ratio: Decimal | None = None
    split_ratio: Decimal | None = None
    rights_ratio: Decimal | None = None
    rights_price: Decimal | None = None
    share_reduction: Decimal | None = None
    action_status: ActionStatus

    @field_validator("published_at")
    @classmethod
    def publication_is_required(cls, value: object, info: ValidationInfo) -> object:
        if value is None:
            raise ValueError(f"{info.field_name} is required")
        return value

    @model_validator(mode="after")
    def validate_action_terms(self) -> "CorporateAction":
        if self.record_date > self.ex_date:
            raise ValueError("record_date must not follow ex_date")
        if self.pay_date is not None and self.pay_date < self.ex_date:
            raise ValueError("pay_date must not precede ex_date")

        required_terms = {
            ActionType.CASH_DIVIDEND: (self.cash_dividend_per_share,),
            ActionType.STOCK_DIVIDEND: (self.stock_dividend_ratio,),
            ActionType.SPLIT: (self.split_ratio,),
            ActionType.RIGHTS_ISSUE: (self.rights_ratio, self.rights_price),
            ActionType.BUYBACK_CANCELLATION: (self.share_reduction,),
        }[self.action_type]
        if any(value is None or value <= 0 for value in required_terms):
            raise ValueError("action terms must be positive and complete")
        if self.cash_dividend_total is not None and self.cash_dividend_total <= 0:
            raise ValueError("cash dividend total must be positive")
        if self.fiscal_year is not None and not 2000 <= self.fiscal_year <= 2100:
            raise ValueError("fiscal year is out of range")
        return self
