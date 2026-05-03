---
name: testing-ctrld-sync
description: Test the ctrld-sync Python CLI end-to-end. Use when validating CLI dry-run behavior, folder URL validation, SSRF safety checks, or sync planning changes.
---

# ctrld-sync CLI Testing

## App shape

- Single-file Python CLI app in `main.py`; no browser UI, database, or Docker service is required.
- Use shell-based testing evidence. Do not record the desktop unless a future change adds a GUI.
- Python >= 3.13 is required; use `uv` so the repo interpreter is selected.

## Setup

```bash
uv sync --all-extras
```

Repo environment config usually already runs this as maintenance. Re-run only if dependencies are missing or stale.

## Devin Secrets Needed

- `TOKEN`: Control D API token, required only for live sync runs.
- `PROFILE`: Control D profile ID, required only for live sync runs.

Dry-run and mocked SSRF validation tests do not require Control D secrets.

## Standard verification commands

```bash
uv run pytest tests/ -v
uv run ruff check .
uv run mypy main.py
uv run pre-commit run --all-files
```

For SSRF-only changes, the focused checks are usually sufficient before broader CI:

```bash
uv run pytest tests/test_ssrf_enhanced.py -v
uv run ruff check main.py tests/test_ssrf_enhanced.py
uv run mypy main.py
```

## Runtime dry-run testing

The documented safe runtime path is:

```bash
uv run python main.py --dry-run --folder-url https://example.com/config.json
```

`--dry-run` does not require `TOKEN` or `PROFILE`; `main.py` uses a `dry-run-placeholder` profile and avoids Control D API writes.

For deterministic SSRF tests, use a temporary Python harness that:

1. Calls `main.main()` with `--dry-run` and explicit `--folder-url` values.
2. Patches `socket.getaddrinfo` to return controlled IP addresses.
3. Patches `main._gh_get` so unsafe URLs fail immediately if fetched and safe URLs return minimal valid folder JSON.
4. Does not patch `main.main()`, `sync_profile()`, `validate_folder_url()`, `validate_hostname()`, or `_is_safe_ip()`.
5. Attaches an explicit in-memory handler to logger `control-d-sync`; the module-level logging handler is created at import time, so `contextlib.redirect_stderr()` alone might not capture warnings.
6. Patches `main.prompt_for_interactive_restart` to a no-op when running in a PTY so dry-run success does not block on the restart prompt.

Useful SSRF cases:

- `240.0.0.1` should be rejected as unsafe/reserved IPv4.
- `::ffff:8.8.8.8` should be allowed after IPv4-mapped IPv6 unwrapping.
- `::ffff:240.0.0.1` should be rejected.
- `64:ff9b::1` should be rejected and is useful for proving the explicit `is_reserved` guard blocks a reserved IPv6 address that may otherwise report `is_global=True`.

Expected dry-run evidence for a passing one-folder safe case:

- Output contains `DRY RUN SUMMARY`.
- Output includes the accepted folder name.
- Summary shows `1` folder, `1` rule, `Planned`, and `Ready`.
- Captured logger output contains unsafe-host warnings for rejected cases.
- Fetched URL list contains only the safe URL.

## Notes

- Clear `main.validate_hostname` and `main.validate_folder_url` caches before/after mocked DNS tests.
- Clear `_cache` and `_disk_cache` in runtime harnesses to avoid cached blocklist data bypassing fetch assertions.
- Avoid committing temporary harnesses, evidence files, screenshots, or test reports unless explicitly requested.
