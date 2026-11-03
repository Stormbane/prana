"""`narada doctor` from a checkout: puts THIS tree's src first on the
path (the editable install may point at another working copy), then
runs prana.doctor. Same flags as `python -m prana.doctor`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from prana.doctor.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
