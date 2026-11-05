"""Where exported files (PDF, STL, 3MF, STEP, URDF/MJCF, ...) are written locally."""

from __future__ import annotations

import os
import re
from typing import Optional

DEFAULT_EXPORT_DIR = "~/onshape-exports"


def export_dir(override: Optional[str] = None) -> str:
    path = os.path.expanduser(override or os.getenv("ONSHAPE_EXPORT_DIR", DEFAULT_EXPORT_DIR))
    os.makedirs(path, exist_ok=True)
    return path


def safe_name(name: str) -> str:
    name = re.sub(r"[^\w\-. ()äöüÄÖÜß]+", "_", name).strip(" ._")
    return name or "export"


def save_bytes(
    data: bytes, filename: str, directory: Optional[str] = None, overwrite: bool = False
) -> str:
    """Write ``data`` to ``directory/filename`` (never silently overwrites unless asked)."""
    folder = export_dir(directory)
    base, ext = os.path.splitext(safe_name(filename))
    path = os.path.join(folder, base + ext)
    counter = 1
    while not overwrite and os.path.exists(path):
        path = os.path.join(folder, f"{base}_{counter}{ext}")
        counter += 1
    with open(path, "wb") as fh:
        fh.write(data)
    return path
