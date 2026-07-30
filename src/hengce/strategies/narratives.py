from __future__ import annotations

from dataclasses import dataclass

from hengce.contracts.enums import StrategyType
from hengce.contracts.strategy import FactorDetail, StrategyCandidate

_TEMPLATE_VERSION = "candidate-narrative-v1"
_STRATEGY_LABELS = {
    StrategyType.QUALITY_GROWTH: "质量成长",
    StrategyType.DEEP_VALUE: "低估值价值",
    StrategyType.STABLE_DIVIDEND: "稳定高股息",
}
_FACTOR_LABELS = {
    "capital_return": "资本回报",
    "growth_quality": "增长质量",
    "cash_flow_quality": "现金流质量",
    "profitability_stability": "盈利稳定性",
    "balance_sheet_quality": "资产负债表质量",
    "valuation_attractiveness": "估值吸引力",
    "absolute_valuation": "绝对估值",
    "relative_valuation": "相对估值",
    "asset_quality": "资产质量",
    "cash_debt_quality": "现金债务质量",
    "cycle_position": "周期位置",
    "value_trap_safety": "价值陷阱安全度",
    "dividend_yield": "已公告股息率",
    "dividend_continuity": "分红连续性",
    "payout_sustainability": "派息可持续性",
    "cashflow_coverage": "自由现金流覆盖",
    "dividend_cut_safety": "分红削减安全度",
}


@dataclass(frozen=True, slots=True)
class NarrativeStatement:
    text: str
    template_id: str
    template_version: str
    trigger_factor_names: tuple[str, ...]
    source_record_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CandidateNarrative:
    selection_reasons: tuple[NarrativeStatement, ...]
    risks: tuple[NarrativeStatement, ...]
    observe_conditions: tuple[NarrativeStatement, ...]
    invalidate_conditions: tuple[NarrativeStatement, ...]
    catalysts: tuple[NarrativeStatement, ...]


class RuleNarrativeRenderer:
    def render(
        self,
        strategy_type: StrategyType,
        candidate: StrategyCandidate,
    ) -> CandidateNarrative:
        if candidate.strategy_type is not strategy_type:
            raise ValueError("NARRATIVE_STRATEGY_MISMATCH")
        factors = tuple(
            sorted(
                (
                    detail
                    for detail in candidate.factor_details
                    if detail.raw_value is not None
                    and detail.normalized_score is not None
                ),
                key=lambda detail: (
                    -detail.normalized_score,
                    detail.factor_name,
                ),
            )[:2]
        )
        factor_names = tuple(detail.factor_name for detail in factors)
        source_ids = _source_ids(factors)
        strategy_label = _STRATEGY_LABELS[strategy_type]
        factor_text = "，".join(
            f"{_FACTOR_LABELS.get(detail.factor_name, detail.factor_name)}"
            f"原始值{detail.raw_value}"
            for detail in factors
        )
        selection = NarrativeStatement(
            text=f"{strategy_label}入选依据：{factor_text}。",
            template_id=f"{strategy_type.value.lower()}.selection.top-factors",
            template_version=_TEMPLATE_VERSION,
            trigger_factor_names=factor_names,
            source_record_ids=source_ids,
        )
        risk = NarrativeStatement(
            text=f"{strategy_label}风险：关键因子回落或数据质量下降会降低置信度。",
            template_id=f"{strategy_type.value.lower()}.risk.factor-deterioration",
            template_version=_TEMPLATE_VERSION,
            trigger_factor_names=factor_names,
            source_record_ids=source_ids,
        )
        observe = NarrativeStatement(
            text=f"观察条件：下一报告期继续核验{_joined_labels(factors)}。",
            template_id=f"{strategy_type.value.lower()}.observe.next-period",
            template_version=_TEMPLATE_VERSION,
            trigger_factor_names=factor_names,
            source_record_ids=source_ids,
        )
        invalidate = NarrativeStatement(
            text="失效条件：关键因子不可用、谱系缺失或触发通用硬过滤。",
            template_id=f"{strategy_type.value.lower()}.invalidate.hard-filter",
            template_version=_TEMPLATE_VERSION,
            trigger_factor_names=factor_names,
            source_record_ids=source_ids,
        )
        catalyst = NarrativeStatement(
            text="暂无可验证官方催化剂",
            template_id=f"{strategy_type.value.lower()}.catalyst.none-verified",
            template_version=_TEMPLATE_VERSION,
            trigger_factor_names=(),
            source_record_ids=(),
        )
        return CandidateNarrative(
            selection_reasons=(selection,),
            risks=(risk,),
            observe_conditions=(observe,),
            invalidate_conditions=(invalidate,),
            catalysts=(catalyst,),
        )


def _source_ids(
    factors: tuple[FactorDetail, ...],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                record_id
                for factor in factors
                for record_id in factor.source_record_ids
            }
        )
    )


def _joined_labels(factors: tuple[FactorDetail, ...]) -> str:
    return "与".join(
        _FACTOR_LABELS.get(factor.factor_name, factor.factor_name)
        for factor in factors
    )


__all__ = [
    "CandidateNarrative",
    "NarrativeStatement",
    "RuleNarrativeRenderer",
]
