from datetime import date, datetime
from decimal import Decimal

from pydantic import (
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .base import FactBase
from .enums import QualityStatus


class DerivedFinancialMetric(FactBase):
    model_config = ConfigDict(extra="forbid")

    ts_code: str
    report_period: date
    metric_name: str
    metric_value: Decimal
    unit: str
    as_of: datetime
    known_at: datetime
    input_fact_ids: tuple[str, ...]
    algorithm_version: str
    formula_metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("as_of", "known_at")
    @classmethod
    def cutoff_must_be_aware(cls, value: datetime, info: ValidationInfo) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{info.field_name} must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_lineage(self) -> "DerivedFinancialMetric":
        if not self.input_fact_ids:
            raise ValueError("input_fact_ids must not be empty")
        if self.quality_status is not QualityStatus.DERIVED:
            raise ValueError("derived metrics must use DERIVED quality")
        return self
