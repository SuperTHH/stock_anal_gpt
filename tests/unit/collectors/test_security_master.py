from pathlib import Path

import pytest

from hengce.collectors.security_master import OfficialSecurityMasterCsvImporter


@pytest.mark.parametrize(
    ("source_id", "fixture", "codes"),
    [
        ("sse", "security_master_sse.csv", ["600000.SH", "688001.SH"]),
        ("szse", "security_master_szse.csv", ["000001.SZ", "300001.SZ"]),
    ],
)
def test_importer_enforces_declared_official_source(
    source_id: str, fixture: str, codes: list[str]
) -> None:
    records = OfficialSecurityMasterCsvImporter().parse(
        Path("tests/fixtures") / fixture,
        source_id=source_id,
    )

    assert [record.ts_code for record in records] == codes
    assert all(record.is_in_scope for record in records)


def test_importer_rejects_cross_exchange_in_scope_row(tmp_path: Path) -> None:
    path = tmp_path / "wrong-source.csv"
    path.write_text(
        "ts_code,symbol,name,exchange,board,currency,list_date,security_type\n"
        "000001.SZ,000001,Wrong,SZSE,MAIN_SZ,CNY,19910403,A_SHARE\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="SECURITY_MASTER_SOURCE_MISMATCH"):
        OfficialSecurityMasterCsvImporter().parse(path, source_id="sse")


def test_importer_excludes_wrong_currency_and_unknown_board(tmp_path: Path) -> None:
    path = tmp_path / "master.csv"
    path.write_text(
        "ts_code,symbol,name,exchange,board,currency,list_date,security_type\n"
        "600001.SH,600001,人民币主板,SSE,MAIN_SH,CNY,20100101,A_SHARE\n"
        "600002.SH,600002,美元主板,SSE,MAIN_SH,USD,20100101,A_SHARE\n"
        "600003.SH,600003,未知板块,SSE,OTHER,CNY,20100101,A_SHARE\n",
        encoding="utf-8",
    )

    records = OfficialSecurityMasterCsvImporter().parse(path, source_id="sse")

    assert [record.ts_code for record in records] == ["600001.SH"]


def test_importer_rejects_duplicate_codes_after_scope_filtering(tmp_path: Path) -> None:
    path = tmp_path / "duplicate-master.csv"
    path.write_text(
        "ts_code,symbol,name,exchange,board,currency,list_date,security_type\n"
        "600001.SH,600001,First,SSE,MAIN_SH,CNY,20100101,A_SHARE\n"
        "600001.SH,600001,Second,SSE,MAIN_SH,CNY,20100101,A_SHARE\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="SECURITY_MASTER_DUPLICATE_TS_CODE"):
        OfficialSecurityMasterCsvImporter().parse(path, source_id="sse")
