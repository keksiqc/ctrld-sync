## 2025-04-07 - Add explicit loopback IP check to prevent SSRF bypass
**Vulnerability:** The `_is_safe_ip` function relied primarily on `is_private` and `is_global` properties of Python's `ipaddress` module to prevent SSRF loopback connections. While these often cover `127.0.0.1` and `::1`, edge cases and alternative loopback addresses may bypass these checks depending on OS/network configurations.
**Learning:** Defense-in-depth is essential when validating IPs. Relying solely on `is_private` or `is_global` without explicitly checking `is_loopback` creates potential edge cases where loopback traffic might not be caught, increasing SSRF risk.
**Prevention:** Explicitly check for `is_loopback` along with `is_unspecified` and `is_private` to ensure comprehensive outbound SSRF filtering.

## 2025-04-13 - Add explicit link-local IP check to prevent SSRF bypass
**Vulnerability:** The `_is_safe_ip` function lacked an explicit check for link-local IP addresses (e.g., `169.254.169.254`). This omission exposed the application to SSRF vulnerabilities targeting cloud provider metadata APIs (such as AWS IMDS, GCP Metadata, Azure Instance Metadata), which could lead to severe credential exposure.
**Learning:** Cloud metadata services reside on non-routable link-local IP addresses that are not always covered by standard `is_private` or `is_global` properties.
**Prevention:** Explicitly check `ip.is_link_local` alongside `is_loopback`, `is_unspecified`, and `is_private` when validating outbound destination IPs.

## 2025-05-02 - Add explicit reserved IP check to prevent SSRF bypass
**Vulnerability:** The `_is_safe_ip` function lacked an explicit check for reserved IP addresses (e.g., `240.0.0.0/4`). This omission exposed the application to potential SSRF vulnerabilities targeting these non-global, but potentially routable or internally handled IP ranges that bypass the `is_private` check.
**Learning:** Reserved IP addresses are not always covered by standard `is_private` or `is_global` properties and must be explicitly handled. The supported Python runtime exposes `is_reserved` on IP address objects, so use it directly to avoid fail-open behavior.
**Prevention:** Explicitly check `ip.is_reserved` alongside other non-global IP checks when validating outbound destination IPs.
