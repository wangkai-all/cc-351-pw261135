"""Small path helpers shared by the source and the web UI."""

from __future__ import annotations

import posixpath


def normalize_path(path: str) -> str:
    """Normalize an HTTP path, rejecting traversal tricks.

    Returns a leading-slash path with dot segments resolved.  Callers only
    ever serve files below a fixed root, so a remaining ``..`` is suspicious.
    """

    if not path.startswith("/"):
        path = "/" + path
    normalized = posixpath.normpath(path)
    if normalized == ".":
        normalized = "/"
    if normalized.startswith("//"):
        normalized = "/" + normalized.lstrip("/")
    return normalized
