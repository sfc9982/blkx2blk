from __future__ import annotations

import argparse
import sys
import warnings
from collections import defaultdict
from pathlib import Path

from ._blkx import LegacyTypeWarning, check_type_map, load_json
from ._types import ConversionError
from .converter import InputPaths, _blk_to_blkx, _blkx_to_blk, file_key, path_key, write_output


def _run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Convert between Dagor BLK text and typed BLKX JSON')
    parser.add_argument('input', nargs='+', type=Path, help='one or more .blk or .blkx files')
    parser.add_argument(
        '-o', '--output', type=Path, help='output file (single input) or existing output directory'
    )
    parser.add_argument('--force', action='store_true', help='replace existing outputs, but never any input')
    parser.add_argument('--strict', action='store_true', help='legacy BLKX: reject ambiguous mappings')
    parser.add_argument(
        '--type-map', type=Path, help='legacy BLKX: JSON file mapping field pointers to BLK types'
    )
    args = parser.parse_args(argv)
    protected = tuple(args.input)
    directory = args.output is not None and args.output.is_dir()
    if len(protected) > 1 and args.output is not None and not directory:
        parser.error('-o/--output must be an existing directory when converting multiple files')
    hints = None
    if args.type_map is not None:
        try:
            hints = check_type_map(load_json(args.type_map.read_bytes()))
        except (OSError, ValueError) as exc:
            parser.error(f'invalid type map {str(args.type_map)!r}: {exc}')
        protected += (args.type_map,)

    plans: dict[int, tuple[Path, Path]] = {}
    failures: dict[int, str] = {}
    outputs: dict[object, list[int]] = defaultdict(list)
    for i, src in enumerate(args.input):
        ext = src.suffix.lower()
        if ext not in ('.blk', '.blkx'):
            failures[i] = 'expected a .blk or .blkx input'
            continue
        dst = src.with_suffix('.blkx' if ext == '.blk' else '.blk')
        if args.output is not None:
            dst = args.output / dst.name if directory else args.output
        plans[i] = src, dst
        try:
            outputs[path_key(dst)].append(i)
            identity = file_key(dst)
            if identity is not None:
                outputs[identity].append(i)
        except (OSError, RuntimeError, ValueError) as exc:
            failures[i] = f'cannot resolve output {str(dst)!r}: {exc}'

    for group in outputs.values():
        if len(group) > 1:
            sources = ', '.join(str(args.input[i]) for i in group)
            for i in group:
                failures[i] = f'output collision for {str(plans[i][1])!r}: {sources}'

    input_paths = InputPaths({}, {})
    for i, src in enumerate(protected):
        try:
            input_paths.add(src)
        except (OSError, RuntimeError, ValueError) as exc:
            if i < len(args.input):
                failures.setdefault(i, f'cannot resolve input path: {exc}')
            else:
                parser.error(f'cannot protect type map: {exc}')

    for i, (_, dst) in plans.items():
        if i not in failures:
            try:
                input_paths.check_output(dst, force=args.force)
            except (OSError, RuntimeError, ValueError) as exc:
                failures[i] = str(exc)

    failed = False
    for i, src in enumerate(args.input):
        try:
            if i in failures:
                raise ConversionError(failures[i])
            _, dst = plans[i]
            data = src.read_bytes()
            with warnings.catch_warnings(record=True) as messages:
                warnings.simplefilter('always', LegacyTypeWarning)
                content = (
                    _blk_to_blkx(data)
                    if src.suffix.lower() == '.blk'
                    else _blkx_to_blk(data, strict=args.strict, type_map=hints)
                )
            write_output(dst, content, input_paths, force=args.force)
            for message in messages:
                print(f"Warning converting '{src}': {message.message}", file=sys.stderr)
            print(f"Successfully converted '{src}' to '{dst}'")
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"Error converting '{src}': {exc}", file=sys.stderr)
            failed = True
    return int(failed)


def main() -> None:
    sys.exit(_run())


if __name__ == '__main__':
    main()
