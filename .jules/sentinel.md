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

## 2025-05-03 - Add missing Content-Type validation in fallback HTTP request branch
**Vulnerability:** A missing Content-Type validation check existed in the retry block of the `_gh_get` function. While the main HTTP request block checked that `Content-Type` was one of the allowed types (e.g. `application/json`), the fallback request branch (executed when the cache returns a 304 without cached data) did not. This omission allowed processing of unexpected or potentially malicious content types.
**Learning:** Security validations must be enforced consistently across all code paths, particularly in fallback, retry, and error-handling branches. Omitting checks in less frequently traversed paths creates defense-in-depth gaps that can be exploited if an attacker can trigger the fallback behavior.
**Prevention:** Apply identical security validation and sanitization logic to both primary and fallback code paths. Abstracting shared validation logic into dedicated helper functions can further prevent such discrepancies.

## 2025-05-14 - Sanitize Content-Type headers in exception messages to prevent log injection
**Vulnerability:** The application constructs `ValueError` exception strings that include dynamic, attacker-controlled values, specifically the `Content-Type` HTTP header and `url`, without sanitization. These exception strings are ultimately caught and logged by the system, creating a vulnerability for log injection or secret leakage if an attacker returns a malicious `Content-Type` or constructs a malicious URL.
**Learning:** Attacker-controlled values must be explicitly sanitized before being embedded in exception messages that will be logged. Relying on exception handlers to log strings safely without sanitizing the underlying data first creates a vector for log injection attacks, where malicious payloads can pollute the logs or exploit log viewing systems.
**Prevention:** Apply a sanitization function, such as `sanitize_for_log()`, to all dynamic HTTP headers or external inputs before concatenating them into exception messages.

## 2025-05-10 - Unsanitized Headers/URLs in Error Messages
**Vulnerability:** Log Injection & Secret Leakage via un-sanitized API response fields (e.g. `Content-Type` header) and un-sanitized target URLs embedded directly into exception messages (which are subsequently logged).
**Learning:** Even internal exception messages (like `ValueError`) that are meant to provide diagnostic context can become log injection vectors or leak redacted secrets (like tokens in query strings) if the embedded dynamic values (URLs, Headers) bypass the central logging sanitizer.
**Prevention:** Ensure that ALL dynamic variables passed into exception strings are explicitly wrapped in `sanitize_for_log()` if the exception string is eventually rendered to the console or log system.
