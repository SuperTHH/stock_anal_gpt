import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest

from hengce.contracts.enums import ReviewStatus, RunStatus
from hengce.contracts.market import SecurityMaster
from hengce.contracts.policy import SourcePolicy
from hengce.contracts.run import RefusalRecord, RunRecord
from hengce.policy.guard import PolicyDenied
from hengce.state.repository import SecurityMasterSnapshot, StateRepository


def approved_policy() -> SourcePolicy:
    return SourcePolicy(
        source_id="tushare",
        source_name="Tushare",
        allowed_domains=["api.tushare.pro"],
        allowed_schemes=["http"],
        allowed_purposes=["market_daily"],
        fetch_frequency="trading_day",
        full_text_rule="structured_only",
        attachment_rule="none",
        rate_limit_per_minute=1,
        robots_policy="api_terms",
        terms_url="https://tushare.pro/document/1?doc_id=290",
        terms_reviewed_at=datetime(2026, 7, 24, 9, 0, tzinfo=UTC),
        review_status=ReviewStatus.APPROVED,
        connection_status="UNKNOWN",
        enabled=True,
    )


def security_master_policy(**overrides: object) -> SourcePolicy:
    values: dict[str, object] = {
        "source_id": "sse",
        "source_name": "SSE",
        "allowed_domains": ["sse.com.cn", "www.sse.com.cn"],
        "allowed_schemes": ["https"],
        "allowed_purposes": ["security_master"],
        "fetch_frequency": "policy_defined",
        "full_text_rule": "necessary_public_attachment",
        "attachment_rule": "pdf_xbrl_only",
        "rate_limit_per_minute": 6,
        "robots_policy": "respect",
        "terms_url": "https://www.sse.com.cn/home/legal/",
        "terms_reviewed_at": datetime(2026, 7, 24, 9, 0, tzinfo=UTC),
        "review_status": ReviewStatus.APPROVED,
        "connection_status": "UNKNOWN",
        "enabled": True,
    }
    values.update(overrides)
    return SourcePolicy.model_validate(values)


def completed_run() -> RunRecord:
    return RunRecord(
        run_id="daily-2026-07-24",
        trade_date=date(2026, 7, 24),
        run_type="market_daily",
        started_at=datetime(2026, 7, 24, 15, 0, tzinfo=UTC),
        finished_at=datetime(2026, 7, 24, 15, 5, tzinfo=UTC),
        run_status=RunStatus.SUCCEEDED,
        stage_statuses={"collect": "SUCCEEDED"},
    )


def refusal() -> RefusalRecord:
    return RefusalRecord(
        refusal_id="refusal-1",
        requested_url="https://example.com/private",
        resolved_domain="example.com",
        requested_purpose="market_daily",
        policy_rule="domain_not_allowed",
        refused_at=datetime(2026, 7, 24, 15, 1, tzinfo=UTC),
        reason_code="DOMAIN_DENIED",
        requesting_module="collector",
    )


def security(code: str, board: str) -> SecurityMaster:
    return SecurityMaster(
        ts_code=code,
        symbol=code.split(".")[0],
        name=f"Security {code}",
        exchange="SSE" if code.endswith(".SH") else "SZSE",
        board=board,
        list_date=date(2020, 1, 1),
        is_in_scope=True,
    )


def exchange_policy(source_id: str, *, enabled: bool = True) -> SourcePolicy:
    if source_id == "sse":
        return security_master_policy(enabled=enabled)
    return security_master_policy(
        source_id="szse",
        source_name="SZSE",
        allowed_domains=["szse.cn", "www.szse.cn"],
        terms_url="https://www.szse.cn/application/laws/",
        enabled=enabled,
    )


def save_exchange_snapshot(
    repository: StateRepository,
    source_id: str,
    *,
    content_hash: str,
    version: str,
    collected_hour: int,
    securities: list[SecurityMaster],
) -> SecurityMasterSnapshot:
    domain = "www.sse.com.cn" if source_id == "sse" else "www.szse.cn"
    return repository.save_security_master_snapshot(
        securities,
        source_id=source_id,
        source_url=f"https://{domain}/master.csv",
        collected_at=datetime(2026, 7, 24, collected_hour, tzinfo=UTC),
        content_hash=content_hash,
        version=version,
        quality_lineage={"filter": "a_share_cny_four_boards"},
    )


def test_security_master_snapshot_round_trips_exchange_boards_and_lineage(
    tmp_path: Path,
) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    repository.upsert_policy(security_master_policy())
    records = [
        security("600000.SH", "MAIN_SH"), security("688001.SH", "STAR"),
    ]

    snapshot = repository.save_security_master_snapshot(
        records, source_id="sse", source_url="https://www.sse.com.cn/master.csv",
        collected_at=datetime(2026, 7, 24, 9, 0, tzinfo=UTC), content_hash="a" * 64,
        version="2026-07-24", quality_lineage={"filter": "a_share_cny_four_boards", "row_count": 2},
    )

    assert snapshot.source_id == "sse"
    assert snapshot.source_url == "https://www.sse.com.cn/master.csv"
    assert snapshot.collected_at == datetime(2026, 7, 24, 9, 0, tzinfo=UTC)
    assert snapshot.content_hash == "a" * 64
    assert snapshot.version == "2026-07-24"
    assert snapshot.quality_lineage == {"filter": "a_share_cny_four_boards", "row_count": 2}
    assert [item.ts_code for item in snapshot.securities] == [
        "600000.SH",
        "688001.SH",
    ]
    assert repository.get_security_master_snapshot("sse", "a" * 64) == snapshot


@pytest.mark.parametrize(
    ("source_id", "record"),
    [
        ("sse", security("000001.SZ", "MAIN_SZ")),
        ("szse", security("600000.SH", "MAIN_SH")),
        ("unknown", security("600000.SH", "MAIN_SH")),
    ],
)
def test_security_master_snapshot_rejects_source_lineage_before_persisting(
    tmp_path: Path,
    source_id: str,
    record: SecurityMaster,
) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()

    with pytest.raises(ValueError, match="^SECURITY_MASTER_SOURCE_MISMATCH$"):
        repository.save_security_master_snapshot(
            [record],
            source_id=source_id,
            source_url="https://example.test/master.csv",
            collected_at=datetime(2026, 7, 24, tzinfo=UTC),
            content_hash="9" * 64,
            version="v1",
            quality_lineage={},
        )

    assert repository.get_security_master_snapshot(source_id, "9" * 64) is None


def test_exact_security_master_snapshot_rejects_legacy_source_mismatch(
    tmp_path: Path,
) -> None:
    repository = StateRepository(tmp_path / "legacy-mismatch.sqlite3")
    repository.migrate()
    legacy_security = security("000001.SZ", "MAIN_SZ")
    with sqlite3.connect(repository.path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO security_master_snapshots(
                source_id, source_url, collected_at, content_hash, version,
                quality_lineage_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "sse",
                "https://www.sse.com.cn/master.csv",
                datetime(2026, 7, 24, tzinfo=UTC).isoformat(),
                "8" * 64,
                "legacy-mismatch",
                "{}",
                datetime(2026, 7, 24, tzinfo=UTC).isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO security_master_members(snapshot_id, ts_code, payload_json)
            VALUES (?, ?, ?)
            """,
            (
                cursor.lastrowid,
                legacy_security.ts_code,
                legacy_security.model_dump_json(),
            ),
        )

    with pytest.raises(ValueError, match="^SECURITY_MASTER_SOURCE_MISMATCH$"):
        repository.get_security_master_snapshot("sse", "8" * 64)


@pytest.mark.parametrize(
    ("policy_overrides", "source_url", "reason"),
    [
        (None, "https://www.sse.com.cn/master.csv", "SOURCE_POLICY_MISSING"),
        ({"enabled": False}, "https://www.sse.com.cn/master.csv", "SOURCE_DISABLED"),
        (
            {"enabled": False, "review_status": ReviewStatus.REVIEW_REQUIRED},
            "https://www.sse.com.cn/master.csv",
            "SOURCE_REVIEW_REQUIRED",
        ),
        (
            {"allowed_purposes": ["trading_status"]},
            "https://www.sse.com.cn/master.csv",
            "PURPOSE_NOT_ALLOWED",
        ),
        ({}, "http://www.sse.com.cn/master.csv", "HTTP_ENDPOINT_NOT_ALLOWED"),
        ({}, "https://download.sse.com.cn/master.csv", "DOMAIN_NOT_ALLOWED"),
    ],
)
def test_security_master_snapshot_denials_are_audited_and_never_persist(
    tmp_path: Path,
    policy_overrides: dict[str, object] | None,
    source_url: str,
    reason: str,
) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    if policy_overrides is not None:
        repository.upsert_policy(security_master_policy(**policy_overrides))

    with pytest.raises(PolicyDenied, match=f"^{reason}$"):
        repository.save_security_master_snapshot(
            [security("600000.SH", "MAIN_SH")],
            source_id="sse",
            source_url=source_url,
            collected_at=datetime(2026, 7, 24, tzinfo=UTC),
            content_hash="f" * 64,
            version="v1",
            quality_lineage={},
        )

    assert repository.get_security_master_snapshot("sse", "f" * 64) is None
    assert repository.count_refusals() == 1


def test_disabled_policy_prevents_an_existing_snapshot_from_gating_ingestion(
    tmp_path: Path,
) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    repository.upsert_policy(security_master_policy())
    repository.save_security_master_snapshot(
        [security("600000.SH", "MAIN_SH")],
        source_id="sse",
        source_url="https://www.sse.com.cn/master.csv",
        collected_at=datetime(2026, 7, 24, tzinfo=UTC),
        content_hash="f" * 64,
        version="v1",
        quality_lineage={},
    )
    repository.upsert_policy(security_master_policy(enabled=False))

    with pytest.raises(ValueError, match="SECURITY_MASTER_POLICY_DENIED"):
        repository.get_latest_security_master_snapshot("sse")
    assert repository.count_refusals() == 1


def test_security_master_snapshot_rejects_duplicates_without_persisting(tmp_path: Path) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()

    with pytest.raises(ValueError, match="SECURITY_MASTER_DUPLICATE_TS_CODE"):
        repository.save_security_master_snapshot(
            [security("600000.SH", "MAIN_SH"), security("600000.SH", "MAIN_SH")],
            source_id="sse", source_url="https://example.test/master.csv",
            collected_at=datetime(2026, 7, 24, tzinfo=UTC), content_hash="b" * 64,
            version="v1", quality_lineage={},
        )

    assert repository.get_security_master_snapshot("sse", "b" * 64) is None


def test_latest_security_master_snapshot_uses_most_recent_collection(tmp_path: Path) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    repository.upsert_policy(security_master_policy())
    repository.save_security_master_snapshot(
        [security("600000.SH", "MAIN_SH")],
        source_id="sse", source_url="https://www.sse.com.cn/master.csv",
        collected_at=datetime(2026, 7, 23, tzinfo=UTC), content_hash="d" * 64,
        version="v1", quality_lineage={},
    )
    newer = repository.save_security_master_snapshot(
        [security("688001.SH", "STAR")],
        source_id="sse", source_url="https://www.sse.com.cn/master.csv",
        collected_at=datetime(2026, 7, 24, tzinfo=UTC), content_hash="e" * 64,
        version="v2", quality_lineage={},
    )

    assert repository.get_latest_security_master_snapshot("sse") == newer


def test_latest_security_master_snapshot_uses_absolute_time_then_snapshot_id(
    tmp_path: Path,
) -> None:
    repository = StateRepository(tmp_path / "absolute-time.sqlite3")
    repository.migrate()
    repository.upsert_policies([exchange_policy("sse"), exchange_policy("szse")])
    earlier = repository.save_security_master_snapshot(
        [security("600000.SH", "MAIN_SH")],
        source_id="sse",
        source_url="https://www.sse.com.cn/master.csv",
        collected_at=datetime(2026, 7, 24, 10, tzinfo=timezone(timedelta(hours=8))),
        content_hash="a" * 64,
        version="earlier",
        quality_lineage={},
    )
    later = repository.save_security_master_snapshot(
        [security("688001.SH", "STAR")],
        source_id="sse",
        source_url="https://www.sse.com.cn/master.csv",
        collected_at=datetime(2026, 7, 24, 3, tzinfo=UTC),
        content_hash="b" * 64,
        version="later",
        quality_lineage={},
    )
    assert repository.get_latest_security_master_snapshot("sse") == later

    same_instant_later_id = repository.save_security_master_snapshot(
        [security("600001.SH", "MAIN_SH")],
        source_id="sse",
        source_url="https://www.sse.com.cn/master.csv",
        collected_at=datetime(2026, 7, 24, 11, tzinfo=timezone(timedelta(hours=8))),
        content_hash="c" * 64,
        version="same-instant-later-id",
        quality_lineage={},
    )
    save_exchange_snapshot(
        repository,
        "szse",
        content_hash="d" * 64,
        version="isolated-newer-source",
        collected_hour=12,
        securities=[security("000001.SZ", "MAIN_SZ")],
    )

    assert earlier.collected_at.isoformat() == "2026-07-24T10:00:00+08:00"
    assert later.collected_at.isoformat() == "2026-07-24T03:00:00+00:00"
    assert repository.get_latest_security_master_snapshot("sse") == same_instant_later_id


def test_latest_security_master_snapshot_rejects_empty_newest_without_fallback(
    tmp_path: Path,
) -> None:
    repository = StateRepository(tmp_path / "empty-newest.sqlite3")
    repository.migrate()
    repository.upsert_policy(exchange_policy("sse"))
    save_exchange_snapshot(
        repository,
        "sse",
        content_hash="b" * 64,
        version="sse-valid-old",
        collected_hour=8,
        securities=[security("600000.SH", "MAIN_SH")],
    )
    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            """
            INSERT INTO security_master_snapshots(
                source_id, source_url, collected_at, content_hash, version,
                quality_lineage_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "sse",
                "https://www.sse.com.cn/master.csv",
                datetime(2026, 7, 24, 10, tzinfo=UTC).isoformat(),
                "c" * 64,
                "sse-empty-newest",
                "{}",
                datetime(2026, 7, 24, 10, tzinfo=UTC).isoformat(),
            ),
        )

    with pytest.raises(ValueError, match="^SECURITY_MASTER_SNAPSHOT_INVALID$"):
        repository.get_latest_security_master_snapshot("sse")


def test_latest_security_master_snapshot_rejects_invalid_newest_without_fallback(
    tmp_path: Path,
) -> None:
    repository = StateRepository(tmp_path / "invalid-newest.sqlite3")
    repository.migrate()
    repository.upsert_policy(exchange_policy("sse"))
    save_exchange_snapshot(
        repository,
        "sse",
        content_hash="d" * 64,
        version="sse-valid-old",
        collected_hour=8,
        securities=[security("600000.SH", "MAIN_SH")],
    )
    invalid_security = security("688001.SH", "STAR").model_copy(
        update={"is_in_scope": False}
    )
    with sqlite3.connect(repository.path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO security_master_snapshots(
                source_id, source_url, collected_at, content_hash, version,
                quality_lineage_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "sse",
                "https://www.sse.com.cn/master.csv",
                datetime(2026, 7, 24, 10, tzinfo=UTC).isoformat(),
                "e" * 64,
                "sse-invalid-newest",
                "{}",
                datetime(2026, 7, 24, 10, tzinfo=UTC).isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO security_master_members(snapshot_id, ts_code, payload_json)
            VALUES (?, ?, ?)
            """,
            (
                cursor.lastrowid,
                invalid_security.ts_code,
                invalid_security.model_dump_json(),
            ),
        )

    with pytest.raises(ValueError, match="^SECURITY_MASTER_SCOPE_INVALID$"):
        repository.get_latest_security_master_snapshot("sse")


def test_universe_selects_latest_snapshot_per_required_source(tmp_path: Path) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    repository.upsert_policies([exchange_policy("sse"), exchange_policy("szse")])
    old_sse = save_exchange_snapshot(
        repository,
        "sse",
        content_hash="1" * 64,
        version="sse-old",
        collected_hour=8,
        securities=[security("600000.SH", "MAIN_SH")],
    )
    new_sse = save_exchange_snapshot(
        repository,
        "sse",
        content_hash="2" * 64,
        version="sse-new",
        collected_hour=10,
        securities=[security("600000.SH", "MAIN_SH"), security("688001.SH", "STAR")],
    )
    szse = save_exchange_snapshot(
        repository,
        "szse",
        content_hash="3" * 64,
        version="szse-only",
        collected_hour=11,
        securities=[security("000001.SZ", "MAIN_SZ"), security("300001.SZ", "CHINEXT")],
    )

    universe = repository.get_security_master_universe()

    assert [item.version for item in universe.components] == ["sse-new", "szse-only"]
    assert [item.ts_code for item in universe.securities] == [
        "000001.SZ",
        "300001.SZ",
        "600000.SH",
        "688001.SH",
    ]
    assert universe.as_of == min(new_sse.collected_at, szse.collected_at)
    assert old_sse not in universe.components


def test_universe_requires_both_exchange_snapshots_symmetrically(tmp_path: Path) -> None:
    only_sse = StateRepository(tmp_path / "only-sse.sqlite3")
    only_sse.migrate()
    only_sse.upsert_policies([exchange_policy("sse"), exchange_policy("szse")])
    save_exchange_snapshot(
        only_sse,
        "sse",
        content_hash="4" * 64,
        version="sse-only",
        collected_hour=9,
        securities=[security("600000.SH", "MAIN_SH")],
    )

    with pytest.raises(ValueError, match="SECURITY_MASTER_SZSE_UNAVAILABLE"):
        only_sse.get_security_master_universe()

    only_szse = StateRepository(tmp_path / "only-szse.sqlite3")
    only_szse.migrate()
    only_szse.upsert_policies([exchange_policy("sse"), exchange_policy("szse")])
    save_exchange_snapshot(
        only_szse,
        "szse",
        content_hash="5" * 64,
        version="szse-only",
        collected_hour=9,
        securities=[security("000001.SZ", "MAIN_SZ")],
    )

    with pytest.raises(ValueError, match="SECURITY_MASTER_SSE_UNAVAILABLE"):
        only_szse.get_security_master_universe()


def test_universe_rejects_duplicate_codes_across_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    duplicate_components = StateRepository(tmp_path / "duplicate-components.sqlite3")
    duplicate_components.migrate()
    duplicate_components.upsert_policies(
        [exchange_policy("sse"), exchange_policy("szse")]
    )
    sse = save_exchange_snapshot(
        duplicate_components,
        "sse",
        content_hash="5" * 64,
        version="sse-duplicate",
        collected_hour=9,
        securities=[security("600000.SH", "MAIN_SH")],
    )
    szse = save_exchange_snapshot(
        duplicate_components,
        "szse",
        content_hash="6" * 64,
        version="szse-duplicate",
        collected_hour=10,
        securities=[security("000001.SZ", "MAIN_SZ")],
    )
    duplicate_szse = SecurityMasterSnapshot(
        source_id=szse.source_id,
        source_url=szse.source_url,
        collected_at=szse.collected_at,
        content_hash=szse.content_hash,
        version=szse.version,
        quality_lineage=szse.quality_lineage,
        securities=[szse.securities[0].model_copy(update={"ts_code": "600000.SH"})],
    )
    monkeypatch.setattr(
        duplicate_components,
        "get_latest_security_master_snapshot",
        lambda source_id: sse if source_id == "sse" else duplicate_szse,
    )

    with pytest.raises(ValueError, match="SECURITY_MASTER_DUPLICATE_TS_CODE"):
        duplicate_components.get_security_master_universe()


def test_universe_rejects_a_component_denied_by_current_policy(tmp_path: Path) -> None:
    disabled_sse = StateRepository(tmp_path / "disabled-sse.sqlite3")
    disabled_sse.migrate()
    disabled_sse.upsert_policies([exchange_policy("sse"), exchange_policy("szse")])
    save_exchange_snapshot(
        disabled_sse,
        "sse",
        content_hash="7" * 64,
        version="sse-disabled",
        collected_hour=9,
        securities=[security("600000.SH", "MAIN_SH")],
    )
    save_exchange_snapshot(
        disabled_sse,
        "szse",
        content_hash="8" * 64,
        version="szse-enabled",
        collected_hour=10,
        securities=[security("000001.SZ", "MAIN_SZ")],
    )
    disabled_sse.upsert_policy(exchange_policy("sse", enabled=False))

    with pytest.raises(ValueError, match="SECURITY_MASTER_POLICY_DENIED"):
        disabled_sse.get_security_master_universe()

    assert disabled_sse.count_refusals() == 1


def test_universe_hash_is_deterministic(tmp_path: Path) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    repository.upsert_policies([exchange_policy("sse"), exchange_policy("szse")])
    save_exchange_snapshot(
        repository,
        "sse",
        content_hash="9" * 64,
        version="sse",
        collected_hour=9,
        securities=[security("600000.SH", "MAIN_SH")],
    )
    save_exchange_snapshot(
        repository,
        "szse",
        content_hash="a" * 64,
        version="szse",
        collected_hour=10,
        securities=[security("000001.SZ", "MAIN_SZ")],
    )

    first = repository.get_security_master_universe()
    second = repository.get_security_master_universe()
    expected_identity = [
        {
            "source_id": "sse",
            "version": "sse",
            "content_hash": "9" * 64,
            "collected_at": "2026-07-24T09:00:00+00:00",
        },
        {
            "source_id": "szse",
            "version": "szse",
            "content_hash": "a" * 64,
            "collected_at": "2026-07-24T10:00:00+00:00",
        },
    ]
    expected_digest = hashlib.sha256(
        json.dumps(
            expected_identity, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()

    assert first.universe_hash == second.universe_hash
    assert expected_digest == "7608ec5aea4b0276a2bdde7a17223a3acf770ed8085e235bcc2bd8707a16f338"
    assert first.universe_hash == expected_digest

    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            "UPDATE security_master_snapshots SET version=? WHERE source_id=?",
            ("sse-changed", "sse"),
        )
    version_changed = repository.get_security_master_universe()

    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            """
            UPDATE security_master_snapshots
            SET version=?, content_hash=?
            WHERE source_id=?
            """,
            ("sse", "b" * 64, "sse"),
        )
    content_hash_changed = repository.get_security_master_universe()

    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            """
            UPDATE security_master_snapshots
            SET content_hash=?, collected_at=?
            WHERE source_id=?
            """,
            (
                "9" * 64,
                datetime(2026, 7, 24, 12, tzinfo=UTC).isoformat(),
                "sse",
            ),
        )
    collected_at_changed = repository.get_security_master_universe()

    assert version_changed.universe_hash != first.universe_hash
    assert content_hash_changed.universe_hash != first.universe_hash
    assert collected_at_changed.universe_hash != first.universe_hash


def test_security_master_snapshot_rolls_back_metadata_when_row_insert_fails(tmp_path: Path) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    repository.upsert_policy(security_master_policy())
    with sqlite3.connect(repository.path) as connection:
        connection.execute("""
            CREATE TRIGGER abort_security_row BEFORE INSERT ON security_master_members
            WHEN NEW.ts_code = '688001.SH'
            BEGIN SELECT RAISE(ABORT, 'forced failure'); END;
        """)

    with pytest.raises(sqlite3.IntegrityError, match="forced failure"):
        repository.save_security_master_snapshot(
            [security("600000.SH", "MAIN_SH"), security("688001.SH", "STAR")],
            source_id="sse", source_url="https://www.sse.com.cn/master.csv",
            collected_at=datetime(2026, 7, 24, tzinfo=UTC), content_hash="c" * 64,
            version="v1", quality_lineage={},
        )

    assert repository.get_security_master_snapshot("sse", "c" * 64) is None


def test_repository_round_trips_policy_and_checkpoint(tmp_path: Path) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    repository.migrate()
    repository.upsert_policy(approved_policy())
    replacement_policy = approved_policy().model_copy(update={"connection_status": "AVAILABLE"})
    repository.upsert_policy(replacement_policy)
    repository.save_checkpoint("init:last_trade_date", "2026-07-24")
    repository.save_checkpoint("init:last_trade_date", "2026-07-25")

    assert repository.get_policy("tushare") == replacement_policy
    assert repository.get_policy("missing") is None
    assert repository.get_checkpoint("init:last_trade_date") == "2026-07-25"
    assert repository.get_checkpoint("missing") is None


def test_repository_counts_policies(tmp_path: Path) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()

    assert repository.count_policies() == 0
    repository.upsert_policy(approved_policy())
    assert repository.count_policies() == 1


def test_migrate_applies_state_migrations_idempotently(tmp_path: Path) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")

    repository.migrate()
    repository.migrate()

    with sqlite3.connect(repository.path) as connection:
        migrations = {
            row[0] for row in connection.execute("SELECT version FROM schema_migrations")
        }
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(rate_reservations)")
        }
        lease_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(ingestion_leases)")
        }

    assert migrations == {
        "001_initial",
        "002_rate_reservations",
        "003_ingestion_leases",
        "004_security_master_snapshots",
        "005_ingestion_run_link",
        "006_financial_filings",
    }
    assert columns == {"source_id", "next_allowed_at", "updated_at"}
    assert lease_columns == {
        "trade_date",
        "owner_id",
        "lease_expires_at",
        "lifecycle_state",
        "staged_result_json",
        "updated_at",
        "active_run_id",
    }


def test_bulk_upsert_policies_is_atomic_when_later_write_fails(tmp_path: Path) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    original = approved_policy().model_copy(update={"connection_status": "ORIGINAL"})
    repository.upsert_policy(original)
    replacement = approved_policy().model_copy(update={"connection_status": "REPLACED"})
    later = approved_policy().model_copy(update={"source_id": "later-source"})

    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            """
            CREATE TRIGGER abort_later_policy
            BEFORE INSERT ON source_policies
            WHEN NEW.source_id = 'later-source'
            BEGIN SELECT RAISE(ABORT, 'forced failure'); END;
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced failure"):
        repository.upsert_policies([replacement, later])

    assert repository.get_policy("tushare") == original
    assert repository.get_policy("later-source") is None


def test_repository_records_runs_as_upserts(tmp_path: Path) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    record = completed_run()

    repository.record_run(record)
    repository.record_run(record.model_copy(update={"retry_count": 1}))

    with sqlite3.connect(repository.path) as connection:
        row = connection.execute(
            "SELECT payload_json FROM run_records WHERE run_id = ?", (record.run_id,)
        ).fetchone()
    assert row is not None
    persisted = RunRecord.model_validate_json(row[0])
    assert persisted == record.model_copy(update={"retry_count": 1})


def test_repository_records_refusals_idempotently(tmp_path: Path) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    record = refusal()

    repository.record_refusal(record)
    repository.record_refusal(record)

    with sqlite3.connect(repository.path) as connection:
        rows = connection.execute(
            "SELECT payload_json FROM refusal_records WHERE refusal_id = ?", (record.refusal_id,)
        ).fetchall()
    assert len(rows) == 1
    assert RefusalRecord.model_validate_json(rows[0][0]) == record


def test_rate_reservations_are_atomic_across_instances_and_release_connections(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    repository = StateRepository(path)
    repository.migrate()
    repository.upsert_policy(approved_policy())
    requested_at = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)
    barrier = Barrier(2)

    def reserve_slot() -> float:
        barrier.wait()
        return StateRepository(path).reserve_rate_slot(
            "tushare",
            requested_at=requested_at,
            rate_limit_per_minute=1,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        delays = sorted(executor.map(lambda _: reserve_slot(), range(2)))

    assert delays == [0.0, 60.0]
    repository.save_checkpoint("rate-test", "complete")
    path.unlink()


def test_ingestion_lease_is_atomic_across_repository_instances(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    StateRepository(path).migrate()
    now = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)
    barrier = Barrier(2)

    def acquire(owner_id: str):  # type: ignore[no-untyped-def]
        barrier.wait()
        return StateRepository(path).acquire_ingestion_lease(
            date(2026, 7, 24),
            owner_id=owner_id,
            now=now,
            lease_seconds=60,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        leases = list(executor.map(acquire, ["owner-one", "owner-two"]))

    assert sum(lease.acquired for lease in leases) == 1
    winner = next(lease for lease in leases if lease.acquired)
    assert winner.owner_id in {"owner-one", "owner-two"}


def test_acquire_ingestion_lease_atomically_records_linked_running_run(
    tmp_path: Path,
) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    trade_date = date(2026, 7, 24)
    started = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)
    running = RunRecord(
        run_id="market_daily:2026-07-24:owner",
        trade_date=trade_date,
        run_type="market_daily",
        started_at=started,
        run_status=RunStatus.RUNNING,
        stage_statuses={"checkpoint": "RUNNING"},
    )

    lease = repository.acquire_ingestion_lease(
        trade_date,
        owner_id="owner",
        running_run=running,
        now=started,
        lease_seconds=60,
    )

    assert lease.acquired
    assert lease.active_run_id == running.run_id
    assert repository.list_runs(run_type="market_daily") == [running]


def test_acquire_ingestion_lease_rolls_back_when_running_run_insert_fails(
    tmp_path: Path,
) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    trade_date = date(2026, 7, 24)
    started = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)
    running = RunRecord(
        run_id="market_daily:2026-07-24:owner",
        trade_date=trade_date,
        run_type="market_daily",
        started_at=started,
        run_status=RunStatus.RUNNING,
        stage_statuses={"checkpoint": "RUNNING"},
    )
    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            """
            CREATE TRIGGER abort_running_run
            BEFORE INSERT ON run_records
            WHEN NEW.run_id = 'market_daily:2026-07-24:owner'
            BEGIN SELECT RAISE(ABORT, 'forced acquire failure'); END;
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced acquire failure"):
        repository.acquire_ingestion_lease(
            trade_date,
            owner_id="owner",
            running_run=running,
            now=started,
            lease_seconds=60,
        )

    assert repository.get_ingestion_state(trade_date) is None
    assert repository.list_runs(run_type="market_daily") == []


def test_expired_ingestion_lease_can_be_taken_over_and_terminal_release_is_safe(
    tmp_path: Path,
) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    now = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)

    assert repository.acquire_ingestion_lease(
        date(2026, 7, 24), owner_id="first", now=now, lease_seconds=60
    ).acquired
    assert repository.acquire_ingestion_lease(
        date(2026, 7, 24), owner_id="second", now=now, lease_seconds=60
    ).acquired is False
    assert repository.acquire_ingestion_lease(
        date(2026, 7, 24), owner_id="second", now=now.replace(minute=2), lease_seconds=60
    ).acquired

    assert repository.release_ingestion_lease(date(2026, 7, 24), owner_id="first") is False
    assert repository.release_ingestion_lease(date(2026, 7, 24), owner_id="second") is True
    assert repository.release_ingestion_lease(date(2026, 7, 24), owner_id="second") is False


def test_expired_lease_takeover_terminalizes_linked_running_record_before_new_run(
    tmp_path: Path,
) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    trade_date = date(2026, 7, 24)
    started = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)
    abandoned = RunRecord(
        run_id="market_daily:2026-07-24:first",
        trade_date=trade_date,
        run_type="market_daily",
        started_at=started,
        run_status="RUNNING",
        stage_statuses={"lease": "RUNNING"},
    )
    repository.record_run(abandoned)
    assert repository.acquire_ingestion_lease(
        trade_date,
        owner_id="first",
        run_id=abandoned.run_id,
        now=started,
        lease_seconds=60,
    ).acquired

    takeover = repository.acquire_ingestion_lease(
        trade_date,
        owner_id="second",
        run_id="market_daily:2026-07-24:second",
        now=started.replace(minute=2),
        lease_seconds=60,
    )

    assert takeover.acquired
    assert takeover.abandoned_run_id == abandoned.run_id
    persisted = {
        run.run_id: run for run in repository.list_runs(run_type="market_daily")
    }[abandoned.run_id]
    assert persisted.run_status == "FAILED"
    assert persisted.finished_at == started.replace(minute=2)
    assert persisted.error_code == "MARKET_INGESTION_LEASE_EXPIRED"
    assert persisted.stage_statuses == {"lease": "FAILED"}


@pytest.mark.parametrize(
    ("lifecycle_state", "run_status"),
    [("FAILED", RunStatus.FAILED), ("BLOCKED", RunStatus.BLOCKED)],
)
def test_finalize_ingestion_run_atomically_terminalizes_failure_or_block(
    tmp_path: Path,
    lifecycle_state: str,
    run_status: RunStatus,
) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    trade_date = date(2026, 7, 24)
    started = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)
    running = RunRecord(
        run_id=f"market_daily:2026-07-24:{lifecycle_state.lower()}",
        trade_date=trade_date,
        run_type="market_daily",
        started_at=started,
        run_status=RunStatus.RUNNING,
        stage_statuses={"lease": "RUNNING"},
    )
    repository.record_run(running)
    assert repository.acquire_ingestion_lease(
        trade_date,
        owner_id=lifecycle_state.lower(),
        run_id=running.run_id,
        now=started,
        lease_seconds=60,
    ).acquired
    terminal = running.model_copy(
        update={
            "finished_at": started.replace(minute=1),
            "run_status": run_status,
            "stage_statuses": {"ingestion": lifecycle_state},
            "error_code": f"MARKET_{lifecycle_state}",
        }
    )

    assert repository.finalize_ingestion_run(
        trade_date,
        owner_id=lifecycle_state.lower(),
        lifecycle_state=lifecycle_state,
        terminal_run=terminal,
    )

    lease = repository.get_ingestion_state(trade_date)
    assert lease is not None
    assert lease.lifecycle_state == lifecycle_state
    assert lease.owner_id is None
    assert lease.active_run_id is None
    assert repository.list_runs(run_type="market_daily")[0] == terminal


def test_finalize_ingestion_run_rolls_back_lease_when_run_update_fails(
    tmp_path: Path,
) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    trade_date = date(2026, 7, 24)
    started = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)
    running = RunRecord(
        run_id="market_daily:2026-07-24:owner",
        trade_date=trade_date,
        run_type="market_daily",
        started_at=started,
        run_status=RunStatus.RUNNING,
        stage_statuses={"lease": "RUNNING"},
    )
    repository.record_run(running)
    repository.acquire_ingestion_lease(
        trade_date,
        owner_id="owner",
        run_id=running.run_id,
        now=started,
        lease_seconds=60,
    )
    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            """
            CREATE TRIGGER abort_terminal_run
            BEFORE UPDATE ON run_records
            WHEN NEW.run_id = 'market_daily:2026-07-24:owner'
            BEGIN SELECT RAISE(ABORT, 'forced finalize failure'); END;
            """
        )
    terminal = running.model_copy(
        update={"finished_at": started, "run_status": RunStatus.SUCCEEDED}
    )

    with pytest.raises(sqlite3.IntegrityError, match="forced finalize failure"):
        repository.finalize_ingestion_run(
            trade_date,
            owner_id="owner",
            lifecycle_state="SUCCEEDED",
            terminal_run=terminal,
        )

    lease = repository.get_ingestion_state(trade_date)
    assert lease is not None
    assert lease.owner_id == "owner"
    assert lease.active_run_id == running.run_id
    assert repository.list_runs(run_type="market_daily")[0].run_status == "RUNNING"


def test_checkpoint_publish_rolls_back_with_lease_and_run_update_failure(
    tmp_path: Path,
) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    trade_date = date(2026, 7, 24)
    started = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)
    running = RunRecord(
        run_id="market_daily:2026-07-24:owner",
        trade_date=trade_date,
        run_type="market_daily",
        started_at=started,
        run_status=RunStatus.RUNNING,
        stage_statuses={"checkpoint": "RUNNING"},
    )
    repository.acquire_ingestion_lease(
        trade_date,
        owner_id="owner",
        running_run=running,
        now=started,
        lease_seconds=60,
    )
    result = {
        "trade_date": trade_date.isoformat(),
        "bar_count": 1,
        "raw_content_hash": "a" * 64,
        "parquet_path": "part-a.parquet",
        "parquet_content_hash": "b" * 64,
    }
    repository.stage_ingestion_artifact(
        trade_date,
        owner_id="owner",
        staged_result_json=json.dumps({"raw_payload_path": "raw-a", "result": result}),
    )
    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            """
            CREATE TRIGGER abort_published_run
            BEFORE UPDATE ON run_records
            WHEN NEW.run_id = 'market_daily:2026-07-24:owner'
            BEGIN SELECT RAISE(ABORT, 'forced publish failure'); END;
            """
        )
    terminal = running.model_copy(
        update={"finished_at": started.replace(minute=1), "run_status": RunStatus.SUCCEEDED}
    )

    with pytest.raises(sqlite3.IntegrityError, match="forced publish failure"):
        repository.publish_and_finalize_ingestion(
            "market_daily:2026-07-24",
            json.dumps(result),
            trade_date,
            owner_id="owner",
            terminal_run=terminal,
        )

    lease = repository.get_ingestion_state(trade_date)
    assert repository.get_checkpoint("market_daily:2026-07-24") is None
    assert lease is not None
    assert lease.owner_id == "owner"
    assert lease.active_run_id == running.run_id
    assert repository.list_runs(run_type="market_daily")[0].run_status == "RUNNING"


def test_stale_owner_cannot_finalize_new_owner_lease_or_run(tmp_path: Path) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    trade_date = date(2026, 7, 24)
    started = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)
    first = RunRecord(
        run_id="market_daily:2026-07-24:first",
        trade_date=trade_date,
        run_type="market_daily",
        started_at=started,
        run_status=RunStatus.RUNNING,
        stage_statuses={"lease": "RUNNING"},
    )
    second = first.model_copy(
        update={"run_id": "market_daily:2026-07-24:second", "started_at": started.replace(minute=2)}
    )
    repository.record_run(first)
    repository.acquire_ingestion_lease(
        trade_date, owner_id="first", run_id=first.run_id, now=started, lease_seconds=60
    )
    repository.acquire_ingestion_lease(
        trade_date,
        owner_id="second",
        run_id=second.run_id,
        now=started.replace(minute=2),
        lease_seconds=60,
    )
    repository.record_run(second)

    assert repository.finalize_ingestion_run(
        trade_date,
        owner_id="first",
        lifecycle_state="SUCCEEDED",
        terminal_run=first.model_copy(
            update={"finished_at": started.replace(minute=3), "run_status": RunStatus.SUCCEEDED}
        ),
    ) is False

    lease = repository.get_ingestion_state(trade_date)
    runs = {run.run_id: run for run in repository.list_runs(run_type="market_daily")}
    assert lease is not None
    assert lease.owner_id == "second"
    assert lease.active_run_id == second.run_id
    assert runs[second.run_id].run_status == "RUNNING"


def test_only_current_lease_owner_can_stage_or_read_artifact(tmp_path: Path) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    trade_date = date(2026, 7, 24)
    now = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)
    repository.acquire_ingestion_lease(
        trade_date, owner_id="owner", now=now, lease_seconds=60
    )

    assert repository.stage_ingestion_artifact(
        trade_date, owner_id="other", staged_result_json="{}"
    ) is False
    assert repository.stage_ingestion_artifact(
        trade_date, owner_id="owner", staged_result_json='{"ready":true}'
    ) is True
    stored = repository.get_ingestion_state(trade_date)

    assert stored is not None
    assert stored.staged_result_json == '{"ready":true}'
    assert stored.lifecycle_state == "STAGED"


def test_lease_renewal_prevents_expiry_takeover_until_renewed_expiry(tmp_path: Path) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    trade_date = date(2026, 7, 24)
    started = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)
    assert repository.acquire_ingestion_lease(
        trade_date, owner_id="owner", now=started, lease_seconds=60
    ).acquired

    assert repository.renew_ingestion_lease(
        trade_date,
        owner_id="owner",
        now=started.replace(second=50),
        lease_seconds=60,
    )
    assert repository.acquire_ingestion_lease(
        trade_date,
        owner_id="other",
        now=started.replace(minute=1, second=1),
        lease_seconds=60,
    ).acquired is False
    assert repository.acquire_ingestion_lease(
        trade_date,
        owner_id="other",
        now=started.replace(minute=1, second=51),
        lease_seconds=60,
    ).acquired
