from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from hengce.contracts.enums import (
    CandidateStatus,
    QualityStatus,
    StrategyType,
)
from hengce.contracts.strategy import (
    FactorDetail,
    StrategyCandidate,
    StrategyRunEvidence,
)


@dataclass(frozen=True, slots=True)
class FactorInput:
    value: Decimal | None
    quality_status: QualityStatus
    source_record_ids: tuple[str, ...]
    published_at: datetime
    effective_at: datetime
    collected_at: datetime
    valid_from: datetime


@dataclass(frozen=True, slots=True)
class FactorSpec:
    name: str
    weight: Decimal
    higher_is_better: bool = True
    critical: bool = True


@dataclass(frozen=True, slots=True)
class StrategyDefinition:
    strategy_type: StrategyType
    version: str
    factors: tuple[FactorSpec, ...]

    def __post_init__(self) -> None:
        if sum((factor.weight for factor in self.factors), Decimal(0)) != Decimal(1):
            raise ValueError("STRATEGY_WEIGHTS_INVALID")


@dataclass(frozen=True, slots=True)
class SecurityStrategyInput:
    ts_code: str
    industry_l1: str | None
    factors: dict[str, FactorInput]
    hard_filter_passed: bool
    selection_reasons: tuple[str, ...]
    risk_flags: tuple[str, ...]
    catalysts: tuple[str, ...]
    observe_conditions: tuple[str, ...]
    invalidate_conditions: tuple[str, ...]
    cycle_position_available: bool
    announced_dividend_only: bool


@dataclass(frozen=True, slots=True)
class StrategyEvaluationResult:
    candidates: tuple[StrategyCandidate, ...]
    evidence: StrategyRunEvidence


class StrategyEngine:
    def __init__(self, definition: StrategyDefinition) -> None:
        self.definition = definition

    def rank(
        self,
        inputs: list[SecurityStrategyInput],
        *,
        report_date: date,
        data_cutoff_at: datetime,
        known_at: datetime,
    ) -> list[StrategyCandidate]:
        return list(
            self.evaluate(
                inputs,
                report_date=report_date,
                data_cutoff_at=data_cutoff_at,
                known_at=known_at,
            ).candidates
        )

    def evaluate(
        self,
        inputs: list[SecurityStrategyInput],
        *,
        report_date: date,
        data_cutoff_at: datetime,
        known_at: datetime,
    ) -> StrategyEvaluationResult:
        if data_cutoff_at.tzinfo is None or data_cutoff_at.utcoffset() is None:
            raise ValueError("STRATEGY_CUTOFF_INVALID")
        if known_at.tzinfo is None or known_at.utcoffset() is None:
            raise ValueError("STRATEGY_KNOWN_AT_INVALID")
        excluded = [item for item in inputs if self._excluded(item)]
        remaining = [item for item in inputs if not self._excluded(item)]
        insufficient = [
            item
            for item in remaining
            if self._critical_data_insufficient(item, data_cutoff_at, known_at)
        ]
        eligible = [item for item in remaining if item not in insufficient]
        scored: list[tuple[SecurityStrategyInput, tuple[FactorDetail, ...], Decimal]] = []
        for item in eligible:
            details = tuple(
                self._factor_detail(
                    item,
                    spec,
                    eligible,
                    data_cutoff_at,
                    known_at,
                )
                for spec in self.definition.factors
            )
            score = sum(
                (
                    detail.weighted_score
                    for detail in details
                    if detail.weighted_score is not None
                ),
                Decimal(0),
            )
            scored.append((item, details, score))
        scored.sort(key=lambda row: (-row[2], row[0].ts_code))

        candidates: list[StrategyCandidate] = []
        for rank, (item, details, score) in enumerate(scored[:30], start=1):
            completeness = sum(
                (
                    detail.weight
                    for detail in details
                    if detail.weighted_score is not None
                ),
                Decimal(0),
            )
            status = CandidateStatus.CANDIDATE if score >= Decimal(60) else CandidateStatus.WATCH
            if (
                self.definition.strategy_type is StrategyType.DEEP_VALUE
                and not item.cycle_position_available
            ):
                status = CandidateStatus.WATCH
            candidates.append(
                StrategyCandidate(
                    report_date=report_date,
                    strategy_type=self.definition.strategy_type,
                    strategy_version=self.definition.version,
                    ts_code=item.ts_code,
                    rank_in_strategy=rank,
                    strategy_score=score.quantize(Decimal("0.01")),
                    factor_details=details,
                    selection_reasons=item.selection_reasons,
                    risk_flags=item.risk_flags,
                    catalysts=item.catalysts,
                    observe_conditions=item.observe_conditions,
                    invalidate_conditions=item.invalidate_conditions,
                    data_completeness=completeness,
                    confidence=completeness,
                    data_cutoff_at=data_cutoff_at,
                    known_at=known_at,
                    candidate_status=status,
                )
            )
        evidence = StrategyRunEvidence(
            strategy_type=self.definition.strategy_type,
            strategy_version=self.definition.version,
            input_count=len(inputs),
            excluded_count=len(excluded),
            data_insufficient_count=len(insufficient),
            qualified_count=len(scored),
            published_candidate_count=len(candidates),
            completed=True,
        )
        return StrategyEvaluationResult(tuple(candidates), evidence)

    def _excluded(self, item: SecurityStrategyInput) -> bool:
        if not item.hard_filter_passed:
            return True
        if (
            self.definition.strategy_type is StrategyType.STABLE_DIVIDEND
            and not item.announced_dividend_only
        ):
            return True
        return False

    def _critical_data_insufficient(
        self,
        item: SecurityStrategyInput,
        data_cutoff_at: datetime,
        known_at: datetime,
    ) -> bool:
        return any(
            spec.critical
            and (
                (factor := item.factors.get(spec.name)) is None
                or not self._factor_available(factor, data_cutoff_at, known_at)
            )
            for spec in self.definition.factors
        )

    def _factor_detail(
        self,
        item: SecurityStrategyInput,
        spec: FactorSpec,
        population: list[SecurityStrategyInput],
        data_cutoff_at: datetime,
        known_at: datetime,
    ) -> FactorDetail:
        factor = item.factors.get(spec.name)
        if factor is None or not self._factor_available(
            factor,
            data_cutoff_at,
            known_at,
        ):
            return FactorDetail(
                factor_name=spec.name,
                raw_value=None,
                normalized_score=None,
                weight=spec.weight,
                weighted_score=None,
                quality_status=QualityStatus.MISSING,
                source_record_ids=(
                    factor.source_record_ids if factor is not None else ("missing-input",)
                ),
                normalization_scope="unavailable",
                used_market_fallback=False,
            )

        values = self._values(
            population,
            spec.name,
            data_cutoff_at=data_cutoff_at,
            known_at=known_at,
        )
        winsorized = self._winsorize(values)
        raw = self._winsorize_value(factor.value, values)
        normalized = self._percentile(raw, winsorized)
        if not spec.higher_is_better:
            normalized = Decimal(100) - normalized
        return FactorDetail(
            factor_name=spec.name,
            raw_value=factor.value,
            normalized_score=normalized,
            weight=spec.weight,
            weighted_score=normalized * spec.weight,
            quality_status=factor.quality_status,
            source_record_ids=factor.source_record_ids,
            normalization_scope="pilot_universe",
            used_market_fallback=False,
        )

    @staticmethod
    def _values(
        population: list[SecurityStrategyInput],
        factor_name: str,
        *,
        data_cutoff_at: datetime,
        known_at: datetime,
    ) -> list[Decimal]:
        values = [
            factor.value
            for item in population
            if (factor := item.factors.get(factor_name)) is not None
            and StrategyEngine._factor_available(
                factor,
                data_cutoff_at,
                known_at,
            )
        ]
        return sorted(value for value in values if value is not None)

    @staticmethod
    def _factor_available(
        factor: FactorInput,
        data_cutoff_at: datetime,
        known_at: datetime,
    ) -> bool:
        return (
            factor.value is not None
            and factor.quality_status in {QualityStatus.DERIVED, QualityStatus.VALID}
            and factor.published_at <= data_cutoff_at
            and factor.effective_at <= data_cutoff_at
            and factor.collected_at <= known_at
            and factor.valid_from <= known_at
        )

    @classmethod
    def _winsorize(cls, values: list[Decimal]) -> list[Decimal]:
        if not values:
            return []
        low = cls._quantile(values, Decimal("0.01"))
        high = cls._quantile(values, Decimal("0.99"))
        return [min(max(value, low), high) for value in values]

    @classmethod
    def _winsorize_value(cls, value: Decimal, values: list[Decimal]) -> Decimal:
        if not values:
            return value
        return min(
            max(value, cls._quantile(values, Decimal("0.01"))),
            cls._quantile(values, Decimal("0.99")),
        )

    @staticmethod
    def _quantile(values: list[Decimal], percentile: Decimal) -> Decimal:
        if len(values) == 1:
            return values[0]
        position = percentile * Decimal(len(values) - 1)
        lower = int(position)
        upper = min(lower + 1, len(values) - 1)
        fraction = position - Decimal(lower)
        return values[lower] + (values[upper] - values[lower]) * fraction

    @staticmethod
    def _percentile(value: Decimal, values: list[Decimal]) -> Decimal:
        if len(values) <= 1:
            return Decimal(50)
        below = sum(1 for candidate in values if candidate < value)
        equal = sum(1 for candidate in values if candidate == value)
        rank = Decimal(below) + Decimal(equal - 1) / Decimal(2)
        return rank / Decimal(len(values) - 1) * Decimal(100)
