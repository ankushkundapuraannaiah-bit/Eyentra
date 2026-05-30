import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PHOTOSHARE_DIR = ROOT / "photoshare"

for path in (ROOT, PHOTOSHARE_DIR):
    path_text = str(path)
    if path_text not in sys.path:
        sys.path.insert(0, path_text)

from photoshare.app import app


application = app
