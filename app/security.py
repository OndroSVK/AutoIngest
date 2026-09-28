from __future__ import annotations

import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit, urlunsplit


CHANNEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
SECRET_QUERY_KEYS = {"password", "pass", "pwd", "token", "key", "secret", "auth"}


def validate_channel_name(name: str) -> str:
    cleaned = name.strip()
    if not CHANNEL_NAME_RE.fullmatch(cleaned):
        raise ValueError("Use 1-64 letters, numbers, spaces, dots, underscores, or hyphens.")
    return cleaned


def safe_channel_slug(name: str) -> str:
    slug = SAFE_FILENAME_RE.sub("_", name.strip()).strip("._-")
    return slug[:64] or "channel"


def validate_output_folder(folder: str | None) -> str:
    folder = (folder or "").strip()
    if not folder:
        return ""
    path = PurePosixPath(folder)
    if path.is_absolute() or any(part in {"..", ""} for part in path.parts):
        raise ValueError("Output folder must be a relative path without '..'.")
    return str(path)


def redact_url(value: str | None) -> str:
    if not value:
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return "<redacted-invalid-url>"

    netloc = parts.netloc
    if "@" in netloc:
        creds, host = netloc.rsplit("@", 1)
        user = creds.split(":", 1)[0]
        netloc = f"{user}:***@{host}" if user else f"***@{host}"

    query_pairs = []
    for item in parts.query.split("&") if parts.query else []:
        key = item.split("=", 1)[0].lower()
        query_pairs.append(f"{item.split('=', 1)[0]}=***" if key in SECRET_QUERY_KEYS else item)
    return urlunsplit((parts.scheme, netloc, parts.path, "&".join(query_pairs), parts.fragment))
