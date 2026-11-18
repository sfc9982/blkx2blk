"""Parser and writer for Dagor's UTF-8 text DataBlock syntax."""

from __future__ import annotations

import re
from typing import Any

from ._types import TYPES, ConversionError, ValueTypeError, parse_value, text_value

# Entries are ordered at every level. A dict here describes one entry, not a block.
Entry = dict[str, Any]
MAX_DEPTH = 400  # Leaves room for the JSON object + array at each level.
IDENT = re.compile(r'[A-Za-z0-9_.~\-]+')
_BINARY = (b'BBF\0', b'BBz\0', b'blk 1.1', b'\x01', b'\x02', b'\x03', b'\x04', b'\x05', b'\x06')


def track_type(types: dict[str, str], entry: Entry) -> None:
    """Check unambiguous type conflicts without evaluating any directives."""
    kind = entry['kind']
    if kind == 'include':
        types.clear()  # External content can apply directives to existing entries.
        return
    if kind not in ('param', 'array'):
        return
    name = entry['name']
    if name.startswith('@') and name != '@null':
        if name.startswith(('@delete:', '@delete-all:')):
            # The directive may or may not remove the last value of this name.
            types.pop(name.split(':', 1)[1].split('[', 1)[0], None)
        return
    if kind == 'array' and not entry['values']:
        return
    tag = entry['type']
    previous = types.setdefault(name, tag)
    if previous != tag:
        raise ConversionError(f'parameter {name!r} already has type {previous}, not {tag}')


def _block_comment_end(source: str, start: int) -> int | None:
    level = 0
    for match in _COMMENT.finditer(source, start):
        level += 1 if match[0] == '/*' else -1
        if level == 0:
            return match.end()
    return None


_COMMENT = re.compile(r'/\*|\*/')
_EOL = re.compile(r'\r\n|\r|\n')
_MULTI_OPEN = re.compile(r'[ \t\r]*\n')
_ESCAPES = {'r': '\r', 'n': '\n', 't': '\t'}


class _Parser:
    def __init__(self, source: str):
        self.source = source
        self.pos = 0
        self.size = len(source)

    def fail(self, message: str, pos: int | None = None) -> ConversionError:
        pos = self.pos if pos is None else pos
        line = 1
        start = 0
        for end in _EOL.finditer(self.source, 0, pos):
            line += 1
            start = end.end()
        return ConversionError(f'line {line}, column {pos - start + 1}: {message}')

    def take(self, token: str) -> bool:
        if self.source.startswith(token, self.pos):
            self.pos += len(token)
            return True
        return False

    def comment(self) -> Entry | None:
        start = self.pos
        if self.take('//'):
            eol = _EOL.search(self.source, self.pos)
            self.pos = eol.start() if eol else self.size
            return {'kind': 'comment', 'style': 'line', 'text': self.source[start + 2 : self.pos]}
        if self.source.startswith('/*', start):
            end = _block_comment_end(self.source, start)
            if end is None:
                raise self.fail('unterminated block comment')
            self.pos = end
            return {
                'kind': 'comment',
                'style': 'block',
                'text': _EOL.sub('\n', self.source[start + 2 : end - 2]),
            }
        return None

    def space(self, comments: list[Entry], *, newline: bool = True) -> None:
        allowed = ' \t\x1a\r\n' if newline else ' \t\x1a'
        while self.pos < self.size:
            if self.source[self.pos] in allowed:
                self.pos += 1
            elif (comment := self.comment()) is not None:
                comments.append(comment)
            else:
                return

    def quoted(self) -> str:
        start = self.pos
        quote = self.source[self.pos]
        self.pos += 1
        multiline = self.take(quote * 2)
        if multiline and (match := _MULTI_OPEN.match(self.source, self.pos)):
            self.pos = match.end()
        closing = quote * (3 if multiline else 1)
        parts: list[str] = []
        while self.pos < self.size:
            if self.take(closing):
                if multiline and len(parts) > 1 and parts[-1] == '\n':
                    parts.pop()
                return ''.join(parts)
            char = self.source[self.pos]
            self.pos += 1
            if char == '~':
                if self.pos == self.size:
                    break
                char = self.source[self.pos]
                self.pos += 1
                parts.append(_ESCAPES.get(char, char))
            elif char in '\r\n' and not multiline:
                break
            elif char != '\r':
                parts.append(char)
        raise self.fail('unterminated quoted string', start)

    def ident(self) -> str:
        if self.pos < self.size and self.source[self.pos] in '"\'':
            return self.quoted()
        if match := IDENT.match(self.source, self.pos):
            self.pos = match.end()
            return match[0]
        raise self.fail('expected an identifier')

    def value(self, tag: str, comments: list[Entry]) -> object:
        start = self.pos
        source = self.source
        if self.pos == self.size or source[self.pos] in '\r\n':
            raise self.fail('expected a value')
        if source[self.pos] in '"\'':
            raw = self.quoted()
        else:
            chunks: list[str] = []
            chunk = self.pos
            while self.pos < self.size and source[self.pos] not in ';\r\n}':
                if source.startswith('//', self.pos):
                    break
                if source.startswith('/*', self.pos):
                    chunks.append(source[chunk : self.pos])
                    comment = self.comment()
                    assert comment is not None
                    comments.append(comment)
                    chunk = self.pos
                    if _EOL.search(comment['text']):
                        break
                else:
                    self.pos += 1
            chunks.append(source[chunk : self.pos])
            raw = ''.join(chunks).rstrip(' \t')
        while self.pos < self.size and source[self.pos] in ' \t\x1a':
            self.pos += 1
        self.take(';')
        try:
            return parse_value(tag, raw)
        except ConversionError as exc:
            location = (
                ' at component ' + '.'.join(str(i + 1) for i in exc.indices)
                if isinstance(exc, ValueTypeError) and exc.indices
                else ''
            )
            raise self.fail(str(exc) + location, start) from exc

    def entries(self, depth: int = 0) -> list[Entry]:
        out: list[Entry] = []
        types: dict[str, str] = {}
        while True:
            self.space(out)
            if self.pos >= self.size:
                if depth:
                    raise self.fail('expected a closing brace')
                return out
            if self.take('}'):
                if not depth:
                    raise self.fail('unexpected closing brace', self.pos - 1)
                return out
            start = self.pos
            name = self.ident()
            comments: list[Entry] = []
            self.space(comments)
            if self.take('{'):
                if depth >= MAX_DEPTH:
                    raise self.fail(f'block nesting exceeds {MAX_DEPTH}')
                out.extend(comments)
                out.append({'kind': 'block', 'name': name, 'entries': self.entries(depth + 1)})
                continue
            if self.take(':'):
                self.space(comments)
                tag = self.ident()
                if tag not in TYPES:
                    raise self.fail(f'unknown BLK type {tag!r}')
                self.space(comments)
                is_array = self.take('[]')
                if is_array:
                    self.space(comments)
                if not self.take('='):
                    raise self.fail("expected '='")
                self.space(comments, newline=False)
                if is_array:
                    if name.startswith('@') and name != '@null':
                        raise self.fail('directives cannot be arrays')
                    if not self.take('['):
                        raise self.fail("expected '[' opening an array")
                    values = []
                    while True:
                        self.space(comments)
                        if self.take(']'):
                            break
                        if self.pos >= self.size or self.source[self.pos] in '}':
                            raise self.fail("expected ']' closing the array")
                        values.append(self.value(tag, comments))
                    out.append({'kind': 'array', 'name': name, 'type': tag, 'values': values})
                else:
                    out.append(
                        {'kind': 'param', 'name': name, 'type': tag, 'value': self.value(tag, comments)}
                    )
            elif name.lower() == 'include':
                path = self.value('t', comments)
                if not path:
                    raise self.fail('expected an include path')
                out.append({'kind': 'include', 'path': path})
            else:
                raise self.fail("expected ':', '{', or an include path")
            try:
                track_type(types, out[-1])
            except ConversionError as exc:
                raise self.fail(str(exc), start) from exc
            out.extend(comments)


def parse_blk(data: bytes) -> list[Entry]:
    if data.startswith(_BINARY) or b'\0' in data:
        raise ConversionError('binary BLK or NUL bytes are not supported (expected UTF-8 BLK text)')
    try:
        source = data.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ConversionError(f'expected UTF-8 BLK text (invalid byte at offset {exc.start})') from exc
    return _Parser(source).entries()


def _quote(text: str) -> str:
    escapes = {'~': '~~', '"': '~"', '\n': '~n', '\r': '~r', '\t': '~t'}
    return '"' + ''.join(escapes.get(char, char) for char in text) + '"'


def _render_value(tag: str, value: Any) -> str:
    spec = TYPES[tag]
    if spec.atom == 'text':
        return _quote(value)
    if spec.atom == 'bool':
        return 'yes' if value else 'no'
    if spec.rows:
        return '[' + ' '.join('[' + ', '.join(row) + ']' for row in value) + ']'
    if spec.widths:
        return ', '.join(value)
    return value


def render_blk(entries: list[Entry]) -> bytes:
    lines: list[str] = []

    def render(block: list[Entry], depth: int) -> None:
        pad = '  ' * depth
        for entry in block:
            kind = entry['kind']
            if kind == 'comment':
                text = entry['text']
                comment = '//' + text if entry['style'] == 'line' else '/*' + text + '*/'
                lines.append(pad + comment)
                continue
            if kind == 'include':
                lines.append(f'{pad}include {_quote(entry["path"])}')
                continue
            name = entry['name']
            name = name if IDENT.fullmatch(name) else _quote(name)
            if kind == 'block':
                lines.append(f'{pad}{name}{{')
                render(entry['entries'], depth + 1)
                lines.append(pad + '}')
            else:
                tag = entry['type']
                if kind == 'array':
                    values = ''.join(_render_value(tag, value) + '; ' for value in entry['values'])
                    lines.append(f'{pad}{name}:{tag}[]=[{values.rstrip()}]')
                else:
                    lines.append(f'{pad}{name}:{tag}={_render_value(tag, entry["value"])}')

    render(entries, 0)
    return ('\n'.join(lines) + ('\n' if lines else '')).encode('utf-8')


def check_comment(style: object, text: object) -> None:
    value = text_value(text)
    if style == 'line' and not _EOL.search(value):
        return
    if style == 'block':
        comment = f'/*{value}*/'
        if _block_comment_end(comment, 0) == len(comment):
            return
    raise ConversionError('invalid comment style or contents')
