from __future__ import annotations

from hengce.contracts.base import FactBase


class OfficialRiskScreen(FactBase):
    ts_code: str
    audit_opinion_standard: bool
    major_investigation_open: bool
    delisting_risk: bool
    st_status: str | None
    is_suspended: bool
    publication_order_known: bool


__all__ = ["OfficialRiskScreen"]
