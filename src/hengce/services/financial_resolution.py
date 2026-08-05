from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from hengce.contracts.enums import QualityStatus


class FinancialDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filing_id: str
    ts_code: str
    source_id: str
    source_kind: Literal["XBRL", "PDF"]
    source_url: AnyHttpUrl
    published_at: datetime
    valid_from: datetime
    version: str
    supersedes_id: str | None
    quality_status: QualityStatus
    facts: dict[str, Decimal]
    normalization_metadata: dict[str, str] = Field(default_factory=dict)
    fact_lineage: dict[str, "FinancialFactLineage"] = Field(default_factory=dict)

    @field_validator("published_at", "valid_from")
    @classmethod
    def time_must_be_aware(cls, value: datetime, info: ValidationInfo) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{info.field_name} must include a timezone")
        return value


class FinancialResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document: FinancialDocument | None
    blocked_reasons: tuple[str, ...]


class FinancialFactLineage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_number: int = Field(gt=0)
    source_text_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    unit_multiplier: str
    currency: str
    parser_version: str
    pdf_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class PreferredFinancialResolver:
    def resolve(
        self,
        *,
        exchange_xbrl: FinancialDocument | None,
        cninfo_pdf_provider: Callable[
            [],
            FinancialDocument | Sequence[FinancialDocument],
        ],
        as_of: datetime,
        known_at: datetime,
        compare_fallback: bool = False,
    ) -> FinancialResolution:
        self._validate_cutoff(as_of)
        self._validate_cutoff(known_at)
        xbrl = self._visible(exchange_xbrl, as_of, known_at)
        if xbrl is not None and xbrl.quality_status is QualityStatus.VALID:
            if not compare_fallback:
                return FinancialResolution(document=xbrl, blocked_reasons=())
            pdf = self._visible_latest(cninfo_pdf_provider(), as_of, known_at)
            if (
                pdf is not None
                and pdf.quality_status is QualityStatus.VALID
                and self._has_conflict(xbrl, pdf)
            ):
                return FinancialResolution(
                    document=xbrl,
                    blocked_reasons=("XBRL_PDF_FACT_CONFLICT",),
                )
            return FinancialResolution(document=xbrl, blocked_reasons=())

        pdf = self._visible_latest(cninfo_pdf_provider(), as_of, known_at)
        if pdf is None:
            return FinancialResolution(document=None, blocked_reasons=("FINANCIAL_SOURCE_MISSING",))
        if pdf.quality_status is not QualityStatus.VALID:
            return FinancialResolution(document=None, blocked_reasons=("CNINFO_PDF_UNVERIFIED",))
        return FinancialResolution(document=pdf, blocked_reasons=())

    @staticmethod
    def _visible(
        document: FinancialDocument | None,
        as_of: datetime,
        known_at: datetime,
    ) -> FinancialDocument | None:
        if (
            document is None
            or document.published_at > as_of
            or document.valid_from > known_at
        ):
            return None
        return document

    @classmethod
    def _visible_latest(
        cls,
        documents: FinancialDocument | Sequence[FinancialDocument],
        as_of: datetime,
        known_at: datetime,
    ) -> FinancialDocument | None:
        candidates = (
            (documents,)
            if isinstance(documents, FinancialDocument)
            else tuple(documents)
        )
        visible = [
            document
            for document in candidates
            if cls._visible(document, as_of, known_at) is not None
        ]
        return (
            max(
                visible,
                key=lambda document: (
                    document.published_at,
                    document.valid_from,
                    document.filing_id,
                ),
            )
            if visible
            else None
        )

    @staticmethod
    def _has_conflict(first: FinancialDocument, second: FinancialDocument) -> bool:
        shared = first.facts.keys() & second.facts.keys()
        return any(first.facts[name] != second.facts[name] for name in shared)

    @staticmethod
    def _validate_cutoff(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("FINANCIAL_RESOLUTION_CUTOFF_INVALID")
