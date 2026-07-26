from __future__ import annotations

import codecs
import os
import re
import shutil
import stat
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import BinaryIO
from zipfile import BadZipFile, LargeZipFile, ZipFile, ZipInfo

from hengce.contracts.financial import FilingDescriptor, TaxonomyPackageRef
from hengce.raw_store import RawObjectStore


@dataclass(frozen=True)
class AttachmentLimits:
    max_files: int = 5_000
    max_file_bytes: int = 64 * 1024 * 1024
    max_total_bytes: int = 512 * 1024 * 1024
    max_compression_ratio: int = 100


@dataclass(frozen=True)
class MaterializedFiling:
    entrypoint_path: Path
    taxonomy_package_paths: tuple[Path, ...]
    root: Path


ALLOWED_INSTANCE_TYPES = {
    ".xml": {"application/xml", "text/xml", "application/xbrl+xml"},
    ".xbrl": {"application/xml", "text/xml", "application/xbrl+xml"},
    ".zip": {"application/zip"},
}
ALLOWED_TAXONOMY_TYPES = {
    **ALLOWED_INSTANCE_TYPES,
    ".xsd": {"application/xml", "text/xml", "application/xml-schema"},
}

_XML_SUFFIXES = frozenset({".xml", ".xbrl", ".xsd"})
_UNSAFE_XML_BYTES = (b"<!doctype", b"<!entity")
_UNSAFE_XML_TEXT = ("<!doctype", "<!entity")
_COPY_CHUNK_BYTES = 1024 * 1024


def _error(code: str) -> ValueError:
    return ValueError(code)


def _normalized_content_type(content_type: str) -> str:
    return content_type.partition(";")[0].strip().lower()


def _has_xml_signature(prefix: bytes) -> bool:
    if prefix.startswith(codecs.BOM_UTF16_LE):
        decoded = prefix[len(codecs.BOM_UTF16_LE) :].decode(
            "utf-16-le",
            errors="ignore",
        )
        return decoded.lstrip().startswith("<")
    if prefix.startswith(codecs.BOM_UTF16_BE):
        decoded = prefix[len(codecs.BOM_UTF16_BE) :].decode(
            "utf-16-be",
            errors="ignore",
        )
        return decoded.lstrip().startswith("<")
    return prefix.lstrip(codecs.BOM_UTF8 + b" \t\r\n").startswith(b"<")


def _reject_unsafe_xml(path: Path) -> None:
    try:
        with path.open("rb") as stream:
            prefix = stream.read(4)
            stream.seek(0)
            if prefix.startswith(codecs.BOM_UTF16_LE):
                _scan_text_xml(stream, "utf-16-le")
            elif prefix.startswith(codecs.BOM_UTF16_BE):
                _scan_text_xml(stream, "utf-16-be")
            else:
                _scan_byte_xml(stream)
    except OSError:
        raise _error("FINANCIAL_ATTACHMENT_TYPE_INVALID") from None


def _scan_byte_xml(stream: BinaryIO) -> None:
    tail = b""
    while chunk := stream.read(_COPY_CHUNK_BYTES):
        lowered = (tail + chunk).lower()
        if any(token in lowered for token in _UNSAFE_XML_BYTES):
            raise _error("FINANCIAL_XML_UNSAFE")
        tail = lowered[-10:]


def _scan_text_xml(stream: BinaryIO, encoding: str) -> None:
    decoder = codecs.getincrementaldecoder(encoding)(errors="ignore")
    tail = ""
    while chunk := stream.read(_COPY_CHUNK_BYTES):
        lowered = (tail + decoder.decode(chunk)).casefold()
        if any(token in lowered for token in _UNSAFE_XML_TEXT):
            raise _error("FINANCIAL_XML_UNSAFE")
        tail = lowered[-10:]


class LocalAttachmentInspector:
    def validate(self, path: Path, content_type: str, *, taxonomy: bool) -> None:
        allowed_types = ALLOWED_TAXONOMY_TYPES if taxonomy else ALLOWED_INSTANCE_TYPES
        suffix = path.suffix.lower()
        if _normalized_content_type(content_type) not in allowed_types.get(suffix, set()):
            raise _error("FINANCIAL_ATTACHMENT_TYPE_INVALID")

        try:
            with path.open("rb") as stream:
                prefix = stream.read(256)
        except OSError:
            raise _error("FINANCIAL_ATTACHMENT_TYPE_INVALID") from None

        if suffix == ".zip":
            if not prefix.startswith(b"PK"):
                raise _error("FINANCIAL_ATTACHMENT_TYPE_INVALID")
            return

        if not _has_xml_signature(prefix):
            raise _error("FINANCIAL_ATTACHMENT_TYPE_INVALID")
        _reject_unsafe_xml(path)


class SafePackageMaterializer:
    def __init__(self, store: RawObjectStore) -> None:
        self._store = store
        self._limits = AttachmentLimits()
        self._inspector = LocalAttachmentInspector()

    @contextmanager
    def materialize(
        self,
        descriptor: FilingDescriptor,
        taxonomies: Sequence[TaxonomyPackageRef],
    ) -> Iterator[MaterializedFiling]:
        instance_source = self._store.validate_content_hash(descriptor.raw_object_hash)
        taxonomy_sources = tuple(
            self._store.validate_content_hash(taxonomy.raw_object_hash)
            for taxonomy in taxonomies
        )

        with TemporaryDirectory(prefix="hengce-xbrl-") as temporary_name:
            root = Path(temporary_name).resolve()
            entrypoint_path = self._materialize_instance(root, instance_source, descriptor)
            taxonomy_paths = tuple(
                self._materialize_taxonomy(root, source, taxonomy, index)
                for index, (source, taxonomy) in enumerate(
                    zip(taxonomy_sources, taxonomies, strict=True)
                )
            )
            yield MaterializedFiling(
                entrypoint_path=entrypoint_path,
                taxonomy_package_paths=taxonomy_paths,
                root=root,
            )

    def _materialize_instance(
        self,
        root: Path,
        source: Path,
        descriptor: FilingDescriptor,
    ) -> Path:
        declared_name = _sanitized_declared_name(descriptor.attachment_name)
        suffix = Path(declared_name).suffix.lower()
        if suffix != ".zip" and descriptor.instance_entrypoint is not None:
            raise _error("FINANCIAL_ENTRYPOINT_INVALID")

        attachment = self._copy_attachment(
            root,
            source,
            Path("attachments") / "instance" / declared_name,
        )
        self._inspector.validate(attachment, descriptor.content_type, taxonomy=False)
        if suffix != ".zip":
            return attachment
        if descriptor.instance_entrypoint is None:
            raise _error("FINANCIAL_ENTRYPOINT_INVALID")

        extracted = root / "instance"
        self._extract_zip(attachment, extracted, root)
        return _resolve_entrypoint(
            extracted,
            descriptor.instance_entrypoint,
            root,
        )

    def _materialize_taxonomy(
        self,
        root: Path,
        source: Path,
        taxonomy: TaxonomyPackageRef,
        index: int,
    ) -> Path:
        declared_name = _sanitized_declared_name(taxonomy.package_name)
        attachment = self._copy_attachment(
            root,
            source,
            Path("attachments") / f"taxonomy-{index}" / declared_name,
        )
        self._inspector.validate(attachment, taxonomy.content_type, taxonomy=True)
        if attachment.suffix.lower() != ".zip":
            return attachment

        extracted = root / f"taxonomy-{index}"
        self._extract_zip(attachment, extracted, root)
        return _resolve_entrypoint(extracted, taxonomy.entrypoint, root)

    @staticmethod
    def _copy_attachment(root: Path, source: Path, relative_target: Path) -> Path:
        target = _contained_target(root, root / relative_target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        return target

    def _extract_zip(self, archive: Path, destination: Path, root: Path) -> None:
        try:
            with ZipFile(archive) as handle:
                members = handle.infolist()
                planned = self._validate_central_directory(members, destination, root)
                destination.mkdir(parents=True, exist_ok=True)
                self._stream_members(handle, planned)
        except ValueError:
            raise
        except (BadZipFile, LargeZipFile, OSError, RuntimeError, EOFError):
            raise _error("FINANCIAL_ARCHIVE_INVALID") from None

    def _validate_central_directory(
        self,
        members: list[ZipInfo],
        destination: Path,
        root: Path,
    ) -> list[tuple[ZipInfo, Path]]:
        if len(members) > self._limits.max_files:
            raise _error("FINANCIAL_ARCHIVE_LIMIT_EXCEEDED")

        declared_total = 0
        seen_targets: set[str] = set()
        planned: list[tuple[ZipInfo, Path]] = []
        for member in members:
            raw_name = member.orig_filename
            relative = _safe_archive_member(raw_name)
            if _is_symlink(member):
                raise _error("FINANCIAL_ARCHIVE_UNSAFE_PATH")

            target = _contained_target(root, destination.joinpath(*relative.parts))
            target_key = os.path.normcase(str(target))
            if target_key in seen_targets:
                raise _error("FINANCIAL_ARCHIVE_UNSAFE_PATH")
            seen_targets.add(target_key)

            if member.file_size > self._limits.max_file_bytes:
                raise _error("FINANCIAL_ARCHIVE_LIMIT_EXCEEDED")
            declared_total += member.file_size
            if declared_total > self._limits.max_total_bytes:
                raise _error("FINANCIAL_ARCHIVE_LIMIT_EXCEEDED")
            if member.file_size and (
                member.compress_size == 0
                or member.file_size
                > member.compress_size * self._limits.max_compression_ratio
            ):
                raise _error("FINANCIAL_ARCHIVE_LIMIT_EXCEEDED")
            planned.append((member, target))
        return planned

    def _stream_members(
        self,
        archive: ZipFile,
        planned: list[tuple[ZipInfo, Path]],
    ) -> None:
        actual_total = 0
        for member, target in planned:
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            actual_member = 0
            with archive.open(member, "r") as source, target.open("xb") as output:
                while chunk := source.read(_COPY_CHUNK_BYTES):
                    actual_member += len(chunk)
                    actual_total += len(chunk)
                    if (
                        actual_member > self._limits.max_file_bytes
                        or actual_total > self._limits.max_total_bytes
                    ):
                        raise _error("FINANCIAL_ARCHIVE_LIMIT_EXCEEDED")
                    output.write(chunk)
            if actual_member != member.file_size:
                raise _error("FINANCIAL_ARCHIVE_INVALID")
            if target.suffix.lower() in _XML_SUFFIXES:
                _reject_unsafe_xml(target)


def _sanitized_declared_name(name: str) -> str:
    leaf = PurePosixPath(name.replace("\\", "/")).name
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", leaf)
    if sanitized in {"", ".", ".."}:
        sanitized = "attachment"
    return sanitized


def _safe_archive_member(raw_name: str) -> PurePosixPath:
    if (
        "\x00" in raw_name
        or "\\" in raw_name
        or raw_name.startswith("/")
        or raw_name.startswith("//")
        or re.match(r"^[A-Za-z]:", raw_name)
    ):
        raise _error("FINANCIAL_ARCHIVE_UNSAFE_PATH")
    path = PurePosixPath(raw_name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise _error("FINANCIAL_ARCHIVE_UNSAFE_PATH")
    return path


def _contained_target(root: Path, target: Path) -> Path:
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    if resolved_target != resolved_root and resolved_root not in resolved_target.parents:
        raise _error("FINANCIAL_ARCHIVE_UNSAFE_PATH")
    return resolved_target


def _is_symlink(member: ZipInfo) -> bool:
    return member.create_system == 3 and stat.S_ISLNK(member.external_attr >> 16)


def _resolve_entrypoint(directory: Path, declared: str, root: Path) -> Path:
    try:
        relative = _safe_entrypoint(declared)
        entrypoint = _contained_target(root, directory.joinpath(*relative.parts))
    except ValueError:
        raise _error("FINANCIAL_ENTRYPOINT_INVALID") from None
    if not entrypoint.is_file():
        raise _error("FINANCIAL_ENTRYPOINT_INVALID")
    return entrypoint


def _safe_entrypoint(declared: str) -> PurePosixPath:
    if (
        not declared
        or "\x00" in declared
        or "\\" in declared
        or declared.startswith("/")
        or declared.startswith("//")
        or re.match(r"^[A-Za-z]:", declared)
    ):
        raise _error("FINANCIAL_ENTRYPOINT_INVALID")
    path = PurePosixPath(declared)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise _error("FINANCIAL_ENTRYPOINT_INVALID")
    return path
