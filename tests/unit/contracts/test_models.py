from datetime import date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from hengce.contracts.enums import QualityStatus, ReviewStatus
from hengce.contracts.market import MarketBar
from hengce.contracts.policy import SourcePolicy


def test_market_bar_rejects_inconsistent_price_range() -> None:
    with pytest.raises(ValidationError):
        MarketBar(
            record_id="bar-1",
            source_id="tushare",
            source_url="https://tushare.pro/",
            collected_at=datetime(2026, 7, 24, 21, 31),
            version="raw-1",
            content_hash="a" * 64,
            license_policy="tushare-daily",
            quality_status=QualityStatus.VALID,
            valid_from=datetime(2026, 7, 24, 21, 31),
            ts_code="600000.SH",
            trade_date=date(2026, 7, 24),
            open=Decimal("10"),
            high=Decimal("9"),
            low=Decimal("8"),
            close=Decimal("9"),
            pre_close=Decimal("9"),
            volume=Decimal("100"),
            amount=Decimal("900"),
        )


def test_source_policy_requires_approved_review_to_be_enabled() -> None:
    with pytest.raises(ValidationError):
        SourcePolicy(
            source_id="sse",
            source_name="上交所",
            allowed_domains=["www.sse.com.cn"],
            allowed_schemes=["https"],
            allowed_purposes=["security_master"],
            fetch_frequency="daily",
            full_text_rule="necessary_public_attachment",
            attachment_rule="pdf_xbrl_only",
            rate_limit_per_minute=6,
            robots_policy="respect",
            terms_url="https://www.sse.com.cn/home/legal/",
            review_status=ReviewStatus.REVIEW_REQUIRED,
            connection_status="UNKNOWN",
            enabled=True,
        )
