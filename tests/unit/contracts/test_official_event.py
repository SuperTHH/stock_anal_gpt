from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from hengce.contracts.enums import QualityStatus
from hengce.contracts.official_event import OfficialEvent, ReportSource

NOW = datetime(2026, 7, 22, 13, 31, tzinfo=UTC)


def test_official_event_preserves_traceability_and_impact_fields() -> None:
    event = OfficialEvent(
        record_id="event-1",
        source_id="sse",
        source_url="https://www.sse.com.cn/",
        published_at=NOW,
        effective_at=NOW,
        collected_at=NOW,
        version="v1",
        content_hash="a" * 64,
        license_policy="personal-research",
        quality_status=QualityStatus.VALID,
        valid_from=NOW,
        institution="上海证券交易所",
        event_type="COMPANY_ANNOUNCEMENT",
        title="经营进展说明",
        factual_summary="公司披露经营进展。",
        affected_ts_codes=("688901.SH",),
        impact_horizon="MEDIUM_TERM",
        confidence=Decimal("0.90"),
    )

    assert event.affected_ts_codes == ("688901.SH",)
    assert event.confidence == Decimal("0.90")


def test_report_source_requires_collection_time_and_license_policy() -> None:
    payload = {
        "record_id": "market-1",
        "domain": "market",
        "source_name": "Tushare 日线接口",
        "source_url": "https://tushare.pro/document/2?doc_id=27",
        "published_at": NOW,
        "effective_at": NOW,
        "collected_at": NOW,
        "valid_from": NOW,
        "version": "2026-07-22",
        "license_policy": "personal-research",
        "quality_status": QualityStatus.VALID,
    }
    assert ReportSource(**payload).domain == "market"

    with pytest.raises(ValidationError):
        ReportSource(**{key: value for key, value in payload.items() if key != "license_policy"})
