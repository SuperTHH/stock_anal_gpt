"""Composition helpers for the local M1 data foundation."""

import json
from pathlib import Path

from pydantic import ValidationError

from hengce.config import Settings
from hengce.contracts.policy import SourcePolicy
from hengce.state.repository import StateRepository


def bootstrap_state(settings: Settings, policy_file: Path) -> StateRepository:
    """Migrate local state and atomically validate then seed source policies."""
    try:
        payload = json.loads(policy_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("SOURCE_POLICIES_INVALID") from error
    if not isinstance(payload, list):
        raise ValueError("SOURCE_POLICIES_INVALID")

    try:
        policies = [SourcePolicy.model_validate(item) for item in payload]
    except ValidationError as error:
        raise ValueError("SOURCE_POLICIES_INVALID") from error
    if len({policy.source_id for policy in policies}) != len(policies):
        raise ValueError("SOURCE_POLICIES_DUPLICATE_SOURCE_ID")

    settings.ensure_local_dirs()
    repository = StateRepository(settings.data_dir / "state" / "hengce.sqlite3")
    repository.migrate()
    for policy in policies:
        repository.upsert_policy(policy)
    return repository
