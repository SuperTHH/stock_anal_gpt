from decimal import Decimal

from hengce.contracts.enums import StrategyType

from .engine import FactorSpec, StrategyDefinition

DEEP_VALUE_V1 = StrategyDefinition(
    strategy_type=StrategyType.DEEP_VALUE,
    version="deep-value-pilot-v1",
    factors=(
        FactorSpec("absolute_valuation", Decimal("0.25")),
        FactorSpec("relative_valuation", Decimal("0.20")),
        FactorSpec("asset_quality", Decimal("0.20")),
        FactorSpec("cash_debt_quality", Decimal("0.15")),
        FactorSpec("cycle_position", Decimal("0.10"), critical=False),
        FactorSpec("value_trap_safety", Decimal("0.10")),
    ),
)
