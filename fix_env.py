import contextlib
import os
import re
import tempfile

__all__ = ["fix_env", "clean_val", "escape_val"]


# Helper to clean quotes (curly or straight)
def clean_val(val):
    if not val:
        return ""
    # Remove surrounding quotes of any kind
    val = val.strip()
    return re.sub(r"^[\"\u201c\u201d\']|[\"\u201c\u201d\']$", "", val)


# Helper to escape value for shell
def escape_val(val):
    if not val:
        return ""
    # Escape backslashes first, then double quotes
    return val.replace("\\", "\\\\").replace('"', '\\"')


def _parse_env_content(content):
    parsed = {}
    for line in content.splitlines():
        if "=" in line:
            key, val = line.split("=", 1)
            parsed[key.strip()] = clean_val(val.strip())
    return parsed


def _resolve_assignments(parsed):
    token_val = parsed.get("TOKEN", "")
    profile_val = parsed.get("PROFILE", "")

    real_token = ""
    real_profiles = ""

    # Heuristic: Token usually starts with 'api.' or is long/alphanumeric
    # Profiles are usually comma-separated lists of ~12 chars
    if "api." in profile_val or len(profile_val) > 40:
        real_token = profile_val
    elif "api." in token_val or len(token_val) > 40:
        real_token = token_val

    if "," in token_val or (
        len(token_val) < 20 and len(token_val) > 0 and "api." not in token_val
    ):
        real_profiles = token_val
    elif "," in profile_val or (
        len(profile_val) < 20 and len(profile_val) > 0 and "api." not in profile_val
    ):
        real_profiles = profile_val

    # If we couldn't resolve clearly, fall back to what was there but cleaned
    if not real_token:
        real_token = token_val
    if not real_profiles:
        real_profiles = profile_val

    return real_token, real_profiles


def _write_env_securely(new_content):
    # Security: Write using os.open to a temp file, then os.replace to prevent TOCTOU
    # symlink attacks and ensure 0o600 permissions at creation time.
    # Use O_EXCL to prevent writing to an existing symlink or file.
    temp_file = None
    try:
        # tempfile.NamedTemporaryFile securely creates a unique file with O_CREAT | O_EXCL and 0o600 permissions
        # We specify dir="." to keep it on the same filesystem as .env for atomic os.replace
        with tempfile.NamedTemporaryFile(
            mode="w",
            delete=False,
            prefix=".env.",
            suffix=".tmp",
            dir=".",
            encoding="utf-8",
        ) as f:
            temp_file = f.name
            f.write(new_content)

        # Atomic replace
        os.replace(temp_file, ".env")
        return True

    except OSError as e:
        print(f"Error writing .env: {e}")
        # Clean up temp file on error
        if temp_file and os.path.exists(temp_file):
            with contextlib.suppress(OSError):
                os.unlink(temp_file)
        return False


def fix_env():
    """Read `.env`, correct swapped TOKEN/PROFILE assignments, and rewrite securely.

    Uses heuristics to detect if TOKEN and PROFILE values have been swapped
    (e.g., the API key ends up in PROFILE and the profile ID in TOKEN).
    Writes the corrected values back using an atomic O_EXCL temp-file replace
    with 0o600 permissions to prevent symlink attacks and privilege escalation.

    Prints a notice and returns early if `.env` is not found.
    """
    # Security: Don't follow symlinks when fixing .env
    # This prevents attacks where .env is symlinked to a system file
    if os.path.islink(".env"):
        print(
            "Security Warning: .env is a symlink. Skipping to avoid damaging target file."
        )
        return

    try:
        with open(".env") as f:
            content = f.read()
    except FileNotFoundError:
        print("No .env file found.")
        return

    parsed = _parse_env_content(content)
    real_token, real_profiles = _resolve_assignments(parsed)

    # Write back with standard quotes
    new_content = (
        f'TOKEN="{escape_val(real_token)}"\nPROFILE="{escape_val(real_profiles)}"\n'
    )

    if _write_env_securely(new_content):
        print(
            "Fixed .env file: standardized quotes and corrected variable assignments."
        )
        print("Security: .env permissions set to 600 (read/write only by owner).")


if __name__ == "__main__":
    fix_env()
