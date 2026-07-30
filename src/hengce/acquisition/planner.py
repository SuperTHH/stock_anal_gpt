import hashlib
import json
from datetime import date, datetime

from hengce.contracts.enums import (
    AcquisitionStatus,
    DocumentKind,
    QualityStatus,
    ReportType,
)
from hengce.contracts.pilot import (
    AcquisitionManifestItem,
    PilotUniverseSnapshot,
)
from hengce.services.pilot_universe import PILOT_QUOTAS

_PERIODIC_REPORTS = (
    (ReportType.ANNUAL, date(2023, 12, 31)),
    (ReportType.ANNUAL, date(2024, 12, 31)),
    (ReportType.ANNUAL, date(2025, 12, 31)),
    (ReportType.Q1, date(2025, 3, 31)),
    (ReportType.Q1, date(2026, 3, 31)),
)
_DIVIDEND_PERIODS = tuple(date(year, 12, 31) for year in range(2021, 2026))


class AcquisitionPlanner:
    def build(
        self,
        universe: PilotUniverseSnapshot,
        created_at: datetime,
    ) -> tuple[AcquisitionManifestItem, ...]:
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("ACQUISITION_PLAN_TIME_INVALID")
        if len(universe.members) != 30 or universe.quotas != PILOT_QUOTAS:
            raise ValueError("ACQUISITION_PLAN_UNIVERSE_INVALID")

        items: list[AcquisitionManifestItem] = []
        for member in universe.members:
            source_id = (
                "sse" if member.board in {"MAIN_SH", "STAR"} else "szse"
            )
            for report_type, report_period in _PERIODIC_REPORTS:
                items.append(
                    self._item(
                        universe=universe,
                        ts_code=member.ts_code,
                        document_kind=DocumentKind.PERIODIC_REPORT,
                        report_type=report_type,
                        report_period=report_period,
                        source_id=source_id,
                    )
                )
            for report_period in _DIVIDEND_PERIODS:
                items.append(
                    self._item(
                        universe=universe,
                        ts_code=member.ts_code,
                        document_kind=DocumentKind.DIVIDEND_RECORD,
                        report_type=None,
                        report_period=report_period,
                        source_id=source_id,
                    )
                )
            for document_kind in (
                DocumentKind.CAPITAL_ACTION_TIMELINE,
                DocumentKind.RISK_SCREEN,
            ):
                items.append(
                    self._item(
                        universe=universe,
                        ts_code=member.ts_code,
                        document_kind=document_kind,
                        report_type=None,
                        report_period=None,
                        source_id=source_id,
                    )
                )

        items.sort(
            key=lambda item: (
                item.ts_code,
                item.document_kind.value,
                item.report_period or date.min,
            )
        )
        return tuple(items)

    @staticmethod
    def _item(
        *,
        universe: PilotUniverseSnapshot,
        ts_code: str,
        document_kind: DocumentKind,
        report_type: ReportType | None,
        report_period: date | None,
        source_id: str,
    ) -> AcquisitionManifestItem:
        identity = {
            "universe_id": universe.universe_id,
            "ts_code": ts_code,
            "document_kind": document_kind.value,
            "report_type": report_type.value if report_type is not None else None,
            "report_period": (
                report_period.isoformat() if report_period is not None else None
            ),
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return AcquisitionManifestItem(
            item_id=f"acq-{digest}",
            universe_id=universe.universe_id,
            ts_code=ts_code,
            document_kind=document_kind,
            report_type=report_type,
            report_period=report_period,
            source_id=source_id,
            report_cutoff_at=universe.report_cutoff_at,
            status=AcquisitionStatus.PLANNED,
            source_url=None,
            discovery_method=None,
            published_at=None,
            effective_at=None,
            collected_at=None,
            content_hash=None,
            version=None,
            supersedes_id=None,
            raw_object_hash=None,
            quality_status=QualityStatus.MISSING,
            error_code=None,
            attempt_count=0,
        )


__all__ = ["AcquisitionPlanner"]
