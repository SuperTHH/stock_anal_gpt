from decimal import Decimal

from hengce.contracts.enums import StrategyType

from .engine import FactorSpec, StrategyDefinition

STABLE_DIVIDEND_V1 = StrategyDefinition(
    strategy_type=StrategyType.STABLE_DIVIDEND,
    version="stable-dividend-pilot-v1",
    factors=(
        FactorSpec("dividend_yield", Decimal("0.25")),
        FactorSpec("dividend_continuity", Decimal("0.20")),
        FactorSpec("payout_sustainability", Decimal("0.15")),
        FactorSpec("cashflow_coverage", Decimal("0.20")),
        FactorSpec("balance_sheet_quality", Decimal("0.10"), critical=False),
        FactorSpec("dividend_cut_safety", Decimal("0.10")),
    ),
)
