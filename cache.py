"""
cache.py — Persistent disk-cache subsystem for ctrld-sync.

Provides:
- Platform-specific cache directory resolution (get_cache_dir)
- Loading/saving a JSON blocklist cache with graceful degradation (load_disk_cache,
  save_disk_cache)

Module-level state
------------------
_disk_cache : dict[str, dict[str, Any]]
    Blocklist entries loaded from disk at startup.  Keys are URLs; values are
    dicts with at minimum a ``data`` key.  Access via in-place mutations
    (``_disk_cache.clear()``, ``_disk_cache.update(…)``) so that callers that
    imported the name still reference the same live object.

_cache_stats : dict[str, int]
    Running counters for hits, misses, conditional-request validations, and
    errors.  Updated in-place so importers always see current values.

CACHE_TTL_SECONDS : int
    How long (in seconds) a cached entry is considered fresh before a
    conditional HTTP request is sent to validate it (default: 24 h).
"""

from __future__ import annotations

import json
import logging
import os
import platform
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("control-d-sync")

__all__ = [
    "CACHE_TTL_SECONDS",
    "_disk_cache",  # live reference kept by main.py
    "_cache_stats",  # accessed by main.py for reporting
    "_sanitize_fn",  # injection point for token-aware sanitizer (see api_client.py)
    "get_cache_dir",
    "load_disk_cache",
    "save_disk_cache",
]

# --------------------------------------------------------------------------- #
# Module-level cache state
# --------------------------------------------------------------------------- #
# 24 hours: within TTL, serve from disk without an HTTP request.
CACHE_TTL_SECONDS: int = 24 * 60 * 60

# Blocklist entries keyed by URL.  Populated by load_disk_cache() at startup.
# Always mutate in-place so that names imported via ``from cache import …``
# continue to reference the same underlying dict object.
_disk_cache: dict[str, dict[str, Any]] = {}

# Running counters – updated by load_disk_cache, save_disk_cache, and _gh_get.
_cache_stats: dict[str, int] = {
    "hits": 0,
    "misses": 0,
    "validations": 0,
    "errors": 0,
}


# --------------------------------------------------------------------------- #
# Sanitisation hook — mirrors the injection contract of api_client.py.
# Defaults to repr() so cache.py is usable in isolation (e.g., tests).
# Note: repr() wraps strings in outer quotes (e.g. 'hello'); the injected
# sanitize_for_log from main.py strips those quotes so production log lines
# read naturally.  The quote-wrapping is acceptable for the bootstrap default
# because it is only visible when running without main.py injection.
# main.py injects sanitize_for_log after startup to enable token redaction.
# --------------------------------------------------------------------------- #
_sanitize_fn: Callable[[Any], str] = repr


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def get_cache_dir() -> Path:
    """Return the platform-specific cache directory for ctrld-sync.

    Uses standard cache locations:
    - Linux/Unix: ~/.cache/ctrld-sync  (or $XDG_CACHE_HOME/ctrld-sync)
    - macOS:      ~/Library/Caches/ctrld-sync
    - Windows:    %LOCALAPPDATA%/ctrld-sync/cache

    SECURITY: No user input reaches path construction – prevents path
    traversal attacks.
    """
    system = platform.system()
    if system == "Darwin":  # macOS
        return Path.home() / "Library" / "Caches" / "ctrld-sync"
    if system == "Windows":
        appdata = os.getenv("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        return Path(appdata) / "ctrld-sync" / "cache"
    # Linux, Unix, and others – follow XDG Base Directory spec
    xdg_cache = os.getenv("XDG_CACHE_HOME")
    if xdg_cache:
        return Path(xdg_cache) / "ctrld-sync"
    return Path.home() / ".cache" / "ctrld-sync"


def load_disk_cache() -> None:
    """Load the persistent blocklist cache from disk at startup.

    GRACEFUL DEGRADATION: Any error (corrupted JSON, missing file, permission
    denied, etc.) is logged but otherwise ignored – the sync continues with an
    empty cache.  This prevents crashes from a stale or corrupted cache file.

    The function mutates ``_disk_cache`` **in-place** (clear + update) rather
    than reassigning the module-level name so that all importers that hold a
    reference to the dict always see the live data.
    """
    try:
        cache_file = get_cache_dir() / "blocklists.json"
        if not cache_file.exists():
            log.debug("No existing cache file found, starting fresh")
            return

        with open(cache_file, encoding="utf-8") as f:
            data = json.load(f)

        # Validate cache structure at the top level.
        if not isinstance(data, dict):
            log.warning("Cache file has invalid format (root is not a dict), ignoring")
            return

        # Sanitize individual entries:  key must be str, value must be a dict
        # containing at least a 'data' field.  Drop anything malformed so that
        # a partly-corrupt cache never causes a crash downstream.
        sanitized_cache: dict[str, Any] = {}
        dropped_entries = 0

        for key, value in data.items():
            if not isinstance(key, str):
                dropped_entries += 1
                log.debug("Dropping cache entry with non-string key: %r", key)
                continue

            if not isinstance(value, dict):
                dropped_entries += 1
                log.debug("Dropping cache entry %r: value is not a dict", key)
                continue

            if "data" not in value:
                dropped_entries += 1
                log.debug("Dropping cache entry %r: missing required 'data' field", key)
                continue

            sanitized_cache[key] = value

        if not sanitized_cache:
            # Nothing valid – reset to empty and return.
            _disk_cache.clear()
            log.warning(
                "Cache file contained no valid entries; starting with empty cache"
            )
            return

        if dropped_entries:
            log.info(
                "Loaded %d valid entries from disk cache (dropped %d malformed entries)",
                len(sanitized_cache),
                dropped_entries,
            )
        else:
            log.info("Loaded %d entries from disk cache", len(sanitized_cache))

        # In-place update so all existing references to _disk_cache stay valid.
        _disk_cache.clear()
        _disk_cache.update(sanitized_cache)
    except json.JSONDecodeError as e:
        log.warning(
            f"Corrupted cache file (invalid JSON), starting fresh: {_sanitize_fn(e)}"
        )
        _cache_stats["errors"] += 1
    except PermissionError as e:
        log.warning(
            f"Cannot read cache file (permission denied), starting fresh: {_sanitize_fn(e)}"
        )
        _cache_stats["errors"] += 1
    except Exception as e:
        # Catch-all for unexpected errors (disk full, etc.)
        log.warning(f"Failed to load cache, starting fresh: {_sanitize_fn(e)}")
        _cache_stats["errors"] += 1


def save_disk_cache() -> None:
    """Flush the in-memory disk cache to disk after a successful sync.

    SECURITY: Creates the cache directory with user-only permissions (0o700)
    and the cache file with 0o600 to prevent other OS users from reading
    cached blocklist data.

    Writes atomically via a temp file + rename so that a process crash
    mid-write cannot leave a corrupted cache.
    """
    try:
        cache_dir = get_cache_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Set directory permissions to user-only (rwx------).
        if platform.system() != "Windows":
            cache_dir.chmod(0o700)

        cache_file = cache_dir / "blocklists.json"

        # Security: use tempfile.NamedTemporaryFile to securely create a unique temporary file
        # with O_CREAT | O_EXCL and 0o600 permissions, preventing predictable
        # temporary file vulnerabilities and TOCTOU races.
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                delete=False,
                prefix="blocklists.",
                suffix=".tmp",
                dir=str(cache_dir),
                encoding="utf-8",
            ) as f:
                temp_path = Path(f.name)
                json.dump(_disk_cache, f, indent=2)

            # POSIX guarantees rename is atomic.
            temp_path.replace(cache_file)
        finally:
            # Robust cleanup: Ensure temporary file is removed if it wasn't successfully renamed
            try:
                if temp_path and temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass

        if log.isEnabledFor(logging.DEBUG):
            log.debug(f"Saved {len(_disk_cache):,} entries to disk cache")

    except Exception as e:
        # Cache save failures are non-fatal; next run simply starts without cache.
        log.warning(f"Failed to save cache (non-fatal): {_sanitize_fn(e)}")
        _cache_stats["errors"] += 1
