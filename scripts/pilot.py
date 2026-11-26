"""Run project commands with data-disk paths before importing the product."""

import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pilot_host.storage_cli import main  # noqa: E402

if __name__ == "__main__":
    main()
