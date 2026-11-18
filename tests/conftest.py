import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture(scope='session')
def dagor_binblk() -> str:
    tool = os.environ.get('DAGOR_BINBLK') or shutil.which('binBlk')
    if not tool:
        pytest.skip('set DAGOR_BINBLK to the official binBlk executable')
    return str(Path(tool).resolve(strict=True))


@pytest.fixture
def dagor_compile(dagor_binblk: str) -> Callable[[Path], bytes]:
    def compile_(path: Path) -> bytes:
        output = path.with_suffix('.bin')
        process = subprocess.run(
            [dagor_binblk, path.name, output.name, '-b', '-final'],
            cwd=path.parent,
            capture_output=True,
            timeout=30,
        )
        if process.returncode or not output.is_file():
            detail = (process.stdout + process.stderr).decode('utf-8', errors='replace').strip()
            pytest.fail(f'binBlk failed for {path.name}:\n{detail}')
        return output.read_bytes()

    return compile_
