from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from .enums import RunStatus


class RunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    trade_date: date
    run_type: str
    started_at: datetime
    finished_at: datetime | None = None
    run_status: RunStatus
    stage_statuses: dict[str, str]
    retry_count: int = 0
    error_code: str | None = None
    error_summary: str | None = None
    published_report_id: str | None = None


class RefusalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refusal_id: str
    requested_url: str
    resolved_domain: str
    requested_purpose: str
    policy_rule: str
    refused_at: datetime
    reason_code: str
    requesting_module: str
