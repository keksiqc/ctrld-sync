import socket
from unittest.mock import patch

import main


def test_validate_hostname_caching():
    """
    Verify that validate_hostname caches results and avoids redundant DNS lookups.
    """
    # Mock socket.getaddrinfo
    with patch("socket.getaddrinfo") as mock_dns:
        # Setup mock return value (valid IP)
        mock_dns.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ]

        # Clear cache to start fresh
        main.validate_hostname.cache_clear()

        # First call - should trigger DNS lookup
        assert main.validate_hostname("example.com") is True
        assert mock_dns.call_count == 1

        # Second call - should use cache
        assert main.validate_hostname("example.com") is True
        assert mock_dns.call_count == 1  # Still 1

        # different hostname - should trigger DNS lookup
        assert main.validate_hostname("google.com") is True
        assert mock_dns.call_count == 2


def test_validate_hostname_security():
    """
    Verify security checks in validate_hostname.
    """
    # Localhost
    assert main.validate_hostname("localhost") is False
    assert main.validate_hostname("127.0.0.1") is False
    assert main.validate_hostname("::1") is False

    # Private IP
    assert main.validate_hostname("192.168.1.1") is False

    # Domain resolving to private IP
    with patch("socket.getaddrinfo") as mock_dns:
        # Return private IP
        mock_dns.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 443))
        ]
        main.validate_hostname.cache_clear()

        assert main.validate_hostname("private.local") is False


def test_validate_folder_url_uses_validate_hostname():
    """
    Verify that validate_folder_url calls validate_hostname.
    """
    with patch("main.validate_hostname") as mock_validate:
        mock_validate.return_value = True

        # Clear cache
        main.validate_folder_url.cache_clear()
        main.validate_hostname.cache_clear()

        url = "https://example.com/data.json"
        assert main.validate_folder_url(url) is True

        mock_validate.assert_called_with("example.com")

        # Invalid hostname
        mock_validate.return_value = False

        # Clear cache again because URL is the same
        main.validate_folder_url.cache_clear()
        main.validate_hostname.cache_clear()

        assert main.validate_folder_url(url) is False
