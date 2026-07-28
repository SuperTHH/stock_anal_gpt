from __future__ import annotations

import codecs
import ctypes
import os
import re
import stat
import struct
import zlib
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import BinaryIO
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

if os.name == "nt":
    import msvcrt
    from ctypes import wintypes

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


@dataclass(frozen=True)
class _MaterializedResource:
    resource_root: Path
    entrypoint_path: Path
    files: tuple[Path, ...]


ALLOWED_INSTANCE_TYPES = {
    ".xml": {"application/xml", "text/xml", "application/xbrl+xml"},
    ".xbrl": {"application/xml", "text/xml", "application/xbrl+xml"},
    ".zip": {"application/zip"},
}
ALLOWED_TAXONOMY_TYPES = {
    **ALLOWED_INSTANCE_TYPES,
    ".xsd": {"application/xml", "text/xml", "application/xml-schema"},
}

_UNSAFE_XML_BYTES = (b"<!doctype", b"<!entity")
_UNSAFE_XML_TEXT = ("<!doctype", "<!entity")
_COPY_CHUNK_BYTES = 1024 * 1024
_LOCAL_HEADER = struct.Struct("<4s5H3I2H")
_DATA_DESCRIPTOR = struct.Struct("<III")
_LOCAL_SIGNATURE = b"PK\x03\x04"
_DESCRIPTOR_SIGNATURE = b"PK\x07\x08"
_DATA_DESCRIPTOR_FLAG = 0x08
_WINDOWS_RESERVED_STEMS = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
_WINDOWS_INVALID_CHARACTERS = frozenset('<>:"|?*')

if os.name == "nt":
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _CREATE_NEW = 1
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("reparse_tag", wintypes.DWORD),
        ]

    _CREATE_FILE = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    _CREATE_FILE.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _CREATE_FILE.restype = wintypes.HANDLE
    _GET_FILE_INFORMATION = ctypes.WinDLL(
        "kernel32", use_last_error=True
    ).GetFileInformationByHandleEx
    _GET_FILE_INFORMATION.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _GET_FILE_INFORMATION.restype = wintypes.BOOL
    _CLOSE_HANDLE = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    _CLOSE_HANDLE.argtypes = [wintypes.HANDLE]
    _CLOSE_HANDLE.restype = wintypes.BOOL


class _PackageError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _error(code: str) -> _PackageError:
    return _PackageError(code)


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
            _reject_unsafe_xml_stream(stream)
    except OSError:
        raise _error("FINANCIAL_ATTACHMENT_TYPE_INVALID") from None


def _reject_unsafe_xml_stream(stream: BinaryIO) -> None:
    stream.seek(0)
    prefix = stream.read(4)
    stream.seek(0)
    if prefix.startswith(codecs.BOM_UTF16_LE):
        _scan_text_xml(stream, "utf-16-le")
    elif prefix.startswith(codecs.BOM_UTF16_BE):
        _scan_text_xml(stream, "utf-16-be")
    else:
        _scan_byte_xml(stream)


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


@contextmanager
def _safe_output_file(root: Path, target: Path) -> Iterator[BinaryIO]:
    if os.name == "nt":
        with _locked_windows_directory_chain(root, target.parent):
            handle = _open_windows_path(target, directory=False, create=True)
            try:
                descriptor = msvcrt.open_osfhandle(handle, os.O_BINARY)
            except BaseException:
                _CLOSE_HANDLE(handle)
                raise
            with os.fdopen(descriptor, "w+b") as output:
                yield output
        return

    with _locked_posix_directory_chain(root, target.parent) as parent_descriptor:
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        descriptor = os.open(target.name, flags, 0o600, dir_fd=parent_descriptor)
        with os.fdopen(descriptor, "w+b") as output:
            yield output


@contextmanager
def _safe_input_file(root: Path, target: Path) -> Iterator[BinaryIO]:
    if os.name == "nt":
        with _locked_windows_directory_chain(root, target.parent):
            handle = _open_windows_path(target, directory=False, create=False)
            try:
                descriptor = msvcrt.open_osfhandle(
                    handle,
                    os.O_RDONLY | os.O_BINARY,
                )
            except BaseException:
                _CLOSE_HANDLE(handle)
                raise
            with os.fdopen(descriptor, "rb") as input_stream:
                yield input_stream
        return

    with _locked_posix_directory_chain(root, target.parent) as parent_descriptor:
        descriptor = os.open(
            target.name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
        with os.fdopen(descriptor, "rb") as input_stream:
            yield input_stream


def _ensure_safe_directory(root: Path, target: Path) -> None:
    if os.name == "nt":
        with _locked_windows_directory_chain(root, target):
            return
    with _locked_posix_directory_chain(root, target):
        return


@contextmanager
def _locked_windows_directory_chain(root: Path, target: Path) -> Iterator[None]:
    relative = target.relative_to(root)
    handles: list[int] = []
    current = root
    try:
        handles.append(_open_windows_path(current, directory=True, create=False))
        for component in relative.parts:
            current = current / component
            try:
                current.mkdir()
            except FileExistsError:
                pass
            handles.append(_open_windows_path(current, directory=True, create=False))
        yield
    except OSError:
        raise _error("FINANCIAL_ARCHIVE_UNSAFE_PATH") from None
    finally:
        for handle in reversed(handles):
            _CLOSE_HANDLE(handle)


def _open_windows_path(
    path: Path,
    *,
    directory: bool,
    create: bool,
    deny_write: bool = False,
) -> int:
    flags = _FILE_FLAG_OPEN_REPARSE_POINT
    if directory:
        flags |= _FILE_FLAG_BACKUP_SEMANTICS
    share_mode = _FILE_SHARE_READ
    if not directory and not deny_write:
        share_mode |= _FILE_SHARE_WRITE
    handle = _CREATE_FILE(
        str(path),
        _GENERIC_READ | (_GENERIC_WRITE if create else 0),
        share_mode,
        None,
        _CREATE_NEW if create else _OPEN_EXISTING,
        flags,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())

    information = _FileAttributeTagInfo()
    if not _GET_FILE_INFORMATION(
        handle,
        _FILE_ATTRIBUTE_TAG_INFO_CLASS,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        error = ctypes.WinError(ctypes.get_last_error())
        _CLOSE_HANDLE(handle)
        raise error
    is_directory = bool(information.file_attributes & _FILE_ATTRIBUTE_DIRECTORY)
    is_reparse_point = bool(information.file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT)
    if is_reparse_point or is_directory != directory:
        _CLOSE_HANDLE(handle)
        raise OSError("unsafe filesystem object")
    return handle


@contextmanager
def _hold_materialized_tree_for_parser(
    materialized: MaterializedFiling,
) -> Iterator[None]:
    """Keep parser path inputs stable for the duration of a path-based reopen."""
    root = materialized.root.absolute()
    required_paths = (
        materialized.entrypoint_path.absolute(),
        *(path.absolute() for path in materialized.taxonomy_package_paths),
    )
    try:
        for path in required_paths:
            path.relative_to(root)
    except ValueError:
        raise _error("FINANCIAL_ARCHIVE_UNSAFE_PATH") from None

    if os.name != "nt":
        with ExitStack() as stack:
            for path in required_paths:
                stack.enter_context(_safe_input_file(root, path))
            yield
        return

    handles: list[int] = []
    try:
        handles.append(
            _open_windows_path(
                root,
                directory=True,
                create=False,
                deny_write=True,
            )
        )
        _hold_windows_tree(root, handles)
        yield
    except OSError:
        raise _error("FINANCIAL_ARCHIVE_UNSAFE_PATH") from None
    finally:
        for handle in reversed(handles):
            _CLOSE_HANDLE(handle)


def _hold_windows_tree(directory: Path, handles: list[int]) -> None:
    with os.scandir(directory) as entries:
        children = sorted(entries, key=lambda entry: entry.name)
    for child in children:
        child_path = Path(child.path)
        is_directory = child.is_dir(follow_symlinks=False)
        handles.append(
            _open_windows_path(
                child_path,
                directory=is_directory,
                create=False,
                deny_write=True,
            )
        )
        if is_directory:
            _hold_windows_tree(child_path, handles)


@contextmanager
def _locked_posix_directory_chain(root: Path, target: Path) -> Iterator[int]:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors = [os.open(root, flags)]
    try:
        for component in target.relative_to(root).parts:
            try:
                os.mkdir(component, mode=0o700, dir_fd=descriptors[-1])
            except FileExistsError:
                pass
            descriptors.append(os.open(component, flags, dir_fd=descriptors[-1]))
        yield descriptors[-1]
    except OSError:
        raise _error("FINANCIAL_ARCHIVE_UNSAFE_PATH") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


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
            self._store.validate_content_hash(taxonomy.raw_object_hash) for taxonomy in taxonomies
        )

        with TemporaryDirectory(prefix="hengce-xbrl-") as temporary_name:
            root = Path(temporary_name).resolve()
            instance = self._materialize_instance(root, instance_source, descriptor)
            taxonomy_resources = tuple(
                self._materialize_taxonomy(root, source, taxonomy, index)
                for index, (source, taxonomy) in enumerate(
                    zip(taxonomy_sources, taxonomies, strict=True)
                )
            )
            yield self._compose_parser_space(root, instance, taxonomy_resources)

    def _materialize_instance(
        self,
        root: Path,
        source: Path,
        descriptor: FilingDescriptor,
    ) -> _MaterializedResource:
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
            return _MaterializedResource(
                resource_root=attachment.parent,
                entrypoint_path=attachment,
                files=(attachment,),
            )
        if descriptor.instance_entrypoint is None:
            raise _error("FINANCIAL_ENTRYPOINT_INVALID")

        extracted = root / "instance"
        files = self._extract_zip(attachment, extracted, root)
        entrypoint = _resolve_entrypoint(
            extracted,
            descriptor.instance_entrypoint,
            root,
        )
        return _MaterializedResource(
            resource_root=extracted,
            entrypoint_path=entrypoint,
            files=files,
        )

    def _materialize_taxonomy(
        self,
        root: Path,
        source: Path,
        taxonomy: TaxonomyPackageRef,
        index: int,
    ) -> _MaterializedResource:
        declared_name = _sanitized_declared_name(taxonomy.package_name)
        attachment = self._copy_attachment(
            root,
            source,
            Path("attachments") / f"taxonomy-{index}" / declared_name,
        )
        self._inspector.validate(attachment, taxonomy.content_type, taxonomy=True)
        if attachment.suffix.lower() != ".zip":
            try:
                direct_entrypoint = _safe_entrypoint(taxonomy.entrypoint)
                if direct_entrypoint != PurePosixPath(declared_name):
                    raise _error("FINANCIAL_ENTRYPOINT_INVALID")
                with _safe_input_file(root, attachment):
                    pass
            except (OSError, _PackageError):
                raise _error("FINANCIAL_ENTRYPOINT_INVALID") from None
            return _MaterializedResource(
                resource_root=attachment.parent,
                entrypoint_path=attachment,
                files=(attachment,),
            )

        extracted = root / f"taxonomy-{index}"
        files = self._extract_zip(attachment, extracted, root)
        entrypoint = _resolve_entrypoint(extracted, taxonomy.entrypoint, root)
        return _MaterializedResource(
            resource_root=extracted,
            entrypoint_path=entrypoint,
            files=files,
        )

    def _compose_parser_space(
        self,
        root: Path,
        instance: _MaterializedResource,
        taxonomies: tuple[_MaterializedResource, ...],
    ) -> MaterializedFiling:
        parser_root = _contained_target(root, root / "parser")
        instance_entrypoint_relative = instance.entrypoint_path.relative_to(instance.resource_root)
        taxonomy_base = instance_entrypoint_relative.parent
        planned: list[tuple[Path, Path]] = [
            (
                source,
                source.relative_to(instance.resource_root),
            )
            for source in instance.files
        ]
        for taxonomy in taxonomies:
            planned.extend(
                (
                    source,
                    taxonomy_base / source.relative_to(taxonomy.resource_root),
                )
                for source in taxonomy.files
            )

        targets_by_source = self._copy_parser_overlay(root, parser_root, planned)
        return MaterializedFiling(
            entrypoint_path=targets_by_source[instance.entrypoint_path],
            taxonomy_package_paths=tuple(
                targets_by_source[taxonomy.entrypoint_path] for taxonomy in taxonomies
            ),
            root=root,
        )

    @staticmethod
    def _copy_parser_overlay(
        root: Path,
        parser_root: Path,
        planned: list[tuple[Path, Path]],
    ) -> dict[Path, Path]:
        landing_keys: list[tuple[str, ...]] = []
        targets_by_source: dict[Path, Path] = {}
        for source, relative in planned:
            landing_key = _windows_landing_key(PurePosixPath(*relative.parts))
            if any(_landing_keys_conflict(landing_key, existing) for existing in landing_keys):
                raise _error("FINANCIAL_OVERLAY_CONFLICT")
            landing_keys.append(landing_key)
            target = _contained_target(
                root,
                parser_root.joinpath(*relative.parts),
            )
            try:
                with (
                    _safe_input_file(root, source) as input_stream,
                    _safe_output_file(
                        root,
                        target,
                    ) as output,
                ):
                    while chunk := input_stream.read(_COPY_CHUNK_BYTES):
                        output.write(chunk)
            except OSError:
                raise _error("FINANCIAL_ARCHIVE_UNSAFE_PATH") from None
            targets_by_source[source] = target
        return targets_by_source

    @staticmethod
    def _copy_attachment(root: Path, source: Path, relative_target: Path) -> Path:
        target = _contained_target(root, root / relative_target)
        with (
            source.open("rb") as input_stream,
            _safe_output_file(
                root,
                target,
            ) as output,
        ):
            while chunk := input_stream.read(_COPY_CHUNK_BYTES):
                output.write(chunk)
        return target

    def _extract_zip(
        self,
        archive: Path,
        destination: Path,
        root: Path,
    ) -> tuple[Path, ...]:
        try:
            with ZipFile(archive) as handle:
                members = handle.infolist()
                planned = self._validate_central_directory(members, destination, root)
                _validate_raw_records(archive, handle, members)
                _ensure_safe_directory(root, destination)
                self._stream_members(handle, planned, root)
                return tuple(target for member, target in planned if not member.is_dir())
        except _PackageError:
            raise
        except Exception:
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
        seen_targets: set[tuple[str, ...]] = set()
        planned: list[tuple[ZipInfo, Path]] = []
        for member in members:
            raw_name = member.orig_filename
            relative = _safe_archive_member(raw_name)
            if _is_symlink(member):
                raise _error("FINANCIAL_ARCHIVE_UNSAFE_PATH")

            target = _contained_target(root, destination.joinpath(*relative.parts))
            target_key = _windows_landing_key(relative)
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
                or member.file_size > member.compress_size * self._limits.max_compression_ratio
            ):
                raise _error("FINANCIAL_ARCHIVE_LIMIT_EXCEEDED")
            planned.append((member, target))
        return planned

    def _stream_members(
        self,
        archive: ZipFile,
        planned: list[tuple[ZipInfo, Path]],
        root: Path,
    ) -> None:
        actual_total = 0
        for member, target in planned:
            if member.is_dir():
                _ensure_safe_directory(root, target)
                continue

            actual_member = 0
            with (
                archive.open(member, "r") as source,
                _safe_output_file(
                    root,
                    target,
                ) as output,
            ):
                while chunk := source.read(_COPY_CHUNK_BYTES):
                    actual_member += len(chunk)
                    actual_total += len(chunk)
                    if (
                        actual_member > self._limits.max_file_bytes
                        or actual_total > self._limits.max_total_bytes
                    ):
                        raise _error("FINANCIAL_ARCHIVE_LIMIT_EXCEEDED")
                    output.write(chunk)
                output.flush()
                output.seek(0)
                prefix = output.read(256)
                if _has_xml_signature(prefix):
                    _reject_unsafe_xml_stream(output)
            if actual_member != member.file_size:
                raise _error("FINANCIAL_ARCHIVE_INVALID")


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
    if (
        path.is_absolute()
        or ".." in path.parts
        or not path.parts
        or _has_unsafe_windows_component(path.parts)
    ):
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


def _validate_raw_records(
    archive_path: Path,
    archive: ZipFile,
    members: list[ZipInfo],
) -> None:
    ordered = sorted(members, key=lambda member: member.header_offset)
    next_offsets = [
        *(member.header_offset for member in ordered[1:]),
        archive.start_dir,
    ]
    try:
        with archive_path.open("rb") as raw:
            for member, next_offset in zip(ordered, next_offsets, strict=True):
                raw.seek(member.header_offset)
                header = raw.read(_LOCAL_HEADER.size)
                if len(header) != _LOCAL_HEADER.size:
                    raise _error("FINANCIAL_ARCHIVE_INVALID")
                (
                    signature,
                    _version,
                    flags,
                    compression,
                    _modified_time,
                    _modified_date,
                    crc,
                    compressed_size,
                    file_size,
                    filename_size,
                    extra_size,
                ) = _LOCAL_HEADER.unpack(header)
                if (
                    signature != _LOCAL_SIGNATURE
                    or flags != member.flag_bits
                    or compression != member.compress_type
                    or compressed_size == 0xFFFFFFFF
                    or file_size == 0xFFFFFFFF
                ):
                    raise _error("FINANCIAL_ARCHIVE_INVALID")

                raw_filename = raw.read(filename_size)
                encoding = "utf-8" if flags & 0x800 else "cp437"
                if raw_filename.decode(encoding) != member.orig_filename:
                    raise _error("FINANCIAL_ARCHIVE_INVALID")
                if len(raw.read(extra_size)) != extra_size:
                    raise _error("FINANCIAL_ARCHIVE_INVALID")
                data_offset = raw.tell()
                _validate_compressed_stream(
                    raw,
                    data_offset,
                    member,
                    AttachmentLimits(),
                )

                if flags & _DATA_DESCRIPTOR_FLAG:
                    if crc not in {0, member.CRC}:
                        raise _error("FINANCIAL_ARCHIVE_INVALID")
                    if compressed_size not in {0, member.compress_size}:
                        raise _error("FINANCIAL_ARCHIVE_INVALID")
                    if file_size not in {0, member.file_size}:
                        raise _error("FINANCIAL_ARCHIVE_INVALID")
                    raw.seek(data_offset + member.compress_size)
                    descriptor = raw.read(16)
                    if descriptor.startswith(_DESCRIPTOR_SIGNATURE):
                        descriptor = descriptor[4:]
                        descriptor_size = 16
                    else:
                        descriptor = descriptor[:12]
                        descriptor_size = 12
                    if len(descriptor) != _DATA_DESCRIPTOR.size:
                        raise _error("FINANCIAL_ARCHIVE_INVALID")
                    if _DATA_DESCRIPTOR.unpack(descriptor) != (
                        member.CRC,
                        member.compress_size,
                        member.file_size,
                    ):
                        raise _error("FINANCIAL_ARCHIVE_INVALID")
                    record_end = raw.tell() - (16 - descriptor_size)
                else:
                    if (crc, compressed_size, file_size) != (
                        member.CRC,
                        member.compress_size,
                        member.file_size,
                    ):
                        raise _error("FINANCIAL_ARCHIVE_INVALID")
                    record_end = data_offset + member.compress_size

                if record_end != next_offset:
                    raise _error("FINANCIAL_ARCHIVE_INVALID")
    except (OSError, UnicodeError, struct.error):
        raise _error("FINANCIAL_ARCHIVE_INVALID") from None


def _validate_compressed_stream(
    raw: BinaryIO,
    data_offset: int,
    member: ZipInfo,
    limits: AttachmentLimits,
) -> int:
    if member.compress_type == ZIP_STORED:
        if member.compress_size != member.file_size:
            raise _error("FINANCIAL_ARCHIVE_INVALID")
        return member.compress_size
    if member.compress_type != ZIP_DEFLATED:
        raise _error("FINANCIAL_ARCHIVE_INVALID")

    raw.seek(data_offset)
    decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
    remaining = member.compress_size
    pending = b""
    consumed = 0
    expanded = 0
    checksum = 0
    while remaining or pending:
        if not pending:
            pending = raw.read(min(_COPY_CHUNK_BYTES, remaining))
            if not pending:
                raise _error("FINANCIAL_ARCHIVE_INVALID")
            remaining -= len(pending)

        before = len(pending)
        output = decompressor.decompress(pending, _COPY_CHUNK_BYTES)
        consumed_now = before - len(decompressor.unconsumed_tail) - len(decompressor.unused_data)
        consumed += consumed_now
        pending = decompressor.unconsumed_tail
        expanded += len(output)
        checksum = zlib.crc32(output, checksum)
        if expanded > limits.max_file_bytes:
            raise _error("FINANCIAL_ARCHIVE_LIMIT_EXCEEDED")
        if decompressor.unused_data or (decompressor.eof and (remaining or pending)):
            raise _error("FINANCIAL_ARCHIVE_INVALID")
        if decompressor.eof:
            break
        if consumed_now == 0 and not output:
            raise _error("FINANCIAL_ARCHIVE_INVALID")

    if (
        not decompressor.eof
        or consumed != member.compress_size
        or expanded != member.file_size
        or checksum != member.CRC
    ):
        raise _error("FINANCIAL_ARCHIVE_INVALID")
    if expanded and (consumed == 0 or expanded > consumed * limits.max_compression_ratio):
        raise _error("FINANCIAL_ARCHIVE_LIMIT_EXCEEDED")
    return consumed


def _resolve_entrypoint(directory: Path, declared: str, root: Path) -> Path:
    try:
        relative = _safe_entrypoint(declared)
        entrypoint = _contained_target(root, directory.joinpath(*relative.parts))
        with _safe_input_file(root, entrypoint) as input_stream:
            _reject_unsafe_xml_stream(input_stream)
    except _PackageError as error:
        if error.code == "FINANCIAL_XML_UNSAFE":
            raise
        raise _error("FINANCIAL_ENTRYPOINT_INVALID") from None
    except OSError:
        raise _error("FINANCIAL_ENTRYPOINT_INVALID") from None
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
    if (
        path.is_absolute()
        or ".." in path.parts
        or not path.parts
        or _has_unsafe_windows_component(path.parts)
    ):
        raise _error("FINANCIAL_ENTRYPOINT_INVALID")
    return path


def _has_unsafe_windows_component(parts: tuple[str, ...]) -> bool:
    for component in parts:
        stem = component.partition(".")[0].upper()
        if (
            component.endswith((" ", "."))
            or any(character in _WINDOWS_INVALID_CHARACTERS for character in component)
            or stem in _WINDOWS_RESERVED_STEMS
        ):
            return True
    return False


def _windows_landing_key(path: PurePosixPath) -> tuple[str, ...]:
    return tuple(component.casefold() for component in path.parts)


def _landing_keys_conflict(
    first: tuple[str, ...],
    second: tuple[str, ...],
) -> bool:
    shared_length = min(len(first), len(second))
    return first[:shared_length] == second[:shared_length]
