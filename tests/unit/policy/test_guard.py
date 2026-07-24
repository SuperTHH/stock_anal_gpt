from datetime import datetime
from pathlib import Path

import pytest

from hengce.contracts.enums import ReviewStatus
from hengce.contracts.policy import SourcePolicy
from hengce.policy.guard import PolicyDenied, PolicyGuard
from hengce.state.repository import StateRepository


def policy(**overrides: object) -> SourcePolicy:
    values: dict[str, object] = {
        "source_id": "tushare",
        "source_name": "Tushare",
        "allowed_domains": ["api.tushare.pro"],
        "allowed_schemes": ["http"],
        "allowed_purposes": ["market_daily"],
        "fetch_frequency": "trading_day",
        "full_text_rule": "structured_only",
        "attachment_rule": "none",
        "rate_limit_per_minute": 1,
        "robots_policy": "api_terms",
        "terms_url": "https://tushare.pro/document/1?doc_id=290",
        "terms_reviewed_at": datetime(2026, 7, 24, 9, 0),
        "review_status": ReviewStatus.APPROVED,
        "connection_status": "UNKNOWN",
        "enabled": True,
    }
    values.update(overrides)
    return SourcePolicy.model_validate(values)


def repository_with_policy(tmp_path: Path, **overrides: object) -> StateRepository:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    repository.upsert_policy(policy(**overrides))
    return repository


def test_guard_allows_exact_approved_domain_and_purpose(tmp_path: Path) -> None:
    repository = repository_with_policy(tmp_path)

    PolicyGuard(repository).authorize(
        "tushare", "http://api.tushare.pro/", "market_daily", "collectors.tushare"
    )

    assert repository.count_refusals() == 0


def test_guard_denies_tushare_http_subdomain_even_when_policy_allows_parent(
    tmp_path: Path,
) -> None:
    repository = repository_with_policy(tmp_path)

    with pytest.raises(PolicyDenied, match="^HTTP_ENDPOINT_NOT_ALLOWED$"):
        PolicyGuard(repository).authorize(
            "tushare",
            "http://daily.api.tushare.pro/data",
            "market_daily",
            "collectors.tushare",
        )

    assert repository.count_refusals() == 1


def test_guard_denies_http_for_non_tushare_even_when_editable_policy_allows_it(
    tmp_path: Path,
) -> None:
    repository = repository_with_policy(
        tmp_path,
        source_id="evil",
        allowed_domains=["example.com"],
        allowed_schemes=["http"],
    )

    with pytest.raises(PolicyDenied, match="^HTTP_ENDPOINT_NOT_ALLOWED$"):
        PolicyGuard(repository).authorize(
            "evil", "http://example.com/", "market_daily", "fixture"
        )

    assert repository.count_refusals() == 1


def test_guard_persists_rate_reservations_across_repository_instances(
    tmp_path: Path,
) -> None:
    repository = repository_with_policy(tmp_path)
    second_repository = StateRepository(repository.path)
    now = datetime(2026, 7, 24, 9, 0)
    sleeps: list[float] = []

    PolicyGuard(repository, clock=lambda: now, sleeper=sleeps.append).authorize(
        "tushare", "http://api.tushare.pro/", "market_daily", "first"
    )
    PolicyGuard(second_repository, clock=lambda: now, sleeper=sleeps.append).authorize(
        "tushare", "http://api.tushare.pro/", "market_daily", "second"
    )

    assert sleeps == [60.0]


@pytest.mark.parametrize(
    ("source_id", "url", "purpose", "overrides", "reason"),
    [
        ("missing", "http://api.tushare.pro/", "market_daily", {}, "SOURCE_POLICY_MISSING"),
        (
            "tushare",
            "http://api.tushare.pro/",
            "market_daily",
            {"enabled": False, "review_status": ReviewStatus.REVIEW_REQUIRED},
            "SOURCE_REVIEW_REQUIRED",
        ),
        (
            "tushare",
            "http://api.tushare.pro/",
            "market_daily",
            {"enabled": False},
            "SOURCE_DISABLED",
        ),
        ("tushare", "https://api.tushare.pro/", "market_daily", {}, "SCHEME_NOT_ALLOWED"),
        ("tushare", "http://example.com/", "market_daily", {}, "HTTP_ENDPOINT_NOT_ALLOWED"),
        ("tushare", "http://api.tushare.pro/", "profile", {}, "PURPOSE_NOT_ALLOWED"),
    ],
)
def test_guard_refuses_each_disallowed_request_once(
    tmp_path: Path,
    source_id: str,
    url: str,
    purpose: str,
    overrides: dict[str, object],
    reason: str,
) -> None:
    repository = repository_with_policy(tmp_path, **overrides)

    with pytest.raises(PolicyDenied, match=f"^{reason}$"):
        PolicyGuard(repository).authorize(source_id, url, purpose, "collectors.tushare")

    assert repository.count_refusals() == 1
