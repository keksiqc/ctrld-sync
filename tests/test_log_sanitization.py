import unittest
from unittest.mock import MagicMock, patch

import main


class TestLogSanitization(unittest.TestCase):
    def test_sanitize_for_log_escapes_ansi(self):
        """Test that sanitize_for_log escapes ANSI characters."""
        # ANSI Red color
        malicious_input = "\x1b[31mMalicious"
        sanitized = main.sanitize_for_log(malicious_input)

        # repr() escapes \x1b as \x1b (4 chars: \, x, 1, b)
        # So the output string should contain literal backslash
        self.assertIn("\\x1b", sanitized)
        # It should NOT contain the actual escape character
        self.assertNotIn("\x1b", sanitized)

    @patch("main.log")
    @patch("main.time.sleep")
    @patch("main._api_post")
    @patch("main._api_get")
    def test_create_folder_logs_unsafe_name(
        self, mock_get, mock_post, mock_sleep, mock_log
    ):
        """
        Verify that create_folder logs the raw name if not sanitized.
        We expect this to FAIL (or show raw usage) before the fix.
        """
        # Setup
        main.MAX_RETRIES = 1
        main.FOLDER_CREATION_DELAY = 0

        # Mock POST to succeed (returns None, assuming polling needed if direct ID missing)
        mock_post.return_value.json.return_value = {
            "body": {"group": {"something": "else"}}
        }

        # Mock GET to return empty groups (fail to find)
        mock_get.return_value.json.return_value = {"body": {"groups": []}}

        unsafe_name = "\x1b[31mUNSAFE"

        # Call
        ctx = main.SyncContext(
            profile_id="pid", client=MagicMock(), existing_rules=set()
        )
        action = main.RuleAction(do=0, status=1)
        main.create_folder(ctx, unsafe_name, action)

        # Check logs: ensure we do not log the raw unsafe name, but do log the sanitized name.
        # For this test file, I want it to PASS when the code is FIXED.
        # So I should assert that I DO NOT find raw unsafe_name, but I DO find sanitized name.

        sanitized_name = main.sanitize_for_log(unsafe_name)

        found_sanitized = False
        found_raw = False

        for call in mock_log.info.call_args_list:
            args = call[0]
            # Since it is an f-string in the source, we can't easily check format args.
            # We have to check the string content.
            # But wait, if the source is f"Folder '{name}'...", logging receives the formatted string.
            log_msg = args[0]
            if unsafe_name in log_msg:
                found_raw = True
            if sanitized_name in log_msg:
                found_sanitized = True

        if found_raw:
            print("VULNERABILITY DETECTED: Raw unsafe name found in logs.")

        # This assertion will FAIL before fix, and PASS after fix.
        self.assertTrue(found_sanitized, "Should find sanitized name in logs")
        self.assertFalse(found_raw, "Should not find raw name in logs")

    def test_sanitize_for_log_redacts_basic_auth(self):
        """Test that sanitize_for_log redacts Basic Auth credentials in URLs."""
        url = "https://user:password123@example.com/folder.json"
        sanitized = main.sanitize_for_log(url)
        self.assertNotIn("password123", sanitized)
        self.assertIn("[REDACTED]", sanitized)

    def test_sanitize_for_log_redacts_query_params(self):
        """Test that sanitize_for_log redacts sensitive query parameters."""
        url = "https://example.com/api?secret=mysecretkey&authorization=Bearer123"
        sanitized = main.sanitize_for_log(url)
        self.assertNotIn("mysecretkey", sanitized)
        self.assertNotIn("Bearer123", sanitized)
        self.assertIn("secret=[REDACTED]", sanitized)
        self.assertIn("authorization=[REDACTED]", sanitized)

    def test_sanitize_for_log_redacts_multiple_params(self):
        """Test redaction of multiple sensitive params while preserving safe ones."""
        url = "https://example.com/api?id=123&token=abc&name=user&api_key=def"
        sanitized = main.sanitize_for_log(url)
        self.assertIn("id=123", sanitized)
        self.assertIn("name=user", sanitized)
        self.assertIn("token=[REDACTED]", sanitized)
        self.assertIn("api_key=[REDACTED]", sanitized)

    def test_sanitize_for_log_case_insensitive(self):
        """Test that query param redaction is case-insensitive."""
        url = "https://example.com/api?TOKEN=mytoken"
        sanitized = main.sanitize_for_log(url)
        self.assertNotIn("mytoken", sanitized)
        self.assertIn("[REDACTED]", sanitized)


if __name__ == "__main__":
    unittest.main()
