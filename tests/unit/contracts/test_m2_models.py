from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from hengce.contracts.derived import DerivedFinancialMetric
from hengce.contracts.enums import ActionStatus, ActionType, QualityStatus
from hengce.contracts.market import CorporateAction

NOW = datetime(2026, 7, 29, 12, tzinfo=UTC)
HASH = "a" * 64


def action_payload() -> dict[str, object]:
    return {
        "record_id": "action-1",
        "source_id": "sse",
        "source_url": "https://www.sse.com.cn/disclosure/action-1.pdf",
        "published_at": NOW,
        "effective_at": datetime(2026, 6, 15, tzinfo=UTC),
        "collected_at": NOW,
        "version": "official-v1",
        "content_hash": HASH,
        "license_policy": "sse-personal-research",
        "quality_status": QualityStatus.VALID,
        "valid_from": NOW,
        "ts_code": "699999.SH",
        "action_type": ActionType.CASH_DIVIDEND,
        "record_date": date(2026, 6, 14),
        "ex_date": date(2026, 6, 15),
        "pay_date": date(2026, 6, 20),
        "cash_dividend_per_share": Decimal("0.50"),
        "stock_dividend_ratio": None,
        "split_ratio": None,
        "rights_ratio": None,
        "rights_price": None,
        "action_status": ActionStatus.IMPLEMENTED,
    }


def test_cash_dividend_requires_publication_and_positive_cash_amount() -> None:
    """Catches accepting a cash action that cannot be ordered or has no economic value."""
    assert CorporateAction.model_validate(action_payload()).cash_dividend_per_share == Decimal(
        "0.50"
    )
    with pytest.raises(ValidationError):
        CorporateAction.model_validate({**action_payload(), "published_at": None})
    with pytest.raises(ValidationError):
        CorporateAction.model_validate(
            {**action_payload(), "cash_dividend_per_share": Decimal("0")}
        )


def test_rights_issue_requires_ratio_and_price_and_valid_date_order() -> None:
    """Catches silently treating an incomplete rights issue as an executable action."""
    valid = {
        **action_payload(),
        "action_type": ActionType.RIGHTS_ISSUE,
        "cash_dividend_per_share": None,
        "rights_ratio": Decimal("0.30"),
        "rights_price": Decimal("8.50"),
    }
    assert CorporateAction.model_validate(valid).rights_price == Decimal("8.50")
    with pytest.raises(ValidationError):
        CorporateAction.model_validate({**valid, "rights_price": None})
    with pytest.raises(ValidationError):
        CorporateAction.model_validate({**valid, "record_date": date(2026, 6, 16)})


def metric_payload() -> dict[str, object]:
    return {
        "record_id": "metric-1",
        "source_id": "derived",
        "source_url": "https://localhost.invalid/lineage/metric-1",
        "published_at": NOW,
        "effective_at": NOW,
        "collected_at": NOW,
        "version": "metrics-v1",
        "content_hash": HASH,
        "license_policy": "derived-from-approved-facts",
        "quality_status": QualityStatus.DERIVED,
        "valid_from": NOW,
        "ts_code": "699999.SH",
        "report_period": date(2025, 12, 31),
        "metric_name": "roe",
        "metric_value": Decimal("0.135"),
        "unit": "ratio",
        "as_of": NOW,
        "known_at": NOW,
        "input_fact_ids": ("net-profit-1", "equity-1"),
        "algorithm_version": "financial-metrics-v1",
    }


def test_derived_metric_requires_explicit_cutoffs_and_lineage() -> None:
    """Catches creating a metric that cannot be reproduced as-of a historical report."""
    metric = DerivedFinancialMetric.model_validate(metric_payload())
    assert metric.input_fact_ids == ("net-profit-1", "equity-1")
    with pytest.raises(ValidationError):
        DerivedFinancialMetric.model_validate({**metric_payload(), "input_fact_ids": ()})
    with pytest.raises(ValidationError):
        DerivedFinancialMetric.model_validate(
            {**metric_payload(), "as_of": datetime(2026, 7, 29, 12)}
        )


def test_derived_metric_rejects_non_derived_quality() -> None:
    """Catches a calculated value masquerading as a directly sourced valid fact."""
    with pytest.raises(ValidationError):
        DerivedFinancialMetric.model_validate(
            {**metric_payload(), "quality_status": QualityStatus.VALID}
        )


def test_derived_metric_preserves_formula_assumptions() -> None:
    """Catches presenting an explicit tax proxy as if it were a sourced company value."""
    metric = DerivedFinancialMetric.model_validate(
        {
            **metric_payload(),
            "metric_name": "roic_2025",
            "formula_metadata": {
                "tax_rate_proxy": "0.25",
                "tax_rate_kind": "pilot_proxy_not_company_actual",
            },
        }
    )

    assert metric.formula_metadata == {
        "tax_rate_proxy": "0.25",
        "tax_rate_kind": "pilot_proxy_not_company_actual",
    }
