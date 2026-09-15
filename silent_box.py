# -*- coding: utf-8 -*-
"""Start Chatterbox server.py without a console or extra browser tab."""

from __future__ import annotations

import os
import runpy
import sys
import webbrowser
from pathlib import Path


def _noop(*_args, **_kwargs):
    return False


webbrowser.open = _noop
webbrowser.open_new = _noop
webbrowser.open_new_tab = _noop
os.environ["BROWSER"] = ""


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("silent_box.py <chatterbox_dir>")
    root = Path(sys.argv[1]).resolve()
    os.chdir(root)
    sys.path.insert(0, str(root))
    embedded = Path(sys.executable).resolve().parent
    if embedded.name.lower() == "python_embedded" or (embedded / "python310.dll").exists():
        extra = str(embedded) + os.pathsep + str(embedded / "Scripts")
        os.environ["PATH"] = extra + os.pathsep + os.environ.get("PATH", "")
    server = root / "server.py"
    if not server.exists():
        raise SystemExit(f"No server.py in {root}")
    sys.argv = [str(server)]
    runpy.run_path(str(server), run_name="__main__")


if __name__ == "__main__":
    main()
