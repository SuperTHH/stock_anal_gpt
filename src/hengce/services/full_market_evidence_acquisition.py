from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from pypdf import PdfReader

from hengce.acquisition.downloader import validate_attachment_payload
from hengce.collectors.cninfo_reports import CninfoPeriodicReportCollector, CninfoReport
from hengce.contracts.dividend import AnnualDividendRecord
from hengce.contracts.enums import (
    ActionStatus,
    DiscoveryMethod,
    EvidenceCohort,
    EvidenceKind,
    EvidenceTaskStatus,
    QualityStatus,
    ReportType,
)
from hengce.contracts.evidence import FullMarketEvidenceTask
from hengce.contracts.financial import FilingDescriptor
from hengce.financials.pdf_extractor import CninfoPdfExtractor
from hengce.policy.guard import PolicyGuard
from hengce.raw_store.store import RawObjectStore
from hengce.state.dividend_repository import AnnualDividendRepository
from hengce.state.evidence_repository import FullMarketEvidenceRepository
from hengce.state.pdf_financial_repository import PdfFinancialDocumentRepository

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class FullMarketEvidenceAcquisitionService:
    def __init__(
        self,
        *,
        repository: FullMarketEvidenceRepository,
        collector: CninfoPeriodicReportCollector,
        client: httpx.Client,
        guard: PolicyGuard,
        raw_store: RawObjectStore,
        clock,
    ) -> None:
        self.repository = repository
        self.collector = collector
        self.client = client
        self.guard = guard
        self.raw_store = raw_store
        self.clock = clock

    def discover(
        self,
        run_id: str,
        *,
        max_securities: int,
        ts_code: str | None = None,
    ) -> dict[str, int]:
        self._assert_cohort_gate(run_id)
        _, tasks = self.repository.list_tasks(
            run_id=run_id,
            statuses=(EvidenceTaskStatus.PLANNED,),
            page_size=10000,
        )
        by_code: dict[str, list[FullMarketEvidenceTask]] = defaultdict(list)
        for task in tasks:
            if (ts_code is None or task.ts_code == ts_code) and task.evidence_kind in {
                EvidenceKind.PERIODIC_REPORT,
                EvidenceKind.DIVIDEND_YEAR,
            }:
                by_code[task.ts_code].append(task)
        discovered = failed = 0
        for ts_code in sorted(by_code)[:max_securities]:
            periodic_reports: tuple[CninfoReport, ...] | None = None
            for task in by_code[ts_code]:
                try:
                    if task.evidence_kind is EvidenceKind.PERIODIC_REPORT:
                        if periodic_reports is None:
                            periodic_reports = self.collector.reports(ts_code)
                        report = self._match(
                            task.evidence_period,
                            periodic_reports,
                            task.market_date,
                        )
                        if report is None:
                            report = self._match(
                                task.evidence_period,
                                self.collector.reports_for_period(
                                    ts_code,
                                    task.evidence_period,
                                ),
                                task.market_date,
                            )
                    else:
                        announcements = self.collector.dividend_announcements(
                            ts_code,
                            int(task.evidence_period),
                        )
                        report = next(
                            (
                                item
                                for item in reversed(announcements)
                                if item.published_at.date() <= task.market_date
                            ),
                            None,
                        )
                except (httpx.HTTPError, ValueError):
                    self.repository.transition(
                        task.task_id,
                        expected_version=task.version,
                        status=EvidenceTaskStatus.RETRYABLE_FAILED,
                        observed_at=self.clock(),
                        updates={
                            "attempt_count": task.attempt_count + 1,
                            "error_code": "CNINFO_REPORT_DISCOVERY_FAILED",
                        },
                    )
                    failed += 1
                    continue
                if report is None:
                    self.repository.transition(
                        task.task_id,
                        expected_version=task.version,
                        status=EvidenceTaskStatus.BLOCKED,
                        observed_at=self.clock(),
                        updates={
                            "error_code": (
                                "OFFICIAL_PERIODIC_REPORT_NOT_FOUND"
                                if task.evidence_kind is EvidenceKind.PERIODIC_REPORT
                                else "OFFICIAL_DIVIDEND_IMPLEMENTATION_NOT_FOUND"
                            )
                        },
                    )
                    failed += 1
                    continue
                self.repository.transition(
                    task.task_id,
                    expected_version=task.version,
                    status=EvidenceTaskStatus.DISCOVERED,
                    observed_at=self.clock(),
                    updates={
                        "source_id": "cninfo",
                        "source_url": report.attachment_url,
                        "source_title": report.title,
                        "source_record_ids": (f"cninfo-announcement:{report.announcement_id}",),
                        "published_at": report.published_at,
                        "error_code": None,
                    },
                )
                discovered += 1
        return {"discovered": discovered, "failed": failed}

    def download(
        self,
        run_id: str,
        *,
        max_tasks: int,
        ts_code: str | None = None,
    ) -> dict[str, int]:
        self._assert_cohort_gate(run_id)
        _, tasks = self.repository.list_tasks(
            run_id=run_id,
            statuses=(EvidenceTaskStatus.DISCOVERED,),
            page_size=max(max_tasks, 1),
        )
        downloaded = failed = 0
        selected = [task for task in tasks if ts_code is None or task.ts_code == ts_code]
        for task in selected[:max_tasks]:
            assert task.source_url is not None
            try:
                self.guard.authorize(
                    "cninfo", task.source_url, "financial_pdf", "full_market.download"
                )
                response = self.client.get(
                    task.source_url,
                    follow_redirects=False,
                    timeout=httpx.Timeout(60, connect=10),
                )
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").partition(";")[0]
                name = PurePosixPath(urlparse(task.source_url).path).name or "report.pdf"
                if content_type != "application/pdf" or not validate_attachment_payload(
                    name, content_type, response.content
                ):
                    raise ValueError("OFFICIAL_PDF_INVALID")
                collected_at = self.clock()
                raw = self.raw_store.put(
                    source_id="cninfo",
                    source_url=task.source_url,
                    collected_at=collected_at,
                    content_type=content_type,
                    payload=response.content,
                )
                self.repository.transition(
                    task.task_id,
                    expected_version=task.version,
                    status=EvidenceTaskStatus.DOWNLOADED,
                    observed_at=collected_at,
                    updates={
                        "collected_at": collected_at,
                        "raw_object_hash": raw.content_hash,
                        "source_record_ids": tuple(
                            dict.fromkeys((*task.source_record_ids, raw.content_hash))
                        ),
                        "attempt_count": task.attempt_count + 1,
                        "error_code": None,
                    },
                )
                downloaded += 1
            except (httpx.HTTPError, ValueError):
                self.repository.transition(
                    task.task_id,
                    expected_version=task.version,
                    status=EvidenceTaskStatus.RETRYABLE_FAILED,
                    observed_at=self.clock(),
                    updates={
                        "attempt_count": task.attempt_count + 1,
                        "error_code": "OFFICIAL_PDF_DOWNLOAD_FAILED",
                    },
                )
                failed += 1
        return {"downloaded": downloaded, "failed": failed}

    def prefill_risk(self, run_id: str, *, max_tasks: int) -> dict[str, int]:
        self._assert_cohort_gate(run_id)
        _, tasks = self.repository.list_tasks(run_id=run_id, page_size=10000)
        annual_sources: dict[str, FullMarketEvidenceTask] = {}
        for task in tasks:
            if (
                task.evidence_kind is EvidenceKind.PERIODIC_REPORT
                and task.evidence_period.endswith("-12-31")
                and task.raw_object_hash is not None
                and task.source_url is not None
                and task.published_at is not None
                and task.collected_at is not None
            ):
                current = annual_sources.get(task.ts_code)
                if current is None or task.evidence_period > current.evidence_period:
                    annual_sources[task.ts_code] = task
        planned = [
            task
            for task in tasks
            if task.evidence_kind is EvidenceKind.RISK_SCREEN
            and task.status is EvidenceTaskStatus.PLANNED
            and task.ts_code in annual_sources
        ]
        awaiting = failed = 0
        for task in planned[:max_tasks]:
            annual = annual_sources[task.ts_code]
            assert annual.raw_object_hash is not None
            try:
                page, excerpt, audit_standard = self._audit_opinion_prefill(
                    self.raw_store.validate_content_hash(annual.raw_object_hash)
                )
                st_status = self._st_status(task.security_name)
                self.repository.transition(
                    task.task_id,
                    expected_version=task.version,
                    status=EvidenceTaskStatus.AWAITING_REVIEW,
                    observed_at=self.clock(),
                    updates={
                        "source_id": "cninfo",
                        "source_url": annual.source_url,
                        "source_title": annual.source_title,
                        "published_at": annual.published_at,
                        "collected_at": annual.collected_at,
                        "raw_object_hash": annual.raw_object_hash,
                        "source_record_ids": annual.source_record_ids,
                        "source_page": page,
                        "excerpt": excerpt,
                        "prefilled_values": {
                            "audit_opinion_standard": audit_standard,
                            "major_investigation_open": None,
                            "delisting_risk": bool(st_status and "*ST" in st_status),
                            "st_status": st_status,
                            "is_suspended": None,
                            "publication_order_known": True,
                        },
                        "error_code": None,
                    },
                )
                awaiting += 1
            except (OSError, ValueError):
                self.repository.transition(
                    task.task_id,
                    expected_version=task.version,
                    status=EvidenceTaskStatus.RETRYABLE_FAILED,
                    observed_at=self.clock(),
                    updates={"error_code": "RISK_PREFILL_FAILED"},
                )
                failed += 1
        return {"awaiting_review": awaiting, "failed": failed}

    def parse(
        self,
        run_id: str,
        *,
        max_tasks: int,
        include_blocked: bool = False,
        ts_code: str | None = None,
        error_codes: frozenset[str] | None = None,
    ) -> dict[str, int]:
        self._assert_cohort_gate(run_id)
        _, tasks = self.repository.list_tasks(
            run_id=run_id,
            statuses=(
                (EvidenceTaskStatus.DOWNLOADED, EvidenceTaskStatus.BLOCKED)
                if include_blocked
                else (EvidenceTaskStatus.DOWNLOADED,)
            ),
            page_size=10000,
        )
        parsed = failed = 0
        documents = PdfFinancialDocumentRepository(self.repository.path)
        dividends = AnnualDividendRepository(self.repository.path)
        extractor = CninfoPdfExtractor(
            parser_version="cninfo-pdf-full-market-v9",
            ocr_cache_root=self.raw_store.root.parent / "normalized" / "ocr",
        )
        retryable_tasks = [
            task
            for task in tasks
            if task.evidence_kind
            in {
                EvidenceKind.PERIODIC_REPORT,
                EvidenceKind.DIVIDEND_YEAR,
            }
            and (ts_code is None or task.ts_code == ts_code)
            and (error_codes is None or task.error_code in error_codes)
            and task.raw_object_hash is not None
            and (
                task.status is EvidenceTaskStatus.DOWNLOADED
                or (
                    include_blocked
                    and (
                        (task.error_code or "").startswith("PDF_")
                        or task.error_code == "OFFICIAL_DIVIDEND_TERMS_MISSING"
                    )
                )
            )
        ]
        for task in retryable_tasks[:max_tasks]:
            if task.evidence_kind is EvidenceKind.DIVIDEND_YEAR:
                if self._parse_dividend_task(task, dividends):
                    parsed += 1
                else:
                    failed += 1
                continue
            assert task.source_url is not None
            assert task.published_at is not None
            assert task.collected_at is not None
            assert task.raw_object_hash is not None
            period = date.fromisoformat(task.evidence_period)
            descriptor = FilingDescriptor(
                source_id="cninfo",
                source_url=task.source_url,
                ts_code=task.ts_code,
                exchange="SSE" if task.ts_code.endswith(".SH") else "SZSE",
                report_period=period,
                report_type=ReportType.ANNUAL if period.month == 12 else ReportType.Q1,
                published_at=task.published_at,
                collected_at=task.collected_at,
                attachment_name=PurePosixPath(urlparse(task.source_url).path).name,
                content_type="application/pdf",
                raw_object_hash=task.raw_object_hash,
                taxonomy_refs=(),
                discovery_method=DiscoveryMethod.PUBLIC_PAGE,
                instance_entrypoint=None,
                issuer_name=task.security_name,
            )
            extracted = extractor.extract(
                pdf_path=self.raw_store.validate_content_hash(task.raw_object_hash),
                descriptor=descriptor,
            )
            parsed_updates = {
                "source_page": (
                    min(candidate.page_number for candidate in extracted.candidates)
                    if extracted.candidates
                    else None
                ),
                "prefilled_values": {
                    "parser_issues": ",".join(extracted.issues) or None,
                    "parser_version": extracted.parser_version,
                },
                "error_code": None,
            }
            if extracted.quality_status is not QualityStatus.VALID:
                intermediate = self.repository.transition(
                    task.task_id,
                    expected_version=task.version,
                    status=EvidenceTaskStatus.PARSED,
                    observed_at=self.clock(),
                    updates=parsed_updates,
                )
                self.repository.transition(
                    task.task_id,
                    expected_version=intermediate.version,
                    status=EvidenceTaskStatus.BLOCKED,
                    observed_at=self.clock(),
                    updates={
                        "error_code": extracted.issues[0]
                        if extracted.issues
                        else "OFFICIAL_PDF_PARSE_FAILED"
                    },
                )
                failed += 1
                continue
            filing_id = self._filing_id(descriptor, extracted.parser_version)
            versions = documents.list_versions(task.ts_code, period)
            supersedes_id = versions[-1].filing_id if versions else None
            document = extractor.build_document(
                filing_id=filing_id,
                descriptor=descriptor,
                extracted=extracted,
                supersedes_id=(supersedes_id if supersedes_id != filing_id else None),
            )
            documents.save(period, document)
            intermediate = self.repository.transition(
                task.task_id,
                expected_version=task.version,
                status=EvidenceTaskStatus.PARSED,
                observed_at=self.clock(),
                updates={
                    **parsed_updates,
                    "source_record_ids": (filing_id,),
                },
            )
            self.repository.transition(
                task.task_id,
                expected_version=intermediate.version,
                status=EvidenceTaskStatus.SATISFIED,
                observed_at=self.clock(),
            )
            parsed += 1
        return {"parsed": parsed, "failed": failed}

    def _parse_dividend_task(
        self,
        task: FullMarketEvidenceTask,
        repository: AnnualDividendRepository,
    ) -> bool:
        assert task.raw_object_hash is not None
        assert task.source_url is not None
        assert task.published_at is not None
        assert task.collected_at is not None
        evidence_task = task
        pdf_path = self.raw_store.validate_content_hash(evidence_task.raw_object_hash)
        try:
            page_number, excerpt, per_share, ex_date = self._dividend_terms(
                pdf_path,
                fiscal_year=int(task.evidence_period),
                allow_positive="实施" in (task.source_title or ""),
            )
        except (OSError, ValueError):
            _, run_tasks = self.repository.list_tasks(
                run_id=task.run_id,
                page_size=10000,
            )
            fallback = self._annual_dividend_fallback(task, run_tasks)
            try:
                if fallback is None:
                    raise ValueError("ANNUAL_DIVIDEND_FALLBACK_MISSING")
                assert fallback.raw_object_hash is not None
                evidence_task = fallback
                page_number, excerpt, per_share, ex_date = self._dividend_terms(
                    self.raw_store.validate_content_hash(fallback.raw_object_hash),
                    fiscal_year=int(task.evidence_period),
                    allow_positive=False,
                )
            except (OSError, ValueError):
                intermediate = self.repository.transition(
                    task.task_id,
                    expected_version=task.version,
                    status=EvidenceTaskStatus.PARSED,
                    observed_at=self.clock(),
                    updates={"error_code": None},
                )
                self.repository.transition(
                    task.task_id,
                    expected_version=intermediate.version,
                    status=EvidenceTaskStatus.BLOCKED,
                    observed_at=self.clock(),
                    updates={"error_code": "OFFICIAL_DIVIDEND_TERMS_MISSING"},
                )
                return False
        assert evidence_task.raw_object_hash is not None
        assert evidence_task.source_url is not None
        assert evidence_task.published_at is not None
        assert evidence_task.collected_at is not None
        evidence_hash = evidence_task.raw_object_hash
        evidence_source_ids = evidence_task.source_record_ids
        identity = json.dumps(
            {
                "market_date": task.market_date.isoformat(),
                "ts_code": task.ts_code,
                "fiscal_year": int(task.evidence_period),
                "raw_object_hash": evidence_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        record_id = f"annual-dividend-{hashlib.sha256(identity).hexdigest()}"
        effective_at = (
            datetime.combine(ex_date, time.min, tzinfo=evidence_task.published_at.tzinfo)
            if ex_date is not None
            else evidence_task.published_at
        )
        repository.save_version(
            AnnualDividendRecord(
                record_id=record_id,
                source_id=evidence_task.source_id or "cninfo",
                source_url=evidence_task.source_url,
                published_at=evidence_task.published_at,
                effective_at=effective_at,
                collected_at=evidence_task.collected_at,
                version=f"cninfo-dividend-{evidence_hash}",
                content_hash=evidence_hash,
                license_policy="official-public-attachment-personal-research",
                quality_status=QualityStatus.VALID,
                supersedes_id=None,
                valid_from=evidence_task.collected_at,
                ts_code=task.ts_code,
                fiscal_year=int(task.evidence_period),
                has_cash_dividend=per_share is not None,
                cash_dividend_per_share=per_share,
                cash_dividend_total=None,
                implementation_status=ActionStatus.IMPLEMENTED,
            )
        )
        intermediate = self.repository.transition(
            task.task_id,
            expected_version=task.version,
            status=EvidenceTaskStatus.PARSED,
            observed_at=self.clock(),
            updates={
                "source_id": evidence_task.source_id or "cninfo",
                "source_url": evidence_task.source_url,
                "source_title": evidence_task.source_title,
                "published_at": evidence_task.published_at,
                "collected_at": evidence_task.collected_at,
                "raw_object_hash": evidence_hash,
                "source_page": page_number,
                "excerpt": excerpt,
                "prefilled_values": {
                    "has_cash_dividend": per_share is not None,
                    "cash_dividend_per_share": (str(per_share) if per_share is not None else None),
                    "ex_date": ex_date.isoformat() if ex_date is not None else None,
                },
                "source_record_ids": tuple(dict.fromkeys((*evidence_source_ids, record_id))),
                "error_code": None,
            },
        )
        self.repository.transition(
            task.task_id,
            expected_version=intermediate.version,
            status=EvidenceTaskStatus.SATISFIED,
            observed_at=self.clock(),
        )
        return True

    @staticmethod
    def _annual_dividend_fallback(
        task: FullMarketEvidenceTask,
        candidates: tuple[FullMarketEvidenceTask, ...],
    ) -> FullMarketEvidenceTask | None:
        target_period = f"{task.evidence_period}-12-31"
        matches = [
            candidate
            for candidate in candidates
            if candidate.ts_code == task.ts_code
            and candidate.evidence_kind is EvidenceKind.PERIODIC_REPORT
            and candidate.evidence_period == target_period
            and candidate.raw_object_hash is not None
            and candidate.raw_object_hash != task.raw_object_hash
            and candidate.source_url is not None
            and candidate.published_at is not None
            and candidate.collected_at is not None
        ]
        return (
            max(matches, key=lambda candidate: (candidate.published_at, candidate.task_id))
            if matches
            else None
        )

    @staticmethod
    def _dividend_terms(
        pdf_path: Path,
        *,
        fiscal_year: int | None = None,
        allow_positive: bool = True,
    ) -> tuple[int, str, Decimal | None, date | None]:
        amount_pattern = re.compile(
            r"每\s*(?P<shares>10|1)\s*股[^。；]{0,30}?"
            r"(?:派发?|分配)[^。；\d]{0,30}?"
            r"(?P<amount>\d+(?:\.\d+)?)\s*元(?:人民币)?(?:现金)?"
        )
        date_pattern = re.compile(
            r"(?:除权除息日|除息日)\s*(?:为)?\s*[：:]?\s*"
            r"(?P<year>20\d{2})[年/-](?P<month>\d{1,2})[月/-]"
            r"(?P<day>\d{1,2})日?"
        )
        amount_match = None
        amount_page = None
        amount_excerpt = None
        ex_date = None
        no_dividend = None
        for page_number, page in enumerate(PdfReader(pdf_path).pages, start=1):
            text = re.sub(r"\s+", "", page.extract_text() or "")
            year_pattern = (
                rf"(?:本年度|{fiscal_year}年度|公司(?:计划|拟)(?:年度)?)"
                if fiscal_year
                else r"(?:本年度|20\d{2}年度|公司(?:计划|拟)(?:年度)?)"
            )
            if (
                no_dividend is None
                and (
                    match := re.search(
                        year_pattern + r"[^。；]{0,160}?(?:"
                        r"不派发现金红利|不发放现金红利|不进行现金分红|"
                        r"不进行利润分配|不进行现金分配(?:和送股)?"
                        r")",
                        text,
                    )
                )
                is not None
            ):
                no_dividend = (page_number, match.group(0)[:160])
            if (
                allow_positive
                and amount_match is None
                and (match := amount_pattern.search(text)) is not None
            ):
                amount_match = match
                amount_page = page_number
                amount_excerpt = match.group(0)[:160]
            if (
                allow_positive
                and ex_date is None
                and (match := date_pattern.search(text)) is not None
            ):
                ex_date = date(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                )
        if amount_match is None and no_dividend is not None:
            return no_dividend[0], no_dividend[1], None, None
        if amount_match is None or amount_page is None or amount_excerpt is None:
            raise ValueError("DIVIDEND_AMOUNT_MISSING")
        if ex_date is None:
            raise ValueError("DIVIDEND_EX_DATE_MISSING")
        try:
            per_share = Decimal(amount_match.group("amount")) / Decimal(
                amount_match.group("shares")
            )
        except InvalidOperation as error:
            raise ValueError("DIVIDEND_AMOUNT_INVALID") from error
        if per_share <= 0:
            raise ValueError("DIVIDEND_AMOUNT_INVALID")
        return amount_page, amount_excerpt, per_share, ex_date

    def retry_failed(
        self,
        run_id: str,
        *,
        max_tasks: int,
        error_codes: frozenset[str] | None = None,
        ts_code: str | None = None,
    ) -> dict[str, int]:
        self._assert_cohort_gate(run_id)
        _, tasks = self.repository.list_tasks(
            run_id=run_id,
            statuses=(EvidenceTaskStatus.RETRYABLE_FAILED, EvidenceTaskStatus.BLOCKED),
            page_size=10000,
        )
        tasks = tuple(
            sorted(
                (
                    task
                    for task in tasks
                    if (error_codes is None or task.error_code in error_codes)
                    and (ts_code is None or task.ts_code == ts_code)
                    and (
                        task.status is EvidenceTaskStatus.RETRYABLE_FAILED
                        or task.error_code
                        in {
                            "OFFICIAL_DIVIDEND_IMPLEMENTATION_NOT_FOUND",
                            "OFFICIAL_DIVIDEND_TERMS_MISSING",
                            "OFFICIAL_PERIODIC_REPORT_NOT_FOUND",
                        }
                    )
                ),
                key=lambda task: (
                    task.status is not EvidenceTaskStatus.RETRYABLE_FAILED,
                    task.ts_code,
                    task.evidence_kind.value,
                    task.evidence_period,
                ),
            )
        )
        retried = 0
        for task in tasks[:max_tasks]:
            rediscover_dividend = task.error_code == "OFFICIAL_DIVIDEND_TERMS_MISSING"
            target = (
                EvidenceTaskStatus.PLANNED
                if rediscover_dividend
                else EvidenceTaskStatus.DOWNLOADED
                if task.raw_object_hash is not None
                else EvidenceTaskStatus.DISCOVERED
                if task.source_url is not None
                else EvidenceTaskStatus.PLANNED
            )
            updates: dict[str, object] = {"error_code": None}
            if rediscover_dividend:
                updates.update(
                    {
                        "source_id": None,
                        "source_url": None,
                        "source_title": None,
                        "source_record_ids": (),
                        "published_at": None,
                        "collected_at": None,
                        "raw_object_hash": None,
                        "source_page": None,
                        "excerpt": None,
                        "prefilled_values": {},
                    }
                )
            self.repository.transition(
                task.task_id,
                expected_version=task.version,
                status=target,
                observed_at=self.clock(),
                updates=updates,
            )
            retried += 1
        return {"retried": retried}

    def _assert_cohort_gate(self, run_id: str) -> None:
        run = self.repository.get_run(run_id)
        if run is None:
            raise ValueError("EVIDENCE_RUN_NOT_FOUND")
        prerequisites = {
            EvidenceCohort.YIELD_GE_5: (),
            EvidenceCohort.YIELD_3_TO_5: (EvidenceCohort.YIELD_GE_5,),
            EvidenceCohort.LIQUIDITY_FILL: (
                EvidenceCohort.YIELD_GE_5,
                EvidenceCohort.YIELD_3_TO_5,
            ),
        }[run.cohort]
        latest = {item.cohort: item for item in self.repository.latest_runs(run.market_date)}
        for cohort in prerequisites:
            predecessor = latest.get(cohort)
            if predecessor is None:
                raise ValueError("EVIDENCE_COHORT_GATE_BLOCKED")
            counts = self.repository.status_counts(predecessor.run_id)
            if counts.get(EvidenceTaskStatus.SATISFIED.value, 0) != predecessor.task_count:
                raise ValueError("EVIDENCE_COHORT_GATE_BLOCKED")

    @staticmethod
    def _filing_id(descriptor: FilingDescriptor, parser_version: str) -> str:
        identity = json.dumps(
            {
                "source_id": descriptor.source_id,
                "ts_code": descriptor.ts_code,
                "report_period": descriptor.report_period.isoformat(),
                "report_type": descriptor.report_type.value,
                "raw_object_hash": descriptor.raw_object_hash,
                "parser_version": parser_version,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return f"pdf-filing-{hashlib.sha256(identity).hexdigest()}"

    @staticmethod
    def _st_status(security_name: str) -> str | None:
        normalized = security_name.upper().replace(" ", "")
        if "*ST" in normalized:
            return "*ST"
        if "ST" in normalized:
            return "ST"
        return None

    @staticmethod
    def _audit_opinion_prefill(
        pdf_path: Path,
    ) -> tuple[int | None, str | None, bool | None]:
        negative_markers = ("否定意见", "无法表示意见")
        modified_markers = ("强调事项段", "持续经营重大不确定性")
        conditional_markers = (
            "最近一年审计报告为",
            "是否为",
            "是否存在",
            "触及",
        )
        explicit_type_markers = (
            "审计意见类型",
            "审计报告意见类型",
            "内部控制审计报告中的审计意见",
        )
        pages = [page.extract_text() or "" for page in PdfReader(pdf_path).pages]

        def classify(window: str) -> bool | None:
            compact = re.sub(r"\s+", "", window)
            if any(marker in compact for marker in conditional_markers):
                return None
            without_unmodified = compact.replace("无保留意见", "")
            if any(marker in compact for marker in negative_markers) or (
                "保留意见" in without_unmodified
            ):
                return False
            if "无保留意见" in compact and not any(
                marker in compact for marker in modified_markers
            ):
                return True
            return None

        # Annual reports often repeat historical audit findings before the current
        # financial statements.  Prefer the report's explicit current opinion-type
        # field wherever it appears, rather than returning the first narrative hit.
        for page_number, text in enumerate(pages, start=1):
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            for index, line in enumerate(lines):
                compact_line = re.sub(r"\s+", "", line)
                if not any(marker in compact_line for marker in explicit_type_markers):
                    continue
                window = " ".join(lines[index : index + 2])[:500]
                result = classify(window)
                if result is not None:
                    return page_number, window or None, result

        for page_number, text in enumerate(pages, start=1):
            if "审计意见" not in text and "无保留意见" not in text:
                continue
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            for index, line in enumerate(lines):
                if "审计意见" not in line and "审计报告意见" not in line and "保留意见" not in line:
                    continue
                window = " ".join(lines[index : index + 2])[:500]
                result = classify(window)
                if result is not None:
                    return page_number, window or None, result
        return None, None, None

    @staticmethod
    def _match(
        evidence_period: str,
        reports: tuple[CninfoReport, ...],
        market_date: date,
    ) -> CninfoReport | None:
        period = date.fromisoformat(evidence_period)
        def normalized_title(report: CninfoReport) -> str:
            return re.sub(r"[\s_]", "", report.title)

        if period.month == 12:
            markers = (
                f"{period.year}年年度报告",
                f"{period.year}年度报告",
                f"{period.year}年年报",
                f"{period.year}年报",
            )
        else:
            markers = (
                f"{period.year}年一季度报告",
                f"{period.year}一季度报告",
                f"{period.year}年第一季度报告",
            )
        cutoff = datetime.combine(market_date, time(21, 30), tzinfo=_SHANGHAI)
        matches = [
            report
            for report in reports
            if report.published_at <= cutoff
            and (
                any(marker in normalized_title(report) for marker in markers)
                or (
                    period.month == 12
                    and normalized_title(report) == "年报全文"
                    and report.published_at.year == period.year + 1
                )
            )
        ]
        return (
            max(matches, key=lambda item: (item.published_at, item.announcement_id))
            if matches
            else None
        )


__all__ = ["FullMarketEvidenceAcquisitionService"]
