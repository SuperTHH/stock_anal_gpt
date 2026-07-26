from __future__ import annotations

import stat
import struct
from datetime import UTC, date, datetime
from pathlib import Path
from zipfile import ZipFile, ZipInfo

import pytest

from hengce.contracts.enums import DiscoveryMethod, ReportType
from hengce.contracts.financial import FilingDescriptor, TaxonomyPackageRef
from hengce.financials.package import LocalAttachmentInspector, SafePackageMaterializer
from hengce.raw_store import RawObjectStore

NOW = datetime(2026, 7, 26, 12, tzinfo=UTC)
MIB = 1024 * 1024


def store_payload(store: RawObjectStore, name: str, content_type: str, payload: bytes) -> str:
    return store.put(
        source_id="sse",
        source_url=f"https://www.sse.com.cn/{name}",
        collected_at=NOW,
        content_type=content_type,
        payload=payload,
    ).content_hash


def descriptor_for(
    content_hash: str,
    *,
    attachment_name: str = "instance.xml",
    content_type: str = "application/xml",
    instance_entrypoint: str | None = None,
) -> FilingDescriptor:
    return FilingDescriptor(
        source_id="sse",
        source_url="https://www.sse.com.cn/instance",
        ts_code="600001.SH",
        exchange="SSE",
        report_period=date(2025, 12, 31),
        report_type=ReportType.ANNUAL,
        published_at=NOW,
        collected_at=NOW,
        attachment_name=attachment_name,
        content_type=content_type,
        raw_object_hash=content_hash,
        taxonomy_refs=("test-gaap-2025",),
        discovery_method=DiscoveryMethod.FIXTURE,
        instance_entrypoint=instance_entrypoint,
    )


def taxonomy_for(
    content_hash: str,
    *,
    package_name: str = "taxonomy.zip",
    entrypoint: str = "entry.xsd",
    content_type: str = "application/zip",
) -> TaxonomyPackageRef:
    return TaxonomyPackageRef(
        taxonomy_id="test-gaap-2025",
        source_id="sse",
        source_url="https://www.sse.com.cn/taxonomy",
        raw_object_hash=content_hash,
        package_name=package_name,
        entrypoint=entrypoint,
        content_type=content_type,
        collected_at=NOW,
    )


def write_zip(path: Path, members: list[tuple[str, bytes]]) -> None:
    with ZipFile(path, "w") as handle:
        for name, payload in members:
            handle.writestr(name, payload)


def stored_fixture(
    tmp_path: Path,
    taxonomy_archive: Path,
) -> tuple[RawObjectStore, FilingDescriptor, TaxonomyPackageRef]:
    store = RawObjectStore(tmp_path / "raw")
    instance_hash = store_payload(store, "instance.xml", "application/xml", b"<x/>")
    taxonomy_hash = store_payload(
        store,
        "taxonomy.zip",
        "application/zip",
        taxonomy_archive.read_bytes(),
    )
    return store, descriptor_for(instance_hash), taxonomy_for(taxonomy_hash)


def stored_valid_fixture(
    tmp_path: Path,
) -> tuple[RawObjectStore, FilingDescriptor, TaxonomyPackageRef]:
    store = RawObjectStore(tmp_path / "raw")
    instance_hash = store_payload(store, "instance.xml", "application/xml", b"<x/>")
    taxonomy_hash = store_payload(
        store,
        "taxonomy.xsd",
        "application/xml-schema",
        b"<schema/>",
    )
    return (
        store,
        descriptor_for(instance_hash),
        taxonomy_for(
            taxonomy_hash,
            package_name="taxonomy.xsd",
            entrypoint="taxonomy.xsd",
            content_type="application/xml-schema",
        ),
    )


def patch_central_sizes(
    path: Path,
    sizes: list[tuple[int, int]],
) -> None:
    content = bytearray(path.read_bytes())
    offset = 0
    for compressed_size, file_size in sizes:
        offset = content.index(b"PK\x01\x02", offset)
        struct.pack_into("<I", content, offset + 20, compressed_size)
        struct.pack_into("<I", content, offset + 24, file_size)
        offset += 46
    path.write_bytes(content)


def test_inspector_rejects_mime_extension_mismatch_and_doctype(tmp_path: Path) -> None:
    xml = tmp_path / "filing.xml"
    xml.write_bytes(b"<!DOCTYPE x [<!ENTITY x SYSTEM 'file:///secret'>]><x/>")
    inspector = LocalAttachmentInspector()
    with pytest.raises(ValueError, match="FINANCIAL_XML_UNSAFE"):
        inspector.validate(xml, "application/xml", taxonomy=False)
    with pytest.raises(ValueError, match="FINANCIAL_ATTACHMENT_TYPE_INVALID"):
        inspector.validate(xml, "application/zip", taxonomy=False)


@pytest.mark.parametrize("token", [b"<!doctype x><x/>", b"<!EnTiTy x 'value'><x/>"])
def test_inspector_rejects_xml_directives_case_insensitively(
    tmp_path: Path,
    token: bytes,
) -> None:
    xml = tmp_path / "filing.xbrl"
    xml.write_bytes(token)

    with pytest.raises(ValueError, match="FINANCIAL_XML_UNSAFE"):
        LocalAttachmentInspector().validate(xml, "application/xbrl+xml", taxonomy=False)


@pytest.mark.parametrize(
    ("name", "content_type", "payload"),
    [
        ("instance.xsd", "application/xml-schema", b"<schema/>"),
        ("instance.xml", "application/xml", b"PK\x03\x04not-xml"),
        ("instance.zip", "application/zip", b"<x/>"),
        ("instance.xml", "application/octet-stream", b"<x/>"),
    ],
)
def test_inspector_requires_consistent_role_extension_mime_and_signature(
    tmp_path: Path,
    name: str,
    content_type: str,
    payload: bytes,
) -> None:
    attachment = tmp_path / name
    attachment.write_bytes(payload)

    with pytest.raises(ValueError, match="FINANCIAL_ATTACHMENT_TYPE_INVALID"):
        LocalAttachmentInspector().validate(attachment, content_type, taxonomy=False)


def test_materializer_rejects_zip_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "taxonomy.zip"
    write_zip(archive, [("../escape.xsd", b"<x/>")])
    store, descriptor, taxonomy = stored_fixture(tmp_path, archive)
    with pytest.raises(ValueError, match="FINANCIAL_ARCHIVE_UNSAFE_PATH"):
        with SafePackageMaterializer(store).materialize(descriptor, (taxonomy,)):
            pass


@pytest.mark.parametrize(
    "member_name",
    [
        "/absolute.xsd",
        "C:/drive.xsd",
        "//server/share.xsd",
        r"folder\backslash.xsd",
        "folder/../../escape.xsd",
    ],
)
def test_materializer_rejects_unsafe_archive_member_names(
    tmp_path: Path,
    member_name: str,
) -> None:
    archive = tmp_path / "taxonomy.zip"
    archive_name = (
        member_name.replace("\\", "!") if "\\" in member_name else member_name
    )
    write_zip(archive, [(archive_name, b"<x/>")])
    if "\\" in member_name:
        archive.write_bytes(
            archive.read_bytes().replace(
                archive_name.encode(),
                member_name.encode(),
            )
        )
    store, descriptor, taxonomy = stored_fixture(tmp_path, archive)

    with pytest.raises(ValueError, match="FINANCIAL_ARCHIVE_UNSAFE_PATH"):
        with SafePackageMaterializer(store).materialize(descriptor, (taxonomy,)):
            pass


def test_materializer_rejects_nul_in_raw_archive_member_name(tmp_path: Path) -> None:
    archive = tmp_path / "taxonomy.zip"
    write_zip(archive, [("nul!.xsd", b"<x/>")])
    archive.write_bytes(archive.read_bytes().replace(b"nul!.xsd", b"nul\x00.xsd"))
    store, descriptor, taxonomy = stored_fixture(tmp_path, archive)

    with pytest.raises(ValueError, match="FINANCIAL_ARCHIVE_UNSAFE_PATH"):
        with SafePackageMaterializer(store).materialize(descriptor, (taxonomy,)):
            pass


def test_materializer_rejects_symlink_members(tmp_path: Path) -> None:
    archive = tmp_path / "taxonomy.zip"
    link = ZipInfo("entry.xsd")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with ZipFile(archive, "w") as handle:
        handle.writestr(link, b"target.xsd")
    store, descriptor, taxonomy = stored_fixture(tmp_path, archive)

    with pytest.raises(ValueError, match="FINANCIAL_ARCHIVE_UNSAFE_PATH"):
        with SafePackageMaterializer(store).materialize(descriptor, (taxonomy,)):
            pass


def test_materializer_rejects_duplicate_normalized_output_paths(tmp_path: Path) -> None:
    archive = tmp_path / "taxonomy.zip"
    write_zip(
        archive,
        [
            ("folder/entry.xsd", b"<one/>"),
            ("folder//entry.xsd", b"<two/>"),
        ],
    )
    store, descriptor, taxonomy = stored_fixture(tmp_path, archive)

    with pytest.raises(ValueError, match="FINANCIAL_ARCHIVE_UNSAFE_PATH"):
        with SafePackageMaterializer(store).materialize(descriptor, (taxonomy,)):
            pass


def limit_archive(tmp_path: Path, case: str) -> Path:
    archive = tmp_path / f"{case}.zip"
    if case == "file-count":
        write_zip(archive, [(f"{index}.txt", b"") for index in range(5_001)])
    elif case == "single-file":
        write_zip(archive, [("large.bin", b"x")])
        patch_central_sizes(archive, [(64 * MIB + 1, 64 * MIB + 1)])
    elif case == "total-size":
        write_zip(archive, [(f"{index}.bin", b"x") for index in range(9)])
        patch_central_sizes(archive, [(63 * MIB, 63 * MIB)] * 9)
    elif case == "compression-ratio":
        write_zip(archive, [("ratio.bin", b"x")])
        patch_central_sizes(archive, [(1, 101)])
    else:
        raise AssertionError(f"unhandled test case: {case}")
    return archive


@pytest.mark.parametrize(
    "case",
    ["file-count", "single-file", "total-size", "compression-ratio"],
)
def test_materializer_rejects_declared_archive_limits(tmp_path: Path, case: str) -> None:
    archive = limit_archive(tmp_path, case)
    store, descriptor, taxonomy = stored_fixture(tmp_path, archive)

    with pytest.raises(ValueError, match="FINANCIAL_ARCHIVE_LIMIT_EXCEEDED"):
        with SafePackageMaterializer(store).materialize(descriptor, (taxonomy,)):
            pass


def test_materializer_rejects_corrupt_zip_with_stable_error(tmp_path: Path) -> None:
    store = RawObjectStore(tmp_path / "raw")
    instance_hash = store_payload(store, "instance.xml", "application/xml", b"<x/>")
    taxonomy_hash = store_payload(
        store,
        "taxonomy.zip",
        "application/zip",
        b"PK\x03\x04broken",
    )

    with pytest.raises(ValueError, match="FINANCIAL_ARCHIVE_INVALID"):
        with SafePackageMaterializer(store).materialize(
            descriptor_for(instance_hash),
            (taxonomy_for(taxonomy_hash),),
        ):
            pass


def test_materializer_rejects_unsafe_xml_inside_zip(tmp_path: Path) -> None:
    archive = tmp_path / "taxonomy.zip"
    write_zip(archive, [("entry.xsd", b"<!ENTITY x SYSTEM 'file:///secret'><schema/>")])
    store, descriptor, taxonomy = stored_fixture(tmp_path, archive)

    with pytest.raises(ValueError, match="FINANCIAL_XML_UNSAFE"):
        with SafePackageMaterializer(store).materialize(descriptor, (taxonomy,)):
            pass


def test_materializer_cleans_temporary_tree_and_preserves_safe_names(tmp_path: Path) -> None:
    store, descriptor, taxonomy = stored_valid_fixture(tmp_path)
    with SafePackageMaterializer(store).materialize(
        descriptor, (taxonomy,)
    ) as materialized:
        root = materialized.root
        assert materialized.entrypoint_path.name == "instance.xml"
        assert materialized.entrypoint_path.is_file()
        assert all(path.is_file() for path in materialized.taxonomy_package_paths)
        assert materialized.taxonomy_package_paths[0].name == "taxonomy.xsd"
        assert all(
            root.resolve() in path.resolve().parents
            for path in materialized.taxonomy_package_paths
        )
    assert not root.exists()


def test_materializer_cleans_temporary_tree_when_consumer_raises(tmp_path: Path) -> None:
    store, descriptor, taxonomy = stored_valid_fixture(tmp_path)
    root: Path | None = None

    with pytest.raises(RuntimeError, match="consumer failed"):
        with SafePackageMaterializer(store).materialize(
            descriptor, (taxonomy,)
        ) as materialized:
            root = materialized.root
            raise RuntimeError("consumer failed")

    assert root is not None
    assert not root.exists()


def test_materializer_extracts_instance_zip_entrypoint(tmp_path: Path) -> None:
    archive = tmp_path / "instance.zip"
    write_zip(archive, [("reports/instance.xml", b"<x/>"), ("readme.txt", b"safe")])
    store = RawObjectStore(tmp_path / "raw")
    instance_hash = store_payload(
        store,
        "instance.zip",
        "application/zip",
        archive.read_bytes(),
    )
    taxonomy_hash = store_payload(
        store,
        "taxonomy.xsd",
        "application/xml-schema",
        b"<schema/>",
    )

    with SafePackageMaterializer(store).materialize(
        descriptor_for(
            instance_hash,
            attachment_name="instance.zip",
            content_type="application/zip",
            instance_entrypoint="reports/instance.xml",
        ),
        (
            taxonomy_for(
                taxonomy_hash,
                package_name="taxonomy.xsd",
                entrypoint="taxonomy.xsd",
                content_type="application/xml-schema",
            ),
        ),
    ) as materialized:
        assert materialized.entrypoint_path.relative_to(materialized.root).as_posix().endswith(
            "reports/instance.xml"
        )
        assert materialized.entrypoint_path.read_bytes() == b"<x/>"


@pytest.mark.parametrize(
    ("instance_entrypoint", "members"),
    [
        (None, [("instance.xml", b"<x/>")]),
        ("missing.xml", [("instance.xml", b"<x/>")]),
        ("directory/", [("directory/", b""), ("directory/instance.xml", b"<x/>")]),
        ("../instance.xml", [("instance.xml", b"<x/>")]),
    ],
)
def test_instance_zip_requires_valid_file_entrypoint(
    tmp_path: Path,
    instance_entrypoint: str | None,
    members: list[tuple[str, bytes]],
) -> None:
    archive = tmp_path / "instance.zip"
    write_zip(archive, members)
    store = RawObjectStore(tmp_path / "raw")
    instance_hash = store_payload(
        store,
        "instance.zip",
        "application/zip",
        archive.read_bytes(),
    )

    with pytest.raises(ValueError, match="FINANCIAL_ENTRYPOINT_INVALID"):
        with SafePackageMaterializer(store).materialize(
            descriptor_for(
                instance_hash,
                attachment_name="instance.zip",
                content_type="application/zip",
                instance_entrypoint=instance_entrypoint,
            ),
            (),
        ):
            pass


def test_direct_instance_rejects_instance_entrypoint(tmp_path: Path) -> None:
    store = RawObjectStore(tmp_path / "raw")
    instance_hash = store_payload(store, "instance.xml", "application/xml", b"<x/>")

    with pytest.raises(ValueError, match="FINANCIAL_ENTRYPOINT_INVALID"):
        with SafePackageMaterializer(store).materialize(
            descriptor_for(instance_hash, instance_entrypoint="instance.xml"),
            (),
        ):
            pass


def test_materializer_validates_hash_before_copying_payload(tmp_path: Path) -> None:
    store, descriptor, taxonomy = stored_valid_fixture(tmp_path)
    store.payload_path_for_hash(taxonomy.raw_object_hash).write_bytes(b"<tampered/>")

    with pytest.raises(ValueError, match="RAW_PAYLOAD_INTEGRITY_ERROR"):
        with SafePackageMaterializer(store).materialize(descriptor, (taxonomy,)):
            pass
