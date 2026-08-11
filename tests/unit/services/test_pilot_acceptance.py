import json
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from hengce.acquisition.planner import AcquisitionPlanner
from hengce.contracts.enums import (
    CandidateStatus,
    PoolReadinessStatus,
    QualityStatus,
    StrategyType,
)
from hengce.contracts.official_event import ReportSource
from hengce.contracts.pilot import (
    PilotUniverseMember,
    PilotUniverseSnapshot,
    PoolReadiness,
)
from hengce.contracts.strategy import (
    FactorDetail,
    ReportSnapshot,
    StrategyCandidate,
    StrategyRunEvidence,
)
from hengce.reports.integrity import compute_artifact_hash
from hengce.reports.publisher import ReportPublisher
from hengce.services.pilot_acceptance import PilotAcceptanceValidator
from hengce.services.pilot_universe import PILOT_QUOTAS
from hengce.state.pilot_repository import PilotRepository
from hengce.state.report_repository import ReportRepository
from hengce.state.repository import StateRepository

MARKET_DATE = date(2026, 7, 22)
CUTOFF = datetime(2026, 7, 22, 13, 30, tzinfo=UTC)
NOW = datetime(2026, 7, 30, 4, tzinfo=UTC)
VERSIONS = {
    StrategyType.QUALITY_GROWTH: "quality-growth-pilot-v1",
    StrategyType.DEEP_VALUE: "deep-value-pilot-v1",
    StrategyType.STABLE_DIVIDEND: "stable-dividend-pilot-v1",
}


def _universe_hash_payload(
    members: tuple[PilotUniverseMember, ...],
    quotas: dict[str, int],
) -> str:
    import hashlib

    payload = {
        "market_date": MARKET_DATE.isoformat(),
        "report_cutoff_at": CUTOFF.isoformat(),
        "algorithm_version": "board-liquidity-pilot-v1",
        "quotas": quotas,
        "members": [member.model_dump(mode="json") for member in members],
        "input_hashes": {"market": "a" * 64, "security_master": "b" * 64},
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _universe() -> PilotUniverseSnapshot:
    specs = (
        ("MAIN_SH", "600", ".SH", 8),
        ("MAIN_SZ", "000", ".SZ", 8),
        ("CHINEXT", "300", ".SZ", 7),
        ("STAR", "688", ".SH", 7),
    )
    members = tuple(
        PilotUniverseMember(
            ts_code=f"{prefix}{rank:03d}{suffix}",
            security_name=f"虚构{board}{rank}",
            board=board,
            amount=Decimal(1_000_000 - rank),
            rank_in_board=rank,
            evidence_record_ids=(
                f"bar-{board}-{rank}",
                f"master-{board}-{rank}",
            ),
        )
        for board, prefix, suffix, quota in specs
        for rank in range(1, quota + 1)
    )
    manifest_hash = _universe_hash_payload(members, dict(PILOT_QUOTAS))
    return PilotUniverseSnapshot(
        universe_id=f"pilot-{MARKET_DATE.isoformat()}-{manifest_hash[:16]}",
        market_date=MARKET_DATE,
        report_cutoff_at=CUTOFF,
        algorithm_version="board-liquidity-pilot-v1",
        quotas=dict(PILOT_QUOTAS),
        members=members,
        input_hashes={"market": "a" * 64, "security_master": "b" * 64},
        manifest_hash=manifest_hash,
        created_at=NOW,
    )


def _candidate(strategy: StrategyType) -> StrategyCandidate:
    detail = FactorDetail(
        factor_name="capital_return",
        raw_value=Decimal("18.6"),
        normalized_score=Decimal("80"),
        weight=Decimal("1"),
        weighted_score=Decimal("80"),
        quality_status=QualityStatus.DERIVED,
        source_record_ids=("source-1",),
        normalization_scope="pilot_universe",
        used_market_fallback=False,
    )
    return StrategyCandidate(
        report_date=MARKET_DATE,
        strategy_type=strategy,
        strategy_version=VERSIONS[strategy],
        ts_code="600001.SH",
        rank_in_strategy=1,
        strategy_score=Decimal("80"),
        factor_details=(detail,),
        selection_reasons=("规则模板生成的入选理由",),
        risk_flags=("规则模板生成的风险",),
        catalysts=("暂无可验证官方催化剂",),
        observe_conditions=("下一报告期继续核验",),
        invalidate_conditions=("关键因子不可用时失效",),
        data_completeness=Decimal("1"),
        confidence=Decimal("1"),
        data_cutoff_at=CUTOFF,
        known_at=NOW,
        candidate_status=CandidateStatus.CANDIDATE,
    )


def _build_fixture(tmp_path: Path) -> tuple[Path, ReportRepository]:
    data_dir = tmp_path / "data"
    state = StateRepository(data_dir / "state" / "hengce.sqlite3")
    state.path.parent.mkdir(parents=True)
    state.migrate()
    universe = _universe()
    pilot_repository = PilotRepository(state.path)
    pilot_repository.publish_universe(universe)
    manifest = AcquisitionPlanner().build(universe, NOW)
    assert len(manifest) == 360
    pilot_repository.insert_manifest(manifest)

    report_repository = ReportRepository(state.path)
    pools = {strategy: [_candidate(strategy)] for strategy in StrategyType}
    readiness = {
        strategy: PoolReadiness(
            strategy_type=strategy,
            universe_size=30,
            eligible_count=30,
            complete_factor_count=30,
            coverage_ratio=Decimal("1"),
            required_coverage_ratio=Decimal("0.80"),
            status=PoolReadinessStatus.READY,
            missing_by_security={},
            blocking_codes=(),
            strategy_version=VERSIONS[strategy],
            factor_version="pilot-financial-metrics-v1",
        )
        for strategy in StrategyType
    }
    evidence = {
        strategy: StrategyRunEvidence(
            strategy_type=strategy,
            strategy_version=VERSIONS[strategy],
            input_count=1,
            excluded_count=0,
            data_insufficient_count=0,
            qualified_count=1,
            published_candidate_count=1,
            completed=True,
        )
        for strategy in StrategyType
    }
    result = ReportPublisher(
        report_repository,
        data_dir / "reports",
    ).publish(
        report_id="pilot-report-v1",
        report_date=MARKET_DATE,
        market_cutoff_at=CUTOFF,
        event_cutoff_at=CUTOFF,
        generated_at=NOW,
        candidate_pools=pools,
        data_domain_statuses={
            "market": QualityStatus.VALID,
            "security_master": QualityStatus.VALID,
            "pilot_universe": QualityStatus.VALID,
            "manifest": QualityStatus.VALID,
            "financials": QualityStatus.VALID,
        },
        strategy_evidence=evidence,
        source_records=(
            ReportSource(
                record_id="source-1",
                domain="financials",
                source_name="虚构交易所 XBRL",
                source_url="https://www.sse.com.cn/",
                published_at=CUTOFF,
                effective_at=CUTOFF,
                collected_at=NOW,
                valid_from=NOW,
                version="v1",
                license_policy="personal-research",
                quality_status=QualityStatus.VALID,
            ),
        ),
        pool_readiness=readiness,
        universe_id=universe.universe_id,
        report_cutoff_at=CUTOFF,
        known_at=NOW,
        generation_started_at=NOW,
        quality_summary={
            "manifest_status_distribution": {"PLANNED": 360},
            "xbrl_used_count": 120,
            "pdf_used_count": 30,
            "fallback_reason_counts": {"XBRL_UNAVAILABLE": 30},
            "financial_fact_count": 900,
            "corporate_action_count": 60,
            "share_capital_count": 30,
            "derived_metric_count": 450,
            "narrative_template_versions": {
                strategy.value: "candidate-narrative-v1"
                for strategy in StrategyType
            },
        },
    )
    assert result.published is True
    return data_dir, report_repository


def _validator(*, ignored: bool = True) -> PilotAcceptanceValidator:
    return PilotAcceptanceValidator(ignore_checker=lambda _path: ignored)


def test_source_visibility_allows_contract_optional_publication_times() -> None:
    source = {
        "published_at": None,
        "effective_at": None,
        "collected_at": NOW.isoformat(),
        "valid_from": NOW.isoformat(),
    }

    assert PilotAcceptanceValidator._source_visible(source, CUTOFF, NOW)


def _mutate_artifact(
    repository: ReportRepository,
    mutate: object,
    *,
    preserve_hash: bool,
) -> None:
    stored = repository.latest_report()
    assert stored is not None
    payload = json.loads(stored.artifact_path.read_text(encoding="utf-8"))
    assert callable(mutate)
    mutate(payload)
    path = stored.artifact_path
    if preserve_hash:
        digest = compute_artifact_hash(payload)
        payload["snapshot"]["manifest_hash"] = digest
        new_path = path.with_name(f"{stored.snapshot.report_id}-{digest}.json")
        path = new_path
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    if preserve_hash:
        snapshot = ReportSnapshot.model_validate(payload["snapshot"])
        with sqlite3.connect(repository.path) as connection:
            connection.execute(
                """
                UPDATE report_snapshots
                SET manifest_hash=?, artifact_path=?, payload_json=?
                WHERE report_id=?
                """,
                (
                    snapshot.manifest_hash,
                    str(path),
                    snapshot.model_dump_json(),
                    snapshot.report_id,
                ),
            )


def test_acceptance_summary_passes_and_contains_only_aggregate_counts(
    tmp_path: Path,
) -> None:
    data_dir, _repository = _build_fixture(tmp_path)

    summary = _validator().validate(
        data_dir=data_dir,
        market_date=MARKET_DATE,
        report_cutoff_at=CUTOFF,
    )

    assert summary.passed is True
    assert summary.errors == ()
    assert summary.universe_count == 30
    assert summary.board_quotas == PILOT_QUOTAS
    assert summary.manifest_total == 360
    assert summary.periodic_report_count == 150
    assert summary.xbrl_used_count == 120
    assert summary.pdf_used_count == 30
    assert summary.pool_candidate_counts == {
        "DEEP_VALUE": 1,
        "QUALITY_GROWTH": 1,
        "STABLE_DIVIDEND": 1,
    }
    serialized = summary.model_dump_json()
    assert "600001.SH" not in serialized
    assert "虚构MAIN_SH" not in serialized


def test_wrong_board_quota_is_rejected(tmp_path: Path) -> None:
    data_dir, _repository = _build_fixture(tmp_path)
    database = data_dir / "state" / "hengce.sqlite3"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT payload_json FROM pilot_universes"
        ).fetchone()
        payload = json.loads(str(row[0]))
        payload["quotas"]["MAIN_SH"] = 7
        payload["members"] = [
            member
            for member in payload["members"]
            if not (
                member["board"] == "MAIN_SH"
                and member["rank_in_board"] == 8
            )
        ]
        connection.execute(
            "UPDATE pilot_universes SET payload_json=?",
            (json.dumps(payload, ensure_ascii=False),),
        )

    summary = _validator().validate(
        data_dir=data_dir,
        market_date=MARKET_DATE,
        report_cutoff_at=CUTOFF,
    )

    assert "PILOT_QUOTA_INVALID" in summary.errors


def test_missing_manifest_item_is_rejected(tmp_path: Path) -> None:
    data_dir, _repository = _build_fixture(tmp_path)
    with sqlite3.connect(data_dir / "state" / "hengce.sqlite3") as connection:
        connection.execute(
            """
            DELETE FROM acquisition_manifest_items
            WHERE item_id=(
                SELECT item_id FROM acquisition_manifest_items LIMIT 1
            )
            """
        )

    summary = _validator().validate(
        data_dir=data_dir,
        market_date=MARKET_DATE,
        report_cutoff_at=CUTOFF,
    )

    assert "ACQUISITION_MANIFEST_COUNT_INVALID" in summary.errors


def test_universe_and_artifact_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    data_dir, repository = _build_fixture(tmp_path)
    with sqlite3.connect(repository.path) as connection:
        row = connection.execute(
            "SELECT payload_json FROM pilot_universes"
        ).fetchone()
        payload = json.loads(str(row[0]))
        payload["manifest_hash"] = "f" * 64
        connection.execute(
            "UPDATE pilot_universes SET payload_json=?, manifest_hash=?",
            (json.dumps(payload, ensure_ascii=False), "f" * 64),
        )
    _mutate_artifact(
        repository,
        lambda payload: payload["quality_summary"].update(
            {"financial_fact_count": 901}
        ),
        preserve_hash=False,
    )

    summary = _validator().validate(
        data_dir=data_dir,
        market_date=MARKET_DATE,
        report_cutoff_at=CUTOFF,
    )

    assert "PILOT_UNIVERSE_HASH_MISMATCH" in summary.errors
    assert "REPORT_ARTIFACT_HASH_MISMATCH" in summary.errors


def test_latest_pointer_mismatch_is_rejected(tmp_path: Path) -> None:
    data_dir, repository = _build_fixture(tmp_path)
    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            "UPDATE report_pointer SET report_id='missing-report' WHERE pointer_name='latest'"
        )

    summary = _validator().validate(
        data_dir=data_dir,
        market_date=MARKET_DATE,
        report_cutoff_at=CUTOFF,
    )

    assert "REPORT_LATEST_POINTER_INVALID" in summary.errors


def test_candidate_with_broken_source_lineage_is_rejected(tmp_path: Path) -> None:
    data_dir, repository = _build_fixture(tmp_path)
    _mutate_artifact(
        repository,
        lambda payload: payload.update({"source_records": []}),
        preserve_hash=True,
    )

    summary = _validator().validate(
        data_dir=data_dir,
        market_date=MARKET_DATE,
        report_cutoff_at=CUTOFF,
    )

    assert "CANDIDATE_SOURCE_LINEAGE_INVALID" in summary.errors


def test_sensitive_runtime_path_not_ignored_is_rejected(tmp_path: Path) -> None:
    data_dir, _repository = _build_fixture(tmp_path)
    validator = PilotAcceptanceValidator(
        ignore_checker=lambda path: path != data_dir / "manual_inbox",
    )

    summary = validator.validate(
        data_dir=data_dir,
        market_date=MARKET_DATE,
        report_cutoff_at=CUTOFF,
    )

    assert "SENSITIVE_RUNTIME_PATH_NOT_IGNORED" in summary.errors
