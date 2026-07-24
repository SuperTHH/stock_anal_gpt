from pathlib import Path

from hengce.collectors.security_master import OfficialSecurityMasterCsvImporter


def test_importer_keeps_only_in_scope_a_shares() -> None:
    records = OfficialSecurityMasterCsvImporter().parse(Path("tests/fixtures/security_master.csv"))

    assert [record.ts_code for record in records] == [
        "000001.SZ",
        "300001.SZ",
        "600000.SH",
        "688001.SH",
    ]
    assert all(record.is_in_scope for record in records)


def test_importer_excludes_wrong_currency_and_unknown_board(tmp_path: Path) -> None:
    path = tmp_path / "master.csv"
    path.write_text(
        "ts_code,symbol,name,exchange,board,currency,list_date,security_type\n"
        "600001.SH,600001,人民币主板,SSE,MAIN_SH,CNY,20100101,A_SHARE\n"
        "600002.SH,600002,美元主板,SSE,MAIN_SH,USD,20100101,A_SHARE\n"
        "600003.SH,600003,未知板块,SSE,OTHER,CNY,20100101,A_SHARE\n",
        encoding="utf-8",
    )

    records = OfficialSecurityMasterCsvImporter().parse(path)

    assert [record.ts_code for record in records] == ["600001.SH"]
