from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

import pytest

from blkx2blk import ConversionError, LegacyTypeWarning, blk_to_blkx, blkx_to_blk
from blkx2blk.__main__ import _run
from blkx2blk._blk import parse_blk

FIXTURES = Path(__file__).resolve().parents[1] / 'examples' / 'fixtures'

ALL_TYPES = (
    """// a line comment
/* outer /* nested */ comment */
"display: name":t="汉字 ~" ~~ ~n ~r ~t"
message:t="""
    + "'''\r\nfirst line\r\nlast line\r\n'''\n"
    + """flag:b=ON
min:i=-2147483648
mask:i=0xffffffff
large:i64=9223372036854775807
negativeZero:r=-0.0
real:r=+1.25e-1
rgb:c=128, 2, 3
rgba:c=1, 2, 3, 4
point2:p2=-1.5, 2
point3:p3=1, 2, 3
point4:p4=1, 2, 3, 4
ipoint2:ip2=1, 2
ipoint3:ip3=1, 2, 3
ipoint4:ip4=1, 2, 3, 4
matrix:m=[[1,0,0] [0,1,0] [0,0,1] [1,2,3]]
nums:i[]=[1; 2; /* between */ 3;]
words:t[]=["a;b"; "c]d"]
points:p2[]=[1, 2; 3, 4;]
matrices:m[]=[[[1,0,0][0,1,0][0,0,1][4,5,6]];]
empty:t[]=[]
unit{n:i=1; n:i=2} // after first unit
unit{}
"":t="empty name"
"@null":i=3"""
)


def write(path: Path, text: str) -> Path:
    path.write_bytes(text.encode('utf-8'))
    return path


def read_json(path: Path):
    return json.loads(path.read_bytes())


def test_bytes_round_trip(tmp_path, capsys):
    src = tmp_path / 'source.blk'
    original = b'\xef\xbb\xbf' + ALL_TYPES.replace('// a line comment\n', '// a line comment\r\n').encode(
        'utf-8'
    )
    src.write_bytes(original)
    exchange, result = tmp_path / 'export.blkx', tmp_path / 'result.blk'
    blk_to_blkx(src, exchange)
    doc = read_json(exchange)
    assert doc['$format'] == 'blkx2blk/1'
    assert base64.b64decode(doc['source_b64']) == original
    assert [e['name'] for e in doc['entries'] if e['kind'] == 'block'] == ['unit', 'unit']
    params = {e['name']: e['value'] for e in doc['entries'] if e['kind'] == 'param'}
    assert params['large'] == '9223372036854775807'
    assert params['negativeZero'] == '-0.0'
    assert params['display: name'] == '汉字 " ~ \n \r \t'
    assert params['message'] == 'first line\nlast line'
    # JSON formatting and object key order are not part of the entries' semantics.
    write(exchange, json.dumps(doc, sort_keys=True))
    blkx_to_blk(exchange, result)
    assert result.read_bytes() == original
    assert capsys.readouterr().out == ''


def test_edit_reorder_add_delete_and_render(tmp_path):
    src = write(tmp_path / 'source.blk', ALL_TYPES)
    exchange, result = tmp_path / 'export.blkx', tmp_path / 'result.blk'
    blk_to_blkx(src, exchange)
    doc = read_json(exchange)
    entries = doc['entries']
    entries.remove(next(e for e in entries if e.get('name') == 'flag'))
    entries.insert(0, entries.pop(-1))
    next(e for e in entries if e.get('name') == 'large')['value'] = '-9223372036854775808'
    first_unit = next(e for e in entries if e.get('name') == 'unit')
    first_unit['entries'].append({'kind': 'param', 'name': 'n', 'type': 'i', 'value': '3'})
    entries.append({'kind': 'include', 'path': 'not present/missing.blk'})
    write(exchange, json.dumps(doc))
    blkx_to_blk(exchange, result)
    rendered = result.read_bytes()
    assert b'\r' not in rendered
    assert parse_blk(rendered) == entries
    assert b'  n:i=3\n' in rendered
    assert b'/* between */' in rendered


def test_directives_preserved_but_not_executed(tmp_path):
    text = (
        'include "unavailable.blk"\n'
        'item{text:t="first"}\n'
        '"@override:item"{"@override:text":t="second"}\n'
        '"@clone-last:next"{}\n'
        '"@delete:item[2]"{}\n'
        '"@delete-all:text":b=no\n'
        '"@delete-last"{}'
    )
    src = write(tmp_path / 'a.blk', text)
    exchange, dst = tmp_path / 'a.blkx', tmp_path / 'b.blk'
    blk_to_blkx(src, exchange)
    doc = read_json(exchange)
    assert doc['entries'][0] == {'kind': 'include', 'path': 'unavailable.blk'}
    assert doc['entries'][2]['entries'][0]['name'] == '@override:text'
    blkx_to_blk(exchange, dst)
    assert dst.read_bytes() == src.read_bytes()
    doc.pop('source_b64')
    write(exchange, json.dumps(doc))
    blkx_to_blk(exchange, dst, force=True)
    assert parse_blk(dst.read_bytes()) == doc['entries']


def test_empty_snapshot_round_trip(tmp_path):
    src = tmp_path / 'empty.blk'
    src.write_bytes(b'')
    exchange, result = tmp_path / 'empty.blkx', tmp_path / 'restored.blk'
    blk_to_blkx(src, exchange)
    assert read_json(exchange)['source_b64'] == ''
    blkx_to_blk(exchange, result)
    assert result.read_bytes() == b''


def test_legacy_reference_sample(tmp_path):
    target = tmp_path / 'sample.blk'
    with pytest.warns(LegacyTypeWarning) as observed:
        blkx_to_blk(FIXTURES / 'sample.blkx', target)
    assert any('/origin:' in str(w.message) for w in observed)
    assert target.read_text(encoding='utf-8') == (FIXTURES / 'sample.blk').read_text(encoding='utf-8')


def test_legacy_inference(tmp_path):
    src = write(
        tmp_path / 'x.blkx',
        '{"big":9007199254740993,"value":42,"strs":["a","b"],'
        '"bools":[true,false],"floats":[0.05,13000.0],"viscosity":0.333}',
    )
    dst = tmp_path / 'x.blk'
    with pytest.warns(LegacyTypeWarning) as observed:
        blkx_to_blk(src, dst)
    assert len(observed) == 5
    assert [str(w.message).split(':')[0] for w in observed] == [
        '/big',
        '/value',
        '/strs',
        '/bools',
        '/floats',
    ]
    assert dst.read_text(encoding='utf-8').startswith(
        'big:i64=9007199254740993\nvalue:i=42\n'
        'strs:t[]=["a"; "b";]\nbools:b[]=[yes; no;]\nfloats:p2=0.05, 13000.0\nviscosity:r=0.333\n'
    )


def test_legacy_strict_and_type_map(tmp_path):
    src = write(
        tmp_path / 'x.blkx',
        '{"camera/size":[1,2],"~tags":[],"unit":[{"id":15},{"id":16}],'
        '"color":[1,2,3,4],"intervals":[[1,2],[3,4]],"plain":1.5,"numbers":[7]}',
    )
    dst = tmp_path / 'x.blk'
    with pytest.raises(ConversionError, match='/camera~1size: ambiguous'):
        blkx_to_blk(src, dst, strict=True)
    blkx_to_blk(
        src,
        dst,
        strict=True,
        type_map={
            '/camera~1size': 'ip2',
            '/~0tags': 't[]',
            '/unit/0/id': 'i',
            '/unit/1/id': 'i64',
            '/color': 'c',
            '/intervals': 'p2[]',
            '/numbers': 'i[]',
        },
    )
    actual = dst.read_text(encoding='utf-8')
    assert actual == (
        '"camera/size":ip2=1, 2\n~tags:t[]=[]\nunit{\n  id:i=15\n}\nunit{\n'
        '  id:i64=16\n}\ncolor:c=1, 2, 3, 4\nintervals:p2[]=[1, 2; 3, 4;]\n'
        'plain:r=1.5\nnumbers:i[]=[7;]\n'
    )
    assert len(parse_blk(dst.read_bytes())) == 8


@pytest.mark.parametrize(
    ('data', 'message'),
    [
        ('[]', '<root>: expected'),
        ('{"null":null}', '/null: null'),
        ('{"a":{"b":1,"b":2}}', '/a/b: duplicate'),
        ('{"a":NaN}', '/a: non-finite'),
        ('{"a":1e999}', '/a: real number'),
        ('{"a":[1,true]}', '/a: cannot infer'),
        ('{"a":[]}', '/a: empty'),
        ('{"a":[16777217,1]}', '/a/0: integer is not exact'),
        ('{"a":9223372036854775808}', '/a: integer out'),
        ('{"a":"\\ud800"}', '/a: expected a UTF-8'),
        ('{"$format":"future","entries":[]}', r'/\$format: unsupported'),
        ('{"$format":"blkx2blk/1","entries":[],"source_b64":"!"}', '/source_b64: invalid Base64'),
        ('{"$format":"blkx2blk/1","entries":[],"source_b64":"eDp6PTE="}', '/source_b64: line 1'),
        (
            '{"$format":"blkx2blk/1","entries":[{"kind":"param","name":"x","type":"i","value":1}]}',
            '/entries/0/value: expected a UTF-8',
        ),
        (
            '{"$format":"blkx2blk/1","entries":[{"kind":"param","name":"x","type":"i","value":"1"},'
            '{"kind":"param","name":"x","type":"r","value":"1"}]}',
            '/entries/1: parameter',
        ),
        (
            '{"$format":"blkx2blk/1","entries":[{"kind":"comment","style":"block","text":"*/"}]}',
            '/entries/0/text: invalid comment',
        ),
    ],
)
def test_bad_json_values_do_not_replace_target(tmp_path, data, message):
    src = write(tmp_path / 'in.blkx', data)
    dst = write(tmp_path / 'out.blk', 'existing')
    with pytest.raises(ConversionError, match=message):
        blkx_to_blk(src, dst, force=True)
    assert dst.read_bytes() == b'existing'


def test_malformed_json_preserves_public_exception(tmp_path):
    src = write(tmp_path / 'in.blkx', '{bad')
    with pytest.raises(json.JSONDecodeError):
        blkx_to_blk(src, tmp_path / 'out.blk')


@pytest.mark.parametrize(
    ('data', 'reason'),
    [
        (b'x:i=12\ny:z=3', 'line 2, column 4: unknown BLK type'),
        (b'x:i=1\nx:r=2', 'line 2, column 1: parameter'),
        (b'x:r=NaN', 'line 1, column 5: expected a finite'),
        (b'x:t="newline\n', 'unterminated quoted'),
        (b'x{}\n}', 'unexpected closing'),
        (b'x{\ny:i=1\n', 'closing brace'),
        (b'x:i[]=[1; 2;\n', 'closing the array'),
        (b'/* nope', 'block comment'),
        (b'\x03fake', 'binary BLK'),
        (b'BBF\0', 'binary BLK'),
        (b'bad\xff', 'expected UTF-8'),
    ],
)
def test_bad_blk_does_not_write(tmp_path, data, reason):
    src, dst = tmp_path / 'in.blk', tmp_path / 'out.blkx'
    src.write_bytes(data)
    with pytest.raises(ConversionError, match=reason):
        blk_to_blkx(src, dst)
    assert not dst.exists()


def test_cli_mixed_batch_and_errors(tmp_path, capsys):
    bad = write(tmp_path / 'bad.blkx', '[]')
    first = write(tmp_path / 'a.blkx', '{"a":1}')
    second = write(tmp_path / 'b.BLK', 'b:b=no')
    out = tmp_path / 'out'
    out.mkdir()
    assert _run([str(bad), str(first), str(second), '-o', str(out)]) == 1
    assert not (out / 'bad.blk').exists()
    assert (out / 'a.blk').read_bytes() == b'a:i=1\n'
    assert read_json(out / 'b.blkx')['entries'][0]['value'] is False
    captured = capsys.readouterr()
    assert 'Error converting' in captured.err and 'Traceback' not in captured.err
    assert '/a: inferred legacy BLK type i' in captured.err
    assert captured.out.count('Successfully converted') == 2


def test_cli_type_map(tmp_path, capsys):
    src = write(tmp_path / 'mapme.blkx', '{"a":[1,2]}')
    hints = write(tmp_path / 'hints.json', '{"/a":"ip2"}')
    assert _run([str(src), '--strict', '--type-map', str(hints)]) == 0
    assert src.with_suffix('.blk').read_bytes() == b'a:ip2=1, 2\n'
    assert capsys.readouterr().err == ''
    hints.write_text('{"/bad~2":"i"}')
    with pytest.raises(SystemExit) as exc:
        _run([str(src), '--type-map', str(hints)])
    assert exc.value.code == 2


def test_batch_no_output_collision_or_source_replacement(tmp_path, capsys):
    for name in ('a', 'b'):
        (tmp_path / name).mkdir()
    first = write(tmp_path / 'a' / 'same.blkx', '{"a":1}')
    second = write(tmp_path / 'b' / 'same.blkx', '{"b":1}')
    other = write(tmp_path / 'ok.blkx', '{"ok":true}')
    assert _run([str(first), str(second), str(other), '-o', str(tmp_path), '--force']) == 1
    assert not (tmp_path / 'same.blk').exists()
    assert (tmp_path / 'ok.blk').is_file()
    assert capsys.readouterr().err.count('output collision') == 2
    assert _run([str(first), '-o', str(first), '--force']) == 1
    assert first.read_bytes() == b'{"a":1}'
    existing = write(tmp_path / 'a' / 'same.blk', 'ok:b=yes')
    assert _run([str(first), str(existing), '--force']) == 1
    assert existing.read_bytes() == b'ok:b=yes'
    assert first.read_bytes() == b'{"a":1}'


def test_same_inode_never_written(tmp_path):
    src = write(tmp_path / 'in.blkx', '{"ok":true}')
    alias = tmp_path / 'alias.blk'
    os.link(src, alias)
    with pytest.raises(ConversionError, match='refers to an input'):
        blkx_to_blk(src, alias, force=True)


def test_output_symlink_never_written(tmp_path):
    src = write(tmp_path / 'in.blkx', '{"ok":true}')
    unrelated = write(tmp_path / 'untouched.blk', 'untouched')
    link = tmp_path / 'link.blk'
    try:
        link.symlink_to(unrelated)
    except OSError:
        pytest.skip('creating a symbolic link requires privileges on this host')
    with pytest.raises(ConversionError, match='symbolic link'):
        blkx_to_blk(src, link, force=True)
    assert unrelated.read_bytes() == b'untouched'


def test_replace_requires_force_and_cleans_up_on_error(tmp_path, monkeypatch):
    src, dst = write(tmp_path / 'in.blkx', '{"ok":true}'), write(tmp_path / 'out.blk', 'old')
    with pytest.raises(FileExistsError):
        blkx_to_blk(src, dst)
    assert dst.read_bytes() == b'old'
    original = os.replace

    def reject(*_):
        raise PermissionError('simulated replacement failure')

    monkeypatch.setattr(os, 'replace', reject)
    with pytest.raises(PermissionError):
        blkx_to_blk(src, dst, force=True)
    assert dst.read_bytes() == b'old'
    assert not list(tmp_path.glob('.blkx2blk-*.tmp'))
    monkeypatch.setattr(os, 'replace', original)
    blkx_to_blk(src, dst, force=True)
    assert dst.read_bytes() == b'ok:b=yes\n'


def test_no_replace_also_protects_late_output(tmp_path, monkeypatch):
    src, dst = write(tmp_path / 'in.blkx', '{"ok":true}'), tmp_path / 'out.blk'
    original = os.link

    def late_output(a, b):
        Path(b).write_bytes(b'arrived during conversion')
        return original(a, b)

    monkeypatch.setattr(os, 'link', late_output)
    with pytest.raises(FileExistsError):
        blkx_to_blk(src, dst)
    assert dst.read_bytes() == b'arrived during conversion'
    assert not list(tmp_path.glob('.blkx2blk-*.tmp'))


def test_dagor_binblk_independent(tmp_path, dagor_binblk, dagor_compile):
    src = write(tmp_path / 'representative.blk', ALL_TYPES)
    exchange, dst = tmp_path / 'a.blkx', tmp_path / 'generated.blk'
    blk_to_blkx(src, exchange)
    doc = read_json(exchange)
    next(e for e in doc['entries'] if e.get('name') == 'large')['value'] = '9007199254740993'
    write(exchange, json.dumps(doc))
    blkx_to_blk(exchange, dst)
    reference = write(
        tmp_path / 'reference.blk', ALL_TYPES.replace('9223372036854775807', '9007199254740993')
    )

    assert dagor_compile(dst) == dagor_compile(reference)
    defaults = write(tmp_path / 'defaults.blk', 'settings{mode:i=1}\nlegacy:i=5\n')
    directives = write(
        tmp_path / 'directives.blk',
        f'include "{defaults.name}"\n"@override:settings"{{"@override:mode":i=2}}\n"@delete:legacy":t=""\n',
    )
    directives_json = tmp_path / 'directives.blkx'
    blk_to_blkx(directives, directives_json)
    doc = read_json(directives_json)
    doc.pop('source_b64')
    write(directives_json, json.dumps(doc))
    formatted = tmp_path / 'formatted.blk'
    blkx_to_blk(directives_json, formatted)
    assert dagor_compile(formatted) == dagor_compile(directives)
    subprocess.run(
        [dagor_binblk, str(FIXTURES / 'sample.blk'), '-v'], check=True, capture_output=True, timeout=30
    )
