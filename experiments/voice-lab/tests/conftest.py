import sys
from pathlib import Path

# The lab is a flat script directory, not a package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
