from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from hengce.contracts.dividend import AnnualDividendRecord
from hengce.contracts.enums import (
    AcquisitionStatus,
    ActionStatus,
    ActionType,
    DocumentKind,
    QualityStatus,
)
from hengce.contracts.market import CorporateAction
from hengce.contracts.risk import OfficialRiskScreen
from hengce.raw_store.store import RawObjectStore
from hengce.state.action_repository import CorporateActionRepository
from hengce.state.dividend_repository import AnnualDividendRepository
from hengce.state.pilot_repository import PilotRepository
from hengce.state.risk_repository import OfficialRiskScreenRepository

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class _ActionEvidenceRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_key: str = Field(min_length=1)
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
    supersedes_record_id: str | None = None


class _ActionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["official-action-evidence-v1"]
    attachment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_at: datetime
    extraction_method: Literal["MANUAL_REVIEW"]
    actions: tuple[_ActionEvidenceRow, ...] = ()
    no_dividend_fiscal_year: int | None = None
    no_material_actions: bool = False

    @field_validator("reviewed_at")
    @classmethod
    def reviewed_at_must_be_aware(
        cls,
        value: datetime,
        info: ValidationInfo,
    ) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{info.field_name} must include a timezone")
        return value

    @model_validator(mode="after")
    def action_or_explicit_no_dividend(self) -> _ActionEvidence:
        evidence_modes = sum(
            (
                bool(self.actions),
                self.no_dividend_fiscal_year is not None,
                self.no_material_actions,
            )
        )
        if evidence_modes != 1:
            raise ValueError(
                "exactly one of actions, no_dividend_fiscal_year, "
                "or no_material_actions is required"
            )
        if self.no_dividend_fiscal_year is not None and not (
            2000 <= self.no_dividend_fiscal_year <= 2100
        ):
            raise ValueError("no_dividend_fiscal_year is out of range")
        return self


class _DividendYearEvidenceRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fiscal_year: int = Field(ge=2000, le=2100)
    has_cash_dividend: bool
    cash_dividend_per_share: Decimal | None = None
    cash_dividend_total: Decimal | None = None
    implementation_status: ActionStatus


class _DividendYearEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["official-dividend-year-evidence-v2"]
    attachment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_at: datetime
    extraction_method: Literal["MANUAL_REVIEW"]
    annual_record: _DividendYearEvidenceRow

    @field_validator("reviewed_at")
    @classmethod
    def reviewed_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("reviewed_at must include a timezone")
        return value


class _RiskEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["official-risk-screen-v1"]
    attachment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_at: datetime
    extraction_method: Literal["MANUAL_REVIEW"]
    audit_opinion_standard: bool
    major_investigation_open: bool
    delisting_risk: bool
    st_status: str | None
    is_suspended: bool
    publication_order_known: bool
    supersedes_record_id: str | None = None

    @field_validator("reviewed_at")
    @classmethod
    def reviewed_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("reviewed_at must include a timezone")
        return value


@dataclass(frozen=True, slots=True)
class OfficialEvidenceIngestionResult:
    item_id: str
    ingested: bool
    action_count: int
    risk_record_id: str | None
    source_record_ids: tuple[str, ...]
    error_code: str | None


class OfficialEvidenceIngestionService:
    """Ingest reviewed non-periodic official evidence from the private inbox."""

    def __init__(
        self,
        *,
        raw_store: RawObjectStore,
        pilot_repository: PilotRepository,
        action_repository: CorporateActionRepository,
        dividend_repository: AnnualDividendRepository | None = None,
        risk_repository: OfficialRiskScreenRepository | None = None,
        manual_inbox: Path,
        clock: Callable[[], datetime],
    ) -> None:
        self.raw_store = raw_store
        self.pilot_repository = pilot_repository
        self.action_repository = action_repository
        self.dividend_repository = dividend_repository or AnnualDividendRepository(
            pilot_repository.path
        )
        self.risk_repository = risk_repository or OfficialRiskScreenRepository(
            pilot_repository.path
        )
        self.manual_inbox = manual_inbox
        self.clock = clock

    def run(self, item_id: str) -> OfficialEvidenceIngestionResult:
        item = self.pilot_repository.get_manifest_item(item_id)
        if item is None:
            raise ValueError("ACQUISITION_ITEM_NOT_FOUND")
        if item.status is AcquisitionStatus.INGESTED:
            return OfficialEvidenceIngestionResult(
                item_id=item.item_id,
                ingested=True,
                action_count=0,
                risk_record_id=None,
                source_record_ids=(),
                error_code=None,
            )
        if (
            item.status is not AcquisitionStatus.DOWNLOADED
            or item.document_kind
            not in {
                DocumentKind.DIVIDEND_RECORD,
                DocumentKind.CAPITAL_ACTION_TIMELINE,
                DocumentKind.RISK_SCREEN,
            }
            or item.source_url is None
            or item.published_at is None
            or item.collected_at is None
            or item.raw_object_hash is None
            or item.version is None
        ):
            raise ValueError("OFFICIAL_EVIDENCE_ITEM_INVALID")

        try:
            self.raw_store.validate_content_hash(item.raw_object_hash)
            raw_evidence = self._sidecar_evidence(item.item_id)
            if item.document_kind is DocumentKind.RISK_SCREEN:
                return self._ingest_risk(item, raw_evidence)
            schema_version = (
                raw_evidence.get("schema_version")
                if isinstance(raw_evidence, dict)
                else None
            )
            if schema_version == "official-dividend-year-evidence-v2":
                if item.document_kind is not DocumentKind.DIVIDEND_RECORD:
                    raise ValueError("OFFICIAL_EVIDENCE_KIND_MISMATCH")
                dividend_payload = _DividendYearEvidence.model_validate(raw_evidence)
                self._validate_common_evidence(item, dividend_payload)
                if (
                    item.report_period is None
                    or dividend_payload.annual_record.fiscal_year
                    != item.report_period.year
                ):
                    raise ValueError("OFFICIAL_EVIDENCE_KIND_MISMATCH")
                annual_records = (
                    self._build_annual_record(
                        item,
                        dividend_payload,
                        dividend_payload.annual_record,
                    ),
                )
                actions: tuple[CorporateAction, ...] = ()
                payload = None
            else:
                payload = _ActionEvidence.model_validate(raw_evidence)
                self._validate_common_evidence(item, payload)
                if item.document_kind is DocumentKind.DIVIDEND_RECORD and any(
                    row.action_type is not ActionType.CASH_DIVIDEND for row in payload.actions
                ):
                    raise ValueError("OFFICIAL_EVIDENCE_KIND_MISMATCH")
                if payload.no_dividend_fiscal_year is not None and (
                    item.document_kind is not DocumentKind.DIVIDEND_RECORD
                    or item.report_period is None
                    or payload.no_dividend_fiscal_year != item.report_period.year
                ):
                    raise ValueError("OFFICIAL_EVIDENCE_KIND_MISMATCH")
                if payload.no_material_actions and (
                    item.document_kind is not DocumentKind.CAPITAL_ACTION_TIMELINE
                ):
                    raise ValueError("OFFICIAL_EVIDENCE_KIND_MISMATCH")
                actions = tuple(
                    self._build_action(item, payload, row) for row in payload.actions
                )
                annual_records = self._annual_records_from_v1(item, payload, actions)
        except (ValidationError, ValueError) as error:
            return self._return_to_manual(item, error)
        if actions:
            try:
                self.action_repository.save_versions(actions)
            except ValueError as error:
                return self._return_to_manual(item, error)
        try:
            for annual_record in annual_records:
                self.dividend_repository.save_version(annual_record)
        except ValueError as error:
            return self._return_to_manual(item, error)

        verified = item.model_copy(
            update={
                "status": AcquisitionStatus.VERIFIED,
                "quality_status": QualityStatus.VALID,
                "effective_at": (
                    min(action.effective_at for action in actions)
                    if actions
                    else min(record.effective_at for record in annual_records)
                    if annual_records
                    else item.published_at
                ),
                "error_code": None,
            }
        )
        self.pilot_repository.transition(
            item.item_id,
            AcquisitionStatus.DOWNLOADED,
            verified,
            self.clock(),
        )
        ingested = verified.model_copy(update={"status": AcquisitionStatus.INGESTED})
        self.pilot_repository.transition(
            item.item_id,
            AcquisitionStatus.VERIFIED,
            ingested,
            self.clock(),
        )
        record_ids = (
            *(action.record_id for action in actions),
            *(record.record_id for record in annual_records),
        )
        return OfficialEvidenceIngestionResult(
            item_id=item.item_id,
            ingested=True,
            action_count=len(actions),
            risk_record_id=None,
            source_record_ids=record_ids,
            error_code=None,
        )

    def _validate_common_evidence(
        self,
        item: object,
        payload: _ActionEvidence | _DividendYearEvidence,
    ) -> None:
        if payload.attachment_sha256 != item.raw_object_hash:
            raise ValueError("OFFICIAL_EVIDENCE_HASH_MISMATCH")
        if not item.collected_at <= payload.reviewed_at <= self.clock():
            raise ValueError("OFFICIAL_EVIDENCE_REVIEW_TIME_INVALID")

    def _annual_records_from_v1(
        self,
        item: object,
        payload: _ActionEvidence,
        actions: tuple[CorporateAction, ...],
    ) -> tuple[AnnualDividendRecord, ...]:
        if item.document_kind is not DocumentKind.DIVIDEND_RECORD:
            return ()
        if payload.no_dividend_fiscal_year is not None:
            row = _DividendYearEvidenceRow(
                fiscal_year=payload.no_dividend_fiscal_year,
                has_cash_dividend=False,
                implementation_status=ActionStatus.IMPLEMENTED,
            )
            return (self._build_annual_record(item, payload, row),)
        return tuple(
            self._build_annual_record(
                item,
                payload,
                _DividendYearEvidenceRow(
                    fiscal_year=action.fiscal_year,
                    has_cash_dividend=True,
                    cash_dividend_per_share=action.cash_dividend_per_share,
                    cash_dividend_total=action.cash_dividend_total,
                    implementation_status=action.action_status,
                ),
            )
            for action in actions
            if action.fiscal_year is not None
        )

    @staticmethod
    def _build_annual_record(
        item: object,
        payload: _ActionEvidence | _DividendYearEvidence,
        row: _DividendYearEvidenceRow,
    ) -> AnnualDividendRecord:
        identity = json.dumps(
            {
                "item_id": item.item_id,
                "schema_version": payload.schema_version,
                "fiscal_year": row.fiscal_year,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        record_id = f"annual-dividend-{hashlib.sha256(identity).hexdigest()}"
        return AnnualDividendRecord(
            record_id=record_id,
            source_id=item.source_id,
            source_url=item.source_url,
            published_at=item.published_at,
            effective_at=item.published_at,
            collected_at=item.collected_at,
            version=f"{item.version}:{payload.schema_version}",
            content_hash=item.raw_object_hash,
            license_policy="official-public-attachment-personal-research",
            quality_status=QualityStatus.VALID,
            supersedes_id=None,
            valid_from=payload.reviewed_at,
            ts_code=item.ts_code,
            fiscal_year=row.fiscal_year,
            has_cash_dividend=row.has_cash_dividend,
            cash_dividend_per_share=row.cash_dividend_per_share,
            cash_dividend_total=row.cash_dividend_total,
            implementation_status=row.implementation_status,
        )

    def _return_to_manual(
        self,
        item: object,
        error: ValidationError | ValueError,
    ) -> OfficialEvidenceIngestionResult:
        if isinstance(error, ValidationError):
            error_code = "OFFICIAL_EVIDENCE_SCHEMA_INVALID"
        else:
            candidate = str(error)
            error_code = (
                candidate
                if candidate.startswith(("OFFICIAL_EVIDENCE_", "CORPORATE_ACTION_"))
                else "OFFICIAL_EVIDENCE_RAW_INVALID"
            )
        awaiting = item.model_copy(
            update={
                "status": AcquisitionStatus.AWAITING_MANUAL,
                "quality_status": QualityStatus.UNVERIFIED,
                "error_code": error_code,
            }
        )
        self.pilot_repository.transition(
            item.item_id,
            AcquisitionStatus.DOWNLOADED,
            awaiting,
            self.clock(),
        )
        return OfficialEvidenceIngestionResult(
            item_id=item.item_id,
            ingested=False,
            action_count=0,
            risk_record_id=None,
            source_record_ids=(),
            error_code=error_code,
        )

    def _ingest_risk(
        self,
        item: object,
        raw_evidence: object,
    ) -> OfficialEvidenceIngestionResult:
        payload = _RiskEvidence.model_validate(raw_evidence)
        if payload.attachment_sha256 != item.raw_object_hash:
            raise ValueError("OFFICIAL_EVIDENCE_HASH_MISMATCH")
        if not item.collected_at <= payload.reviewed_at <= self.clock():
            raise ValueError("OFFICIAL_EVIDENCE_REVIEW_TIME_INVALID")
        identity = json.dumps(
            {
                "item_id": item.item_id,
                "schema_version": payload.schema_version,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        record_id = f"official-risk-{hashlib.sha256(identity).hexdigest()}"
        screen = OfficialRiskScreen(
            record_id=record_id,
            source_id=item.source_id,
            source_url=item.source_url,
            published_at=item.published_at,
            effective_at=item.effective_at or item.published_at,
            collected_at=item.collected_at,
            version=f"{item.version}:{payload.schema_version}",
            content_hash=item.raw_object_hash,
            license_policy="official-public-attachment-personal-research",
            quality_status=QualityStatus.VALID,
            supersedes_id=payload.supersedes_record_id,
            valid_from=payload.reviewed_at,
            ts_code=item.ts_code,
            audit_opinion_standard=payload.audit_opinion_standard,
            major_investigation_open=payload.major_investigation_open,
            delisting_risk=payload.delisting_risk,
            st_status=payload.st_status,
            is_suspended=payload.is_suspended,
            publication_order_known=payload.publication_order_known,
        )
        self.risk_repository.save_version(screen)
        verified = item.model_copy(
            update={
                "status": AcquisitionStatus.VERIFIED,
                "quality_status": QualityStatus.VALID,
                "effective_at": screen.effective_at,
                "error_code": None,
            }
        )
        self.pilot_repository.transition(
            item.item_id,
            AcquisitionStatus.DOWNLOADED,
            verified,
            self.clock(),
        )
        self.pilot_repository.transition(
            item.item_id,
            AcquisitionStatus.VERIFIED,
            verified.model_copy(update={"status": AcquisitionStatus.INGESTED}),
            self.clock(),
        )
        return OfficialEvidenceIngestionResult(
            item_id=item.item_id,
            ingested=True,
            action_count=0,
            risk_record_id=record_id,
            source_record_ids=(record_id,),
            error_code=None,
        )

    def _sidecar_evidence(self, item_id: str) -> object:
        matches: list[object] = []
        if self.manual_inbox.is_dir():
            for path in sorted(self.manual_inbox.glob("*.json")):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if (
                    isinstance(payload, dict)
                    and payload.get("item_id") == item_id
                    and "evidence" in payload
                ):
                    matches.append(payload["evidence"])
        if len(matches) != 1:
            raise ValueError("OFFICIAL_EVIDENCE_SIDECAR_INVALID")
        return matches[0]

    @staticmethod
    def _build_action(
        item: object,
        payload: _ActionEvidence,
        row: _ActionEvidenceRow,
    ) -> CorporateAction:
        identity = json.dumps(
            {
                "item_id": item.item_id,
                "schema_version": payload.schema_version,
                "action_key": row.action_key,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        record_id = f"official-action-{hashlib.sha256(identity).hexdigest()}"
        effective_at = datetime.combine(
            row.ex_date,
            datetime.min.time(),
            tzinfo=_SHANGHAI,
        )
        return CorporateAction(
            record_id=record_id,
            source_id=item.source_id,
            source_url=item.source_url,
            published_at=item.published_at,
            effective_at=effective_at,
            collected_at=item.collected_at,
            version=f"{item.version}:{payload.schema_version}",
            content_hash=item.raw_object_hash,
            license_policy="official-public-attachment-personal-research",
            quality_status=QualityStatus.VALID,
            supersedes_id=row.supersedes_record_id,
            valid_from=payload.reviewed_at,
            ts_code=item.ts_code,
            action_type=row.action_type,
            record_date=row.record_date,
            ex_date=row.ex_date,
            pay_date=row.pay_date,
            cash_dividend_per_share=row.cash_dividend_per_share,
            cash_dividend_total=row.cash_dividend_total,
            fiscal_year=row.fiscal_year,
            stock_dividend_ratio=row.stock_dividend_ratio,
            split_ratio=row.split_ratio,
            rights_ratio=row.rights_ratio,
            rights_price=row.rights_price,
            share_reduction=row.share_reduction,
            action_status=row.action_status,
        )


__all__ = [
    "OfficialEvidenceIngestionResult",
    "OfficialEvidenceIngestionService",
]
