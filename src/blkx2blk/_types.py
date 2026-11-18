"""Dagor value types shared by the text codec and both BLKX import paths."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TypeVar


class ConversionError(ValueError):
    """Input cannot be converted safely."""


class ValueTypeError(ConversionError):
    """Invalid typed value, optionally at a component within that value."""

    def __init__(self, message: str, indices: tuple[int, ...] = ()):
        super().__init__(message)
        self.indices = indices


@dataclass(frozen=True)
class TypeSpec:
    atom: str
    widths: tuple[int, ...] = ()
    rows: int = 0


TYPES = {
    't': TypeSpec('text'),
    'b': TypeSpec('bool'),
    'i': TypeSpec('int32'),
    'i64': TypeSpec('int64'),
    'r': TypeSpec('real'),
    'c': TypeSpec('byte', (3, 4)),
    'm': TypeSpec('real', (3,), rows=4),
    **{f'p{n}': TypeSpec('real', (n,)) for n in (2, 3, 4)},
    **{f'ip{n}': TypeSpec('int32', (n,)) for n in (2, 3, 4)},
}
BOOL_LITERALS = {
    'yes': True,
    'true': True,
    'on': True,
    '1': True,
    'no': False,
    'false': False,
    'off': False,
    '0': False,
}
_INTEGER = re.compile(r'(?:[+-]?[0-9]+|\+?0x[0-9a-fA-F]+)')
_REAL = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?')
_MATRIX = re.compile(r'\[ *((?:\[[^\[\]]*\] *){4})\]')
_MATRIX_ROW = re.compile(r'\[([^\[\]]*)\]')
# Ties round to even: at these endpoints binary32 rounds to zero or infinity.
_HALF_MIN_SUBNORMAL = Decimal(f'{5**150}e-150')
_OVERFLOW_MIDPOINT = Decimal(2**128 - 2**103)


def text_value(value: object) -> str:
    if not isinstance(value, str) or '\0' in value or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise ConversionError('expected a UTF-8 string without NUL')
    return value


def integer(token: str, bits: int) -> int:
    if not _INTEGER.fullmatch(token):
        raise ConversionError('expected an integer')
    hexadecimal = '0x' in token
    digits = token.split('x', 1)[-1] if hexadecimal else token.lstrip('+-')
    digits = digits.lstrip('0') or '0'
    if len(digits) > (bits // 4 if hexadecimal else 20):
        raise ConversionError(f'integer out of {bits}-bit range')
    if hexadecimal:
        number = int(digits, 16)
        return number if number < (1 << (bits - 1)) else number - (1 << bits)
    number = int(digits) * (-1 if token.startswith('-') else 1)
    if not -(1 << (bits - 1)) <= number < 1 << (bits - 1):
        raise ConversionError(f'integer out of {bits}-bit range')
    return number


def decimal_token(token: str) -> Decimal:
    try:
        # The Decimal implementation caps exponents, but Dagor accepts zero even with
        # an arbitrarily long exponent. Preserve the original lexeme at the caller.
        mantissa = token.lower().partition('e')[0]
        if not mantissa.strip('0+-.'):
            return Decimal('-0' if mantissa.startswith('-') else '0')
        return Decimal(token)
    except InvalidOperation as exc:
        raise ConversionError('decimal exponent outside supported range') from exc


def _real(token: str) -> Decimal:
    if not _REAL.fullmatch(token):
        raise ConversionError('expected a finite real number')
    magnitude = decimal_token(token).copy_abs()
    if (magnitude and magnitude <= _HALF_MIN_SUBNORMAL) or magnitude >= _OVERFLOW_MIDPOINT:
        raise ConversionError('real number outside float32 range')
    return magnitude


def real_accepts_exact_integer(token: str) -> bool:
    integer_value = int(_real(token))  # The caller supplies a JSON integer token.
    excess_bits = max(0, integer_value.bit_length() - 24)
    return integer_value % (1 << excess_bits) == 0


def import_integral(token: str, atom: str) -> str:
    """Convert JSON decimal/exponent notation only when its integer value is exact."""
    number = decimal_token(token)
    bits = 64 if atom == 'int64' else 32
    if atom == 'byte':
        if not 0 <= number <= 255:
            raise ConversionError('color component outside 0–255')
    elif not -(1 << (bits - 1)) <= number < 1 << (bits - 1):
        raise ConversionError(f'integer out of {bits}-bit range')
    if number != number.to_integral_value():
        raise ConversionError('expected an exact integer, without rounding')
    return str(int(number))


_T = TypeVar('_T')


def map_value(
    tag: str, value: object, atom_fn: Callable[[str, object, tuple[int, ...]], _T]
) -> _T | list[_T] | list[list[_T]]:
    """Check the shape once, and map each atom with its relative component path."""
    spec = TYPES[tag]

    def vector(items: object, path: tuple[int, ...]) -> list[_T]:
        if not isinstance(items, list) or len(items) not in spec.widths:
            sizes = ' or '.join(map(str, spec.widths))
            raise ValueTypeError(f'expected {sizes} components for {tag}', path)
        return [atom_fn(spec.atom, item, (*path, i)) for i, item in enumerate(items)]

    if spec.rows:
        if not isinstance(value, list) or len(value) != spec.rows:
            raise ValueTypeError('expected a matrix with four triples')
        return [vector(row, (i,)) for i, row in enumerate(value)]
    if spec.widths:
        return vector(value, ())
    return atom_fn(spec.atom, value, ())


def check_atom(atom: str, value: object) -> None:
    if atom == 'bool':
        if type(value) is not bool:
            raise ConversionError('expected a JSON boolean')
        return
    token = text_value(value)
    if atom == 'real':
        _real(token)
    elif atom != 'text':
        number = integer(token, 64 if atom == 'int64' else 32)
        if atom == 'byte' and not 0 <= number <= 255:
            raise ConversionError('color component outside 0–255')


def check_value(tag: str, value: object) -> None:
    def check(atom: str, item: object, indices: tuple[int, ...]) -> None:
        try:
            check_atom(atom, item)
        except ConversionError as exc:
            raise ValueTypeError(str(exc), indices) from exc

    map_value(tag, value, check)


def _vector(raw: str) -> list[str]:
    # Dagor skips ASCII spaces around commas, but does not skip tabs or Unicode
    # whitespace inside numeric values. It accepts trailing spaces after the value.
    parts = raw.split(',')
    parts[-1] = parts[-1].rstrip(' \t')
    for i, part in enumerate(parts):
        if i:
            part = part.lstrip(' ')
        parts[i] = part.rstrip(' ')
    return parts


def parse_value(tag: str, raw: str) -> object:
    spec = TYPES[tag]
    if spec.atom == 'bool':
        if raw.lower() not in BOOL_LITERALS:
            raise ConversionError('expected a BLK boolean')
        value: object = BOOL_LITERALS[raw.lower()]
    elif spec.rows:
        rows = _MATRIX.fullmatch(raw.rstrip(' \t'))
        if not rows:
            raise ConversionError('expected [[x,y,z][x,y,z][x,y,z][x,y,z]]')
        value = [_vector(row) for row in _MATRIX_ROW.findall(rows[1])]
    elif spec.widths:
        value = _vector(raw)
    else:
        value = raw
    check_value(tag, value)
    return value
