import os
import sys
import traceback
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PHOTOSHARE_DIR = ROOT / "photoshare"

for path in (ROOT, PHOTOSHARE_DIR):
    path_text = str(path)
    if path_text not in sys.path:
        sys.path.insert(0, path_text)


try:
    from photoshare.app import app
except Exception:
    import_error = traceback.format_exc()

    def app(environ, start_response):
        body = (
            "Eyentra failed to start.\n\n"
            "Check the Vercel Function Logs for this same traceback.\n\n"
            f"{import_error}"
        ).encode("utf-8")
        start_response("500 INTERNAL SERVER ERROR", [
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Content-Length", str(len(body))),
        ])
        return [body]


application = app
