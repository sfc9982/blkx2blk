"""Versioned BLKX and the import adapter for untyped JSON exports."""

from __future__ import annotations

import base64
import binascii
import json
import re
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ._blk import MAX_DEPTH, Entry, check_comment, parse_blk, track_type
from ._types import (
    TYPES,
    ConversionError,
    ValueTypeError,
    check_atom,
    check_value,
    import_integral,
    map_value,
    real_accepts_exact_integer,
    text_value,
)

FORMAT = 'blkx2blk/1'


class LegacyTypeWarning(UserWarning):
    """The old JSON format did not specify a BLK type."""


@dataclass(frozen=True)
class _Number:
    token: str
    decimal: bool


class _Pairs(list):
    pass


def at(path: str, key: str | int) -> str:
    return path + '/' + str(key).replace('~', '~0').replace('/', '~1')


def _error(path: str, message: str) -> ConversionError:
    return ConversionError(f'{path or "<root>"}: {message}')


def load_json(data: bytes) -> object:
    def reject(token: str) -> _Number:
        return _Number(token, True)  # Diagnose at its path after decoding.

    def unpack(value: object, path: str = '') -> object:
        if isinstance(value, _Pairs):
            obj: dict[str, object] = {}
            for key, item in value:
                item_path = at(path, key)
                if key in obj:
                    raise _error(item_path, 'duplicate JSON key')
                obj[key] = unpack(item, item_path)
            return obj
        if isinstance(value, list):
            items = []
            for index, item in enumerate(value):
                items.append(unpack(item, at(path, index)))
            return items
        if isinstance(value, _Number) and value.token in ('NaN', 'Infinity', '-Infinity'):
            raise _error(path, 'non-finite JSON number')
        return value

    try:
        doc = json.loads(
            data.decode('utf-8-sig'),
            object_pairs_hook=_Pairs,
            parse_int=lambda s: _Number(s, False),
            parse_float=lambda s: _Number(s, True),
            parse_constant=reject,
        )
        return unpack(doc)
    except UnicodeDecodeError as exc:
        raise ConversionError(f'expected UTF-8 JSON (invalid byte at offset {exc.start})') from exc
    except RecursionError as exc:
        raise ConversionError('JSON nesting is too deep') from exc


def _check(path: str, fn: Callable, *args: object):
    try:
        return fn(*args)
    except ConversionError as exc:
        if isinstance(exc, ValueTypeError):
            for i in exc.indices:
                path = at(path, i)
        raise _error(path, str(exc)) from exc


def encode(entries: list[Entry], source: bytes) -> bytes:
    document = {'$format': FORMAT, 'entries': entries, 'source_b64': base64.b64encode(source).decode('ascii')}
    return (json.dumps(document, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


_FIELDS = {
    'param': {'kind', 'name', 'type', 'value'},
    'array': {'kind', 'name', 'type', 'values'},
    'block': {'kind', 'name', 'entries'},
    'comment': {'kind', 'style', 'text'},
    'include': {'kind', 'path'},
}


def _entries(entries: object, path: str, depth: int = 0) -> list[Entry]:
    if not isinstance(entries, list):
        raise _error(path, 'expected a list of entries')
    result = []
    types: dict[str, str] = {}
    for index, entry in enumerate(entries):
        here = at(path, index)
        if not isinstance(entry, dict):
            raise _error(here, 'expected an entry object')
        kind = entry.get('kind')
        if not isinstance(kind, str) or kind not in _FIELDS:
            raise _error(at(here, 'kind'), 'unknown entry kind')
        if entry.keys() != _FIELDS[kind]:
            diff = ', '.join(sorted(entry.keys() ^ _FIELDS[kind]))
            raise _error(here, f'missing or unknown entry fields: {diff}')
        checked = dict(entry)
        if kind == 'comment':
            _check(at(here, 'text'), check_comment, entry['style'], entry['text'])
            checked['text'] = entry['text'].replace('\r\n', '\n').replace('\r', '\n')
        elif kind == 'include':
            if not _check(at(here, 'path'), text_value, entry['path']):
                raise _error(at(here, 'path'), 'expected a non-empty include path')
        else:
            name = _check(at(here, 'name'), text_value, entry['name'])
            if kind == 'block':
                if depth >= MAX_DEPTH:
                    raise _error(here, f'block nesting exceeds {MAX_DEPTH}')
                checked['entries'] = _entries(entry['entries'], at(here, 'entries'), depth + 1)
            else:
                tag = entry['type']
                if not isinstance(tag, str) or tag not in TYPES:
                    raise _error(at(here, 'type'), 'unknown BLK type')
                if kind == 'array':
                    if name.startswith('@') and name != '@null':
                        raise _error(at(here, 'name'), 'directives cannot be arrays')
                    values = entry['values']
                    values_path = at(here, 'values')
                    if not isinstance(values, list):
                        raise _error(values_path, 'expected a list of values')
                    for i, value in enumerate(values):
                        _check(at(values_path, i), check_value, tag, value)
                else:
                    _check(at(here, 'value'), check_value, tag, entry['value'])
        _check(here, track_type, types, checked)
        result.append(checked)
    return result


def decode_typed(doc: dict) -> tuple[list[Entry], bytes | None]:
    if doc['$format'] != FORMAT:
        raise _error('/$format', 'unsupported typed BLKX version')
    if 'entries' not in doc or doc.keys() - {'$format', 'entries', 'source_b64'}:
        raise _error('', 'expected entries and optional source_b64, with no extra fields')
    entries = _entries(doc['entries'], '/entries')
    if 'source_b64' not in doc:
        return entries, None
    value = doc['source_b64']
    if not isinstance(value, str):
        raise _error('/source_b64', 'expected a Base64 string')
    try:
        source = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _error('/source_b64', 'invalid Base64 snapshot') from exc
    original = _check('/source_b64', parse_blk, source)
    return entries, source if original == entries else None


def check_type_map(mapping: object) -> dict[str, str]:
    if not isinstance(mapping, Mapping):
        raise _error('', 'expected a JSON Pointer to BLK type map')
    result = {}
    for path, tag in mapping.items():
        if not isinstance(path, str) or not path.startswith('/') or re.search(r'~(?![01])', path):
            raise _error('', f'invalid field JSON Pointer {path!r}')
        if not isinstance(tag, str) or tag.removesuffix('[]') not in TYPES:
            raise _error(path, f'unknown BLK type {tag!r} in type map')
        result[path] = tag
    return result


def _convert_value(tag: str, value: object, path: str) -> object:
    def convert(atom: str, item: object, indices: tuple[int, ...]) -> object:
        try:
            if atom in ('bool', 'text'):
                check_atom(atom, item)
                return item
            if not isinstance(item, _Number):
                raise ConversionError('expected a JSON number')
            token = item.token
            if item.decimal and atom != 'real':
                token = import_integral(token, atom)
            check_atom(atom, token)
            return token
        except ConversionError as exc:
            raise ValueTypeError(str(exc), indices) from exc

    return _check(path, map_value, tag, value, convert)


def _integer_type(token: str, path: str) -> str:
    try:
        check_value('i', token)
        return 'i'
    except ConversionError:
        _check(path, check_value, 'i64', token)
        return 'i64'


def decode_legacy(doc: object, *, strict: bool, type_map: Mapping[str, str] | None) -> list[Entry]:
    hints = check_type_map({} if type_map is None else type_map)
    inferred: list[tuple[str, str]] = []

    def infer(path: str, tag: str) -> str:
        if strict:
            raise _error(path, 'ambiguous legacy type; specify it in --type-map')
        inferred.append((path, tag))
        return tag

    def block(obj: object, path: str, depth: int) -> list[Entry]:
        if not isinstance(obj, dict):
            raise _error(path, 'expected a JSON object')
        if depth > MAX_DEPTH:
            raise _error(path, f'block nesting exceeds {MAX_DEPTH}')
        result: list[Entry] = []
        for name, value in obj.items():
            here = at(path, name)
            _check(here, text_value, name)
            if name.startswith('@'):
                raise _error(here, 'directive names require typed BLKX')
            tag = hints.get(here)
            if tag is None:
                if type(value) is bool:
                    tag = 'b'
                elif isinstance(value, str):
                    tag = 't'
                elif isinstance(value, _Number):
                    tag = 'r' if value.decimal else infer(here, _integer_type(value.token, here))
                elif isinstance(value, dict):
                    result.append({'kind': 'block', 'name': name, 'entries': block(value, here, depth + 1)})
                    continue
                elif isinstance(value, list):
                    if not value:
                        raise _error(here, 'empty legacy array requires an explicit type such as t[]')
                    if all(isinstance(item, dict) for item in value):
                        for i, member in enumerate(value):
                            result.append(
                                {
                                    'kind': 'block',
                                    'name': name,
                                    'entries': block(member, at(here, i), depth + 1),
                                }
                            )
                        continue
                    if all(isinstance(item, _Number) for item in value) and 2 <= len(value) <= 4:
                        tag = infer(here, f'p{len(value)}')
                        for i, number in enumerate(value):
                            if not number.decimal:
                                component = at(here, i)
                                if not _check(component, real_accepts_exact_integer, number.token):
                                    raise _error(
                                        component,
                                        'integer is not exact in a float vector; specify an explicit type',
                                    )
                    elif all(isinstance(item, str) for item in value):
                        tag = infer(here, 't[]')
                    elif all(type(item) is bool for item in value):
                        tag = infer(here, 'b[]')
                    else:
                        raise _error(here, 'cannot infer array type; specify a type for a homogeneous array')
                else:
                    raise _error(here, 'null is not a BLK value')
            if tag.endswith('[]'):
                if not isinstance(value, list):
                    raise _error(here, 'expected a JSON array')
                values = [_convert_value(tag[:-2], item, at(here, i)) for i, item in enumerate(value)]
                result.append({'kind': 'array', 'name': name, 'type': tag[:-2], 'values': values})
            else:
                result.append(
                    {'kind': 'param', 'name': name, 'type': tag, 'value': _convert_value(tag, value, here)}
                )
        return result

    entries = block(doc, '', 0)
    for path, tag in inferred:
        warnings.warn(f'{path}: inferred legacy BLK type {tag}', LegacyTypeWarning, stacklevel=3)
    return entries
