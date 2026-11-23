"""Which engine a session store connection URL names (ADR-0114). No driver is imported here.

`postgresql://` and `postgres://` name PostgreSQL (the URL goes to asyncpg unchanged); `sqlite:///<path>` names a SQLite
file. A relative path lies in the router's state folder, an absolute one (`sqlite:////var/lib/x/sessions.db`, or
`sqlite:///C:/x/sessions.db` on Windows) is used as given. Returns a StoreURL or a Problem; the URL text never appears
in a Problem, because a PostgreSQL URL may carry a password.
"""
import os
from dataclasses import dataclass

from .outcome import invalid

POSTGRES_SCHEMES = ("postgresql", "postgres")
SQLITE_PREFIX = "sqlite:///"
UNSUPPORTED = invalid("session store URL scheme not supported; use postgresql:// or sqlite:///<file>")
BAD_SQLITE_PATH = invalid("session store SQLite URL needs a file path without query, fragment or '..'")


@dataclass(frozen=True)
class StoreURL:
    engine: str          # "postgresql" or "sqlite"
    path: str = ""       # absolute file path for SQLite, empty for PostgreSQL


def scheme(url):
    """The URL's scheme in lower case, or "" when it has none. Only the text before the first colon is read."""
    head, sep, _ = url.partition(":")
    return head.lower() if sep else ""


def parse_store_url(url, base_dir):
    """StoreURL for a supported URL, or a Problem. base_dir (the state folder) is only consulted for a relative SQLite
    path and may be a callable that returns it, so callers without a configuration path can still check the scheme."""
    if not isinstance(url, str) or not url:
        return UNSUPPORTED
    kind = scheme(url)
    if kind in POSTGRES_SCHEMES:
        return StoreURL("postgresql")
    if kind != "sqlite" or url[:len(SQLITE_PREFIX)].lower() != SQLITE_PREFIX:
        return UNSUPPORTED
    path = url[len(SQLITE_PREFIX):]
    parts = path.replace("\\", "/").split("/")
    if (not path or path.endswith(("/", "\\")) or any(c in path for c in "?#\0") or ".." in parts
            or path == ":memory:" or any(ord(c) < 32 for c in path)):
        return BAD_SQLITE_PATH
    if not os.path.isabs(path):
        base = base_dir() if callable(base_dir) else base_dir
        path = os.path.join(base, path)
    return StoreURL("sqlite", os.path.abspath(path))
