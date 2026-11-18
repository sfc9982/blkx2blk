"""File conversions and non-destructive output handling."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ._blk import parse_blk, render_blk
from ._blkx import decode_legacy, decode_typed, encode, load_json
from ._types import ConversionError

PathLike = str | os.PathLike[str]


def path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def file_key(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return (info.st_dev, info.st_ino) if info.st_ino else None


@dataclass
class InputPaths:
    """One batch-wide index of protected path names and hard-link identities."""

    names: dict[str, Path]
    identities: dict[tuple[int, int], Path]

    def add(self, src: Path) -> None:
        self.names[path_key(src)] = src
        if (identity := file_key(src)) is not None:
            self.identities[identity] = src

    @classmethod
    def from_paths(cls, sources: Iterable[Path]) -> InputPaths:
        result = cls({}, {})
        for src in sources:
            result.add(src)
        return result

    def check_output(self, dst: Path, *, force: bool) -> None:
        if dst.is_symlink():
            raise ConversionError(f'output is a symbolic link: {dst}')
        name, identity = path_key(dst), file_key(dst)
        conflict = self.names.get(name) or self.identities.get(identity)
        if conflict is not None:
            raise ConversionError(f'output refers to an input file: {conflict}')
        if dst.exists():
            if not dst.is_file():
                raise ConversionError(f'output is not a regular file: {dst}')
            if not force:
                raise FileExistsError(f'output exists (use --force to replace it): {dst}')


def write_output(dst: Path, content: bytes, protected: InputPaths, *, force: bool) -> None:
    protected.check_output(dst, force=force)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=dst.parent, prefix='.blkx2blk-', suffix='.tmp', delete=False
        ) as stream:
            tmp = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        protected.check_output(dst, force=force)
        if force:
            os.replace(tmp, dst)
        else:
            # A same-directory hard link publishes complete bytes without replacing a
            # destination created after validation. Fail safely if unsupported.
            os.link(tmp, dst)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _blk_to_blkx(data: bytes) -> bytes:
    return encode(parse_blk(data), data)


def _blkx_to_blk(data: bytes, *, strict: bool, type_map: Mapping[str, str] | None) -> bytes:
    document = load_json(data)
    if isinstance(document, dict) and '$format' in document:
        entries, original = decode_typed(document)
        if original is not None:
            return original
    else:
        entries = decode_legacy(document, strict=strict, type_map=type_map)
    return render_blk(entries)


def blk_to_blkx(src: PathLike, dst: PathLike, *, force: bool = False) -> None:
    """Export UTF-8 BLK text to typed BLKX, including its original bytes."""
    source, target = Path(src), Path(dst)
    write_output(target, _blk_to_blkx(source.read_bytes()), InputPaths.from_paths((source,)), force=force)


def blkx_to_blk(
    blkx_file: PathLike,
    blk_file: PathLike,
    *,
    force: bool = False,
    strict: bool = False,
    type_map: Mapping[str, str] | None = None,
) -> None:
    """Restore typed BLKX, or import an untyped JSON export to BLK text."""
    source, target = Path(blkx_file), Path(blk_file)
    result = _blkx_to_blk(source.read_bytes(), strict=strict, type_map=type_map)
    write_output(target, result, InputPaths.from_paths((source,)), force=force)
