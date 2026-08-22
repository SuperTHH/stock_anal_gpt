from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from hengce.contracts.enums import QualityStatus
from hengce.services.financial_resolution import FinancialDocument
from hengce.state.pdf_financial_repository import (
    PdfFinancialDocumentRepository,
)
from hengce.state.repository import StateRepository

PERIOD = date(2025, 12, 31)


def document(
    filing_id: str,
    *,
    published_at: datetime,
    valid_from: datetime,
    supersedes_id: str | None = None,
    value: str = "100",
    source_id: str = "cninfo",
) -> FinancialDocument:
    return FinancialDocument(
        filing_id=filing_id,
        ts_code="600001.SH",
        source_id=source_id,
        source_kind="PDF",
        source_url=f"https://static.cninfo.com.cn/{filing_id}.pdf",
        published_at=published_at,
        valid_from=valid_from,
        version=f"pdf-{filing_id}",
        supersedes_id=supersedes_id,
        quality_status=QualityStatus.VALID,
        facts={"total_assets": Decimal(value)},
        normalization_metadata={"report_period": PERIOD.isoformat()},
    )


def repository(tmp_path: Path) -> PdfFinancialDocumentRepository:
    state = StateRepository(tmp_path / "state.sqlite3")
    state.migrate()
    return PdfFinancialDocumentRepository(state.path)


def test_pdf_versions_are_immutable_and_visible_point_in_time(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    original = document(
        "pdf-original",
        published_at=datetime(2026, 3, 30, tzinfo=UTC),
        valid_from=datetime(2026, 3, 31, tzinfo=UTC),
    )
    correction = document(
        "pdf-correction",
        published_at=datetime(2026, 4, 10, tzinfo=UTC),
        valid_from=datetime(2026, 4, 11, tzinfo=UTC),
        supersedes_id=original.filing_id,
        value="110",
    )

    assert repo.save(PERIOD, original) == original
    assert repo.save(PERIOD, original) == original
    assert repo.save(PERIOD, correction) == correction

    before = repo.visible_documents(
        "600001.SH",
        PERIOD,
        as_of=datetime(2026, 4, 5, tzinfo=UTC),
        known_at=datetime(2026, 4, 5, tzinfo=UTC),
    )
    after = repo.visible_documents(
        "600001.SH",
        PERIOD,
        as_of=datetime(2026, 4, 20, tzinfo=UTC),
        known_at=datetime(2026, 4, 20, tzinfo=UTC),
    )

    assert before == (original,)
    assert after == (original, correction)
    assert repo.list_versions("600001.SH", PERIOD) == (
        original,
        correction,
    )


def test_parser_revision_can_supersede_same_published_attachment(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    published_at = datetime(2026, 3, 30, tzinfo=UTC)
    original = document(
        "pdf-parser-v1",
        published_at=published_at,
        valid_from=datetime(2026, 3, 31, tzinfo=UTC),
    )
    revised = document(
        "pdf-parser-v2",
        published_at=published_at,
        valid_from=datetime(2026, 4, 2, tzinfo=UTC),
        supersedes_id=original.filing_id,
        value="110",
    )

    repo.save(PERIOD, original)

    assert repo.save(PERIOD, revised) == revised


@pytest.mark.parametrize("source_id", ["cninfo", "sse", "szse"])
def test_repository_accepts_approved_official_pdf_sources(
    tmp_path: Path,
    source_id: str,
) -> None:
    repo = repository(tmp_path)
    official = document(
        f"pdf-{source_id}",
        published_at=datetime(2026, 3, 30, tzinfo=UTC),
        valid_from=datetime(2026, 3, 31, tzinfo=UTC),
        source_id=source_id,
    )

    assert repo.save(PERIOD, official) == official


def test_repository_rejects_non_official_pdf_source(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    commercial = document(
        "pdf-commercial",
        published_at=datetime(2026, 3, 30, tzinfo=UTC),
        valid_from=datetime(2026, 3, 31, tzinfo=UTC),
        source_id="commercial",
    )

    with pytest.raises(ValueError, match="^PDF_FINANCIAL_DOCUMENT_INVALID$"):
        repo.save(PERIOD, commercial)


def test_pdf_repository_rejects_conflicts_and_broken_correction_chain(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    original = document(
        "pdf-original",
        published_at=datetime(2026, 3, 30, tzinfo=UTC),
        valid_from=datetime(2026, 3, 31, tzinfo=UTC),
    )
    repo.save(PERIOD, original)

    with pytest.raises(ValueError, match="^PDF_FINANCIAL_VERSION_CONFLICT$"):
        repo.save(PERIOD, original.model_copy(update={"version": "changed"}))
    with pytest.raises(ValueError, match="^PDF_FINANCIAL_CHAIN_GAP$"):
        repo.save(
            PERIOD,
            document(
                "pdf-orphan",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                valid_from=datetime(2026, 4, 11, tzinfo=UTC),
                supersedes_id="missing",
            ),
        )

    first_correction = document(
        "pdf-correction-one",
        published_at=datetime(2026, 4, 10, tzinfo=UTC),
        valid_from=datetime(2026, 4, 11, tzinfo=UTC),
        supersedes_id=original.filing_id,
    )
    repo.save(PERIOD, first_correction)
    with pytest.raises(ValueError, match="^PDF_FINANCIAL_BRANCH_CONFLICT$"):
        repo.save(
            PERIOD,
            document(
                "pdf-correction-two",
                published_at=datetime(2026, 4, 12, tzinfo=UTC),
                valid_from=datetime(2026, 4, 13, tzinfo=UTC),
                supersedes_id=original.filing_id,
            ),
        )
