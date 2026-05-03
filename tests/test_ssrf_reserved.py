import socket
import unittest
from unittest.mock import PropertyMock, patch

import main


class TestSSRFReserved(unittest.TestCase):
    def setUp(self):
        main.validate_folder_url.cache_clear()
        main.validate_hostname.cache_clear()

    def tearDown(self):
        main.validate_folder_url.cache_clear()
        main.validate_hostname.cache_clear()

    def test_domain_resolving_to_reserved_ip(self):
        """
        Test that a domain resolving to a reserved IP (e.g. 240.0.0.1) is blocked.
        """
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            # Simulate resolving to 240.0.0.1
            mock_getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("240.0.0.1", 443))
            ]

            url = "https://reserved.example.com/config.json"
            result = main.validate_folder_url(url)
            self.assertFalse(result, "Should block domain resolving to reserved IP")

    def test_reserved_ip_guard_blocks_when_other_flags_allow(self):
        """
        Test that the explicit is_reserved guard blocks independently of other flags.
        """
        with (
            patch.object(
                main.ipaddress.IPv4Address, "is_private", new_callable=PropertyMock
            ) as mock_private,
            patch.object(
                main.ipaddress.IPv4Address, "is_global", new_callable=PropertyMock
            ) as mock_global,
        ):
            mock_private.return_value = False
            mock_global.return_value = True

            self.assertFalse(
                main._is_safe_ip(main.ipaddress.IPv4Address("240.0.0.1")),
                "Should block reserved IPs even when other flags would allow them",
            )


if __name__ == "__main__":
    unittest.main()
