from datetime import date, timedelta
from decimal import Decimal

import pytest

from hengce.contracts.enums import QualityStatus
from hengce.contracts.market import SecurityMaster, TradingStatus
from hengce.strategies.filters import (
    HardFilterConfig,
    HardFilterEngine,
    SecurityResearchInput,
)

REPORT_DATE = date(2026, 7, 29)


def eligible_input(**updates: object) -> SecurityResearchInput:
    payload: dict[str, object] = {
        "security": SecurityMaster(
            ts_code="699999.SH",
            symbol="699999",
            name="示例公司",
            exchange="SSE",
            board="MAIN_SH",
            list_date=REPORT_DATE - timedelta(days=1000),
            industry_l1="电子",
            is_in_scope=True,
        ),
        "trading_status": TradingStatus(
            ts_code="699999.SH",
            trade_date=REPORT_DATE,
            is_trading=True,
            is_suspended=False,
        ),
        "report_date": REPORT_DATE,
        "recent_amounts": tuple(Decimal("60000000") for _ in range(60)),
        "audit_opinion_standard": True,
        "major_investigation_open": False,
        "financial_completeness": Decimal("0.90"),
        "total_return_quality": QualityStatus.DERIVED,
        "publication_order_known": True,
        "financial_age_days": 120,
        "evidence_record_ids": (
            "security-master-1",
            "trading-status-1",
            "audit-opinion-1",
            "investigation-screen-1",
        ),
    }
    payload.update(updates)
    return SecurityResearchInput(**payload)


def config() -> HardFilterConfig:
    return HardFilterConfig(
        filter_version="hard-filter-v1",
        minimum_listing_days=365,
        minimum_liquidity_days=60,
        minimum_average_amount=Decimal("50000000"),
        minimum_financial_completeness=Decimal("0.85"),
        maximum_financial_age_days=550,
    )


def test_eligible_security_passes_all_twelve_filters() -> None:
    result = HardFilterEngine(config()).evaluate(eligible_input())
    assert result.passed is True
    assert result.reasons == ()
    assert result.filter_version == "hard-filter-v1"
    assert result.source_record_ids == (
        "security-master-1",
        "trading-status-1",
        "audit-opinion-1",
        "investigation-screen-1",
    )


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        (
            {"security": eligible_input().security.model_copy(update={"is_in_scope": False})},
            "HF-01",
        ),
        (
            {
                "trading_status": eligible_input().trading_status.model_copy(
                    update={"delisting_risk": True}
                )
            },
            "HF-02",
        ),
        (
            {
                "trading_status": eligible_input().trading_status.model_copy(
                    update={"st_status": "*ST"}
                )
            },
            "HF-03",
        ),
        (
            {
                "trading_status": eligible_input().trading_status.model_copy(
                    update={"is_suspended": True, "is_trading": False}
                )
            },
            "HF-04",
        ),
        (
            {
                "security": eligible_input().security.model_copy(
                    update={"list_date": REPORT_DATE - timedelta(days=364)}
                )
            },
            "HF-05",
        ),
        ({"recent_amounts": tuple(Decimal("49999999") for _ in range(60))}, "HF-06"),
        ({"audit_opinion_standard": False}, "HF-07"),
        ({"major_investigation_open": True}, "HF-08"),
        ({"financial_completeness": Decimal("0.84")}, "HF-09"),
        ({"total_return_quality": QualityStatus.UNVERIFIED}, "HF-10"),
        ({"publication_order_known": False}, "HF-11"),
        ({"financial_age_days": 551}, "HF-12"),
    ],
)
def test_each_hard_filter_has_a_stable_machine_reason(
    updates: dict[str, object],
    reason: str,
) -> None:
    """Catches silently weakening any PRD HF-01 through HF-12 rule."""
    result = HardFilterEngine(config()).evaluate(eligible_input(**updates))
    assert result.passed is False
    assert reason in result.reasons


def test_insufficient_liquidity_history_fails_instead_of_averaging_available_days() -> None:
    """Catches a newly listed or sparse security passing on a handful of liquid sessions."""
    result = HardFilterEngine(config()).evaluate(
        eligible_input(recent_amounts=tuple(Decimal("100000000") for _ in range(59)))
    )
    assert result.reasons == ("HF-06",)
