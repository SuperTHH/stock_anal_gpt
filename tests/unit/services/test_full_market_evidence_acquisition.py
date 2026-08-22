from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from hengce.collectors.cninfo_reports import CninfoReport
from hengce.contracts.enums import EvidenceCohort, EvidenceTaskStatus
from hengce.raw_store.store import RawObjectStore
from hengce.services.full_market_evidence import FullMarketEvidencePlanner
from hengce.services.full_market_evidence_acquisition import (
    FullMarketEvidenceAcquisitionService,
)
from hengce.state.evidence_repository import FullMarketEvidenceRepository
from tests.unit.services.test_full_market_research import MARKET_DATE, seed


class _Page:
    def __init__(self, text: str) -> None:
        self.text = text

    def extract_text(self) -> str:
        return self.text


class _Reader:
    pages = [
        _Page("封面"),
        _Page("一、审计意见\n我们审计了财务报表，并出具无保留意见。"),
    ]


def test_risk_prefill_uses_audited_pdf_page_without_inventing_other_risks(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "hengce.services.full_market_evidence_acquisition.PdfReader",
        lambda _path: _Reader(),
    )

    page, excerpt, standard = (
        FullMarketEvidenceAcquisitionService._audit_opinion_prefill(
            Path("ignored.pdf")
        )
    )

    assert page == 2
    assert excerpt == "一、审计意见"
    assert standard is True
    assert FullMarketEvidenceAcquisitionService._st_status("平安银行") is None
    assert FullMarketEvidenceAcquisitionService._st_status("*ST示例") == "*ST"


def test_dividend_terms_parse_per_ten_shares_and_ex_date(monkeypatch) -> None:
    reader = _Reader()
    reader.pages = [
        _Page(
            "2021年年度权益分派实施公告\n"
            "向全体股东每10股派发现金红利人民币2.28元（含税）。\n"
            "除权除息日：2022年7月22日"
        )
    ]
    monkeypatch.setattr(
        "hengce.services.full_market_evidence_acquisition.PdfReader",
        lambda _path: reader,
    )

    page, excerpt, per_share, ex_date = (
        FullMarketEvidenceAcquisitionService._dividend_terms(Path("ignored.pdf"))
    )

    assert page == 1
    assert "每10股" in excerpt
    assert per_share == Decimal("0.228")
    assert ex_date == date(2022, 7, 22)


def test_dividend_terms_accept_explicit_official_no_dividend(monkeypatch) -> None:
    reader = _Reader()
    reader.pages = [
        _Page(
            "关于2021年度利润分配预案的公告\n"
            "综合考虑公司经营情况，公司计划不派发现金红利，不送红股。"
        )
    ]
    monkeypatch.setattr(
        "hengce.services.full_market_evidence_acquisition.PdfReader",
        lambda _path: reader,
    )

    page, excerpt, per_share, ex_date = (
        FullMarketEvidenceAcquisitionService._dividend_terms(
            Path("ignored.pdf"), fiscal_year=2021
        )
    )

    assert page == 1
    assert "不派发现金红利" in excerpt
    assert per_share is None
    assert ex_date is None


def test_report_match_prefers_latest_correction_before_frozen_cutoff() -> None:
    reports = (
        CninfoReport(
            ts_code="000001.SZ",
            title="2025年年度报告",
            published_at=datetime(2026, 3, 1, tzinfo=UTC),
            attachment_url="https://static.cninfo.com.cn/original.PDF",
            announcement_id="1",
        ),
        CninfoReport(
            ts_code="000001.SZ",
            title="2025年年度报告（更正后）",
            published_at=datetime(2026, 3, 2, tzinfo=UTC),
            attachment_url="https://static.cninfo.com.cn/corrected.PDF",
            announcement_id="2",
        ),
    )

    matched = FullMarketEvidenceAcquisitionService._match(
        "2025-12-31", reports, date(2026, 8, 21)
    )

    assert matched is not None
    assert matched.announcement_id == "2"


def test_retry_resumes_from_last_durable_state_and_later_cohort_is_gated(
    tmp_path: Path,
) -> None:
    research = seed(tmp_path)
    snapshot = research.build(MARKET_DATE, target_size=2)
    repository = FullMarketEvidenceRepository(research.state.path)
    planner = FullMarketEvidencePlanner(
        repository=repository,
        clock=lambda: datetime(2026, 8, 22, tzinfo=UTC),
    )
    runs = planner.plan_all(snapshot)
    high = next(run for run in runs if run.cohort is EvidenceCohort.YIELD_GE_5)
    liquidity = next(
        run for run in runs if run.cohort is EvidenceCohort.LIQUIDITY_FILL
    )
    _, tasks = repository.list_tasks(run_id=high.run_id, page_size=100)
    task = next(item for item in tasks if item.status is EvidenceTaskStatus.PLANNED)
    failed = repository.transition(
        task.task_id,
        expected_version=task.version,
        status=EvidenceTaskStatus.RETRYABLE_FAILED,
        observed_at=datetime(2026, 8, 22, tzinfo=UTC),
        updates={"source_url": "https://static.cninfo.com.cn/report.PDF"},
    )
    with httpx.Client() as client:
        service = FullMarketEvidenceAcquisitionService(
            repository=repository,
            collector=object(),  # type: ignore[arg-type]
            client=client,
            guard=object(),  # type: ignore[arg-type]
            raw_store=RawObjectStore(tmp_path / "raw"),
            clock=lambda: datetime(2026, 8, 22, tzinfo=UTC),
        )
        assert service.retry_failed(high.run_id, max_tasks=1) == {"retried": 1}
        with pytest.raises(ValueError, match="^EVIDENCE_COHORT_GATE_BLOCKED$"):
            service.retry_failed(liquidity.run_id, max_tasks=1)

    resumed = repository.get_task(failed.task_id)
    assert resumed is not None
    assert resumed.status is EvidenceTaskStatus.DISCOVERED
