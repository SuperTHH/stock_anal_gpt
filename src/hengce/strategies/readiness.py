from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from hengce.contracts.enums import (
    PoolReadinessStatus,
    QualityStatus,
    StrategyType,
)
from hengce.contracts.pilot import PilotUniverseSnapshot, PoolReadiness
from hengce.contracts.strategy import (
    StrategyCandidate,
    StrategyRunEvidence,
)
from hengce.strategies.engine import (
    SecurityStrategyInput,
    StrategyDefinition,
    StrategyEngine,
)

FACTOR_VERSION = "pilot-financial-metrics-v1"


class PoolReadinessEvaluator:
    def evaluate(
        self,
        definition: StrategyDefinition,
        inputs: Sequence[SecurityStrategyInput],
        universe: PilotUniverseSnapshot,
    ) -> PoolReadiness:
        by_code = {item.ts_code: item for item in inputs}
        if len(by_code) != len(inputs):
            raise ValueError("POOL_INPUT_SECURITY_DUPLICATE")
        universe_codes = {member.ts_code for member in universe.members}
        if not set(by_code).issubset(universe_codes):
            raise ValueError("POOL_INPUT_OUTSIDE_UNIVERSE")

        eligible_count = 0
        complete_count = 0
        missing_by_security: dict[str, tuple[str, ...]] = {}
        critical = tuple(
            factor for factor in definition.factors if factor.critical
        )
        for member in universe.members:
            item = by_code.get(member.ts_code)
            if item is None:
                missing_by_security[member.ts_code] = ("SECURITY_INPUT_MISSING",)
                continue
            eligible = item.hard_filter_passed and not (
                definition.strategy_type is StrategyType.STABLE_DIVIDEND
                and not item.announced_dividend_only
            )
            if not eligible:
                missing_by_security[member.ts_code] = (
                    "SECURITY_NOT_ELIGIBLE",
                )
                continue
            eligible_count += 1
            missing: list[str] = []
            for spec in critical:
                factor = item.factors.get(spec.name)
                if (
                    factor is None
                    or factor.value is None
                    or factor.quality_status
                    not in {QualityStatus.VALID, QualityStatus.DERIVED}
                ):
                    missing.append(f"{spec.name}:VALUE_MISSING")
                elif not factor.source_record_ids:
                    missing.append(f"{spec.name}:SOURCE_LINEAGE_MISSING")
                elif any(
                    timestamp.tzinfo is None
                    or timestamp.utcoffset() is None
                    for timestamp in (
                        factor.published_at,
                        factor.effective_at,
                        factor.collected_at,
                        factor.valid_from,
                    )
                ):
                    missing.append(f"{spec.name}:TIME_LINEAGE_INVALID")
            if missing:
                missing_by_security[member.ts_code] = tuple(sorted(missing))
            else:
                complete_count += 1

        universe_size = len(universe.members)
        coverage = Decimal(complete_count) / Decimal(universe_size)
        ready = coverage >= Decimal("0.80")
        return PoolReadiness(
            strategy_type=definition.strategy_type,
            universe_size=universe_size,
            eligible_count=eligible_count,
            complete_factor_count=complete_count,
            coverage_ratio=coverage,
            required_coverage_ratio=Decimal("0.80"),
            status=(
                PoolReadinessStatus.READY
                if ready
                else PoolReadinessStatus.BLOCKED
            ),
            missing_by_security=missing_by_security,
            blocking_codes=(
                ()
                if ready
                else ("POOL_FACTOR_COVERAGE_BELOW_80_PERCENT",)
            ),
            strategy_version=definition.version,
            factor_version=FACTOR_VERSION,
        )


@dataclass(frozen=True, slots=True)
class IndependentPoolResult:
    readiness: PoolReadiness
    candidates: tuple[StrategyCandidate, ...]
    evidence: StrategyRunEvidence | None


class IndependentPoolRunner:
    def __init__(
        self,
        evaluator: PoolReadinessEvaluator | None = None,
    ) -> None:
        self._evaluator = evaluator or PoolReadinessEvaluator()

    def run(
        self,
        *,
        definitions: Mapping[StrategyType, StrategyDefinition],
        inputs: Mapping[StrategyType, Sequence[SecurityStrategyInput]],
        universe: PilotUniverseSnapshot,
        report_date: date,
        report_cutoff_at: datetime,
        known_at: datetime,
    ) -> dict[StrategyType, IndependentPoolResult]:
        if set(definitions) != set(StrategyType) or set(inputs) != set(
            StrategyType
        ):
            raise ValueError("PILOT_POOL_SET_INCOMPLETE")
        results: dict[StrategyType, IndependentPoolResult] = {}
        for strategy_type in StrategyType:
            definition = definitions[strategy_type]
            if definition.strategy_type is not strategy_type:
                raise ValueError("PILOT_POOL_DEFINITION_MISMATCH")
            strategy_inputs = tuple(inputs[strategy_type])
            readiness = self._evaluator.evaluate(
                definition,
                strategy_inputs,
                universe,
            )
            if readiness.status is PoolReadinessStatus.BLOCKED:
                results[strategy_type] = IndependentPoolResult(
                    readiness=readiness,
                    candidates=(),
                    evidence=None,
                )
                continue
            evaluation = StrategyEngine(definition).evaluate(
                list(strategy_inputs),
                report_date=report_date,
                data_cutoff_at=report_cutoff_at,
                known_at=known_at,
            )
            results[strategy_type] = IndependentPoolResult(
                readiness=readiness,
                candidates=evaluation.candidates,
                evidence=evaluation.evidence,
            )
        return results


__all__ = [
    "FACTOR_VERSION",
    "IndependentPoolResult",
    "IndependentPoolRunner",
    "PoolReadinessEvaluator",
]
