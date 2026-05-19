#!/usr/bin/env python3
"""
Control D Sync
----------------------
A tiny helper that keeps your Control D folders in sync with a set of
remote block-lists.

It does three things:
1. Reads the folder names from the JSON files.
2. Deletes any existing folders with those names (so we start fresh).
3. Re-creates the folders and pushes all rules in batches.

Nothing fancy, just works.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import getpass
import ipaddress
import json
import logging
import os
import random
import re
import shutil
import socket
import stat
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, NotRequired, TypedDict, TypeGuard, cast

import httpx
import yaml
from dotenv import load_dotenv

import api_client
import cache
from api_client import (
    _CONNECT_ERROR_HINT,
    _SERVER_ERROR_HINT,
    _TIMEOUT_HINT,
    MAX_RETRIES,
    RETRY_DELAY,
    _api_delete,
    _api_get,
    _api_post,
    _api_post_form,
    _api_stats,
    _rate_limit_info,
    _rate_limit_lock,
)
from cache import (
    CACHE_TTL_SECONDS,
    _cache_stats,
    _disk_cache,
    get_cache_dir,
    load_disk_cache,
    save_disk_cache,
)


@dataclass(frozen=True)
class RuleAction:
    """Represents a rule action (do and status)."""

    do: int
    status: int


@dataclass
class SyncContext:
    """Context for syncing rules and folders."""

    profile_id: str
    client: httpx.Client
    existing_rules: set[str]
    batch_executor: concurrent.futures.Executor | None = None


# --------------------------------------------------------------------------- #
# TypedDicts – document the shapes of API response and plan objects
# --------------------------------------------------------------------------- #


class FolderAction(TypedDict, total=False):
    """The 'action' sub-object on a folder group or rule group.

    ``do`` controls the rule action type (0 = Block, 1 = Allow).
    ``status`` controls whether the rule is active (1 = enabled, 0 = disabled).
    """

    do: int
    status: int


class FolderGroup(TypedDict):
    """The 'group' object inside a folder JSON response."""

    group: str  # folder display name (required in valid data)
    PK: NotRequired[str]  # folder primary key
    action: NotRequired[FolderAction]


class RuleEntry(TypedDict, total=False):
    """A single rule entry inside a folder's rule list."""

    PK: str  # hostname / primary key
    host: str
    action: FolderAction


class RuleGroup(TypedDict, total=False):
    """A rule group (multi-action format) inside a folder JSON response."""

    rules: list[RuleEntry]
    action: FolderAction


class FolderData(TypedDict):
    """Root shape of the JSON object returned by the blocklist endpoint."""

    group: FolderGroup  # required in valid data
    rules: NotRequired[list[RuleEntry]]  # present in legacy single-action format
    rule_groups: NotRequired[list[RuleGroup]]  # present in multi-action format


class PlanRuleGroup(TypedDict):
    """Per-rule-group summary entry inside a dry-run plan folder."""

    rules: int
    action: int | None
    status: int | None


class PlanFolderEntry(TypedDict):
    """Per-folder summary entry inside a dry-run plan."""

    name: str
    rules: int
    action: NotRequired[int | None]  # single-action format
    status: NotRequired[int | None]  # single-action format
    rule_groups: NotRequired[list[PlanRuleGroup]]  # multi-action format


class PlanEntry(TypedDict):
    """Top-level dry-run plan entry for one profile."""

    profile: str
    folders: list[PlanFolderEntry]


class SyncResult(TypedDict):
    """Per-profile result recorded after a sync run."""

    profile: str
    folders: int
    rules: int
    status_label: str
    success: bool
    duration: float


# --------------------------------------------------------------------------- #
# 0. Bootstrap – load secrets and configure logging
# --------------------------------------------------------------------------- #
# SECURITY: load_dotenv() moved to main() to ensure permissions are checked first

# Respect NO_COLOR standard (https://no-color.org/)
if os.getenv("NO_COLOR"):
    USE_COLORS = False
else:
    USE_COLORS = sys.stderr.isatty() and sys.stdout.isatty()

# Evaluate JSON_LOG immediately so USE_COLORS is finalized
# BEFORE the Colors and Box classes are defined.
_use_json_log: bool = bool(os.getenv("JSON_LOG"))
if _use_json_log:
    USE_COLORS = False


class Colors:
    if USE_COLORS:
        HEADER = "\033[95m"
        BLUE = "\033[94m"
        CYAN = "\033[96m"
        GREEN = "\033[92m"
        WARNING = "\033[93m"
        FAIL = "\033[91m"
        ENDC = "\033[0m"
        BOLD = "\033[1m"
        UNDERLINE = "\033[4m"
        DIM = "\033[2m"
    else:
        HEADER = ""
        BLUE = ""
        CYAN = ""
        GREEN = ""
        WARNING = ""
        FAIL = ""
        ENDC = ""
        BOLD = ""
        UNDERLINE = ""
        DIM = ""


class Box:
    """Box drawing characters for pretty tables."""

    if USE_COLORS:
        H, V, TL, TR, BL, BR, T, B, L, R, X = (
            "─",
            "│",
            "┌",
            "┐",
            "└",
            "┘",
            "┬",
            "┴",
            "├",
            "┤",
            "┼",
        )
    else:
        H, V, TL, TR, BL, BR, T, B, L, R, X = (
            "-",
            "|",
            "+",
            "+",
            "+",
            "+",
            "+",
            "+",
            "+",
            "+",
            "+",
        )


class ColoredFormatter(logging.Formatter):
    """Custom formatter to add colors to log levels."""

    LEVEL_COLORS = {
        logging.DEBUG: Colors.BLUE,
        logging.INFO: Colors.CYAN,
        logging.WARNING: Colors.WARNING,
        logging.ERROR: Colors.FAIL,
        logging.CRITICAL: Colors.FAIL + Colors.BOLD,
    }

    def __init__(self, fmt=None, datefmt=None, style="%", validate=True):
        super().__init__(fmt, datefmt, style, validate)
        self.delegate_formatter = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S"
        )

    def format(self, record):
        original_levelname = record.levelname
        color = self.LEVEL_COLORS.get(record.levelno, Colors.ENDC)
        padded_level = f"{original_levelname:<8}"
        record.levelname = f"{color}{padded_level}{Colors.ENDC}"
        result = self.delegate_formatter.format(record)
        record.levelname = original_levelname
        return result


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record for structured/observability pipelines.

    Activated by setting the ``JSON_LOG`` environment variable to a non-empty
    value (e.g. ``JSON_LOG=1``).  When active, ``USE_COLORS`` is also disabled
    so that ANSI escape codes never pollute the JSON stream.

    Each line contains at minimum:
        ``time``    – ISO-8601 timestamp (UTC, second precision)
        ``level``   – log level name (DEBUG / INFO / WARNING / ERROR / CRITICAL)
        ``logger``  – logger name
        ``message`` – formatted log message
    """

    @staticmethod
    def converter(
        t: float | None,
    ) -> time.struct_time:  # ensure timestamps are always UTC
        return time.gmtime(t)

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, str] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            # Mirror stdlib logging.Formatter behavior:
            # cache the formatted exception in record.exc_text so that
            # other formatters/handlers don't need to reformat it.
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
            if record.exc_text:
                payload["exc"] = record.exc_text
        return json.dumps(payload)


handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter() if _use_json_log else ColoredFormatter())
logging.basicConfig(level=logging.INFO, handlers=[handler])
logging.getLogger("httpx").setLevel(logging.WARNING)


class AlertSystem:
    """Handles async enqueue callbacks and structured error logging.

    Attaches to ``concurrent.futures.Future`` objects via
    ``add_done_callback`` so that errors surfacing inside worker threads are
    captured and logged in a single, consistent place.

    **Architectural role:** Rather than scattering ``try/except`` blocks
    around every ``executor.submit()`` call, callers register a single
    ``AlertSystem`` callback on each future.  This centralises error
    observability and makes it easy to extend (e.g. add metrics, alerts, or
    structured logging) without touching every call site.

    Usage::

        system = AlertSystem()
        fut = executor.submit(some_task)
        fut.add_done_callback(system._on_enqueue_done)
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        # Allow callers (and tests) to inject a custom logger; fall back to the
        # module-level logger so production behaviour stays unchanged.
        # Use the same named logger as the rest of this module to keep logs
        # consistent and to honour the "module-level logger" contract.
        self.logger = logger or logging.getLogger("control-d-sync")

    def _on_enqueue_done(
        self,
        future: concurrent.futures.Future[
            Any
        ],  # Accept futures of any return type; we only inspect exceptions
    ) -> None:
        """Callback invoked when an enqueue future completes.

        Three code paths ("branches") are handled here:

        * **Branch A** – ``future.exception()`` returns ``None``: normal
          completion; nothing extra is logged.
        * **Branch B** – ``future.exception()`` returns a non-``None``
          exception object: we log this as an error and pass the exception
          instance as ``exc_info`` so that the full traceback is preserved and
          log handlers (and tests) can inspect the real error.
        * **Branch C** – ``future.exception()`` itself raises (e.g. the future
          was cancelled before we could inspect it): we catch *that* secondary
          exception and log it, again passing the actual exception instance as
          ``exc_info`` so that the full traceback is preserved and callers can
          programmatically inspect the real error.
        """
        try:
            exc = future.exception()
            if exc is not None:
                # We are *not* in an ``except`` block here, so there is no
                # active exception for logging to pull from ``sys.exc_info()``.
                # Construct the (type, value, traceback) tuple explicitly so the
                # original worker-thread traceback is preserved.
                self.logger.error(
                    "Enqueued task raised an exception",
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
        except Exception:
            # Here we *are* in an ``except`` context, so logging can safely use
            # the current exception from ``sys.exc_info()``. Using
            # ``exc_info=True`` is the idiomatic way to log this traceback.
            self.logger.error(
                "Unexpected error while inspecting enqueue future",
                exc_info=True,
            )


def check_env_permissions(env_path: str = ".env") -> None:
    """
    Check .env file permissions and auto-fix if readable by others.

    Security: Automatically sets permissions to 600 (owner read/write only)
    if the file is world-readable. This prevents other users on the system
    from stealing secrets stored in .env files.

    Args:
        env_path: Path to the .env file to check (default: ".env")
    """
    if not os.path.exists(env_path):
        return

    # Security: Don't follow symlinks when checking/fixing permissions
    # This prevents attacks where .env is symlinked to a system file (e.g., /etc/passwd)
    if os.path.islink(env_path):
        sys.stderr.write(
            f"{Colors.WARNING}⚠️  Security Warning: {env_path} is a symlink. "
            f"Skipping permission fix to avoid damaging target file.{Colors.ENDC}\n"
        )
        return

    # Windows doesn't have Unix permissions
    if os.name == "nt":
        # Just warn on Windows, can't auto-fix
        sys.stderr.write(
            f"{Colors.WARNING}⚠️  Security Warning: "
            f"Please ensure {env_path} is only readable by you.{Colors.ENDC}\n"
        )
        return

    try:
        # Security: Use low-level file descriptor operations to avoid TOCTOU (Time-of-Check Time-of-Use)
        # race conditions. We open the file with O_NOFOLLOW to ensure we don't follow symlinks.
        fd = os.open(env_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            file_stat = os.fstat(fd)
            # Check if group or others have any permission
            if file_stat.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                perms = format(stat.S_IMODE(file_stat.st_mode), "03o")

                # Auto-fix: Set to 600 (owner read/write only) using fchmod on the open descriptor
                try:
                    os.fchmod(fd, 0o600)
                    sys.stderr.write(
                        f"{Colors.GREEN}✓ Fixed {env_path} permissions "
                        f"(was {perms}, now set to 600){Colors.ENDC}\n"
                    )
                except OSError as fix_error:
                    # Auto-fix failed, show warning with instructions
                    sys.stderr.write(
                        f"{Colors.WARNING}⚠️  Security Warning: {env_path} is "
                        f"readable by others ({perms})! Auto-fix failed: {fix_error}. "
                        f"Please run: chmod 600 {env_path}{Colors.ENDC}\n"
                    )
        finally:
            os.close(fd)
    except OSError as error:
        # More specific exception type as suggested by bot review
        exception_type = type(error).__name__
        sys.stderr.write(
            f"{Colors.WARNING}⚠️  Security Warning: Could not check {env_path} "
            f"permissions ({exception_type}: {error}){Colors.ENDC}\n"
        )


# SECURITY: Check .env permissions will be called in main() to avoid side effects at import time
log = logging.getLogger("control-d-sync")

# --------------------------------------------------------------------------- #
# 1. Constants – tweak only here
# --------------------------------------------------------------------------- #
API_BASE = "https://api.controld.com/profiles"
USER_AGENT = "Control-D-Sync/0.1.0"

EMPTY_INPUT_HINT = (
    "   💡 Hint: Please type a value and press Enter, or press Ctrl+C/Ctrl+D to cancel."
)
INVALID_INPUT_HINT = "   💡 Hint: Please check your input and try again, or press Ctrl+C/Ctrl+D to cancel."

# Pre-compiled regex patterns for hot-path validation (>2x speedup on 10k+ items)
PROFILE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
_PROFILE_URL_PATTERN = re.compile(r"controld\.com/dashboard/profiles/([^/?#\s]+)")
# Folder IDs (PK) are typically alphanumeric but can contain other safe chars.
# We whitelist to prevent path traversal and injection.
FOLDER_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]+$")

_ALLOWED_RULE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_:*/@"
)

# Parallel processing configuration
DELETE_WORKERS = 3  # Conservative for DELETE operations due to rate limits

# Security: Dangerous characters for folder names
# XSS and HTML injection characters
_DANGEROUS_FOLDER_CHARS = set("<>\"'`")
# Path separators (prevent confusion and directory traversal attempts)
_DANGEROUS_FOLDER_CHARS.update(["/", "\\"])

# Security: Input length limits
MAX_FOLDER_NAME_LENGTH = 64
MAX_RULE_LENGTH = 255
MAX_PROFILE_ID_LENGTH = 64
MAX_FOLDER_ID_LENGTH = 64
MAX_URL_LENGTH = 2048
MAX_HOSTNAME_LENGTH = 253
# In constants section
DEFAULT_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
# Security: Unicode Bidi control characters (prevent RTLO/homograph attacks)
# These characters can be used to mislead users about file extensions or content
# See: https://en.wikipedia.org/wiki/Right-to-left_override
_BIDI_CONTROL_CHARS = {
    "\u202a",  # LEFT-TO-RIGHT EMBEDDING (LRE)
    "\u202b",  # RIGHT-TO-LEFT EMBEDDING (RLE)
    "\u202c",  # POP DIRECTIONAL FORMATTING (PDF)
    "\u202d",  # LEFT-TO-RIGHT OVERRIDE (LRO)
    "\u202e",  # RIGHT-TO-LEFT OVERRIDE (RLO) - primary attack vector
    "\u2066",  # LEFT-TO-RIGHT ISOLATE (LRI)
    "\u2067",  # RIGHT-TO-LEFT ISOLATE (RLI)
    "\u2068",  # FIRST STRONG ISOLATE (FSI)
    "\u2069",  # POP DIRECTIONAL ISOLATE (PDI)
    "\u200e",  # LEFT-TO-RIGHT MARK (LRM) - defense in depth
    "\u200f",  # RIGHT-TO-LEFT MARK (RLM) - defense in depth
}

# Pre-combine forbidden character sets for fast O(N) validation in is_valid_folder_name
_ALL_FORBIDDEN_FOLDER_CHARS = frozenset(_DANGEROUS_FOLDER_CHARS | _BIDI_CONTROL_CHARS)
_UNSAFE_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# Pre-compiled patterns for log sanitization
_BASIC_AUTH_PATTERN = re.compile(r"://[^/@]+@")
_SENSITIVE_PARAM_PATTERN = re.compile(
    r"([?&#])(token|key|secret|password|auth|access_token|api_key|authorization)=[^&#\s]*",
    flags=re.IGNORECASE,
)


def sanitize_for_log(text: Any) -> str:
    """Sanitize text for logging.

    Redacts:
    - TOKEN values
    - Basic Auth credentials in URLs (e.g. https://user:pass@host)
    - Sensitive query parameters (token, key, secret, password, auth, access_token, api_key)
    - Control characters (prevents log injection and terminal hijacking)
    """
    s = str(text)
    if TOKEN and TOKEN in s:
        s = s.replace(TOKEN, "[REDACTED]")

    # Redact Basic Auth in URLs (e.g. https://user:pass@host)
    # Optimization: Check for '://' before running expensive regex substitution
    if "://" in s:
        s = _BASIC_AUTH_PATTERN.sub("://[REDACTED]@", s)

    # Redact sensitive query parameters (handles ?, &, and # separators)
    # Optimization: Check for delimiters before running expensive regex substitution
    if "?" in s or "&" in s or "#" in s:
        s = _SENSITIVE_PARAM_PATTERN.sub(r"\1\2=[REDACTED]", s)

    # repr() safely escapes control characters (e.g., \n -> \\n, \x1b -> \\x1b)
    # This prevents log injection and terminal hijacking.
    safe = repr(s)

    # Security: Prevent CSV Injection (Formula Injection)
    # If the string starts with =, +, -, or @, we keep the quotes from repr()
    # to force spreadsheet software to treat it as a string literal.
    if s and s.startswith(("=", "+", "-", "@")):
        return safe

    if len(safe) >= 2 and safe[0] == safe[-1] and safe[0] in ("'", '"'):
        return safe[1:-1]
    return safe


# Wire the token-aware sanitizer into api_client so that _retry_request
# redacts tokens from log messages without creating a circular import.
api_client._sanitize_fn = sanitize_for_log
# Wire the same sanitizer into cache so that load/save error messages also
# get full token redaction, consistent with the api_client pattern.
cache._sanitize_fn = sanitize_for_log


def pluralize(count: int, singular: str, plural: str | None = None) -> str:
    """Helper to cleanly pluralize nouns based on count."""
    if plural is None:
        plural = f"{singular}s"
    return singular if count == 1 else plural


def print_plan_details(plan_entry: PlanEntry) -> None:
    """Pretty-print the folder-level breakdown during a dry-run."""
    profile = sanitize_for_log(plan_entry.get("profile", "unknown"))
    if profile == "dry-run-placeholder":
        profile = "(Unspecified)"
    folders = plan_entry.get("folders", [])

    if USE_COLORS:
        print(f"\n{Colors.HEADER}📝 Plan Details for {profile}:{Colors.ENDC}")
    else:
        print(f"\n📝 Plan Details for {profile}:")

    if not folders:
        if USE_COLORS:
            print(f"  {Colors.WARNING}⚠️  No folders to sync.{Colors.ENDC}")
        else:
            print("  ⚠️  No folders to sync.")
        _print_hint(
            "  💡 Hint: Add folder URLs using --folder-url or in your config.yaml"
        )
        return

    # Calculate max width for alignment
    max_name_len = max(
        # Use the same default ("Unknown") as when printing, so alignment is accurate
        (len(sanitize_for_log(f.get("name", "Unknown"))) for f in folders),
        default=0,
    )
    max_rules_len = max((len(f"{f.get('rules', 0):,}") for f in folders), default=0)

    for folder in sorted(folders, key=lambda f: f.get("name", "Unknown")):
        name = sanitize_for_log(folder.get("name", "Unknown"))
        rules_count = folder.get("rules", 0)
        formatted_rules = f"{rules_count:,}"

        # Determine action (Block/Allow)
        action_text = ""
        action_color = ""
        action_label = ""

        # Check for multiple rule groups first
        if "rule_groups" in folder and folder["rule_groups"]:
            actions = {rg.get("action") for rg in folder["rule_groups"]}
            if len(actions) > 1:
                action_label = "Mixed"
                action_color = Colors.WARNING
                action_text = (
                    f"({action_color}⚠️  {action_label}{Colors.ENDC})"
                    if USE_COLORS
                    else f"(⚠️  {action_label})"
                )
            else:
                # All groups have same action
                action_val = next(iter(actions))
                if action_val == 0:
                    action_label = "Block"
                    action_color = Colors.FAIL
                    action_text = (
                        f"({action_color}⛔ {action_label}{Colors.ENDC})"
                        if USE_COLORS
                        else f"(⛔ {action_label})"
                    )
                elif action_val == 1:
                    action_label = "Allow"
                    action_color = Colors.GREEN
                    action_text = (
                        f"({action_color}✅ {action_label}{Colors.ENDC})"
                        if USE_COLORS
                        else f"(✅ {action_label})"
                    )

        # Fallback to single action if not set
        if not action_text and "action" in folder:
            action_val = folder["action"]
            if action_val == 0:
                action_label = "Block"
                action_color = Colors.FAIL
                action_text = (
                    f"({action_color}⛔ {action_label}{Colors.ENDC})"
                    if USE_COLORS
                    else f"(⛔ {action_label})"
                )
            elif action_val == 1:
                action_label = "Allow"
                action_color = Colors.GREEN
                action_text = (
                    f"({action_color}✅ {action_label}{Colors.ENDC})"
                    if USE_COLORS
                    else f"(✅ {action_label})"
                )

        # If action is still completely missing/unknown, default to Block (Default) for clearer UX
        if not action_text:
            action_label = "Block (Default)"
            action_color = Colors.FAIL
            action_text = (
                f"({action_color}⛔ {action_label}{Colors.ENDC})"
                if USE_COLORS
                else f"(⛔ {action_label})"
            )

        if USE_COLORS:
            print(
                f"  • {Colors.BOLD}{name:<{max_name_len}}{Colors.ENDC} : {formatted_rules:>{max_rules_len}} {pluralize(rules_count, 'rule'):<5} {action_text}"
            )
        else:
            print(
                f"  - {name:<{max_name_len}} : {formatted_rules:>{max_rules_len}} {pluralize(rules_count, 'rule'):<5} {action_text}"
            )

    print("")


def _get_progress_bar_width() -> int:
    """Calculate dynamic progress bar width based on terminal size.

    Returns width clamped between 15 and 50 characters, approximately
    40% of terminal width. This ensures progress bars are readable on
    narrow terminals while utilizing space on wider displays.
    """
    cols, _ = shutil.get_terminal_size(fallback=(80, 24))
    return max(15, min(50, int(cols * 0.4)))


def countdown_timer(seconds: int, message: str = "Waiting") -> None:
    """Show a countdown in interactive/color mode; in no-color/non-interactive
    mode, sleep silently for short waits and log periodic heartbeat messages
    for longer waits."""
    if not USE_COLORS:
        # UX Improvement: For long waits in non-interactive/no-color mode (e.g. CI),
        # log periodic updates instead of sleeping silently.
        if seconds > 10:
            step = 10
            for remaining in range(seconds, 0, -step):
                # Don't log the first one if we already logged "Waiting..." before calling this
                if remaining < seconds:
                    log.info(f"{sanitize_for_log(message)}: {remaining}s remaining...")

                sleep_time = min(step, remaining)
                time.sleep(sleep_time)
            log.info(f"✅ {sanitize_for_log(message)}: Done!")
            return

        time.sleep(seconds)
        log.info(f"✅ {sanitize_for_log(message)}: Done!")
        return

    width = _get_progress_bar_width()
    max_len = len(str(seconds))

    for remaining in range(seconds, 0, -1):
        progress = (seconds - remaining + 1) / seconds
        filled = int(width * progress)
        bar = "█" * filled + "·" * (width - filled)
        sys.stderr.write(
            f"\r\033[K{Colors.CYAN}⏳ {message}: [{bar}] {remaining:>{max_len}}s...{Colors.ENDC}"
        )
        sys.stderr.flush()
        time.sleep(1)

    sys.stderr.write(f"\r\033[K{Colors.GREEN}✅ {message}: Done!{Colors.ENDC}\n")
    sys.stderr.flush()


def render_progress_bar(
    current: int, total: int, label: str, prefix: str = "🚀"
) -> None:
    """Renders a progress bar to stderr if USE_COLORS is True."""
    if not USE_COLORS or total == 0:
        return

    width = _get_progress_bar_width()

    progress = min(1.0, current / total)
    filled = int(width * progress)
    bar = "█" * filled + "·" * (width - filled)
    percent = int(progress * 100)

    total_str = str(total)

    # Use \033[K to clear line residue
    sys.stderr.write(
        f"\r\033[K{Colors.CYAN}{prefix} {label}: [{bar}] {percent:>3}% ({current:>{len(total_str)}}/{total_str}){Colors.ENDC}"
    )
    sys.stderr.flush()


def _clean_env_kv(value: str | None, key: str) -> str | None:
    """Allow TOKEN/PROFILE values to be provided as either raw values or KEY=value."""
    if not value:
        return value
    v = value.strip()
    if "=" in v:
        k, val = v.split("=", 1)
        if k.strip() == key:
            # String splitting is used here as it's significantly faster than regex for basic KV parsing
            # Emulate regex behavior: only return if value is not empty (.+ match)
            val_stripped = val.strip()
            if val_stripped:
                return val_stripped
    return v


def _print_hint(hint: str) -> None:
    """Helper to cleanly print input hints while respecting USE_COLORS to reduce cyclomatic complexity."""
    if USE_COLORS:
        print(f"{Colors.DIM}{hint}{Colors.ENDC}")
    else:
        print(hint)


def get_validated_input(
    prompt: str,
    validator: Callable[[str], bool],
    error_msg: str,
) -> str:
    """Prompts for input until the validator returns True."""
    if not prompt.endswith(" "):
        prompt += " "

    while True:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
            value = input(prompt).strip()
        except (KeyboardInterrupt, EOFError):
            print(f"\n{Colors.WARNING}⚠️  Input cancelled.{Colors.ENDC}")
            sys.exit(130)

        if not value:
            print(f"{Colors.FAIL}❌ Value cannot be empty{Colors.ENDC}")
            _print_hint(EMPTY_INPUT_HINT)
            continue

        if validator(value):
            return value

        print(f"{Colors.FAIL}❌ {error_msg}{Colors.ENDC}")
        _print_hint(INVALID_INPUT_HINT)


def get_password(
    prompt: str,
    validator: Callable[[str], bool],
    error_msg: str,
) -> str:
    """Prompts for password input until the validator returns True.

    If the prompt does not already advertise that input is hidden, append a
    "(typing will be hidden)" hint so a screen-reader or fresh user knows
    why characters do not echo. Callers that want to render the hint with
    their own styling (e.g. dimmed colors at a specific position) can opt
    out by including the literal substring "(typing will be hidden)" in
    the prompt they pass.
    """
    if "(typing will be hidden)" not in prompt:
        prompt = f"{prompt.rstrip()} (typing will be hidden) "
    if not prompt.endswith(" "):
        prompt += " "

    while True:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
            value = getpass.getpass(prompt).strip()
        except (KeyboardInterrupt, EOFError):
            print(f"\n{Colors.WARNING}⚠️  Input cancelled.{Colors.ENDC}")
            sys.exit(130)

        if not value:
            print(f"{Colors.FAIL}❌ Value cannot be empty{Colors.ENDC}")
            _print_hint(EMPTY_INPUT_HINT)
            continue

        if validator(value):
            return value

        print(f"{Colors.FAIL}❌ {error_msg}{Colors.ENDC}")
        _print_hint(INVALID_INPUT_HINT)


TOKEN = _clean_env_kv(os.getenv("TOKEN"), "TOKEN")

# Default folder sources
DEFAULT_FOLDER_URLS = [
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/apple-private-relay-allow-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/badware-hoster-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/meta-tracker-allow-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/microsoft-allow-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/native-tracker-amazon-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/native-tracker-apple-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/native-tracker-huawei-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/native-tracker-lgwebos-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/native-tracker-microsoft-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/native-tracker-oppo-realme-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/native-tracker-samsung-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/native-tracker-tiktok-aggressive-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/native-tracker-tiktok-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/native-tracker-vivo-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/native-tracker-xiaomi-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/nosafesearch-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/referral-allow-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/spam-idns-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/spam-tlds-allow-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/spam-tlds-combined-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/spam-tlds-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/ultimate-known_issues-allow-folder.json",
    "https://raw.githubusercontent.com/yokoffing/Control-D-Config/main/folders/potentially-malicious-ips.json",
]

BATCH_SIZE = 500
BATCH_KEYS = [f"hostnames[{i}]" for i in range(BATCH_SIZE)]
# MAX_RETRIES, RETRY_DELAY, MAX_RETRY_DELAY imported from api_client above
FOLDER_CREATION_DELAY = 5  # <--- CHANGED: Increased from 2 to 5 for patience
MAX_RESPONSE_SIZE = 10 * 1024 * 1024  # 10MB limit

# Maps common HTTP status codes to actionable operator guidance surfaced in error messages.
# 4xx hints (401, 403, 404) are sourced from api_client._4XX_HINTS to ensure a single
# source of truth — updating the hint text in api_client automatically propagates here.
_STATUS_HINTS: dict[int, str] = {
    **api_client._4XX_HINTS,  # single source of truth for 401, 403, 404
    429: "Rate limited — the sync will retry automatically with backoff.",
    500: _SERVER_ERROR_HINT,
}

# _TIMEOUT_HINT imported from api_client above

# Default config search paths (highest to lowest precedence after CLI flag)
_DEFAULT_CONFIG_PATHS = [
    "config.yaml",
    "config.yml",
    "~/.ctrld-sync/config.yaml",
    "~/.ctrld-sync/config.yml",
]


def get_default_config() -> dict:
    """Return the built-in default configuration (mirrors DEFAULT_FOLDER_URLS)."""
    return {
        "folders": [{"url": u} for u in DEFAULT_FOLDER_URLS],
        "settings": {
            "batch_size": BATCH_SIZE,
            "delete_workers": 3,
            "max_retries": MAX_RETRIES,
        },
    }


def _validate_config(config: dict) -> None:
    """
    Validate a loaded configuration dict and raise ValueError on the first problem.

    Checks:
    - 'folders' key exists and is a non-empty list
    - Each folder entry has a 'url' string (name and action are optional)
    - All URLs are https://
    - 'action' values, if present, are 'block' or 'allow'
    - Settings values, if present, are positive integers
    """
    if "folders" not in config:
        raise ValueError("Configuration is missing the required 'folders' key.")

    folders = config["folders"]
    if not isinstance(folders, list) or not folders:
        raise ValueError("'folders' must be a non-empty list.")

    for i, entry in enumerate(folders):
        if not isinstance(entry, dict):
            raise ValueError(
                f"folders[{i}] must be a mapping, got {type(entry).__name__}."
            )
        url = entry.get("url", "")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ValueError(
                f"folders[{i}]: 'url' must be an https:// string (got {url!r})."
            )
        name = entry.get("name", "")
        if name and (not isinstance(name, str) or not name.strip()):
            raise ValueError(f"folders[{i}]: 'name' must be a non-empty string.")
        action = entry.get("action")
        if action is not None and action not in ("block", "allow"):
            raise ValueError(
                f"folders[{i}]: 'action' must be 'block' or 'allow' (got {action!r})."
            )

    settings = config.get("settings", {})
    if not isinstance(settings, dict):
        raise ValueError("'settings' must be a mapping.")
    for key in ("batch_size", "delete_workers", "max_retries"):
        val = settings.get(key)
        if val is not None and (not isinstance(val, int) or val <= 0):
            raise ValueError(
                f"settings.{key} must be a positive integer (got {val!r})."
            )


def load_config(config_path: str | None = None) -> dict:
    """
    Load and validate configuration from a YAML file.

    Resolution order (first found wins):
    1. Explicit *config_path* argument (e.g. from --config CLI flag)
    2. config.yaml / config.yml in the current working directory
    3. ~/.ctrld-sync/config.yaml / ~/.ctrld-sync/config.yml
    4. Built-in defaults (get_default_config())

    Raises SystemExit on invalid YAML or schema violations so the operator
    sees a clear error message rather than a cryptic traceback.
    """

    paths_to_try: list[str] = (
        [config_path] if config_path else list(_DEFAULT_CONFIG_PATHS)
    )

    for raw_path in paths_to_try:
        p = Path(raw_path).expanduser()
        if not p.exists():
            continue
        try:
            # Opening the file can fail with OSError (e.g. permission denied, is a directory),
            # so we handle it here to avoid an unhelpful traceback.
            with open(p, encoding="utf-8") as fh:
                # Parsing YAML can raise yaml.YAMLError for malformed configuration.
                loaded = yaml.safe_load(fh)
        except OSError as exc:
            print(
                f"{Colors.FAIL}✗ Failed to read configuration file {p}: {exc}{Colors.ENDC}",
                file=sys.stderr,
            )
            sys.exit(1)
        except yaml.YAMLError as exc:
            print(
                f"{Colors.FAIL}✗ Invalid YAML in {p}: {exc}{Colors.ENDC}",
                file=sys.stderr,
            )
            sys.exit(1)

        if loaded is None:
            print(
                f"{Colors.FAIL}✗ Configuration file {p} is empty.{Colors.ENDC}",
                file=sys.stderr,
            )
            sys.exit(1)

        try:
            _validate_config(loaded)
        except ValueError as exc:
            print(
                f"{Colors.FAIL}✗ Configuration error in {p}: {exc}{Colors.ENDC}",
                file=sys.stderr,
            )
            sys.exit(1)

        log.info("Loaded configuration from %s", p)
        return cast(dict, loaded)

    if config_path:
        # Explicit path was given but not found — this is always an error
        print(
            f"{Colors.FAIL}✗ Config file not found: {config_path}{Colors.ENDC}",
            file=sys.stderr,
        )
        sys.exit(1)

    # No config file found; use built-in defaults silently
    return get_default_config()


# --------------------------------------------------------------------------- #
# 2. Clients (configured with secure defaults)
# --------------------------------------------------------------------------- #
def _api_client() -> httpx.Client:
    return httpx.Client(
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {TOKEN}",
            "User-Agent": USER_AGENT,
        },
        # SECURITY: Explicit timeouts prevent resource exhaustion/DoS via Slowloris
        timeout=httpx.Timeout(10.0, connect=5.0),
        follow_redirects=False,
    )


_gh = httpx.Client(
    headers={"User-Agent": USER_AGENT},
    # SECURITY: Explicit timeouts prevent resource exhaustion/DoS via Slowloris
    timeout=httpx.Timeout(10.0, connect=5.0),
    follow_redirects=False,
)
MAX_RESPONSE_SIZE = 10 * 1024 * 1024  # 10 MB limit for external resources

# --------------------------------------------------------------------------- #
# 3. Helpers
# --------------------------------------------------------------------------- #
_cache: dict[str, dict] = {}
# Use RLock (reentrant lock) to allow nested acquisitions by the same thread
# This prevents deadlocks when _fetch_if_valid calls fetch_folder_data which calls _gh_get
_cache_lock = threading.RLock()

# --------------------------------------------------------------------------- #
# 3a. Persistent Disk Cache Support  (implementation lives in cache.py)
# --------------------------------------------------------------------------- #

# _api_stats imported from api_client above

# --------------------------------------------------------------------------- #
# 3b. Rate Limit Tracking
# --------------------------------------------------------------------------- #
# _rate_limit_info, _rate_limit_lock imported from api_client above


# _parse_rate_limit_headers imported from api_client above

_CGNAT_NETWORK = ipaddress.IPv4Network("100.64.0.0/10")


def _is_safe_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Rejects non-global, reserved, link-local, loopback, multicast, unspecified, and IPv4 CGNAT addresses."""
    if ip.is_multicast:
        return False
    if ip.is_unspecified:
        return False
    if ip.is_loopback:
        return False
    if ip.is_private:
        return False
    if ip.is_link_local:
        return False
    if ip.is_reserved:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return _is_safe_ip(ip.ipv4_mapped)
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_NETWORK:
        return False
    return ip.is_global


@lru_cache(maxsize=128)
def validate_hostname(hostname: str) -> bool:
    """
    Validates a hostname (DNS resolution and IP checks).
    Cached to prevent redundant DNS lookups for the same host across different URLs.
    """
    if len(hostname) > MAX_HOSTNAME_LENGTH:
        log.warning(
            f"Skipping unsafe hostname (exceeds {MAX_HOSTNAME_LENGTH} chars): {sanitize_for_log(hostname)}"
        )
        return False

    # Check for potentially malicious hostnames
    if hostname.lower() in _UNSAFE_HOSTS:
        log.warning(
            f"Skipping unsafe hostname (localhost detected): {sanitize_for_log(hostname)}"
        )
        return False

    try:
        ip = ipaddress.ip_address(hostname)
        if not _is_safe_ip(ip):
            log.warning(f"Skipping unsafe IP: {sanitize_for_log(hostname)}")
            return False
        return True
    except ValueError:
        # Not an IP literal, it's a domain. Resolve and check IPs.
        try:
            # Resolve hostname to IPs (IPv4 and IPv6)
            # We filter for AF_INET/AF_INET6 to ensure we get IP addresses
            addr_info = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
            for res in addr_info:
                # res is (family, type, proto, canonname, sockaddr)
                # sockaddr is (address, port) for AF_INET/AF_INET6
                ip_str = res[4][0]
                ip = ipaddress.ip_address(ip_str)
                if not _is_safe_ip(ip):
                    log.warning(
                        f"Skipping unsafe hostname {sanitize_for_log(hostname)} (resolves to non-global/multicast IP {ip})"
                    )
                    return False
            return True
        except (socket.gaierror, ValueError, OSError) as e:
            log.warning(
                f"Failed to resolve/validate domain {sanitize_for_log(hostname)}: {sanitize_for_log(e)}"
            )
            return False


@lru_cache(maxsize=128)
def validate_folder_url(url: str) -> bool:
    """
    Validates a folder URL.
    Cached to avoid repeated URL parsing for the same URL.
    """
    if len(url) > MAX_URL_LENGTH:
        log.warning(
            f"Skipping unsafe URL (exceeds {MAX_URL_LENGTH} chars): {sanitize_for_log(url)}"
        )
        return False

    if not url.startswith("https://"):
        log.warning(
            f"Skipping unsafe or invalid URL (must be https): {sanitize_for_log(url)}"
        )
        return False

    try:
        parsed = httpx.URL(url)
        hostname = parsed.host
        if not hostname:
            return False

        return validate_hostname(hostname)

    except Exception as e:
        log.warning(
            f"Failed to validate URL {sanitize_for_log(url)}: {sanitize_for_log(e)}"
        )
        return False


def extract_profile_id(text: str) -> str:
    """
    Extracts the Profile ID from a Control D URL if present,
    otherwise returns the text as-is (cleaned).
    """
    if not text:
        return ""
    text = text.strip()
    # Pattern for Control D Dashboard URLs
    # e.g. https://controld.com/dashboard/profiles/12345abc/filters
    match = _PROFILE_URL_PATTERN.search(text)
    if match:
        return match.group(1)
    return text


def is_valid_profile_id_format(profile_id: str) -> bool:
    """
    Checks if a profile ID matches the expected format.

    Validates against PROFILE_ID_PATTERN and enforces maximum length of 64 characters.
    """
    if "\x00" in profile_id:
        return False

    if len(profile_id) > MAX_PROFILE_ID_LENGTH:
        return False

    return bool(PROFILE_ID_PATTERN.match(profile_id))


def validate_profile_id(profile_id: str, log_errors: bool = True) -> bool:
    """
    Validates a Control D profile ID with optional error logging.

    Returns True if profile ID is valid, False otherwise.
    Logs specific validation errors when log_errors=True.
    """
    if is_valid_profile_id_format(profile_id):
        return True

    if not PROFILE_ID_PATTERN.match(profile_id):
        return _log_validation_error(
            "Invalid profile ID format (contains unsafe characters)", log_errors
        )

    if len(profile_id) > MAX_PROFILE_ID_LENGTH:
        return _log_validation_error(
            f"Invalid profile ID length (max {MAX_PROFILE_ID_LENGTH} chars)", log_errors
        )

    return False


def _log_validation_error(msg: str, log_errors: bool) -> bool:
    """Helper to conditionally log validation errors and return False."""
    if log_errors:
        log.error(msg)
    return False


def validate_folder_id(folder_id: str, log_errors: bool = True) -> bool:
    """Validates folder ID (PK) format to prevent path traversal."""
    if not folder_id:
        return False

    if len(folder_id) > MAX_FOLDER_ID_LENGTH:
        msg = f"Invalid folder ID length (max {MAX_FOLDER_ID_LENGTH} chars): {sanitize_for_log(folder_id)}"
        return _log_validation_error(msg, log_errors)

    if "\x00" in folder_id:
        msg = f"Invalid folder ID format (null byte): {sanitize_for_log(folder_id)}"
        return _log_validation_error(msg, log_errors)

    is_path_traversal = folder_id in (".", "..")
    is_invalid_format = not FOLDER_ID_PATTERN.match(folder_id)

    if is_path_traversal or is_invalid_format:
        msg = f"Invalid folder ID format: {sanitize_for_log(folder_id)}"
        return _log_validation_error(msg, log_errors)

    return True


def is_valid_rule(rule: str) -> bool:
    """
    Validates that a rule is safe to use.
    Enforces a strict whitelist of allowed characters.
    Allowed: Alphanumeric, hyphen, dot, underscore, asterisk, colon (IPv6), slash (CIDR)
    """
    if not rule:
        return False

    if len(rule) > MAX_RULE_LENGTH:
        return False

    # Strict whitelist to prevent injection
    return bool(rule) and _ALLOWED_RULE_CHARS.issuperset(rule)


def is_valid_folder_name(name: str) -> bool:
    """
    Validates folder name to prevent XSS, path traversal, and homograph attacks.

    Blocks:
    - XSS/HTML injection characters: < > " ' `
    - Path separators: / \\
    - Unicode Bidi control characters (RTLO spoofing)
    - Empty or whitespace-only names
    - Non-printable characters
    """
    if not name or not name.strip() or not name.isprintable():
        return False

    if len(name) > MAX_FOLDER_NAME_LENGTH:
        return False

    # Check for dangerous characters (pre-compiled at module level for performance)
    if not _ALL_FORBIDDEN_FOLDER_CHARS.isdisjoint(name):
        return False

    # Security: Block path traversal attempts
    # Check stripped name to prevent whitespace bypass (e.g. " . ")
    clean_name = name.strip()
    if clean_name in (".", ".."):
        return False

    # Security: Block command option injection (if name is passed to shell)
    return not clean_name.startswith("-")


def _is_valid_rule_list(rules_list: Any) -> bool:
    """Helper to quickly validate a list of rules without generator overhead."""
    if not isinstance(rules_list, list):
        return False
    for r in rules_list:
        if type(r) is not dict or (
            (pk := r.get("PK")) is not None and type(pk) is not str
        ):
            return False
    return True


def validate_folder_data(data: dict[str, Any], url: str) -> TypeGuard[FolderData]:
    """
    Validates folder JSON data structure and content.

    Checks for required fields (name, action, rules), validates folder name
    and action type, and ensures rules are valid. Logs specific validation errors.
    """

    if not isinstance(data, dict):
        log.error(
            f"Invalid data from {sanitize_for_log(url)}: Root must be a JSON object."
        )
        return False
    if "group" not in data:
        log.error(f"Invalid data from {sanitize_for_log(url)}: Missing 'group' key.")
        return False
    if not isinstance(data["group"], dict):
        log.error(
            f"Invalid data from {sanitize_for_log(url)}: 'group' must be an object."
        )
        return False
    if "group" not in data["group"]:
        log.error(
            f"Invalid data from {sanitize_for_log(url)}: Missing 'group.group' (folder name)."
        )
        return False

    folder_name = data["group"]["group"]
    if not isinstance(folder_name, str):
        log.error(
            f"Invalid data from {sanitize_for_log(url)}: Folder name must be a string."
        )
        return False

    if not is_valid_folder_name(folder_name):
        log.error(
            f"Invalid data from {sanitize_for_log(url)}: Invalid folder name (empty, unsafe characters, or non-printable)."
        )
        return False

    # Validate 'rules' if present (must be a list of dicts with string PK values)
    if "rules" in data:
        if not isinstance(data["rules"], list):
            log.error(
                f"Invalid data from {sanitize_for_log(url)}: 'rules' must be a list."
            )
            return False

        # Optimization: Fast path inline type check avoids function call overhead per rule.
        # Fallback identifies the exact error for logging.
        rules_list = data["rules"]
        if not _is_valid_rule_list(rules_list):
            for j, rule in enumerate(rules_list):
                if not isinstance(rule, dict):
                    log.error(
                        f"Invalid data from {sanitize_for_log(url)}: rules[{j}] must be an object."
                    )
                    return False
                if (pk := rule.get("PK")) is not None and not isinstance(pk, str):
                    log.error(
                        f"Invalid data from {sanitize_for_log(url)}: rules[{j}].PK must be a string."
                    )
                    return False

    # Validate 'rule_groups' if present (must be a list of dicts)
    if "rule_groups" in data:
        if not isinstance(data["rule_groups"], list):
            log.error(
                f"Invalid data from {sanitize_for_log(url)}: 'rule_groups' must be a list."
            )
            return False
        for i, rg in enumerate(data["rule_groups"]):
            if not isinstance(rg, dict):
                log.error(
                    f"Invalid data from {sanitize_for_log(url)}: rule_groups[{i}] must be an object."
                )
                return False
            if "rules" in rg:
                if not isinstance(rg["rules"], list):
                    log.error(
                        f"Invalid data from {sanitize_for_log(url)}: rule_groups[{i}].rules must be a list."
                    )
                    return False

                # Ensure each rule within the group is an object (dict) and has a string PK,
                # because later code treats each rule as a mapping (e.g., rule.get(...)).
                rg_rules_list = rg["rules"]
                # Optimization: Fast path inline type check avoids function call overhead per rule.
                # Fallback identifies the exact error for logging.
                if not _is_valid_rule_list(rg_rules_list):
                    for j, rule in enumerate(rg_rules_list):
                        if not isinstance(rule, dict):
                            log.error(
                                f"Invalid data from {sanitize_for_log(url)}: rule_groups[{i}].rules[{j}] must be an object."
                            )
                            return False
                        if (pk := rule.get("PK")) is not None and not isinstance(
                            pk, str
                        ):
                            log.error(
                                f"Invalid data from {sanitize_for_log(url)}: rule_groups[{i}].rules[{j}].PK must be a string."
                            )
                            return False

    return True


# _api_stats_lock, _api_get, _api_delete, _api_post, _api_post_form,
# retry_with_jitter, _retry_request imported from api_client above
def _gh_get(url: str) -> dict:
    """
    Fetch blocklist data from URL with HTTP cache header support.

    CACHING STRATEGY:
    1. Check in-memory cache first (fastest)
    2. Check disk cache and send conditional request (If-None-Match/If-Modified-Since)
    3. If 304 Not Modified: reuse cached data (cache validation)
    4. If 200 OK: download new data and update cache

    SECURITY: Validates data structure regardless of cache source
    """
    # First check: Quick check without holding lock for long
    with _cache_lock:
        if (cached := _cache.get(url)) is not None:
            _cache_stats["hits"] += 1
            return cached

    # Track that we're about to make a blocklist fetch
    with _cache_lock:
        _api_stats["blocklist_fetches"] += 1

    # Check disk cache for TTL-based hit or conditional request headers
    headers = {}
    cached_entry = _disk_cache.get(url)
    if cached_entry:
        last_validated = cached_entry.get("last_validated", 0)
        if time.time() - last_validated < CACHE_TTL_SECONDS:
            # Within TTL: return cached data directly without any HTTP request
            data = cached_entry["data"]
            with _cache_lock:
                _cache[url] = data
            _cache_stats["hits"] += 1
            if log.isEnabledFor(logging.DEBUG):
                log.debug(f"Disk cache hit (within TTL) for {sanitize_for_log(url)}")
            return cast(dict, data)
        # Beyond TTL: send conditional request using cached ETag/Last-Modified
        # Server returns 304 if content hasn't changed
        # NOTE: Cached values may be None if the server didn't send these headers.
        # httpx requires header values to be str/bytes, so we only add headers
        # when the cached value is truthy.
        etag = cached_entry.get("etag")
        if etag:
            headers["If-None-Match"] = etag
        last_modified = cached_entry.get("last_modified")
        if last_modified:
            headers["If-Modified-Since"] = last_modified

    # Fetch data (or validate cache)
    # Explicitly let HTTPError propagate (no need to catch just to re-raise)
    try:
        with _gh.stream("GET", url, headers=headers) as r:
            # Handle 304 Not Modified - cached data is still valid
            if r.status_code == 304:
                if cached_entry and "data" in cached_entry:
                    if log.isEnabledFor(logging.DEBUG):
                        log.debug(f"Cache validated (304) for {sanitize_for_log(url)}")
                    _cache_stats["validations"] += 1

                    # Update in-memory cache with validated data
                    data = cached_entry["data"]
                    with _cache_lock:
                        _cache[url] = data

                    # Update timestamp in disk cache to track last validation
                    cached_entry["last_validated"] = time.time()
                    return cast(dict, data)
                # Shouldn't happen, but handle gracefully
                log.warning(
                    f"Got 304 but no cached data for {sanitize_for_log(url)}, re-fetching"
                )
                _cache_stats["errors"] += 1
                # Close the original streaming response before retrying
                r.close()
                # Retry without conditional headers using streaming again so that
                # MAX_RESPONSE_SIZE and related protections still apply.
                headers = {}
                with _gh.stream("GET", url, headers=headers) as r_retry:
                    r_retry.raise_for_status()

                    # Security: Validate Content-Type in fallback branch
                    content_type = r_retry.headers.get("Content-Type", "").lower()
                    allowed_types = ["application/json", "text/json", "text/plain"]
                    if not any(t in content_type for t in allowed_types):
                        raise ValueError(
                            f"Invalid Content-Type from {sanitize_for_log(url)}: {sanitize_for_log(content_type)}. "
                            f"Expected one of: {', '.join(allowed_types)}"
                        )

                    # 1. Check Content-Length header if present
                    cl = r_retry.headers.get("Content-Length")
                    if cl:
                        try:
                            if int(cl) > MAX_RESPONSE_SIZE:
                                raise ValueError(
                                    f"Response too large from {sanitize_for_log(url)} "
                                    f"({int(cl) / (1024 * 1024):.2f} MB)"
                                )
                        except ValueError as e:
                            # Only catch the conversion error, let the size error propagate
                            if "Response too large" in str(e):
                                raise
                            log.warning(
                                f"Malformed Content-Length header from {sanitize_for_log(url)}: {cl!r}. "
                                "Falling back to streaming size check."
                            )

                    # 2. Stream and check actual size
                    chunks = []
                    current_size = 0
                    for chunk in r_retry.iter_bytes():
                        current_size += len(chunk)
                        if current_size > MAX_RESPONSE_SIZE:
                            raise ValueError(
                                f"Response too large from {sanitize_for_log(url)} "
                                f"(> {MAX_RESPONSE_SIZE / (1024 * 1024):.2f} MB)"
                            )
                        chunks.append(chunk)

                    try:
                        data = json.loads(b"".join(chunks))
                    except json.JSONDecodeError as e:
                        raise ValueError(
                            f"Invalid JSON response from {sanitize_for_log(url)}"
                        ) from e

                    # Store cache headers for future conditional requests
                    # ETag is preferred over Last-Modified (more reliable)
                    etag = r_retry.headers.get("ETag")
                    last_modified = r_retry.headers.get("Last-Modified")

                    # Update disk cache with new data and headers
                    _disk_cache[url] = {
                        "data": data,
                        "etag": etag,
                        "last_modified": last_modified,
                        "fetched_at": time.time(),
                        "last_validated": time.time(),
                    }

                    _cache_stats["misses"] += 1
                    return cast(dict, data)

            r.raise_for_status()

            # Security: Validate Content-Type
            # Prevent processing of unexpected content types (e.g., HTML/XML from captive portals or attack sites)
            content_type = r.headers.get("Content-Type", "").lower()
            allowed_types = ["application/json", "text/json", "text/plain"]
            if not any(t in content_type for t in allowed_types):
                raise ValueError(
                    f"Invalid Content-Type from {sanitize_for_log(url)}: {sanitize_for_log(content_type)}. "
                    f"Expected one of: {', '.join(allowed_types)}"
                )

            # 1. Check Content-Length header if present
            cl = r.headers.get("Content-Length")
            if cl:
                try:
                    if int(cl) > MAX_RESPONSE_SIZE:
                        raise ValueError(
                            f"Response too large from {sanitize_for_log(url)} "
                            f"({int(cl) / (1024 * 1024):.2f} MB)"
                        )
                except ValueError as e:
                    # Only catch the conversion error, let the size error propagate
                    if "Response too large" in str(e):
                        raise
                    log.warning(
                        f"Malformed Content-Length header from {sanitize_for_log(url)}: {cl!r}. "
                        "Falling back to streaming size check."
                    )

            # 2. Stream and check actual size
            chunks = []
            current_size = 0
            # Optimization: Use 16KB chunks to reduce loop overhead/appends for large files
            for chunk in r.iter_bytes(chunk_size=16 * 1024):
                current_size += len(chunk)
                if current_size > MAX_RESPONSE_SIZE:
                    raise ValueError(
                        f"Response too large from {sanitize_for_log(url)} "
                        f"(> {MAX_RESPONSE_SIZE / (1024 * 1024):.2f} MB)"
                    )
                chunks.append(chunk)

            try:
                data = json.loads(b"".join(chunks))
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"Invalid JSON response from {sanitize_for_log(url)}"
                ) from e

            # Store cache headers for future conditional requests
            # ETag is preferred over Last-Modified (more reliable)
            etag = r.headers.get("ETag")
            last_modified = r.headers.get("Last-Modified")

            # Update disk cache with new data and headers
            _disk_cache[url] = {
                "data": data,
                "etag": etag,
                "last_modified": last_modified,
                "fetched_at": time.time(),
                "last_validated": time.time(),
            }

            _cache_stats["misses"] += 1

    except httpx.HTTPStatusError:
        # Re-raise with original exception (don't catch and re-raise)
        raise

    # Double-checked locking: Check again after fetch to avoid duplicate fetches
    # If another thread already cached it while we were fetching, use theirs
    # for consistency (return _cache[url] instead of data to ensure single source of truth)
    with _cache_lock:
        return _cache.setdefault(url, data)


def check_api_access(client: httpx.Client, profile_id: str) -> bool:
    """
    Verifies API access and Profile existence before starting heavy work.
    Returns True if access is good, False otherwise (with helpful logs).
    """
    url = f"{API_BASE}/{profile_id}/groups"
    try:
        # We use a raw request here to avoid the automatic retries of _retry_request
        # for auth errors, which are permanent.
        resp = client.get(url)
        resp.raise_for_status()
        return True
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        if code == 401:
            log.critical(
                f"{Colors.FAIL}❌ Authentication Failed: The API Token is invalid.{Colors.ENDC}"
            )
            log.critical(
                f"{Colors.FAIL}   Please check your token at: https://controld.com/account/manage-account{Colors.ENDC}"
            )
        elif code == 403:
            log.critical(
                f"{Colors.FAIL}🚫 Access Denied: Token lacks permission for Profile {profile_id}.{Colors.ENDC}"
            )
        elif code == 404:
            log.critical(
                f"{Colors.FAIL}🔍 Profile Not Found: The ID '{sanitize_for_log(profile_id)}' does not exist.{Colors.ENDC}"
            )
            log.critical(
                f"{Colors.FAIL}   Please verify the Profile ID from your Control D Dashboard URL.{Colors.ENDC}"
            )
        else:
            log.error(f"API Access Check Failed ({code}): {sanitize_for_log(e)}")
        return False
    except httpx.RequestError as e:
        hint = ""
        if isinstance(e, httpx.TimeoutException):
            hint = f" | hint: {_TIMEOUT_HINT}"
        elif isinstance(e, httpx.ConnectError):
            hint = f" | hint: {_CONNECT_ERROR_HINT}"
        log.error(f"Network Error during access check: {sanitize_for_log(e)}{hint}")
        return False


def list_existing_folders(client: httpx.Client, profile_id: str) -> dict[str, str]:
    """
    Retrieves all existing folders (groups) for a given profile.

    Returns a dictionary mapping folder names to their IDs.
    Returns empty dict on error.
    """
    try:
        data = _api_get(client, f"{API_BASE}/{profile_id}/groups").json()
        folders = data.get("body", {}).get("groups", [])
        result = {}
        for f in folders:
            if not f.get("group") or not f.get("PK"):
                continue
            pk = str(f["PK"])
            if validate_folder_id(pk):
                result[f["group"].strip()] = pk
        return result
    except (httpx.HTTPError, KeyError) as e:
        hint = ""
        if isinstance(e, httpx.HTTPStatusError):
            hint = f" | hint: {_STATUS_HINTS.get(e.response.status_code, f'HTTP {e.response.status_code}')}"
        elif isinstance(e, httpx.TimeoutException):
            hint = f" | hint: {_TIMEOUT_HINT}"
        elif isinstance(e, httpx.ConnectError):
            hint = f" | hint: {_CONNECT_ERROR_HINT}"
        log.error(f"Failed to list existing folders{hint}: {sanitize_for_log(e)}")
        return {}


def _parse_folders_response(data: dict) -> dict[str, str] | None:
    # Ensure we got the expected top-level JSON structure.
    if not isinstance(data, dict):
        log.error(
            "Failed to parse folders data: expected JSON object at top level, "
            f"got {type(data).__name__}"
        )
        return None

    body = data.get("body")
    if not isinstance(body, dict):
        log.error(
            "Failed to parse folders data: expected 'body' to be an object, "
            f"got {type(body).__name__ if body is not None else 'None'}"
        )
        return None

    folders = body.get("groups", [])
    if not isinstance(folders, list):
        log.error(
            "Failed to parse folders data: expected 'body[\"groups\"]' to be a list, "
            f"got {type(folders).__name__}"
        )
        return None

    # Only process entries that are dicts and have the required keys.
    result: dict[str, str] = {}
    for f in folders:
        if not isinstance(f, dict):
            continue
        name = f.get("group")
        pk = f.get("PK")
        # Skip entries with empty or None values for required fields
        if not name or not pk:
            continue

        pk_str = str(pk)
        if not validate_folder_id(pk_str):
            continue

        result[str(name).strip()] = pk_str

    return result


def _log_auth_error(code: int, profile_id: str) -> None:
    if code == 401:
        log.critical(
            f"{Colors.FAIL}❌ Authentication Failed: The API Token is invalid.{Colors.ENDC}"
        )
        log.critical(
            f"{Colors.FAIL}   Please check your token at: https://controld.com/account/manage-account{Colors.ENDC}"
        )
    elif code == 403:
        log.critical(
            "%s🚫 Access Denied: Token lacks permission for Profile %s.%s",
            Colors.FAIL,
            sanitize_for_log(profile_id),
            Colors.ENDC,
        )
    elif code == 404:
        log.critical(
            f"{Colors.FAIL}🔍 Profile Not Found: The ID '{sanitize_for_log(profile_id)}' does not exist.{Colors.ENDC}"
        )
        log.critical(
            f"{Colors.FAIL}   Please verify the Profile ID from your Control D Dashboard URL.{Colors.ENDC}"
        )


def verify_access_and_get_folders(
    client: httpx.Client, profile_id: str
) -> dict[str, str] | None:
    """Combine access check and folder listing into a single API request.

    Returns:
        Dict of {folder_name: folder_id} on success.
        None if access is denied or the request fails after retries.
    """
    url = f"{API_BASE}/{profile_id}/groups"

    for attempt in range(MAX_RETRIES):
        try:
            resp = client.get(url)
            resp.raise_for_status()

            try:
                return _parse_folders_response(resp.json())
            except (ValueError, TypeError, AttributeError) as err:
                log.error("Failed to parse folders data: %s", sanitize_for_log(err))
                return None

        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            if code in (401, 403, 404):
                _log_auth_error(code, profile_id)
                return None

            if attempt == MAX_RETRIES - 1:
                log.error(f"API Request Failed ({code}): {sanitize_for_log(e)}")
                return None

        except httpx.RequestError as err:
            if attempt == MAX_RETRIES - 1:
                hint = ""
                if isinstance(err, httpx.TimeoutException):
                    hint = f" | hint: {_TIMEOUT_HINT}"
                elif isinstance(err, httpx.ConnectError):
                    hint = f" | hint: {_CONNECT_ERROR_HINT}"
                log.error(
                    "Network error during access verification: %s%s",
                    sanitize_for_log(err),
                    hint,
                )
                return None

        wait_time = RETRY_DELAY * (2**attempt)
        log.warning(
            "Request failed (attempt %d/%d). Retrying in %ds...",
            attempt + 1,
            MAX_RETRIES,
            wait_time,
        )
        time.sleep(wait_time)

    return None


def get_all_existing_rules(
    client: httpx.Client,
    profile_id: str,
    known_folders: dict[str, str] | None = None,
) -> set[str]:
    """
    Fetches all existing rules across root and all folders.

    Retrieves rules from the root level and all folders in parallel.
    Uses known_folders to avoid redundant API calls when provided.
    Returns set of rule IDs.
    """
    all_rules = set()

    def _fetch_folder_rules(folder_id: str) -> list[str]:
        try:
            data = _api_get(client, f"{API_BASE}/{profile_id}/rules/{folder_id}").json()
            folder_rules = data.get("body", {}).get("rules", [])
            return [pk for rule in folder_rules if (pk := rule.get("PK"))]
        except httpx.HTTPError as e:
            log.debug(
                "Could not fetch rules for folder %s (will skip): %s",
                folder_id,
                sanitize_for_log(e),
            )
            return []
        except Exception as e:
            # We log error but don't stop the whole process;
            # individual folder failure shouldn't crash the sync
            log.warning(
                f"Error fetching rules for folder {folder_id}: {sanitize_for_log(e)}"
            )
            return []

    try:
        # Get rules from root
        try:
            data = _api_get(client, f"{API_BASE}/{profile_id}/rules").json()
            root_rules = data.get("body", {}).get("rules", [])
            for rule in root_rules:
                if rule.get("PK"):
                    all_rules.add(rule["PK"])
        except httpx.HTTPError as e:
            log.debug(
                "Could not fetch root-level rules (will proceed with folder rules only): %s",
                sanitize_for_log(e),
            )

        # Get rules from folders in parallel
        # Optimization: Use known_folders if provided to avoid redundant API call
        if known_folders is not None:
            folders = known_folders
        else:
            folders = list_existing_folders(client, profile_id)

        # Parallelize fetching rules from folders.
        # Using 5 workers to be safe with rate limits, though GETs are usually cheaper.
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            future_to_folder = {
                executor.submit(_fetch_folder_rules, folder_id): folder_id
                for folder_name, folder_id in folders.items()
            }

            for future in concurrent.futures.as_completed(future_to_folder):
                try:
                    result = future.result()
                    if result:
                        all_rules.update(result)
                except Exception as e:
                    folder_id = future_to_folder[future]
                    log.warning(
                        f"Failed to fetch rules for folder ID {folder_id}: {sanitize_for_log(e)}"
                    )

        log.info(f"Total existing rules across all folders: {len(all_rules):,}")
        return all_rules
    except Exception as e:
        log.error(f"Failed to get existing rules: {sanitize_for_log(e)}")
        return set()


def fetch_folder_data(url: str) -> FolderData:
    """
    Downloads and validates folder JSON data from a URL.

    Uses cached GET request and validates the folder structure.
    Raises httpx.HTTPStatusError (with actionable hint) on HTTP failure,
    or KeyError if validation of the returned data fails.
    """
    try:
        js = _gh_get(url)
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        hint = _STATUS_HINTS.get(status, f"HTTP {status}")
        # Include the original error message so we keep the numeric status code
        # and reason phrase (e.g., "401 Unauthorized") in addition to our hint.
        original_msg = str(e)
        raise httpx.HTTPStatusError(
            f"{original_msg} | hint: {hint} | url: {sanitize_for_log(url)}",
            request=e.request,
            response=e.response,
        ) from e
    if not validate_folder_data(js, url):
        raise KeyError(f"Invalid folder data from {sanitize_for_log(url)}")
    return js


def warm_up_cache(urls: Sequence[str]) -> None:
    """
    Pre-fetches and caches folder data from multiple URLs in parallel.

    Validates URLs and fetches data concurrently to minimize cold-start latency.
    Shows progress bar when USE_COLORS is enabled. Skips invalid URLs while
    emitting warnings/log entries for validation and fetch failures.
    """
    urls = list(set(urls))
    with _cache_lock:
        urls_to_process = [u for u in urls if u not in _cache]
    if not urls_to_process:
        return

    total = len(urls_to_process)
    if not USE_COLORS:
        log.info(f"⏳ Warming up cache for {total:,} {pluralize(total, 'URL')}...")

    # OPTIMIZATION: Combine validation (DNS) and fetching (HTTP) in one task
    # to allow validation latency to be parallelized.
    def _validate_and_fetch(url: str):
        if validate_folder_url(url):
            return _gh_get(url)
        return None

    completed = 0
    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = {
            executor.submit(_validate_and_fetch, url): url for url in urls_to_process
        }

        render_progress_bar(0, total, "Warming up cache", prefix="⏳")

        for future in concurrent.futures.as_completed(futures):
            completed += 1
            render_progress_bar(completed, total, "Warming up cache", prefix="⏳")
            try:
                future.result()
            except Exception as e:
                if USE_COLORS:
                    # Clear line to print warning cleanly
                    sys.stderr.write("\r\033[K")
                    sys.stderr.flush()

                log.warning(
                    f"Failed to pre-fetch {sanitize_for_log(futures[future])}: "
                    f"{sanitize_for_log(e)}"
                )
                # Restore progress bar after warning
                render_progress_bar(completed, total, "Warming up cache", prefix="⏳")

    if USE_COLORS:
        sys.stderr.write(
            f"\r\033[K{Colors.GREEN}✅ Warming up cache: Done!{Colors.ENDC}\n"
        )
        sys.stderr.flush()
    else:
        log.info("✅ Warming up cache: Done!")


def delete_folder(
    client: httpx.Client, profile_id: str, name: str, folder_id: str
) -> bool:
    """
    Deletes a folder (group) from a Control D profile.

    Returns True on success, False on failure. Logs detailed error information.
    """
    try:
        _api_delete(client, f"{API_BASE}/{profile_id}/groups/{folder_id}")
        log.info(
            "Deleted folder %s (ID %s)",
            sanitize_for_log(name),
            sanitize_for_log(folder_id),
        )
        return True
    except httpx.HTTPError as e:
        hint = ""
        if isinstance(e, httpx.HTTPStatusError):
            hint = f" | hint: {_STATUS_HINTS.get(e.response.status_code, f'HTTP {e.response.status_code}')}"
        elif isinstance(e, httpx.TimeoutException):
            hint = f" | hint: {_TIMEOUT_HINT}"
        elif isinstance(e, httpx.ConnectError):
            hint = f" | hint: {_CONNECT_ERROR_HINT}"
        log.error(
            f"Failed to delete folder {sanitize_for_log(name)} (ID {sanitize_for_log(folder_id)}){hint}: {sanitize_for_log(e)}"
        )
        return False


def _process_new_folder_pk(pk: str, name: str, source: str) -> str | None:
    if not validate_folder_id(pk, log_errors=False):
        log.error(f"API returned invalid folder ID: {sanitize_for_log(pk)}")
        return None
    log.info(
        "Created folder %s (ID %s) [%s]",
        sanitize_for_log(name),
        sanitize_for_log(pk),
        source,
    )
    return pk


def _is_matching_group_dict(grp: Any, name: str) -> bool:
    if not isinstance(grp, dict):
        return False
    return grp.get("group", "").strip() == name.strip() and "PK" in grp


def _extract_from_groups_list(groups: list, name: str) -> str | None:
    for grp in groups:
        if _is_matching_group_dict(grp, name):
            pk = _process_new_folder_pk(str(grp["PK"]), name, "Direct")
            if pk:
                return pk
    return None


def _extract_folder_id_from_response(response: httpx.Response, name: str) -> str | None:
    try:
        body = response.json().get("body")
    except Exception as e:
        if log.isEnabledFor(logging.DEBUG):
            log.debug(f"Could not extract ID from POST response: {sanitize_for_log(e)}")
        return None

    if not isinstance(body, dict):
        return None

    group = body.get("group")
    if isinstance(group, dict) and "PK" in group:
        return _process_new_folder_pk(str(group["PK"]), name, "Direct")

    groups = body.get("groups")
    if isinstance(groups, list):
        return _extract_from_groups_list(groups, name)

    return None


def _poll_for_folder_id(ctx: SyncContext, name: str) -> str | None:
    for attempt in range(MAX_RETRIES + 1):
        try:
            data = _api_get(ctx.client, f"{API_BASE}/{ctx.profile_id}/groups").json()
            groups = data.get("body", {}).get("groups", [])

            for grp in groups:
                if _is_matching_group_dict(grp, name):
                    pk = _process_new_folder_pk(str(grp["PK"]), name, "Polled")
                    if pk:
                        return pk
                    return None  # Invalid PK found, stop polling
        except Exception as e:
            log.warning(
                f"Error fetching groups on attempt {attempt}: {sanitize_for_log(e)}"
            )

        if attempt < MAX_RETRIES:
            wait_time = FOLDER_CREATION_DELAY * (attempt + 1)
            log.info(
                f"Folder '{sanitize_for_log(name)}' not found yet. Retrying in {wait_time}s..."
            )
            time.sleep(wait_time)

    log.error(
        f"Folder {sanitize_for_log(name)} was not found after creation and retries."
    )
    return None


def create_folder(ctx: SyncContext, name: str, action: RuleAction) -> str | None:
    """
    Create a new folder and return its ID.
    Attempts to read ID from response first, then falls back to polling.
    """
    try:
        # 1. Send the Create Request
        response = _api_post(
            ctx.client,
            f"{API_BASE}/{ctx.profile_id}/groups",
            data={"name": name, "do": action.do, "status": action.status},
        )

        # OPTIMIZATION: Try to grab ID directly from response to avoid the wait loop
        pk = _extract_folder_id_from_response(response, name)
        if pk:
            return pk

        # 2. Fallback: Poll for the new folder (The Robust Retry Logic)
        return _poll_for_folder_id(ctx, name)

    except (httpx.HTTPError, KeyError) as e:
        hint = ""
        if isinstance(e, httpx.HTTPStatusError):
            hint = f" | hint: {_STATUS_HINTS.get(e.response.status_code, f'HTTP {e.response.status_code}')}"
        log.error(
            f"Failed to create folder {sanitize_for_log(name)}{hint}: {sanitize_for_log(e)}"
        )
        return None



def _filter_rules_for_folder(
    existing_rules: set[str],
    hostnames: list[str],
    folder_name: str,
) -> list[str]:
    """
    Deduplicates and filters hostnames, logging dropped entries.
    """
    original_count = len(hostnames)

    # Optimization 1: Deduplicate and filter existing rules in a C-speed dict comprehension.
    if not existing_rules:
        unique_hostnames_dict = dict.fromkeys(hostnames)
    else:
        unique_hostnames_dict = {h: None for h in hostnames if h not in existing_rules}

    # Optimization 2: Inline method references for hot loop performance
    is_safe = _ALLOWED_RULE_CHARS.issuperset

    # Second pass: Strict safety validation
    # FAST PATH: C-speed list comprehension for the 99.9% case where rules are safe
    filtered_hostnames = [h for h in unique_hostnames_dict if h and is_safe(h)]
    skipped_unsafe = len(unique_hostnames_dict) - len(filtered_hostnames)

    if skipped_unsafe > 0:
        # SLOW PATH: Only iterate again to log if we actually found unsafe rules
        sanitized_folder = sanitize_for_log(folder_name)
        for h in unique_hostnames_dict:
            if not (h and is_safe(h)):
                log.warning(
                    f"Skipping unsafe rule in {sanitized_folder}: {sanitize_for_log(h)}"
                )
        log.warning(
            f"Folder {sanitized_folder}: skipped {skipped_unsafe} unsafe {pluralize(skipped_unsafe, 'rule')}"
        )

    duplicates_count = original_count - len(filtered_hostnames) - skipped_unsafe

    if duplicates_count > 0:
        log.info(
            f"Folder {sanitize_for_log(folder_name)}: skipping {duplicates_count} duplicate {pluralize(duplicates_count, 'rule')}"
        )

    return filtered_hostnames


def _push_single_batch(
    client: httpx.Client,
    profile_id: str,
    sanitized_folder_name: str,
    str_do: str,
    str_status: str,
    str_group: str,
    batch_idx: int,
    batch_data: list[str],
) -> list[str] | None:
    """Processes a single batch of rules by sending API request."""
    data = {
        "do": str_do,
        "status": str_status,
        "group": str_group,
    }
    # Optimization: Use pre-calculated keys and zip for faster dict update
    # strict=False is intentional: batch_data may be shorter than BATCH_KEYS for final batch
    data.update(zip(BATCH_KEYS, batch_data, strict=False))

    try:
        _api_post_form(client, f"{API_BASE}/{profile_id}/rules", data=data)
        if not USE_COLORS:
            log.info(
                "Folder %s – batch %d: added %d %s",
                sanitized_folder_name,
                batch_idx,
                len(batch_data),
                pluralize(len(batch_data), "rule"),
            )
        return batch_data
    except httpx.HTTPError as e:
        if USE_COLORS:
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()
        hint = ""
        if isinstance(e, httpx.HTTPStatusError):
            # Use a more specific name to avoid confusion with the rule "status" payload
            status_code = e.response.status_code
            hint = f" ({_STATUS_HINTS.get(status_code, f'HTTP {status_code}')})"
        log.error(
            f"Failed to push batch {batch_idx} for folder {sanitized_folder_name}{hint}: {sanitize_for_log(e)}"
        )
        if (
            hasattr(e, "response")
            and e.response is not None
            and log.isEnabledFor(logging.DEBUG)
        ):
            log.debug(f"Response content: {sanitize_for_log(e.response.text)}")
        return None


def _push_rule_batches(
    ctx: SyncContext,
    folder_name: str,
    folder_id: str,
    action: RuleAction,
    filtered_hostnames: list[str],
) -> bool:
    """
    Splits rules into batches and pushes them to the API in parallel.
    """
    successful_batches = 0
    batches = [
        filtered_hostnames[start : start + BATCH_SIZE]
        for start in range(0, len(filtered_hostnames), BATCH_SIZE)
    ]
    total_batches = len(batches)

    # Optimization: Hoist loop invariants to avoid redundant computations
    str_do = str(action.do)
    str_status = str(action.status)
    str_group = str(folder_id)
    sanitized_folder_name = sanitize_for_log(folder_name)
    progress_label = f"Folder {sanitized_folder_name}"

    # Optimization 3: Parallelize batch processing
    if total_batches == 1:
        result = _push_single_batch(
            ctx.client,
            ctx.profile_id,
            sanitized_folder_name,
            str_do,
            str_status,
            str_group,
            1,
            batches[0],
        )
        if result:
            successful_batches += 1
            ctx.existing_rules.update(result)

        render_progress_bar(
            successful_batches,
            total_batches,
            progress_label,
        )
    else:
        # Use provided executor or create a local one (fallback)
        if ctx.batch_executor:
            executor_ctx: contextlib.AbstractContextManager[
                concurrent.futures.Executor
            ] = contextlib.nullcontext(ctx.batch_executor)
        else:
            executor_ctx = concurrent.futures.ThreadPoolExecutor(max_workers=3)

        with executor_ctx as executor:
            futures = {
                executor.submit(
                    _push_single_batch,
                    ctx.client,
                    ctx.profile_id,
                    sanitized_folder_name,
                    str_do,
                    str_status,
                    str_group,
                    i,
                    batch,
                ): i
                for i, batch in enumerate(batches, 1)
            }

            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if result:
                    successful_batches += 1
                    ctx.existing_rules.update(result)

                render_progress_bar(
                    successful_batches,
                    total_batches,
                    progress_label,
                )

    if successful_batches == total_batches:
        if USE_COLORS:
            sys.stderr.write(
                f"\r\033[K{Colors.GREEN}✅ Folder {sanitized_folder_name}: Finished ({len(filtered_hostnames):,} {pluralize(len(filtered_hostnames), 'rule')}){Colors.ENDC}\n"
            )
            sys.stderr.flush()
        else:
            log.info(
                f"✅ Folder {sanitized_folder_name} – finished ({len(filtered_hostnames):,} new {pluralize(len(filtered_hostnames), 'rule')} added)"
            )
        return True
    if USE_COLORS:
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()
    log.error(
        "Folder %s – only %d/%d batches succeeded",
        sanitized_folder_name,
        successful_batches,
        total_batches,
    )
    return False


def push_rules(
    ctx: SyncContext,
    folder_name: str,
    folder_id: str,
    action: RuleAction,
    hostnames: list[str],
) -> bool:
    """
    Pushes rules to a folder in batches, filtering duplicates and invalid rules.

    Deduplicates input, validates rules against _ALLOWED_RULE_CHARS, and sends batches
    in parallel for optimal performance. Updates ctx.existing_rules set with newly
    added rules. Returns True if all batches succeed.
    """
    if not hostnames:
        log.info("Folder %s - no rules to push", sanitize_for_log(folder_name))
        return True

    filtered_hostnames = _filter_rules_for_folder(
        ctx.existing_rules, hostnames, folder_name
    )

    if not filtered_hostnames:
        log.info(
            f"Folder {sanitize_for_log(folder_name)} - no new rules to push after filtering duplicates"
        )
        return True

    return _push_rule_batches(
        ctx,
        folder_name,
        folder_id,
        action,
        filtered_hostnames,
    )


def _process_single_folder(
    ctx: SyncContext,
    folder_data: FolderData,
) -> bool:
    grp = folder_data["group"]
    name = grp["group"].strip()

    # Client is now passed in, reusing the connection
    main_do = grp.get("action", {}).get("do", 0)
    main_status = grp.get("action", {}).get("status", 1)
    main_action = RuleAction(do=main_do, status=main_status)

    folder_id = create_folder(ctx, name, main_action)
    if not folder_id:
        return False

    folder_success = True
    if "rule_groups" in folder_data:
        for rule_group in folder_data["rule_groups"]:
            action_data = rule_group.get("action", {})
            action = RuleAction(
                do=action_data.get("do", 0),
                status=action_data.get("status", 1),
            )
            hostnames = [pk for r in rule_group.get("rules", []) if (pk := r.get("PK"))]
            if not push_rules(
                ctx,
                name,
                folder_id,
                action,
                hostnames,
            ):
                folder_success = False
    else:
        hostnames = [pk for r in folder_data.get("rules", []) if (pk := r.get("PK"))]
        if not push_rules(
            ctx,
            name,
            folder_id,
            main_action,
            hostnames,
        ):
            folder_success = False

    return folder_success


# --------------------------------------------------------------------------- #
# 4. Main workflow
# --------------------------------------------------------------------------- #
def _fetch_all_folder_data(folder_urls: Sequence[str]) -> list[FolderData] | None:
    """Fetches folder data for all URLs in parallel."""
    folder_data_list: list[FolderData] = []

    # OPTIMIZATION: Move validation inside the thread pool to parallelize DNS lookups.
    # Previously, sequential validation blocked the main thread.
    def _fetch_if_valid(url: str):
        # Optimization: If we already have the content in cache, return it directly.
        # The content was validated at the time of fetch (warm_up_cache).
        # Read directly from cache to avoid calling fetch_folder_data while holding lock.
        with _cache_lock:
            if (cached := _cache.get(url)) is not None:
                return cached

        if validate_folder_url(url):
            return fetch_folder_data(url)
        return None

    with concurrent.futures.ThreadPoolExecutor() as executor:
        future_to_url = {
            executor.submit(_fetch_if_valid, url): url for url in folder_urls
        }

        for future in concurrent.futures.as_completed(future_to_url):
            url = future_to_url[future]
            try:
                result = future.result()
                if result:
                    folder_data_list.append(result)
            except (httpx.HTTPError, KeyError, ValueError) as e:
                log.error(
                    f"Failed to fetch folder data from {sanitize_for_log(url)}: {sanitize_for_log(e)}"
                )
                continue

    if not folder_data_list:
        log.error("No valid folder data found")
        hint_message = (
            "💡 Hint: Check your --folder-url flags or your config file "
            "(see --config, config.yaml, or config.yml) for typos or unreachable URLs"
        )
        if USE_COLORS:
            log.warning(f"{Colors.DIM}{hint_message}{Colors.ENDC}")
        else:
            log.warning(hint_message)
        return None

    return folder_data_list


def _build_plan_entry(profile_id: str, folder_data_list: list[FolderData]) -> PlanEntry:
    """Builds the plan entry for a given profile."""
    plan_entry: PlanEntry = {"profile": profile_id, "folders": []}
    for folder_data in folder_data_list:
        grp = folder_data["group"]
        name = grp["group"].strip()

        if "rule_groups" in folder_data:
            # Multi-action format
            total_rules = sum(
                len(rg.get("rules", [])) for rg in folder_data["rule_groups"]
            )
            plan_entry["folders"].append(
                {
                    "name": name,
                    "rules": total_rules,
                    "rule_groups": [
                        {
                            "rules": len(rg.get("rules", [])),
                            "action": rg.get("action", {}).get("do"),
                            "status": rg.get("action", {}).get("status"),
                        }
                        for rg in folder_data["rule_groups"]
                    ],
                }
            )
        else:
            # Legacy single-action format
            # OPTIMIZATION: Count valid rules via generator to avoid an intermediate list and lower peak memory use.
            rules_count = sum(1 for r in folder_data.get("rules", []) if r.get("PK"))
            plan_entry["folders"].append(
                {
                    "name": name,
                    "rules": rules_count,
                    "action": grp.get("action", {}).get("do"),
                    "status": grp.get("action", {}).get("status"),
                }
            )
    return plan_entry


def _prepare_folders_and_rules(
    client: httpx.Client,
    profile_id: str,
    folder_data_list: list[FolderData],
    no_delete: bool,
    shared_executor: concurrent.futures.ThreadPoolExecutor,
) -> tuple[dict[str, str] | None, set[str]]:
    """
    Verifies access, deletes old folders, and fetches existing rules in background.
    """
    # Verify access and list existing folders in one request
    existing_folders = verify_access_and_get_folders(client, profile_id)
    if existing_folders is None:
        return None, set()

    # Identify folders to delete and folders to keep (scan)
    folders_to_delete = []
    folders_to_scan = existing_folders.copy()

    if not no_delete:
        for folder_data in folder_data_list:
            name = folder_data["group"]["group"].strip()
            if name in existing_folders:
                folders_to_delete.append((name, existing_folders[name]))
                # OPTIMIZATION: Use dict.pop() to avoid a redundant dictionary lookup.
                folders_to_scan.pop(name, None)

    # Start fetching rules from kept folders in background (parallel to deletions)
    existing_rules_future = shared_executor.submit(
        get_all_existing_rules, client, profile_id, folders_to_scan
    )

    if not no_delete:
        deletion_occurred = False
        if folders_to_delete:
            # Parallel delete to speed up the "clean slate" phase
            # Use shared_executor (3 workers)
            future_to_name = {
                shared_executor.submit(
                    delete_folder, client, profile_id, name, folder_id
                ): name
                for name, folder_id in folders_to_delete
            }

            for future in concurrent.futures.as_completed(future_to_name):
                name = future_to_name[future]
                try:
                    if future.result():
                        del existing_folders[name]
                        deletion_occurred = True
                except Exception as exc:
                    # Sanitize both name and exception to prevent log injection
                    log.error(
                        "Failed to delete folder %s: %s",
                        sanitize_for_log(name),
                        sanitize_for_log(exc),
                    )

        # CRITICAL FIX: Increased wait time for massive folders to clear
        if deletion_occurred:
            if not USE_COLORS:
                log.info(
                    "Waiting 60s for deletions to propagate (prevents 'Badware Hoster' zombie state)..."
                )
            countdown_timer(60, "Waiting for deletions to propagate")

    # Retrieve result from background task
    # If deletion occurred, we effectively used the wait time to fetch rules!
    try:
        existing_rules = existing_rules_future.result()
    except Exception as e:
        log.error(
            f"Failed to fetch existing rules in background: {sanitize_for_log(e)}"
        )
        existing_rules = set()

    return existing_folders, existing_rules


def sync_profile(
    profile_id: str,
    folder_urls: Sequence[str],
    dry_run: bool = False,
    no_delete: bool = False,
    plan_accumulator: list[PlanEntry] | None = None,
) -> bool:
    """
    Synchronizes Control D folders from remote blocklist URLs.

    Fetches folder data, optionally deletes existing folders with same names,
    creates new folders, and pushes rules in batches. In dry-run mode, only
    generates a plan without making API changes. Returns True if all folders
    sync successfully.
    """
    # SECURITY: Clear cached DNS validations at the start of each sync run.
    # This prevents TOCTOU issues where a domain's IP could change between runs.
    validate_folder_url.cache_clear()
    validate_hostname.cache_clear()

    try:
        folder_data_list = _fetch_all_folder_data(folder_urls)
        if folder_data_list is None:
            return False

        # Build plan entries
        plan_entry = _build_plan_entry(profile_id, folder_data_list)

        if plan_accumulator is not None:
            plan_accumulator.append(plan_entry)

        if dry_run:
            print_plan_details(plan_entry)
            log.info("Dry-run complete: no API calls were made.")
            return True

        # Create new folders and push rules
        success_count = 0

        # CRITICAL FIX: Switch to Serial Processing (1 worker)
        # This prevents API rate limits and ensures stability for large folders.
        max_workers = 1

        # Shared executor for rate-limited operations (DELETE, push_rules batches)
        # Reusing this executor prevents thread churn and enforces global rate limits.
        with (
            concurrent.futures.ThreadPoolExecutor(
                max_workers=DELETE_WORKERS
            ) as shared_executor,
            _api_client() as client,
        ):
            existing_folders_and_rules = _prepare_folders_and_rules(
                client, profile_id, folder_data_list, no_delete, shared_executor
            )
            if existing_folders_and_rules[0] is None:
                return False
            existing_folders, existing_rules = existing_folders_and_rules

            ctx = SyncContext(
                profile_id=profile_id,
                client=client,
                existing_rules=existing_rules,
                batch_executor=shared_executor,
            )

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=max_workers
            ) as executor:
                future_to_folder = {
                    executor.submit(
                        _process_single_folder,
                        ctx,
                        folder_data,
                    ): folder_data
                    for folder_data in folder_data_list
                }

                for future in concurrent.futures.as_completed(future_to_folder):
                    folder_data = future_to_folder[future]
                    folder_name = folder_data["group"]["group"].strip()
                    try:
                        if future.result():
                            success_count += 1
                    except Exception as e:
                        log.error(
                            f"Failed to process folder '{sanitize_for_log(folder_name)}': {sanitize_for_log(e)}"
                        )

        log.info(
            f"Sync complete: {success_count}/{len(folder_data_list)} {pluralize(len(folder_data_list), 'folder')} processed successfully"
        )
        return success_count == len(folder_data_list)

    except Exception as e:
        log.error(
            f"Unexpected error during sync for profile {profile_id}: {sanitize_for_log(e)}"
        )
        return False


# --------------------------------------------------------------------------- #
# 5. Entry-point
# --------------------------------------------------------------------------- #
def _get_interactive_restart_confirmation() -> bool:
    """Helper to prompt for and validate interactive restart confirmation."""
    # Pre-compute formatted strings to reduce loop complexity
    prompt_initial = (
        f"\n{Colors.BOLD}🚀 Ready to launch? {Colors.ENDC}Press [Enter] to run now (or type 'n' / Ctrl+C to cancel)... "
        if USE_COLORS
        else "\n🚀 Ready to launch? Press [Enter] to run now (or type 'n' / Ctrl+C to cancel)... "
    )
    prompt_reprompt = (
        f"{Colors.BOLD}🚀 Ready to launch? {Colors.ENDC}Press [Enter] to run now (or type 'n' / Ctrl+C to cancel)... "
        if USE_COLORS
        else "🚀 Ready to launch? Press [Enter] to run now (or type 'n' / Ctrl+C to cancel)... "
    )
    cancel_msg = (
        f"\n{Colors.WARNING}⚠️  Cancelled.{Colors.ENDC}"
        if USE_COLORS
        else "\n⚠️  Cancelled."
    )
    err_msg = (
        f"{Colors.FAIL}❌ Unrecognized input. Please press Enter to continue, or 'n' to cancel.{Colors.ENDC}"
        if USE_COLORS
        else "❌ Unrecognized input. Please press Enter to continue, or 'n' to cancel."
    )

    prompt = prompt_initial

    while True:
        # Flush stdout (and stderr) so the prompt is visible even if output is buffered or redirected
        sys.stdout.flush()
        sys.stderr.flush()
        user_response = input(prompt).strip().lower()

        if user_response in ("", "y", "yes"):
            return True

        if user_response in ("n", "no", "q", "quit", "exit", "cancel"):
            print(cancel_msg)
            return False

        print(err_msg)
        prompt = prompt_reprompt


def prompt_for_interactive_restart(profile_ids: list[str]) -> None:
    """
    Prompts the user to restart the script in live mode (after a successful dry run).

    If the user confirms, the script restarts itself using os.execv, preserving
    all original arguments (except --dry-run) and environment variables.

    This function only runs if sys.stdin is a TTY (interactive session).
    """
    if not sys.stdin.isatty():
        return

    try:
        if not _get_interactive_restart_confirmation():
            return

        # Prepare environment for the new process
        # Pass the current token to avoid re-prompting if it was entered interactively
        if TOKEN:
            os.environ["TOKEN"] = TOKEN

        # Construct command arguments
        # Use sys.argv filtering to preserve all user-provided flags (even future ones)
        # while removing --dry-run to switch to live mode.
        clean_argv = [arg for arg in sys.argv[1:] if arg != "--dry-run"]
        new_argv = [sys.executable, sys.argv[0]] + clean_argv

        # If --profiles wasn't in original args (meaning it came from env/input),
        # inject it explicitly so the user doesn't have to re-enter it.
        if "--profiles" not in sys.argv and profile_ids:
            new_argv.extend(["--profiles", ",".join(profile_ids)])

        print(f"\n{Colors.GREEN}🔄 Restarting in live mode...{Colors.ENDC}")
        # Security: The input to execv is derived from trusted sys.argv and validated profile_ids.
        # It restarts the same script with the same python interpreter.
        os.execv(sys.executable, new_argv)  # nosec B606

    except (KeyboardInterrupt, EOFError):
        print(f"\n{Colors.WARNING}⚠️  Cancelled.{Colors.ENDC}")


def print_line(left_char: str, mid_char: str, right_char: str, w: list[int]) -> str:
    """Format a horizontal table separator line."""
    return f"{Colors.BOLD}{left_char}{mid_char.join('─' * (x + 2) for x in w)}{right_char}{Colors.ENDC}"


def print_row(cols: list[str], w: list[int]) -> str:
    """Format a row of table data."""
    return f"{Colors.BOLD}│{Colors.ENDC} {cols[0]:<{w[0]}} {Colors.BOLD}│{Colors.ENDC} {cols[1]:>{w[1]}} {Colors.BOLD}│{Colors.ENDC} {cols[2]:>{w[2]}} {Colors.BOLD}│{Colors.ENDC} {cols[3]:>{w[3]}} {Colors.BOLD}│{Colors.ENDC} {cols[4]:<{w[4]}} {Colors.BOLD}│{Colors.ENDC}"


def print_summary_table(
    sync_results: list[SyncResult], success_count: int, total: int, dry_run: bool
) -> None:
    # 1. Setup Data
    max_p = max((len(r["profile"]) for r in sync_results), default=25)
    w = [max(25, max_p), 10, 12, 10, 15]

    t_f, t_r, t_d = (
        sum(r["folders"] for r in sync_results),
        sum(r["rules"] for r in sync_results),
        sum(r["duration"] for r in sync_results),
    )
    all_ok = success_count == total
    t_status = ("✅ Ready" if dry_run else "✅ All Good") if all_ok else "❌ Errors"
    t_col = Colors.GREEN if all_ok else Colors.FAIL

    # 2. Render
    if not USE_COLORS:
        # Simple ASCII Fallback
        header = f"{'Profile ID':<{w[0]}} | {'Folders':>{w[1]}} | {'Rules':>{w[2]}} | {'Duration':>{w[3]}} | {'Status':<{w[4]}}"
        sep = "-" * len(header)
        print(
            f"\n{('DRY RUN' if dry_run else 'SYNC') + ' SUMMARY':^{len(header)}}\n{sep}\n{header}\n{sep}"
        )
        for r in sync_results:
            display_profile = "(Unspecified)" if r['profile'] == "dry-run-placeholder" else r['profile']
            print(
                f"{display_profile:<{w[0]}} | {r['folders']:>{w[1]}} | {r['rules']:>{w[2]},} | {r['duration']:>{w[3] - 1}.1f}s | {r['status_label']:<{w[4]}}"
            )
        print(
            f"{sep}\n{'TOTAL':<{w[0]}} | {t_f:>{w[1]}} | {t_r:>{w[2]},} | {t_d:>{w[3] - 1}.1f}s | {t_status:<{w[4]}}\n{sep}\n"
        )
        if t_f == 0:
            print(
                "  💡 Hint: Add folder URLs using --folder-url or in your config.yaml\n"
            )
        return

    # Unicode Table
    print(f"\n{print_line('┌', '─', '┐', w)}")
    title = f"{'DRY RUN' if dry_run else 'SYNC'} SUMMARY"
    print(
        f"{Colors.BOLD}│{Colors.CYAN if dry_run else Colors.HEADER}{title:^{sum(w) + 14}}{Colors.ENDC}{Colors.BOLD}│{Colors.ENDC}"
    )
    print(
        f"{print_line('├', '┬', '┤', w)}\n{print_row([f'{Colors.HEADER}Profile ID{Colors.ENDC}', f'{Colors.HEADER}Folders{Colors.ENDC}', f'{Colors.HEADER}Rules{Colors.ENDC}', f'{Colors.HEADER}Duration{Colors.ENDC}', f'{Colors.HEADER}Status{Colors.ENDC}'], w)}"
    )
    print(print_line("├", "┼", "┤", w))

    for r in sync_results:
        sc = Colors.GREEN if r["success"] else Colors.FAIL
        print(
            print_row(
                [
                    r["profile"],
                    str(r["folders"]),
                    f"{r['rules']:,}",
                    f"{r['duration']:.1f}s",
                    f"{sc}{r['status_label']}{Colors.ENDC}",
                ],
                w,
            )
        )

    print(
        f"{print_line('├', '┼', '┤', w)}\n{print_row(['TOTAL', str(t_f), f'{t_r:,}', f'{t_d:.1f}s', f'{t_col}{t_status}{Colors.ENDC}'], w)}"
    )
    print(f"{print_line('└', '┴', '┘', w)}\n")

    if t_f == 0:
        _print_hint(
            "  💡 Hint: Add folder URLs using --folder-url or in your config.yaml"
        )


def print_success_message(profile_ids: list[str]) -> None:
    """Prints a random success message and a link to the Control D dashboard."""
    success_msgs = [
        "✨ All synced!",
        "🚀 Ready for liftoff!",
        "🎨 Beautifully done!",
        "💎 Smooth operation!",
        "🌈 Perfect harmony!",
    ]
    chosen_msg = random.choice(success_msgs)

    if USE_COLORS:
        print(f"\n{Colors.GREEN}{chosen_msg}{Colors.ENDC}")
    else:
        print(f"\n{chosen_msg}")

    # Construct dashboard URL
    is_single_profile = (
        profile_ids
        and len(profile_ids) == 1
        and profile_ids[0] != "dry-run-placeholder"
    )
    is_multi_profile = len(profile_ids) > 1

    if not is_single_profile and not is_multi_profile:
        return

    dashboard_url = (
        f"https://controld.com/dashboard/profiles/{profile_ids[0]}/filters"
        if is_single_profile
        else "https://controld.com/dashboard/profiles"
    )

    if USE_COLORS:
        print(
            f"{Colors.CYAN}👀 View your changes: {Colors.UNDERLINE}{dashboard_url}{Colors.ENDC}"
        )
    else:
        print(f"👀 View your changes: {dashboard_url}")


def parse_args() -> argparse.Namespace:
    """
    Parses command-line arguments for the Control D sync tool.

    Supports profile IDs, folder URLs, dry-run mode, no-delete flag,
    plan JSON output file path, and an optional config file path.
    """
    parser = argparse.ArgumentParser(
        description="✨ Control D Sync: Keep your folders in sync with remote blocklists.",
        epilog="Run with --dry-run first to preview changes safely. Made with ❤️  for Control D users.",
    )
    parser.add_argument(
        "--profiles", help="Comma-separated list of profile IDs", default=None
    )
    parser.add_argument(
        "--folder-url", action="append", help="Folder JSON URL(s)", default=None
    )
    parser.add_argument("--dry-run", action="store_true", help="Plan only")
    parser.add_argument(
        "--no-delete", action="store_true", help="Do not delete existing folders"
    )
    parser.add_argument("--plan-json", help="Write plan to JSON file", default=None)
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear the persistent blocklist cache and exit",
    )
    parser.add_argument(
        "--config",
        "-c",
        metavar="FILE",
        help=(
            "Path to a YAML configuration file. "
            "Defaults to config.yaml / config.yml in the current directory "
            "or ~/.ctrld-sync/config.yaml / config.yml."
        ),
        default=None,
    )
    return parser.parse_args()


def make_col_separator(
    left: str, mid: str, right: str, horiz: str, col_widths: list[int]
) -> str:
    """Generates a table row separator with given box drawing characters and column widths."""
    parts = [horiz * (w + 2) for w in col_widths]
    return left + mid.join(parts) + right


def main() -> None:
    """
    Main entry point for Control D Sync.

    Loads environment configuration, validates inputs, warms up cache,
    and syncs profiles. Supports interactive prompts for missing credentials
    when running in a TTY. Prints summary statistics and exits with appropriate
    status code.
    """
    # SECURITY: Check .env permissions (after Colors is defined for NO_COLOR support)
    # This must happen BEFORE load_dotenv() to prevent reading secrets from world-readable files
    check_env_permissions()
    load_dotenv()

    global TOKEN
    # Re-initialize TOKEN to pick up values from .env (since load_dotenv was delayed)
    TOKEN = _clean_env_kv(os.getenv("TOKEN"), "TOKEN")

    args = parse_args()

    # Load persistent cache from disk (graceful degradation on any error)
    # NOTE: Called only after successful argument parsing so that `--help` or
    #       argument errors do not perform unnecessary filesystem I/O or logging.
    load_disk_cache()

    # Handle --clear-cache: delete cache file and exit immediately
    if args.clear_cache:
        cache_file = get_cache_dir() / "blocklists.json"
        if cache_file.exists():
            try:
                cache_file.unlink()
                print(
                    f"{Colors.GREEN}✓ Cleared blocklist cache: {cache_file}{Colors.ENDC}"
                )
            except OSError as e:
                print(f"{Colors.FAIL}✗ Failed to clear cache: {e}{Colors.ENDC}")
                exit(1)
        else:
            print(f"{Colors.CYAN}ℹ No cache file found, nothing to clear{Colors.ENDC}")
            _print_hint(
                "💡 Hint: The cache file will be created or updated after a successful sync run without --dry-run"
            )
        _disk_cache.clear()
        exit(0)
    profiles_arg = (
        _clean_env_kv(args.profiles or os.getenv("PROFILE", ""), "PROFILE") or ""
    )
    profile_ids = [extract_profile_id(p) for p in profiles_arg.split(",") if p.strip()]

    # --folder-url flags take highest precedence; otherwise use config file or defaults
    if args.folder_url:
        folder_urls = args.folder_url
    else:
        cfg = load_config(args.config)

        # Apply optional runtime tuning from config["settings"], if present.
        # We deliberately:
        #   * Keep CLI flags and environment variables as the highest-precedence sources.
        #   * Only touch well-known globals when they actually exist.
        #   * Validate that values are sane integers before applying them.
        settings = cfg.get("settings") or {}
        if isinstance(settings, dict):
            # Configure batch size for pushing rules if the global knobs exist.
            batch_size = settings.get("batch_size")
            if isinstance(batch_size, int) and batch_size > 0:
                if "BATCH_SIZE" in globals():
                    globals()["BATCH_SIZE"] = batch_size
                # Regenerate BATCH_KEYS since BATCH_SIZE changed
                if "BATCH_KEYS" in globals():
                    globals()["BATCH_KEYS"] = [
                        f"hostnames[{i}]" for i in range(batch_size)
                    ]

            # Configure number of concurrent workers used for folder deletions.
            delete_workers = settings.get("delete_workers")
            if (
                isinstance(delete_workers, int)
                and delete_workers > 0
                and "DELETE_WORKERS" in globals()
            ):
                globals()["DELETE_WORKERS"] = delete_workers

            # Configure maximum retry attempts for HTTP operations.
            max_retries = settings.get("max_retries")
            if (
                isinstance(max_retries, int)
                and max_retries >= 0
                and "MAX_RETRIES" in globals()
            ):
                globals()["MAX_RETRIES"] = max_retries
        folder_urls = [entry["url"] for entry in cfg.get("folders", [])]

    # Interactive prompts for missing config
    if not args.dry_run and sys.stdin.isatty():
        if not profile_ids:
            print(f"{Colors.CYAN}ℹ Profile ID is missing.{Colors.ENDC}")
            _print_hint(
                "  💡 Hint: You can find this in the URL of your profile in the Control D Dashboard (or just paste the URL)."
            )

            def validate_profile_input(value: str) -> bool:
                """Validates one or more profile IDs from comma-separated input."""
                ids = [extract_profile_id(p) for p in value.split(",") if p.strip()]
                return bool(ids) and all(
                    validate_profile_id(pid, log_errors=False) for pid in ids
                )

            p_input = get_validated_input(
                f"{Colors.BOLD}Enter Control D Profile ID:{Colors.ENDC} ",
                validate_profile_input,
                "Invalid ID(s) or URL(s). Must be a valid Profile ID or a Control D Profile URL. Comma-separate for multiple.",
            )
            profile_ids = [
                extract_profile_id(p) for p in p_input.split(",") if p.strip()
            ]

        if not TOKEN:
            print(f"{Colors.CYAN}ℹ API Token is missing.{Colors.ENDC}")
            _print_hint(
                "  💡 Hint: You can generate one at: https://controld.com/account/manage-account"
            )

            t_input = get_password(
                f"{Colors.BOLD}Enter Control D API Token {Colors.DIM}(typing will be hidden){Colors.ENDC}: ",
                lambda x: len(x) > 8,
                "Token seems too short. Please check your API token.",
            )
            TOKEN = t_input

    if not profile_ids and not args.dry_run:
        log.error(
            "PROFILE missing and --dry-run not set. Provide --profiles or set PROFILE env."
        )
        exit(1)

    if not TOKEN and not args.dry_run:
        log.error("TOKEN missing and --dry-run not set. Set TOKEN env for live sync.")
        exit(1)

    warm_up_cache(folder_urls)

    plan: list[PlanEntry] = []
    success_count = 0
    sync_results: list[SyncResult] = []

    profile_id = "unknown"
    start_time = time.time()

    try:
        for profile_id in profile_ids or ["dry-run-placeholder"]:
            start_time = time.time()
            # Skip validation for dry-run placeholder
            if profile_id != "dry-run-placeholder" and not validate_profile_id(
                profile_id
            ):
                sync_results.append(
                    {
                        "profile": profile_id,
                        "folders": 0,
                        "rules": 0,
                        "status_label": "❌ Invalid Profile ID",
                        "success": False,
                        "duration": 0.0,
                    }
                )
                continue

            log.info("Starting sync for profile %s", profile_id)
            status = sync_profile(
                profile_id,
                folder_urls,
                dry_run=args.dry_run,
                no_delete=args.no_delete,
                plan_accumulator=plan,
            )
            end_time = time.time()
            duration = end_time - start_time

            if status:
                success_count += 1

            # RESTORED STATS LOGIC: Calculate actual counts from the plan
            entry = next((p for p in plan if p["profile"] == profile_id), None)
            folder_count = len(entry["folders"]) if entry else 0
            rule_count = sum(f["rules"] for f in entry["folders"]) if entry else 0

            if args.dry_run:
                status_text = "✅ Planned" if status else "❌ Failed (Dry)"
            else:
                status_text = "✅ Success" if status else "❌ Failed"

            sync_results.append(
                {
                    "profile": profile_id,
                    "folders": folder_count,
                    "rules": rule_count,
                    "status_label": status_text,
                    "success": status,
                    "duration": duration,
                }
            )
    except KeyboardInterrupt:
        duration = time.time() - start_time
        if USE_COLORS:
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()
        print(
            f"\n{Colors.WARNING}⚠️  Sync cancelled by user. Finishing current task...{Colors.ENDC}"
        )

        # Try to recover stats for the interrupted profile
        entry = next((p for p in plan if p["profile"] == profile_id), None)
        folder_count = len(entry["folders"]) if entry else 0
        rule_count = sum(f["rules"] for f in entry["folders"]) if entry else 0

        sync_results.append(
            {
                "profile": profile_id,
                "folders": folder_count,
                "rules": rule_count,
                "status_label": "⛔ Cancelled",
                "success": False,
                "duration": duration,
            }
        )

    if args.plan_json:
        with open(args.plan_json, "w", encoding="utf-8") as f:
            json.dump(plan, f, indent=2)
        log.info("Plan written to %s", args.plan_json)

    # Print Summary Table
    # Determine the width for the Profile ID column (min 25)
    max_profile_len = max((len(r["profile"]) for r in sync_results), default=25)
    profile_col_width = max(25, max_profile_len)

    # Column widths
    w_profile = profile_col_width
    w_folders = 10
    w_rules = 12
    w_duration = 10
    w_status = 15

    col_widths = [w_profile, w_folders, w_rules, w_duration, w_status]

    # Calculate table width using a dummy separator
    dummy_sep = make_col_separator(Box.TL, Box.T, Box.TR, Box.H, col_widths)
    table_width = len(dummy_sep)

    title_text = " DRY RUN SUMMARY " if args.dry_run else " SYNC SUMMARY "
    title_color = Colors.CYAN if args.dry_run else Colors.HEADER

    # Top Border (Single Cell for Title)
    print("\n" + Box.TL + Box.H * (table_width - 2) + Box.TR)

    # Title Row
    visible_title = title_text.strip()
    inner_width = table_width - 2
    pad_left = (inner_width - len(visible_title)) // 2
    pad_right = inner_width - len(visible_title) - pad_left
    print(
        f"{Box.V}{' ' * pad_left}{title_color}{visible_title}{Colors.ENDC}{' ' * pad_right}{Box.V}"
    )

    # Separator between Title and Headers (introduces columns)
    print(make_col_separator(Box.L, Box.T, Box.R, Box.H, col_widths))

    # Header Row
    print(
        f"{Box.V} {Colors.BOLD}{'Profile ID':<{w_profile}}{Colors.ENDC} "
        f"{Box.V} {Colors.BOLD}{'Folders':>{w_folders}}{Colors.ENDC} "
        f"{Box.V} {Colors.BOLD}{'Rules':>{w_rules}}{Colors.ENDC} "
        f"{Box.V} {Colors.BOLD}{'Duration':>{w_duration}}{Colors.ENDC} "
        f"{Box.V} {Colors.BOLD}{'Status':<{w_status}}{Colors.ENDC} {Box.V}"
    )

    # Separator between Header and Body
    print(make_col_separator(Box.L, Box.X, Box.R, Box.H, col_widths))

    # Rows
    total_folders = 0
    total_rules = 0
    total_duration = 0.0

    for res in sync_results:
        # Use boolean success field for color logic
        status_color = Colors.GREEN if res["success"] else Colors.FAIL

        s_folders = f"{res['folders']:,}"
        s_rules = f"{res['rules']:,}"
        s_duration = f"{res['duration']:.1f}s"

        display_profile = "(Unspecified)" if res["profile"] == "dry-run-placeholder" else res["profile"]
        print(
            f"{Box.V} {display_profile:<{w_profile}} "
            f"{Box.V} {s_folders:>{w_folders}} "
            f"{Box.V} {s_rules:>{w_rules}} "
            f"{Box.V} {s_duration:>{w_duration}} "
            f"{Box.V} {status_color}{res['status_label']:<{w_status}}{Colors.ENDC} {Box.V}"
        )
        total_folders += res["folders"]
        total_rules += res["rules"]
        total_duration += res["duration"]

    # Separator between Body and Total
    print(make_col_separator(Box.L, Box.X, Box.R, Box.H, col_widths))

    # Total Row
    total = len(profile_ids or ["dry-run-placeholder"])
    all_success = success_count == total

    if args.dry_run:
        total_status_text = "✅ Ready" if all_success else "❌ Errors"
    else:
        total_status_text = "✅ All Good" if all_success else "❌ Errors"

    total_status_color = Colors.GREEN if all_success else Colors.FAIL

    s_total_folders = f"{total_folders:,}"
    s_total_rules = f"{total_rules:,}"
    s_total_duration = f"{total_duration:.1f}s"

    print(
        f"{Box.V} {Colors.BOLD}{'TOTAL':<{w_profile}}{Colors.ENDC} "
        f"{Box.V} {s_total_folders:>{w_folders}} "
        f"{Box.V} {s_total_rules:>{w_rules}} "
        f"{Box.V} {s_total_duration:>{w_duration}} "
        f"{Box.V} {total_status_color}{total_status_text:<{w_status}}{Colors.ENDC} {Box.V}"
    )
    # Bottom Border
    print(make_col_separator(Box.BL, Box.B, Box.BR, Box.H, col_widths))

    if total_folders == 0:
        print()  # Spacer
        _print_hint(
            "  💡 Hint: Add folder URLs using --folder-url or in your config.yaml"
        )

    # Success Delight
    if all_success and not args.dry_run:
        print_success_message(profile_ids)

    # Dry Run Next Steps
    if args.dry_run:
        print()  # Spacer
        if all_success:
            # Build the suggested command once so it stays consistent between
            # color and non-color output modes.
            cmd_parts = ["python", "main.py"]
            p_str = ",".join(profile_ids) if profile_ids else "<your-profile-id>"
            cmd_parts.append(f"--profiles {p_str}")

            # Reconstruct other args if they were used (optional but helpful)
            if args.folder_url:
                cmd_parts.extend(f"--folder-url {url}" for url in args.folder_url)

            cmd_str = " ".join(cmd_parts)

            if USE_COLORS:
                print(
                    f"{Colors.BOLD}👉 Ready to sync? Run the following command:{Colors.ENDC}"
                )
                print(f"   {Colors.CYAN}{cmd_str}{Colors.ENDC}")
            else:
                print("👉 Ready to sync? Run the following command:")
                print(f"   {cmd_str}")

            # Offer interactive restart if appropriate
            prompt_for_interactive_restart(profile_ids)

        else:
            if USE_COLORS:
                print(
                    f"{Colors.FAIL}⚠️  Dry run encountered errors. Please check the logs above.{Colors.ENDC}"
                )
            else:
                print("⚠️  Dry run encountered errors. Please check the logs above.")

    # Display API statistics
    total_api_calls = (
        _api_stats["control_d_api_calls"] + _api_stats["blocklist_fetches"]
    )
    if total_api_calls > 0:
        print(f"{Colors.BOLD}API Statistics:{Colors.ENDC}")
        print(f"  • Control D API calls: {_api_stats['control_d_api_calls']:>7,}")
        print(f"  • Blocklist fetches:   {_api_stats['blocklist_fetches']:>7,}")
        print(f"  • Total API requests:  {total_api_calls:>7,}")
        print()

    # Display cache statistics if any cache activity occurred
    if _cache_stats["hits"] + _cache_stats["misses"] + _cache_stats["validations"] > 0:
        print(f"{Colors.BOLD}Cache Statistics:{Colors.ENDC}")
        print(f"  • Hits (in-memory):    {_cache_stats['hits']:>7,}")
        print(f"  • Misses (downloaded): {_cache_stats['misses']:>7,}")
        print(f"  • Validations (304):   {_cache_stats['validations']:>7,}")
        if _cache_stats["errors"] > 0:
            print(f"  • Errors (non-fatal):  {_cache_stats['errors']:>7,}")

        # Calculate cache effectiveness
        total_requests = (
            _cache_stats["hits"] + _cache_stats["misses"] + _cache_stats["validations"]
        )
        if total_requests > 0:
            # Hits + validations = avoided full downloads
            cache_effectiveness = (
                (_cache_stats["hits"] + _cache_stats["validations"])
                / total_requests
                * 100
            )
            print(f"  • Cache effectiveness:  {cache_effectiveness:>6.1f}%")
        print()

    # Display rate limit information if available
    with _rate_limit_lock:
        if any(v is not None for v in _rate_limit_info.values()):
            print(f"{Colors.BOLD}API Rate Limit Status:{Colors.ENDC}")

            if _rate_limit_info["limit"] is not None:
                print(f"  • Requests limit:       {_rate_limit_info['limit']:>6,}")

            if _rate_limit_info["remaining"] is not None:
                remaining = _rate_limit_info["remaining"]
                limit = _rate_limit_info["limit"]

                # Color code based on remaining capacity
                if limit and limit > 0:
                    pct = (remaining / limit) * 100
                    if pct < 20:
                        color = Colors.FAIL  # Red for critical
                    elif pct < 50:
                        color = Colors.WARNING  # Yellow for caution
                    else:
                        color = Colors.GREEN  # Green for healthy
                    print(
                        f"  • Requests remaining:   {color}{remaining:>6,} ({pct:>5.1f}%){Colors.ENDC}"
                    )
                else:
                    print(f"  • Requests remaining:   {remaining:>6,}")

            if _rate_limit_info["reset"] is not None:
                reset_time = time.strftime(
                    "%H:%M:%S", time.localtime(_rate_limit_info["reset"])
                )
                print(f"  • Limit resets at:      {reset_time}")

            print()

    # Save cache to disk after successful sync (non-fatal if it fails)
    if not args.dry_run:
        save_disk_cache()

    total = len(profile_ids or ["dry-run-placeholder"])
    log.info(f"All profiles processed: {success_count}/{total} successful")
    exit(0 if success_count == total else 1)


if __name__ == "__main__":
    main()
