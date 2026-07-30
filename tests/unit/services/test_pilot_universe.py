import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from hengce.contracts.market import SecurityMaster
from hengce.services.pilot_universe import PILOT_QUOTAS, PilotUniverseSelector

FIXTURE_ROOT = Path(__file__).parents[2] / "fixtures" / "pilot"
MARKET_DATE = date(2026, 7, 22)
CUTOFF = datetime(2026, 7, 22, 13, 30, tzinfo=UTC)
CREATED = datetime(2026, 7, 30, 9, tzinfo=UTC)
HASH = "a" * 64


def sample_inputs() -> tuple[list[SecurityMaster], list[dict[str, object]]]:
    master_config = json.loads(
        (FIXTURE_ROOT / "security_master.json").read_text(encoding="utf-8")
    )
    bar_config = json.loads(
        (FIXTURE_ROOT / "market_bars.json").read_text(encoding="utf-8")
    )
    securities: list[SecurityMaster] = []
    bars: list[dict[str, object]] = []
    sequence = 0
    for board_config in master_config:
        for index in range(1, board_config["count"] + 1):
            sequence += 1
            symbol = f"{board_config['prefix']}{index:03d}"
            ts_code = f"{symbol}.{board_config['suffix']}"
            securities.append(
                SecurityMaster(
                    ts_code=ts_code,
                    symbol=symbol,
                    name=f"虚构公司{sequence:02d}",
                    exchange=board_config["exchange"],
                    board=board_config["board"],
                    currency="CNY",
                    list_date=date(2020, 1, 1),
                    delist_date=None,
                    industry_l1="虚构行业",
                    security_type="A_SHARE",
                    is_in_scope=True,
                )
            )
            bars.append(
                {
                    "record_id": f"bar-{ts_code}",
                    "ts_code": ts_code,
                    "trade_date": bar_config["trade_date"],
                    "open": bar_config["open"],
                    "high": bar_config["high"],
                    "low": bar_config["low"],
                    "close": bar_config["close"],
                    "pre_close": bar_config["pre_close"],
                    "volume": bar_config["volume"],
                    "amount": str(
                        Decimal(bar_config["amount_start"])
                        - Decimal(bar_config["amount_step"]) * sequence
                    ),
                }
            )
    return securities, bars


def select(
    securities: list[SecurityMaster],
    bars: list[dict[str, object]],
    *,
    created_at: datetime = CREATED,
):
    return PilotUniverseSelector().select(
        market_date=MARKET_DATE,
        report_cutoff_at=CUTOFF,
        bars=bars,
        securities=securities,
        market_content_hash=HASH,
        master_universe_hash="b" * 64,
        created_at=created_at,
    )


def test_selects_exact_board_quotas_and_deterministic_liquidity_order() -> None:
    """Catches cross-board filling or a ranking order other than amount then code."""
    securities, bars = sample_inputs()
    for row in bars:
        if row["ts_code"] in {"600001.SH", "600002.SH"}:
            row["amount"] = "2000000000"

    snapshot = select(list(reversed(securities)), list(reversed(bars)))

    assert snapshot.quotas == PILOT_QUOTAS
    assert len(snapshot.members) == 30
    assert {
        board: sum(member.board == board for member in snapshot.members)
        for board in PILOT_QUOTAS
    } == PILOT_QUOTAS
    main_sh = [member for member in snapshot.members if member.board == "MAIN_SH"]
    assert [member.ts_code for member in main_sh[:2]] == ["600001.SH", "600002.SH"]
    assert [member.rank_in_board for member in main_sh] == list(range(1, 9))
    assert all(len(member.evidence_record_ids) == 2 for member in snapshot.members)


@pytest.mark.parametrize(
    "mutation",
    [
        "out_of_scope",
        "recent_listing",
        "st_name",
        "delisting_name",
        "wrong_suffix",
        "missing_bar",
        "zero_amount",
        "non_finite_amount",
        "invalid_price",
        "duplicate_bar",
    ],
)
def test_hard_filters_remove_ineligible_security_before_ranking(mutation: str) -> None:
    """Catches an ineligible high-liquidity security leaking into the pilot sample."""
    securities, bars = sample_inputs()
    target = "600001.SH"
    security_index = next(
        index for index, item in enumerate(securities) if item.ts_code == target
    )
    bar_index = next(index for index, item in enumerate(bars) if item["ts_code"] == target)

    if mutation == "out_of_scope":
        securities[security_index] = securities[security_index].model_copy(
            update={"is_in_scope": False}
        )
    elif mutation == "recent_listing":
        securities[security_index] = securities[security_index].model_copy(
            update={"list_date": date(2026, 1, 1)}
        )
    elif mutation == "st_name":
        securities[security_index] = securities[security_index].model_copy(
            update={"name": "*ST虚构"}
        )
    elif mutation == "delisting_name":
        securities[security_index] = securities[security_index].model_copy(
            update={"name": "虚构退"}
        )
    elif mutation == "wrong_suffix":
        securities[security_index] = securities[security_index].model_copy(
            update={"ts_code": "600001.SZ"}
        )
    elif mutation == "missing_bar":
        bars.pop(bar_index)
    elif mutation == "zero_amount":
        bars[bar_index]["amount"] = "0"
    elif mutation == "non_finite_amount":
        bars[bar_index]["amount"] = "NaN"
    elif mutation == "invalid_price":
        bars[bar_index]["high"] = "-1"
    elif mutation == "duplicate_bar":
        bars.append(dict(bars[bar_index]))

    snapshot = select(securities, bars)

    assert target not in {member.ts_code for member in snapshot.members}
    assert len(snapshot.members) == 30


def test_board_quota_failure_does_not_borrow_from_another_board() -> None:
    """Catches changing the approved board balance when one board is short."""
    securities, bars = sample_inputs()
    removed = {"600001.SH", "600002.SH", "600003.SH"}
    securities = [item for item in securities if item.ts_code not in removed]
    bars = [item for item in bars if item["ts_code"] not in removed]

    with pytest.raises(ValueError, match="^PILOT_BOARD_QUOTA_UNMET:MAIN_SH$"):
        select(securities, bars)


def test_hash_is_stable_across_input_order_and_created_time_but_changes_with_amount() -> None:
    """Catches a sample identity that depends on runtime order instead of source content."""
    securities, bars = sample_inputs()
    first = select(securities, bars)
    reordered = select(
        list(reversed(securities)),
        list(reversed(bars)),
        created_at=CREATED.replace(hour=10),
    )
    changed_bars = [dict(row) for row in bars]
    changed_bars[0]["amount"] = str(Decimal(str(changed_bars[0]["amount"])) + Decimal("1"))
    changed = select(securities, changed_bars)

    assert first.universe_id == reordered.universe_id
    assert first.manifest_hash == reordered.manifest_hash
    assert first.created_at != reordered.created_at
    assert changed.universe_id != first.universe_id
    assert changed.manifest_hash != first.manifest_hash


def test_rejects_duplicate_security_master_identity() -> None:
    """Catches selecting from an ambiguous official-master identity."""
    securities, bars = sample_inputs()
    securities.append(securities[0])

    with pytest.raises(ValueError, match="^PILOT_SECURITY_MASTER_DUPLICATE$"):
        select(securities, bars)
