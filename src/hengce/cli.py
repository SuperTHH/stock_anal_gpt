"""Local-only command-line entry points for the research workspace."""

import hashlib
import io
import json
import re
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated
from zoneinfo import ZoneInfo

import httpx
import typer
from pydantic import SecretStr

from hengce.bootstrap import bootstrap_state
from hengce.collectors.security_master import OfficialSecurityMasterCsvImporter
from hengce.collectors.tushare import TushareDailyCollector
from hengce.config import Settings
from hengce.contracts.enums import DiscoveryMethod, ReportType
from hengce.contracts.financial import FilingDescriptor, TaxonomyPackageRef
from hengce.financials.mapping import (
    FinancialFactNormalizer,
)
from hengce.financials.package import (
    SafePackageMaterializer,
    validated_attachment_snapshot,
)
from hengce.financials.qname_inventory import QNameInventory
from hengce.financials.quality import FinancialQualityValidator
from hengce.financials.registry_loader import FinancialRegistryLoader
from hengce.financials.xbrl import ArelleXbrlProcessor
from hengce.policy.guard import PolicyGuard
from hengce.raw_store.store import RawObjectStore
from hengce.services.financial_ingestion import FinancialIngestionService
from hengce.services.initializer import HistoricalInitializer
from hengce.services.market_ingestion import MarketIngestionService
from hengce.services.pilot_reconstruction import (
    HistoricalPilotRunner,
    PilotRunSummary,
)
from hengce.state.financial_repository import FinancialFilingRepository
from hengce.state.pilot_repository import PilotRepository
from hengce.state.repository import StateRepository
from hengce.warehouse.financial import FinancialFactWarehouse
from hengce.warehouse.market import MarketWarehouse

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Hengce local research commands."""


def load_trade_dates(path: Path) -> list[date]:
    """Load a JSON calendar containing only ISO-8601 trade-date strings."""
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("TRADE_DATES_INVALID: expected a JSON array of ISO dates") from error
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("TRADE_DATES_INVALID: expected a JSON array of ISO dates")

    parsed: list[date] = []
    for value in values:
        try:
            item = date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("TRADE_DATES_INVALID: expected ISO dates") from error
        if item.isoformat() != value:
            raise ValueError("TRADE_DATES_INVALID: expected ISO dates")
        parsed.append(item)
    return sorted(set(parsed))


def parse_trade_date(value: str) -> date:
    """Parse a strict command-line ISO-8601 trade date."""
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise typer.BadParameter("trade date must use YYYY-MM-DD") from error
    if parsed.isoformat() != value:
        raise typer.BadParameter("trade date must use YYYY-MM-DD")
    return parsed


def parse_financial_date(value: str) -> date:
    """Parse a strict ISO report date."""
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise typer.BadParameter("report-period must use YYYY-MM-DD") from error
    if parsed.isoformat() != value:
        raise typer.BadParameter("report-period must use YYYY-MM-DD")
    return parsed


def parse_offset_datetime(value: str, option_name: str) -> datetime:
    """Parse an ISO datetime that contains an explicit UTC offset."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise typer.BadParameter(f"{option_name} must be ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise typer.BadParameter(f"{option_name} must include an offset")
    return parsed


def validate_financial_identity(source_id: str, ts_code: str, exchange: str) -> None:
    """Require the exchange and Tushare suffix declared by the source."""
    validate_financial_source(source_id)
    expected = {
        "sse": ("SSE", ".SH"),
        "szse": ("SZSE", ".SZ"),
    }[source_id]
    if (
        exchange != expected[0]
        or not re.fullmatch(r"[0-9]{6}\.(?:SH|SZ)", ts_code)
        or not ts_code.endswith(expected[1])
    ):
        raise ValueError("FINANCIAL_SOURCE_MISMATCH")


def validate_financial_source(source_id: str) -> None:
    """Restrict financial XBRL commands to the two exchange policy identities."""
    if source_id not in {"sse", "szse"}:
        raise ValueError("FINANCIAL_SOURCE_MISMATCH")


def validate_instance_entrypoint(file: Path, entrypoint: str | None) -> None:
    """Require a safe relative entrypoint exactly when the instance is a ZIP."""
    if file.suffix.lower() != ".zip":
        if entrypoint is not None:
            raise ValueError("FINANCIAL_ENTRYPOINT_INVALID")
        return
    if entrypoint is None or not is_safe_relative_entrypoint(entrypoint):
        raise ValueError("FINANCIAL_ENTRYPOINT_INVALID")


def validate_taxonomy_entrypoint(file: Path, entrypoint: str) -> None:
    """Require direct schemas to name themselves and ZIP entrypoints to stay relative."""
    if file.suffix.lower() == ".zip":
        valid = is_safe_relative_entrypoint(entrypoint)
    else:
        valid = entrypoint == file.name
    if not valid:
        raise ValueError("FINANCIAL_ENTRYPOINT_INVALID")


def is_safe_relative_entrypoint(entrypoint: str) -> bool:
    if (
        not entrypoint
        or "\x00" in entrypoint
        or "\\" in entrypoint
        or entrypoint.startswith(("/", "//"))
        or re.match(r"^[A-Za-z]:", entrypoint)
    ):
        return False
    relative = PurePosixPath(entrypoint)
    return bool(relative.parts) and not relative.is_absolute() and ".." not in relative.parts


def build_market_ingestion(
    settings: Settings, client: httpx.Client, policy_file: Path | None = None
) -> MarketIngestionService:
    """Compose M1 ingestion without making a request; caller owns ``client`` lifecycle."""
    state = bootstrap_state(settings, policy_file)
    if settings.tushare_token is None:
        raise typer.BadParameter("HENGCE_TUSHARE_TOKEN is required")
    collector = TushareDailyCollector(
        client=client,
        guard=PolicyGuard(state),
        token=SecretStr(settings.tushare_token.get_secret_value()),
        clock=lambda: datetime.now(ZoneInfo(settings.timezone)),
    )
    return MarketIngestionService(
        collector=collector,
        raw_store=RawObjectStore(settings.data_dir / "raw"),
        warehouse=MarketWarehouse(settings.data_dir / "normalized"),
        state=state,
    )


def build_financial_ingestion(
    settings: Settings,
    mapping_file: Path,
    entity_declarations_file: Path,
    report_period: date,
    universe_id: str,
    policy_file: Path | None = None,
) -> FinancialIngestionService:
    """Compose local-only financial ingestion from explicit reviewed registries."""
    state = bootstrap_state(settings, policy_file)
    raw_store = RawObjectStore(settings.data_dir / "raw")
    pilot_repository = PilotRepository(state.path)
    universe = pilot_repository.get_universe(universe_id)
    if universe is None:
        raise ValueError("PILOT_UNIVERSE_NOT_FOUND")
    loader = FinancialRegistryLoader()
    mapping_registry = loader.load_fact_registry(
        mapping_file,
        report_period,
    )
    entity_registry = loader.build_entity_registry(
        universe,
        loader.load_entity_declarations(entity_declarations_file),
    )
    return FinancialIngestionService(
        guard=PolicyGuard(state),
        raw_store=raw_store,
        repository=FinancialFilingRepository(state.path),
        materializer=SafePackageMaterializer(raw_store),
        processor=ArelleXbrlProcessor(),
        normalizer=FinancialFactNormalizer(
            mapping_registry,
            entity_registry,
        ),
        validator=FinancialQualityValidator(),
        warehouse=FinancialFactWarehouse(settings.data_dir / "warehouse"),
        state=state,
        pilot_repository=pilot_repository,
    )


def build_pilot_runner(settings: Settings) -> HistoricalPilotRunner:
    """Compose the resumable pilot shell; production stage wiring is explicit."""
    state = bootstrap_state(settings)

    def unconfigured_stage(context: object) -> dict[str, object]:
        del context
        raise ValueError("PILOT_STAGE_NOT_CONFIGURED")

    return HistoricalPilotRunner(
        state=state,
        data_dir=settings.data_dir,
        stage_handlers={
            stage: unconfigured_stage
            for stage in HistoricalPilotRunner.STAGES[1:]
        },
        clock=lambda: datetime.now(ZoneInfo(settings.timezone)),
    )


@app.command("init-state")
def init_state(
    data_dir: Annotated[Path, typer.Option(file_okay=False)] = Path("data"),
    policy_file: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Create idempotent local state and seed the approved source policies."""
    bootstrap_state(Settings(data_dir=data_dir), policy_file)
    typer.echo("state initialized")


@app.command("ingest-market")
def ingest_market(
    trade_date: Annotated[str, typer.Option()],
    data_dir: Annotated[Path, typer.Option(file_okay=False)] = Path("data"),
    policy_file: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Collect exactly one approved Tushare daily-market response."""
    parsed_trade_date = parse_trade_date(trade_date)
    settings = Settings(data_dir=data_dir)
    with httpx.Client() as client:
        result = build_market_ingestion(settings, client, policy_file).run(parsed_trade_date)
    typer.echo(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))


@app.command("initialize-history")
def initialize_history(
    calendar_file: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    data_dir: Annotated[Path, typer.Option(file_okay=False)] = Path("data"),
    policy_file: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Resume approved trading-day ingestion from its persisted checkpoint."""
    trade_dates = load_trade_dates(calendar_file)
    settings = Settings(data_dir=data_dir)
    with httpx.Client() as client:
        ingestion = build_market_ingestion(settings, client, policy_file)
        state = StateRepository(settings.data_dir / "state" / "hengce.sqlite3")
        result = HistoricalInitializer(ingestion=ingestion, state=state).run(trade_dates)
    typer.echo(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))


@app.command("import-security-master")
def import_security_master(
    file: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    source_id: Annotated[str, typer.Option()],
    source_url: Annotated[str, typer.Option()],
    version: Annotated[str, typer.Option()],
    collected_at: Annotated[str, typer.Option()],
    data_dir: Annotated[Path, typer.Option(file_okay=False)] = Path("data"),
    policy_file: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Import a locally supplied official security-master CSV; this command never fetches."""
    try:
        parsed_collected_at = datetime.fromisoformat(collected_at)
    except ValueError as error:
        raise typer.BadParameter("collected-at must be ISO-8601") from error
    if parsed_collected_at.tzinfo is None:
        raise typer.BadParameter("collected-at must include an offset")
    records = OfficialSecurityMasterCsvImporter().parse(file, source_id=source_id)
    content_hash = hashlib.sha256(file.read_bytes()).hexdigest()
    state = bootstrap_state(Settings(data_dir=data_dir), policy_file)
    snapshot = state.save_security_master_snapshot(
        records,
        source_id=source_id,
        source_url=source_url,
        collected_at=parsed_collected_at,
        content_hash=content_hash,
        version=version,
        quality_lineage={
            "filter": "a_share_cny_four_boards",
            "boards": ["CHINEXT", "MAIN_SH", "MAIN_SZ", "STAR"],
            "record_count": len(records),
        },
    )
    typer.echo(
        json.dumps(
            {
                "content_hash": snapshot.content_hash,
                "security_count": len(snapshot.securities),
                "source_id": snapshot.source_id,
                "version": snapshot.version,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@app.command("register-xbrl-taxonomy")
def register_xbrl_taxonomy(
    file: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    taxonomy_id: Annotated[str, typer.Option()],
    source_id: Annotated[str, typer.Option()],
    source_url: Annotated[str, typer.Option()],
    entrypoint: Annotated[str, typer.Option()],
    content_type: Annotated[str, typer.Option()],
    collected_at: Annotated[str, typer.Option()],
    data_dir: Annotated[Path, typer.Option(file_okay=False)] = Path("data"),
    policy_file: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Register one locally supplied taxonomy package; this command never fetches."""
    parsed_collected_at = parse_offset_datetime(collected_at, "collected-at")
    settings = Settings(data_dir=data_dir)
    state = bootstrap_state(settings, policy_file)
    PolicyGuard(state).validate(
        source_id,
        source_url,
        "xbrl",
        "cli.register_taxonomy",
    )
    validate_financial_source(source_id)
    with validated_attachment_snapshot(
        file,
        content_type,
        taxonomy=True,
    ) as snapshot:
        validate_taxonomy_entrypoint(snapshot.path, entrypoint)
        raw_ref = RawObjectStore(settings.data_dir / "raw").put(
            source_id=source_id,
            source_url=source_url,
            collected_at=parsed_collected_at,
            content_type=content_type,
            payload=snapshot.payload,
        )
    reference = TaxonomyPackageRef(
        taxonomy_id=taxonomy_id,
        source_id=source_id,
        source_url=source_url,
        raw_object_hash=raw_ref.content_hash,
        package_name=file.name,
        entrypoint=entrypoint,
        content_type=content_type,
        collected_at=parsed_collected_at,
    )
    FinancialFilingRepository(state.path).register_taxonomy(reference)
    typer.echo(
        json.dumps(
            reference.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@app.command("import-financial-xbrl")
def import_financial_xbrl(
    file: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    source_id: Annotated[str, typer.Option()],
    source_url: Annotated[str, typer.Option()],
    ts_code: Annotated[str, typer.Option()],
    exchange: Annotated[str, typer.Option()],
    report_period: Annotated[str, typer.Option()],
    report_type: Annotated[ReportType, typer.Option()],
    published_at: Annotated[str, typer.Option()],
    collected_at: Annotated[str, typer.Option()],
    content_type: Annotated[str, typer.Option()],
    taxonomy_id: Annotated[list[str], typer.Option()],
    manifest_item_id: Annotated[str, typer.Option()],
    mapping_file: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    entity_declarations_file: Annotated[
        Path,
        typer.Option(exists=True, dir_okay=False),
    ],
    instance_entrypoint: Annotated[str | None, typer.Option()] = None,
    data_dir: Annotated[Path, typer.Option(file_okay=False)] = Path("data"),
    policy_file: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Import one locally supplied financial XBRL attachment; this command never fetches."""
    parsed_report_period = parse_financial_date(report_period)
    parsed_published_at = parse_offset_datetime(published_at, "published-at")
    parsed_collected_at = parse_offset_datetime(collected_at, "collected-at")
    settings = Settings(data_dir=data_dir)
    state = bootstrap_state(settings, policy_file)
    PolicyGuard(state).validate(
        source_id,
        source_url,
        "xbrl",
        "cli.import_financial_xbrl",
    )
    validate_financial_identity(source_id, ts_code, exchange)
    with validated_attachment_snapshot(
        file,
        content_type,
        taxonomy=False,
    ) as snapshot:
        validate_instance_entrypoint(snapshot.path, instance_entrypoint)
        pilot_repository = PilotRepository(state.path)
        manifest_item = pilot_repository.get_manifest_item(manifest_item_id)
        if manifest_item is None:
            raise ValueError("ACQUISITION_ITEM_NOT_FOUND")
        snapshot_hash = hashlib.sha256(snapshot.payload).hexdigest()
        if snapshot_hash != manifest_item.raw_object_hash:
            raise ValueError("ACQUISITION_DESCRIPTOR_MISMATCH")
        raw_ref = RawObjectStore(settings.data_dir / "raw").put(
            source_id=source_id,
            source_url=source_url,
            collected_at=parsed_collected_at,
            content_type=content_type,
            payload=snapshot.payload,
        )
    descriptor = FilingDescriptor(
        source_id=source_id,
        source_url=source_url,
        ts_code=ts_code,
        exchange=exchange,
        report_period=parsed_report_period,
        report_type=report_type,
        published_at=parsed_published_at,
        collected_at=parsed_collected_at,
        attachment_name=file.name,
        content_type=content_type,
        raw_object_hash=raw_ref.content_hash,
        taxonomy_refs=tuple(taxonomy_id),
        discovery_method=DiscoveryMethod.MANUAL_IMPORT,
        instance_entrypoint=instance_entrypoint,
    )
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        result = build_financial_ingestion(
            settings,
            mapping_file,
            entity_declarations_file,
            parsed_report_period,
            manifest_item.universe_id,
            policy_file,
        ).run(
            descriptor,
            manifest_item_id=manifest_item_id,
        )
    typer.echo(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))


@app.command("inspect-financial-qnames")
def inspect_financial_qnames(
    instance_file: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    taxonomy_file: Annotated[list[Path], typer.Option(exists=True, dir_okay=False)],
) -> None:
    """Enumerate exact local QName evidence without creating canonical mappings."""
    items = QNameInventory().inspect(instance_file, taxonomy_file)
    typer.echo(
        json.dumps(
            [asdict(item) for item in items],
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@app.command("check-security-universe")
def check_security_universe(
    data_dir: Annotated[Path, typer.Option(file_okay=False)] = Path("data"),
    policy_file: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Report whether the locally persisted security universe is complete and approved."""
    settings = Settings.model_construct(
        data_dir=data_dir,
        tushare_token=None,
        timezone="Asia/Shanghai",
    )
    state = bootstrap_state(settings, policy_file)
    universe = state.get_security_master_universe()
    components = sorted(
        (
            {"source_id": component.source_id, "version": component.version}
            for component in universe.components
        ),
        key=lambda component: (component["source_id"], component["version"]),
    )
    typer.echo(
        json.dumps(
            {
                "as_of": universe.as_of.isoformat(),
                "component_count": len(components),
                "components": components,
                "security_count": len(universe.securities),
                "universe_hash": universe.universe_hash,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@app.command("rebuild-pilot-report")
def rebuild_pilot_report(
    market_date: Annotated[str, typer.Option()],
    report_cutoff_at: Annotated[str, typer.Option()],
    acquisition_mode: Annotated[str, typer.Option()] = "manual-only",
    data_dir: Annotated[Path, typer.Option(file_okay=False)] = Path("data"),
) -> None:
    """Resume the historical pilot pipeline and emit aggregate JSON only."""
    parsed_market_date = parse_trade_date(market_date)
    parsed_cutoff = parse_offset_datetime(
        report_cutoff_at,
        "report-cutoff-at",
    )
    if acquisition_mode not in {"manual-only", "approved-public"}:
        raise typer.BadParameter(
            "acquisition-mode must be manual-only or approved-public"
        )
    settings = Settings.model_construct(
        data_dir=data_dir,
        tushare_token=None,
        timezone="Asia/Shanghai",
    )
    known_at = datetime.now(ZoneInfo(settings.timezone))
    summary = build_pilot_runner(settings).run(
        market_date=parsed_market_date,
        report_cutoff_at=parsed_cutoff,
        known_at=known_at,
        acquisition_mode=acquisition_mode,
    )
    output = _aggregate_pilot_summary(summary)
    typer.echo(json.dumps(output, ensure_ascii=False, sort_keys=True))
    if summary.failed_stage is not None:
        raise typer.Exit(code=1)
    if int(output["manual_todo_count"]) > 0 and output["report_id"] is None:
        raise typer.Exit(code=2)


def _aggregate_pilot_summary(
    summary: PilotRunSummary,
) -> dict[str, object]:
    aggregate = summary.aggregate_summary
    return {
        "universe_id": summary.universe_id,
        "stage_statuses": summary.stage_statuses,
        "manifest_status_distribution": aggregate.get(
            "manifest_status_distribution",
            {},
        ),
        "xbrl_used_count": int(aggregate.get("xbrl_used_count", 0)),
        "pdf_used_count": int(aggregate.get("pdf_used_count", 0)),
        "pool_coverage": aggregate.get("pool_coverage", {}),
        "report_id": aggregate.get("report_id"),
        "report_hash": aggregate.get("report_hash"),
        "manual_todo_count": int(aggregate.get("manual_todo_count", 0)),
        "failed_stage": summary.failed_stage,
        "error_code": summary.error_code,
    }


if __name__ == "__main__":
    app()
