# blkx2blk

Convert War Thunder / Dagor **text BLK** to editable, typed BLKX JSON and back. If the entries are unchanged, the restored BLK is byte-for-byte identical to the original, including comments, BOM, line endings, and trailing whitespace. If you edit the entries, the output is reformatted as UTF-8 with LF and two-space indentation; order, types, values, and retained comments are preserved.

## Install and use

```powershell
python -m pip install -e .

# Export, edit the entries in asset.blkx if desired, then restore.
blkx2blk asset.blk
blkx2blk asset.blkx -o restored.blk

# Import an old, untyped JSON export.
blkx2blk examples/fixtures/sample.blkx -o converted.blk
blkx2blk old.blkx --strict --type-map types.json -o typed-values.blk

# Batch conversion accepts either format. The output directory must exist.
blkx2blk a.blkx b.blk -o out\
```

Existing files are not overwritten unless you pass `--force`. A batch never overwrites any of its inputs or writes to output symlinks; conflicting outputs are skipped. Each file is published atomically. Without `--force`, the output filesystem must support hard links. File failures are reported to stderr without stopping the rest of the batch. Exit codes: `0` success, `1` one or more files failed, `2` invalid command-line usage.

```python
from pathlib import Path
from blkx2blk import blk_to_blkx, blkx_to_blk

blk_to_blkx(Path('asset.blk'), Path('asset.blkx'))
blkx_to_blk('asset.blkx', 'restored.blk')
```

Both functions accept `force=False` by default. `blkx_to_blk` also accepts `strict=False` and `type_map=None` (JSON Pointers mapped to BLK types). Untyped inference emits `LegacyTypeWarning`; validation errors use `ConversionError`, except malformed JSON (`json.JSONDecodeError`) and filesystem errors (`OSError`).

## Typed BLKX v1

`$format` is `blkx2blk/1`. Edit `entries`, an ordered list. Duplicate names are allowed; each parameter or block remains a separate entry. `source_b64`, when present, is the original BLK in Base64. Leave it alone for exact restoration. Omit it when creating a new BLKX or to request canonical output even without edits.

```json
{
  "$format": "blkx2blk/1",
  "entries": [
    {"kind": "comment", "style": "line", "text": " Asset settings"},
    {"kind": "include", "path": "defaults.blk"},
    {"kind": "param", "name": "assetId", "type": "i64", "value": "9007199254740993"},
    {"kind": "param", "name": "enabled", "type": "b", "value": true},
    {"kind": "array", "name": "tags", "type": "t", "values": ["one", "two"]},
    {"kind": "block", "name": "detail", "entries": []}
  ]
}
```

- `param` and `array` accept `t`, `b`, `i`, `i64`, `r`, `c`, `m`, `p2`–`p4`, and `ip2`–`ip4`. String and boolean values use their respective JSON types. Each numeric token is a JSON **string**, including vector and color components. Matrices use four triples. An `array` uses the element type without `[]` and may be empty.
- A `comment` uses `style: "line"` or `"block"` and `text` without delimiters. On reformatting, inline comments may move to their own lines, and block-comment line endings are normalized. Override/delete-style directives are represented as typed parameters or blocks with their `@` names.
- Numeric validation follows Dagor’s signed 32/64-bit, binary32, and color types. Use `0x` for fixed-width hexadecimal bit patterns. Decimal integers that would wrap, non-finite or out-of-range reals, color channels outside 0–255, and composite values with extra or malformed components are rejected even where `binBlk` would silently reinterpret them.
- Only UTF-8 Dagor text BLK (with or without BOM) and finite numbers are supported. This tool does not read binary BLK, load include files, or apply directives. Relative include paths retain their spelling; keep the intended directory layout when relocating a file.

## Importing untyped BLKX

Old JSON exports do not contain enough information to recover every original BLK type. By default, decimal/exponent values use `r`; integers use `i` or `i64`; 2–4 component numeric arrays use `pN`; string and boolean arrays use `t[]` and `b[]`. Objects become blocks and object arrays become repeated blocks. Guessed integer and array types generate field-specific warnings. Duplicate JSON keys, null values, non-finite numbers, and unmappable arrays are rejected rather than silently discarded.

Use `--strict` to disallow integer and array inference. Supply a type map to resolve ambiguous fields, declare empty arrays, or choose a different type. It uses JSON Pointers (escape `/` as `~1` and `~` as `~0`); pointers absent in an input are ignored so the same map can be used for a batch. A decimal/exponent JSON number explicitly mapped to an integer type is accepted only if it is exactly integral and in range. An inferred float vector rejects JSON integer components that would lose precision; give those fields an explicit integer type instead.

```json
{
  "/camera/size": "ip2",
  "/camera/fov": "r",
  "/tags": "t[]",
  "/units/0/id": "i64"
}
```

## Development

```powershell
python -m pip install -e ".[dev]"
python -m ruff check .
python -m ruff format --check .
python -m pytest
python -m build

# Optional differential tests using Gaijin's binBlk.
$env:DAGOR_BINBLK = 'C:\path\to\binBlk.exe'
python -m pytest -k dagor_binblk
```

The `dagor_binblk` tests compile reference files and converter output to binary and compare the results, including emitted types and values. They also cover shared rejections and deliberately stricter rejection of potentially lossy inputs. These tests are skipped if `binBlk` is neither in `PATH` nor set by `DAGOR_BINBLK`. The converter itself does not depend on it.

The bundled examples are synthetic. This is an independent tool, not affiliated with Gaijin Entertainment. Game assets and Gaijin tools are not distributed with this repository.

Syntax reference: [Dagor BLK documentation](https://gaijinentertainment.github.io/DagorEngine/dagor-tools/blk/blk.html).
