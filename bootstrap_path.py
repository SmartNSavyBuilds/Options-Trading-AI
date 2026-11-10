from __future__ import annotations

import sys
from pathlib import Path


def ensure_project_root_on_path() -> None:
    project_root = str(Path(__file__).resolve().parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)