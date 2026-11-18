"""Differential tests with the official Dagor binBlk tool, plus local type invariants."""

from __future__ import annotations

import json
import random
import subprocess

import pytest

from blkx2blk import ConversionError, LegacyTypeWarning, blk_to_blkx, blkx_to_blk
from blkx2blk._blk import parse_blk

HALF_MIN = 5**150
OVERFLOW = 2**128 - 2**103
ABOVE_HALF_MIN = f'{HALF_MIN + 1}e-150'
BELOW_OVERFLOW = str(OVERFLOW - 1)


def fixture(tmp_path, name, text):
    path = tmp_path / name
    path.write_bytes(text.encode('utf-8'))
    return path


def rebuild(src):
    exchange = src.with_suffix('.blkx')
    blk_to_blkx(src, exchange)
    doc = json.loads(exchange.read_bytes())
    doc.pop('source_b64')  # Force real reserialization, not the snapshot shortcut.
    exchange.write_text(json.dumps(doc), encoding='utf-8')
    generated = src.with_name('generated.blk')
    blkx_to_blk(exchange, generated)
    return generated, doc['entries']


@pytest.mark.parametrize(
    ('tag', 'literals'),
    [
        ('i', ['0', '+1', '-0', '001', '-2147483648', '2147483647', '0xFFFFFFFF', '+0x80000000', '"5"']),
        (
            'i64',
            [
                '-9223372036854775808',
                '9223372036854775807',
                '9007199254740993',
                '0xffffffffffffffff',
                '+0x8000000000000000',
            ],
        ),
        (
            'r',
            [
                '.5',
                '1.',
                '+1.',
                '-0e999',
                '0e99999999999999999999999999999',
                '1e-45',
                ABOVE_HALF_MIN,
                '3.4028235e38',
                BELOW_OVERFLOW,
                '1.0000000596046447753906250001',
            ],
        ),
        ('c', ['0,127,255', '0,127,255,0', '0x01,0x02,0xFF', '"1, 2, 3, 255 "']),
        ('p2', ['1,2', '1 ,  2', '"1,2\t"', f'-0, {ABOVE_HALF_MIN}', f'{BELOW_OVERFLOW}, -1.0']),
        ('p3', ['-0, 1e-45, +.5']),
        ('p4', ['1,2,3,4']),
        ('ip2', ['-2147483648, 0xffffffff']),
        ('ip3', ['+0x80000000, 0, 2147483647']),
        ('ip4', ['1,2,3,+0xffffffff']),
        (
            'm',
            [
                '[[1,2,3][4,5,6][7,8,9][0,0,0]]',
                '[ [1,2,3] [4,5,6] [7,8,9] [0,0,0] ]',
                f'[[1,2,3 ][4,5,6][7,8,9][{ABOVE_HALF_MIN},0,-0]]',
            ],
        ),
        ('b', ['YES', '0', 'false', 'On', 'off', '"TRUE"', '1', 'NO']),
        (
            't',
            [
                'abc def',
                '""',
                ';',
                '"""\nX\n"""',
                '"""\n\n"""',
                '"""\né\r\n"""',
                '"a~nb~zc"',
                'path\\to\\file',
                'a/*c*/b',
                'a/*c\nd*/',
            ],
        ),
        ('i[]', ['[]', '[1;2;]', '[\n1\n2\n]', '["1""2"]', '[1;] // tail']),
        ('c[]', ['[1,2,3; 4,5,6,7;]']),
        ('t[]', ['[;]', '["a;b"; "c]d"]']),
    ],
    ids=lambda val: val if isinstance(val, str) else None,
)
def test_dagor_binblk_value_corpus(tmp_path, dagor_compile, tag, literals):
    text = ''.join(f'v{i}:{tag}={literal}\n' for i, literal in enumerate(literals))
    src = fixture(tmp_path, 'reference.blk', text)
    generated, entries = rebuild(src)
    assert parse_blk(generated.read_bytes()) == entries
    assert dagor_compile(generated) == dagor_compile(src)


@pytest.mark.parametrize(
    ('tag', 'literal'),
    [
        ('i', '-0xff'),
        ('i', '0XFF'),
        ('i', '1.0'),
        ('r', f'{HALF_MIN}e-150'),
        ('r', str(OVERFLOW)),
        ('r', '1e999'),
        ('r', '1e-999'),
        ('c', '256,0,0'),
        ('c', '1, 2,\t3'),
        ('c', '" 1, 2, 3"'),
        ('p2', '1, \t2'),
        ('p2', '1\t, 2'),
        ('p2', '1,\u00a02'),
        ('p2', '" 1,2"'),
        ('p2', '1,,2'),
        ('m', '[[ 1,2,3][4,5,6][7,8,9][0,0,0]]'),
        ('m', '[[1,2,3]\t[4,5,6][7,8,9][0,0,0]]'),
        ('b', '2'),
        ('b', '"no "'),
        ('i[]', '[1]'),
    ],
)
def test_dagor_binblk_common_rejections(tmp_path, dagor_binblk, tag, literal):
    src = fixture(tmp_path, 'invalid.blk', f'x:{tag}={literal}\n')
    with pytest.raises(ConversionError):
        blk_to_blkx(src, src.with_suffix('.blkx'))
    result = subprocess.run(
        [dagor_binblk, src.name, 'invalid.bin', '-b', '-final'],
        cwd=src.parent,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert not (src.parent / 'invalid.bin').exists()


@pytest.mark.parametrize(
    ('tag', 'literal', 'message'),
    [
        ('i', '4294967295', '32-bit range'),
        ('i', '0x', 'expected an integer'),
        ('i64', '9223372036854775808', '64-bit range'),
        ('r', 'nan', 'finite real'),
        ('r', '-inf', 'finite real'),
        ('c', '0,127,255,256', '0–255'),
        ('p2', '1,2garbage', 'finite real'),
        ('p2', '1,2,3', '2 components'),
        ('ip4', '1,2,3,4.5', 'expected an integer'),
        ('m', '[[1,2,3][4,5,6][7,8,9][0,0,0]]suffix', 'expected'),
    ],
)
def test_dagor_binblk_intentionally_stricter(tmp_path, dagor_compile, tag, literal, message):
    src = fixture(tmp_path, 'lossy.blk', f'x:{tag}={literal}\n')
    assert dagor_compile(src)  # Dagor accepts it; do not silently wrap, truncate, or lose fields.
    with pytest.raises(ConversionError, match=message):
        blk_to_blkx(src, src.with_suffix('.blkx'))


def test_dagor_binblk_seeded_typed_edits(tmp_path, dagor_compile):
    rng = random.Random(14045)
    entries, lines = [], []
    for i in range(128):
        name = f'node_{i}'
        tag = rng.choice(('i', 'i64', 'r', 't', 'b', 'p3', 'ip4', 'c'))
        if tag == 'i' or tag == 'i64':
            bits = 32 if tag == 'i' else 64
            number = rng.getrandbits(bits)
            literal = f'0x{number:x}'
            value = str(number - (1 << bits) if number >= 1 << (bits - 1) else number)
        elif tag == 'r':
            value = literal = f'{rng.randint(-9999, 9999)}e{rng.randint(-42, 32):+d}'
        elif tag == 't':
            value = '枪口~"\n\t' + str(i)
            literal = '\'枪口~~"~n~t' + str(i) + "'"
        elif tag == 'b':
            value = bool(rng.getrandbits(1))
            literal = 'ON' if value else '0'
        else:
            count = 3 if tag == 'p3' else 4
            value = [
                str(rng.randint(0, 255) if tag == 'c' else rng.randint(-1000, 1000)) for _ in range(count)
            ]
            literal = ','.join(value)
        entries.append({'kind': 'param', 'name': name, 'type': tag, 'value': value})
        lines.append(f'{name}:{tag}={literal};')
    encoded = fixture(tmp_path, 'edit.blkx', json.dumps({'$format': 'blkx2blk/1', 'entries': entries}))
    actual = tmp_path / 'actual.blk'
    blkx_to_blk(encoded, actual)
    expected = fixture(tmp_path, 'expected.blk', '\n'.join(lines))
    assert parse_blk(actual.read_bytes()) == entries
    assert dagor_compile(actual) == dagor_compile(expected)


def legacy_fixture(tmp_path):
    encoded = fixture(
        tmp_path,
        'legacy.blkx',
        """{
      "major":[1.0,2e0], "weights":[0.25,-0.0], "color":[255.0,128,0.0],
      "wide":9.007199254740993e15, "repeat":[1.0,2.0,3e0], "enabled":[true,false],
      "empty":[], "matrix":[[1,0,0],[0,1,0],[0,0,1],[1.125,2,3]]
    }""",
    )
    hints = {
        '/major': 'ip2',
        '/weights': 'p2',
        '/color': 'c',
        '/wide': 'i64',
        '/repeat': 'i[]',
        '/empty': 'p4[]',
        '/enabled': 'b[]',
        '/matrix': 'm',
    }
    generated = tmp_path / 'generated.blk'
    blkx_to_blk(encoded, generated, strict=True, type_map=hints)
    return generated


def test_legacy_exact_decimal_to_integer_and_bounded_real(tmp_path):
    entries = parse_blk(legacy_fixture(tmp_path).read_bytes())
    assert [(e['name'], e['type']) for e in entries] == [
        ('major', 'ip2'),
        ('weights', 'p2'),
        ('color', 'c'),
        ('wide', 'i64'),
        ('repeat', 'i'),
        ('enabled', 'b'),
        ('empty', 'p4'),
        ('matrix', 'm'),
    ]
    assert entries[0]['value'] == ['1', '2']
    assert entries[3]['value'] == '9007199254740993'
    with pytest.warns(LegacyTypeWarning):
        src = fixture(tmp_path, 'exact.blkx', '{"vector":[16777218,-33554432]}')
        blkx_to_blk(src, src.with_suffix('.blk'))
    assert parse_blk(f'v:r={ABOVE_HALF_MIN}\nw:r={BELOW_OVERFLOW}'.encode())[0]['value'] == ABOVE_HALF_MIN
    for value in (str(OVERFLOW), f'{HALF_MIN}e-150'):
        with pytest.raises(ConversionError, match='float32 range'):
            parse_blk(f'v:r={value}'.encode())


def test_dagor_binblk_legacy_type_map(tmp_path, dagor_compile):
    generated = legacy_fixture(tmp_path)
    reference = fixture(
        tmp_path,
        'reference.blk',
        """major:ip2=1,2
weights:p2=.25,-0
color:c=255,128,0,255
wide:i64=9007199254740993
repeat:i=1
repeat:i=2
repeat:i=3
"enabled":b=on
"enabled":b=off
matrix:m=[[1,0,0][0,1,0][0,0,1][1.125,2,3]]
""",
    )
    assert dagor_compile(generated) == dagor_compile(reference)


@pytest.mark.parametrize(
    ('value', 'tag', 'error'),
    [
        ('[[1,0,0],[0,1,0],[0,0,1],[1,2,"bad"]]', 'm', '/a/3/2: expected a JSON number'),
        ('[[1,2],[3,4.5]]', 'ip2[]', '/a/1/1: expected an exact integer'),
        ('[1,2,3,255.000000000000000000001]', 'c', '/a/3: color component outside'),
        ('9007199254740993.00000000000000001', 'i64', '/a: expected an exact integer'),
        ('2147483648e0', 'i', '/a: integer out of 32-bit range'),
    ],
)
def test_legacy_component_errors(tmp_path, value, tag, error):
    src = fixture(tmp_path, 'input.blkx', f'{{"a":{value}}}')
    with pytest.raises(ConversionError, match=error):
        blkx_to_blk(src, src.with_suffix('.blk'), strict=True, type_map={'/a': tag})


def test_typed_component_error_reports_array_matrix_and_atom(tmp_path):
    entries = [
        {
            'kind': 'array',
            'name': 'poses',
            'type': 'm',
            'values': [[['1', '0', '0'], ['0', '1', 'invalid'], ['0', '0', '1'], ['0', '0', '0']]],
        }
    ]
    src = fixture(tmp_path, 'input.blkx', json.dumps({'$format': 'blkx2blk/1', 'entries': entries}))
    with pytest.raises(ConversionError, match='/entries/0/values/0/1/2: expected a finite real'):
        blkx_to_blk(src, src.with_suffix('.blk'))
