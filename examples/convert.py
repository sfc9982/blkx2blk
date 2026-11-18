from pathlib import Path
from tempfile import TemporaryDirectory

from blkx2blk import blk_to_blkx, blkx_to_blk


def main() -> None:
    sample = Path(__file__).resolve().parent / 'fixtures' / 'sample.blk'
    with TemporaryDirectory() as temp:
        exported = Path(temp) / 'sample.blkx'
        restored = Path(temp) / 'restored.blk'
        blk_to_blkx(sample, exported)
        blkx_to_blk(exported, restored)
        if restored.read_bytes() != sample.read_bytes():
            raise RuntimeError('Restored sample differs from the original.')
        print('Sample restored byte-for-byte.')


if __name__ == '__main__':
    main()
