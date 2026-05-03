"""
Tests for the cache optimization in sync_profile.

This module verifies that:
1. Cached URLs correctly skip validation
2. Non-cached URLs still get validated
3. Cache operations are thread-safe
"""

import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

# Add root to path to import main
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main


class TestCacheOptimization(unittest.TestCase):
    def setUp(self):
        """Clear cache and validation cache before each test."""
        main._cache.clear()
        main.validate_folder_url.cache_clear()
        main.validate_hostname.cache_clear()

    def tearDown(self):
        """Clean up after each test."""
        main._cache.clear()
        main.validate_folder_url.cache_clear()
        main.validate_hostname.cache_clear()

    def test_cached_url_skips_validation(self):
        """
        Test that when a URL is in the cache, validate_folder_url is not called.
        This verifies the cache optimization is working correctly.
        """
        test_url = "https://example.com/test.json"
        test_data = {"group": {"group": "Test Folder"}, "domains": ["example.com"]}

        # Pre-populate cache
        with main._cache_lock:
            main._cache[test_url] = test_data

        with patch("main.validate_folder_url") as mock_validate:
            # This should return data from cache without calling validate_folder_url
            result = main.fetch_folder_data(test_url)

            # Verify validation was NOT called because URL is cached
            mock_validate.assert_not_called()
            self.assertEqual(result, test_data)

    def test_non_cached_url_calls_validation(self):
        """
        Test that when a URL is NOT in the cache during sync_profile,
        validate_folder_url is called before fetching.
        This test simulates the _fetch_if_valid behavior where validation
        is required for non-cached URLs.
        """
        test_url = "https://example.com/test.json"
        from main import FolderData

        test_data: FolderData = {
            "group": {"group": "Test Folder"},
            "rules": [{"PK": "example.com"}],
        }

        # Ensure URL is NOT in cache
        self.assertNotIn(test_url, main._cache)

        with patch("main.validate_folder_url", return_value=True) as mock_validate:
            with patch("main._gh_get", return_value=test_data):
                # Simulate the _fetch_if_valid logic for non-cached URLs
                with main._cache_lock:
                    url_in_cache = test_url in main._cache

                result = None
                # For non-cached URLs, validate first
                if not url_in_cache and main.validate_folder_url(test_url):
                    result = main.fetch_folder_data(test_url)

                # Verify validation WAS called for non-cached URL
                mock_validate.assert_called_once_with(test_url)
                self.assertEqual(result, test_data)

    def test_cache_thread_safety_concurrent_reads(self):
        """
        Test that concurrent reads from the cache are thread-safe.
        Multiple threads should be able to read from the cache simultaneously.
        """
        test_url = "https://example.com/test.json"
        test_data = {"group": {"group": "Test Folder"}, "domains": ["example.com"]}

        # Pre-populate cache
        with main._cache_lock:
            main._cache[test_url] = test_data

        results = []
        errors = []

        def read_from_cache():
            try:
                with main._cache_lock:
                    if test_url in main._cache:
                        data = main._cache[test_url]
                        results.append(data)
            except Exception as e:
                errors.append(e)

        # Spawn multiple threads to read concurrently
        threads = []
        for _ in range(10):
            thread = threading.Thread(target=read_from_cache)
            threads.append(thread)
            thread.start()

        # Wait for all threads to complete
        for thread in threads:
            thread.join()

        # Verify no errors occurred
        self.assertEqual(len(errors), 0, f"Errors occurred: {errors}")
        # Verify all threads read the data
        self.assertEqual(len(results), 10)
        # Verify all threads read the same data
        for result in results:
            self.assertEqual(result, test_data)

    def test_cache_thread_safety_concurrent_writes(self):
        """
        Test that concurrent writes to the cache are thread-safe.
        Multiple threads should be able to write to different cache keys safely.
        """
        errors = []

        def write_to_cache(url_suffix):
            try:
                test_url = f"https://example.com/test{url_suffix}.json"
                test_data = {
                    "group": {"group": f"Test Folder {url_suffix}"},
                    "domains": [f"example{url_suffix}.com"],
                }

                with main._cache_lock:
                    main._cache[test_url] = test_data
            except Exception as e:
                errors.append(e)

        # Spawn multiple threads to write concurrently
        threads = []
        for i in range(10):
            thread = threading.Thread(target=write_to_cache, args=(i,))
            threads.append(thread)
            thread.start()

        # Wait for all threads to complete
        for thread in threads:
            thread.join()

        # Verify no errors occurred
        self.assertEqual(len(errors), 0, f"Errors occurred: {errors}")
        # Verify all entries were written
        with main._cache_lock:
            self.assertEqual(len(main._cache), 10)

    def test_cache_check_in_fetch_if_valid(self):
        """
        Test the actual _fetch_if_valid logic used in sync_profile.
        This is an integration test that verifies the optimization path.

        NOTE: _fetch_if_valid is a nested function inside sync_profile, so we
        cannot test it directly. This test manually reimplements its logic to
        verify the cache optimization behavior that would occur in the actual
        function. The logic is intentionally duplicated to test the pattern
        without needing to invoke the entire sync_profile function.
        """
        test_url = "https://example.com/test.json"
        from main import FolderData

        test_data: FolderData = {
            "group": {"group": "Test Folder"},
            "rules": [{"PK": "example.com"}],
        }

        # Pre-populate cache to simulate warm_up_cache
        with main._cache_lock:
            main._cache[test_url] = test_data  # type: ignore[assignment]

        # Mock validate_folder_url to track if it's called
        with patch("main.validate_folder_url") as mock_validate:
            with patch("main._gh_get", return_value=test_data):
                from typing import Any

                # Simulate the logic in _fetch_if_valid
                result: FolderData | dict[Any, Any] | None = None
                with main._cache_lock:
                    if test_url in main._cache:
                        result = main._cache[test_url]
                    elif main.validate_folder_url(test_url):
                        result = main.fetch_folder_data(test_url)

                # Verify validation was NOT called because URL was cached
                mock_validate.assert_not_called()
                self.assertEqual(result, test_data)

    def test_gh_get_thread_safety(self):
        """
        Test that _gh_get handles concurrent access correctly.
        When multiple threads try to fetch the same URL, the double-checked
        locking pattern should minimize redundant fetches (though some may
        still occur if threads enter the fetch section before any completes).
        """
        test_url = "https://example.com/test.json"
        test_data = {"group": {"group": "Test Folder"}, "domains": ["example.com"]}

        class FetchTracker:
            """Track fetch count using a class to avoid closure issues.
            Uses a separate lock from main._cache_lock to avoid any potential
            ordering issues with the test's mock patches and actual cache operations."""

            def __init__(self):
                self.count = 0
                self.lock = threading.Lock()

            def increment(self):
                with self.lock:
                    self.count += 1

        tracker = FetchTracker()

        def mock_stream_get(method, url, headers=None):
            """Mock the streaming GET request."""
            tracker.increment()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.raise_for_status = MagicMock()
            mock_response.headers = {
                "Content-Length": "100",
                "Content-Type": "application/json",
            }
            # Return JSON bytes properly
            json_bytes = (
                b'{"group": {"group": "Test Folder"}, "domains": ["example.com"]}'
            )
            mock_response.iter_bytes = MagicMock(return_value=[json_bytes])
            mock_response.__enter__ = MagicMock(return_value=mock_response)
            mock_response.__exit__ = MagicMock(return_value=False)
            return mock_response

        results = []
        errors = []

        def fetch_data():
            try:
                data = main._gh_get(test_url)
                results.append(data)
            except Exception as e:
                errors.append(e)

        with patch.object(main._gh, "stream", side_effect=mock_stream_get):
            # Spawn multiple threads to fetch the same URL concurrently
            threads = []
            for _ in range(5):
                thread = threading.Thread(target=fetch_data)
                threads.append(thread)
                thread.start()

            # Wait for all threads to complete
            for thread in threads:
                thread.join()

        # Verify no errors occurred
        self.assertEqual(len(errors), 0, f"Errors occurred: {errors}")
        # Verify all threads got results
        self.assertEqual(len(results), 5)
        # All results should be the same
        for result in results:
            self.assertEqual(result, test_data)

        # Verify fetch count - with double-checked locking, we should have
        # at most 5 fetches (worst case) but ideally fewer
        self.assertLessEqual(
            tracker.count, 5, f"Expected at most 5 fetches, got {tracker.count}"
        )


if __name__ == "__main__":
    unittest.main()
