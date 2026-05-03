"""Tests for parallel folder deletion functionality."""

import concurrent.futures
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_env(monkeypatch):
    """Set up test environment with required TOKEN."""
    monkeypatch.setenv("TOKEN", "test-token-123")
    monkeypatch.setenv("NO_COLOR", "1")
    # Use monkeypatch.delitem so sys.modules["main"] is restored after each test.
    # This prevents stale cross-module references (e.g. api_client._sanitize_fn)
    # from leaking into subsequent tests.
    monkeypatch.delitem(sys.modules, "main", raising=False)
    # Save and restore api_client._sanitize_fn, which main.py updates when
    # it is (re-)imported.  Without this, the re-import inside the test would
    # leave api_client pointing at a different module instance's sanitize_for_log.
    import api_client as _ac

    orig_sanitize_fn = _ac._sanitize_fn
    monkeypatch.setattr(_ac, "_sanitize_fn", orig_sanitize_fn)
    # Do the same for cache._sanitize_fn for the same reason.
    import cache as _cache_mod

    orig_cache_sanitize_fn = _cache_mod._sanitize_fn
    monkeypatch.setattr(_cache_mod, "_sanitize_fn", orig_cache_sanitize_fn)


def test_delete_workers_constant_exists(mock_env):
    """Test that DELETE_WORKERS constant is defined."""
    import main

    assert hasattr(main, "DELETE_WORKERS")
    assert isinstance(main.DELETE_WORKERS, int)
    assert main.DELETE_WORKERS == 3  # Conservative value for rate limiting


def test_parallel_deletion_uses_threadpool(mock_env, monkeypatch):
    """Test that parallel deletion uses ThreadPoolExecutor with correct workers."""
    import main

    # Mock dependencies
    mock_client = MagicMock()
    mock_client_ctx = MagicMock(
        __enter__=lambda self: mock_client, __exit__=lambda *args: None
    )
    monkeypatch.setattr(main, "_api_client", lambda: mock_client_ctx)
    monkeypatch.setattr(
        main,
        "verify_access_and_get_folders",
        lambda *args: {"FolderA": "id1", "FolderB": "id2"},
    )
    monkeypatch.setattr(main, "delete_folder", lambda *args: True)
    monkeypatch.setattr(main, "get_all_existing_rules", lambda *args: set())
    monkeypatch.setattr(main, "countdown_timer", lambda *args: None)
    monkeypatch.setattr(main, "_process_single_folder", lambda *args: True)
    # Mock validation functions with cache_clear methods
    mock_validate = MagicMock(return_value=True)
    mock_validate.cache_clear = MagicMock()
    monkeypatch.setattr(main, "validate_folder_url", mock_validate)
    mock_validate_hostname = MagicMock(return_value=True)
    mock_validate_hostname.cache_clear = MagicMock()
    monkeypatch.setattr(main, "validate_hostname", mock_validate_hostname)

    def mock_fetch(url):
        if url == "url1":
            return {"group": {"group": "FolderA"}}
        if url == "url2":
            return {"group": {"group": "FolderB"}}
        return None

    monkeypatch.setattr(main, "fetch_folder_data", mock_fetch)

    # Track ThreadPoolExecutor calls
    executor_calls: list[dict[str, object]] = []

    from typing import Any

    class TrackedExecutor(concurrent.futures.ThreadPoolExecutor):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            executor_calls.append(kwargs)
            super().__init__(*args, **kwargs)

    with patch("concurrent.futures.ThreadPoolExecutor", TrackedExecutor):
        main.sync_profile("test-profile", ["url1", "url2"], no_delete=False)

    # Verify ThreadPoolExecutor was called with DELETE_WORKERS
    delete_executor_found = False
    for call in executor_calls:
        if call.get("max_workers") == main.DELETE_WORKERS:
            delete_executor_found = True
            break

    assert delete_executor_found, (
        f"Expected ThreadPoolExecutor with max_workers={main.DELETE_WORKERS} "
        f"for deletion, but got calls: {executor_calls}"
    )


def test_parallel_deletion_handles_exceptions(mock_env, monkeypatch):
    """Test that exceptions during parallel deletion are properly handled and logged."""
    import main

    # Mock client
    mock_client = MagicMock()
    mock_client_ctx = MagicMock(
        __enter__=lambda self: mock_client, __exit__=lambda *args: None
    )
    monkeypatch.setattr(main, "_api_client", lambda: mock_client_ctx)
    monkeypatch.setattr(
        main,
        "verify_access_and_get_folders",
        lambda *args: {"Folder1": "id1"},
    )

    # Mock delete_folder to raise an exception
    def failing_delete(*args):
        raise RuntimeError("API Error")

    monkeypatch.setattr(main, "delete_folder", failing_delete)
    monkeypatch.setattr(main, "get_all_existing_rules", lambda *args: set())
    monkeypatch.setattr(main, "countdown_timer", lambda *args: None)
    monkeypatch.setattr(main, "_process_single_folder", lambda *args: True)

    # Mock validation functions with cache_clear methods
    mock_validate = MagicMock(return_value=True)
    mock_validate.cache_clear = MagicMock()
    monkeypatch.setattr(main, "validate_folder_url", mock_validate)
    mock_validate_hostname = MagicMock(return_value=True)
    mock_validate_hostname.cache_clear = MagicMock()
    monkeypatch.setattr(main, "validate_hostname", mock_validate_hostname)

    monkeypatch.setattr(
        main, "fetch_folder_data", lambda url: {"group": {"group": "Folder1"}}
    )

    # Capture log output
    log_calls = []
    original_error = main.log.error

    def mock_error(*args, **kwargs):
        log_calls.append((args, kwargs))
        return original_error(*args, **kwargs)

    monkeypatch.setattr(main.log, "error", mock_error)

    # Should not crash, should log error
    main.sync_profile("test-profile", ["url"], no_delete=False)

    # Verify error was logged
    assert len(log_calls) > 0, "Expected error to be logged"
    # Check that sanitization was applied
    logged_message = str(log_calls[0][0])
    assert "Folder1" in logged_message or "[REDACTED]" in logged_message


def test_parallel_deletion_sanitizes_exception(mock_env, monkeypatch):
    """Test that exception messages are sanitized before logging."""
    import main

    # Mock client
    mock_client = MagicMock()
    mock_client_ctx = MagicMock(
        __enter__=lambda self: mock_client, __exit__=lambda *args: None
    )
    monkeypatch.setattr(main, "_api_client", lambda: mock_client_ctx)
    monkeypatch.setattr(
        main,
        "verify_access_and_get_folders",
        lambda *args: {"TestFolder": "id1"},
    )

    # Mock delete_folder to raise exception with potentially dangerous content
    def failing_delete(*args):
        raise RuntimeError("Error with TOKEN: test-token-123 and control chars\x1b[0m")

    monkeypatch.setattr(main, "delete_folder", failing_delete)
    monkeypatch.setattr(main, "get_all_existing_rules", lambda *args: set())
    monkeypatch.setattr(main, "countdown_timer", lambda *args: None)
    monkeypatch.setattr(main, "_process_single_folder", lambda *args: True)

    # Mock validation functions with cache_clear methods
    mock_validate = MagicMock(return_value=True)
    mock_validate.cache_clear = MagicMock()
    monkeypatch.setattr(main, "validate_folder_url", mock_validate)
    mock_validate_hostname = MagicMock(return_value=True)
    mock_validate_hostname.cache_clear = MagicMock()
    monkeypatch.setattr(main, "validate_hostname", mock_validate_hostname)

    monkeypatch.setattr(
        main, "fetch_folder_data", lambda url: {"group": {"group": "TestFolder"}}
    )

    # Capture log output
    log_calls = []

    def mock_error(*args, **kwargs):
        log_calls.append((args, kwargs))

    monkeypatch.setattr(main.log, "error", mock_error)

    # Run sync
    main.sync_profile("test-profile", ["url"], no_delete=False)

    # Verify TOKEN was redacted and control chars were escaped
    assert len(log_calls) > 0
    logged_args = log_calls[0][0]
    logged_str = " ".join(str(arg) for arg in logged_args)

    # TOKEN should be redacted
    assert "test-token-123" not in logged_str, "TOKEN should be redacted"
    assert "[REDACTED]" in logged_str, "TOKEN should be replaced with [REDACTED]"

    # Control characters should be escaped
    assert "\x1b" not in logged_str, "Control characters should be escaped"
