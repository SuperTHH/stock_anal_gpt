from decimal import Decimal

from hengce.contracts.enums import StrategyType

from .engine import FactorSpec, StrategyDefinition

QUALITY_GROWTH_V1 = StrategyDefinition(
    strategy_type=StrategyType.QUALITY_GROWTH,
    version="quality-growth-pilot-v1",
    factors=(
        FactorSpec("capital_return", Decimal("0.20")),
        FactorSpec("growth_quality", Decimal("0.20")),
        FactorSpec("cash_flow_quality", Decimal("0.20")),
        FactorSpec(
            "profitability_stability",
            Decimal("0.10"),
            higher_is_better=False,
            critical=False,
        ),
        FactorSpec("balance_sheet_quality", Decimal("0.10"), critical=False),
        FactorSpec("valuation_attractiveness", Decimal("0.20")),
    ),
)
