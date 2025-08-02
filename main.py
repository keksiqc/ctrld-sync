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

Configuration:
- TOKEN: Your Control D API token
- PROFILE: Comma-separated list of profile IDs to sync
- PROFILE_X_FOLDERS: Comma-separated list of folder URLs for profile at index X
  (X=0 for first profile, X=1 for second, etc. If not set, uses default folder URLs)

Example environment variables:
TOKEN=your_api_token_here
PROFILE=first_profile_id,second_profile_id
PROFILE_0_FOLDERS=https://example.com/folder1.json,https://example.com/folder2.json
PROFILE_1_FOLDERS=https://example.com/folder3.json

Nothing fancy, just works.
"""

import os
import logging
import time
from typing import Dict, List, Optional, Any, Set

import httpx
from dotenv import load_dotenv

# --------------------------------------------------------------------------- #
# 0. Bootstrap – load secrets and configure logging
# --------------------------------------------------------------------------- #
load_dotenv()

# Enable debug logging if DEBUG environment variable is set
debug_mode = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")
log_level = logging.DEBUG if debug_mode else logging.INFO

logging.basicConfig(
    level=log_level,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("control-d-sync")

# --------------------------------------------------------------------------- #
# 1. Constants – tweak only here
# --------------------------------------------------------------------------- #
API_BASE = "https://api.controld.com/profiles"
TOKEN = os.getenv("TOKEN")

# Accept either a single profile id or a comma-separated list
PROFILE_IDS = [p.strip() for p in os.getenv("PROFILE", "").split(",") if p.strip()]

# Default URLs of the JSON block-lists we want to import (used if no profile-specific config)
DEFAULT_FOLDER_URLS = [
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/ultimate-known_issues-allow-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/apple-private-relay-allow-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/spam-tlds-allow-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/referral-allow-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/badware-hoster-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/native-tracker-amazon-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/native-tracker-microsoft-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/native-tracker-tiktok-aggressive-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/spam-idns-folder.json",
]

# Profile-specific folder configuration
# Format: {profile_id: [list_of_folder_urls]}
PROFILE_FOLDERS = {}


def get_profile_folders(profile_index: int) -> List[str]:
    """Get folder URLs for a specific profile by index, with fallback to defaults."""
    env_key = f"PROFILE_{profile_index}_FOLDERS"
    folder_urls_str = os.getenv(env_key)

    log.debug(f"Looking for environment variable: {env_key}")
    log.debug(f"Found value: {folder_urls_str}")

    if folder_urls_str:
        # Split by comma and clean each URL (strip whitespace and quotes)
        urls = []
        for url in folder_urls_str.split(","):
            cleaned_url = url.strip().strip('"').strip("'")
            if cleaned_url:
                urls.append(cleaned_url)

        log.info(f"Profile {profile_index + 1}: using {len(urls)} custom folder URLs")
        log.debug(f"Profile {profile_index + 1} custom URLs: {urls}")

        # Log each cleaned URL for debugging
        for idx, cleaned_url in enumerate(urls):
            log.debug(f"Cleaned URL {idx + 1}: {cleaned_url}")

        return urls
    else:
        log.info(
            f"Profile {profile_index + 1}: no custom folders found, using {len(DEFAULT_FOLDER_URLS)} default folder URLs"
        )
        log.debug(f"Profile {profile_index + 1} default URLs: {DEFAULT_FOLDER_URLS}")
        return DEFAULT_FOLDER_URLS


# Initialize profile folders for each profile ID using index-based lookup
for i, profile_id in enumerate(PROFILE_IDS):
    PROFILE_FOLDERS[profile_id] = get_profile_folders(i)

# Debug: Print all environment variables that start with PROFILE_
log.debug("All PROFILE_* environment variables:")
for key, value in os.environ.items():
    if key.startswith("PROFILE_"):
        log.debug(f"  {key}={value}")

BATCH_SIZE = 500
MAX_RETRIES = 3
RETRY_DELAY = 1  # seconds
FOLDER_CREATION_DELAY = 2  # seconds to wait after creating a folder

# --------------------------------------------------------------------------- #
# 2. Clients
# --------------------------------------------------------------------------- #
# Control-D API client (with auth)
_api = httpx.Client(
    headers={
        "Accept": "application/json",
        "Authorization": f"Bearer {TOKEN}",
    },
    timeout=30,
)

# GitHub raw client (no auth, no headers)
_gh = httpx.Client(timeout=30)

# --------------------------------------------------------------------------- #
# 3. Helpers
# --------------------------------------------------------------------------- #
# simple in-memory cache: url -> decoded JSON
_cache: Dict[str, Dict] = {}


def _api_get(url: str) -> httpx.Response:
    """GET helper for Control-D API with retries."""
    return _retry_request(lambda: _api.get(url))


def _api_delete(url: str) -> httpx.Response:
    """DELETE helper for Control-D API with retries."""
    return _retry_request(lambda: _api.delete(url))


def _api_post(url: str, data: Dict) -> httpx.Response:
    """POST helper for Control-D API with retries."""
    return _retry_request(lambda: _api.post(url, data=data))


def _api_post_form(url: str, data: Dict) -> httpx.Response:
    """POST helper for form data with retries."""
    return _retry_request(
        lambda: _api.post(
            url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    )


def _retry_request(request_func, max_retries=MAX_RETRIES, delay=RETRY_DELAY):
    """Retry a request function with exponential backoff."""
    for attempt in range(max_retries):
        try:
            response = request_func()
            response.raise_for_status()
            return response
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            if attempt == max_retries - 1:
                # Log the response content if available
                if hasattr(e, "response") and e.response is not None:
                    log.error(f"Response content: {e.response.text}")
                raise
            wait_time = delay * (2**attempt)
            log.warning(
                f"Request failed (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s..."
            )
            time.sleep(wait_time)


def _gh_get(url: str) -> Dict:
    """Fetch JSON from GitHub (cached)."""
    if url not in _cache:
        r = _gh.get(url)
        r.raise_for_status()
        _cache[url] = r.json()
    return _cache[url]


def list_existing_folders(profile_id: str) -> Dict[str, str]:
    """Return folder-name -> folder-id mapping."""
    try:
        data = _api_get(f"{API_BASE}/{profile_id}/groups").json()
        folders = data.get("body", {}).get("groups", [])
        return {
            f["group"].strip(): f["PK"]
            for f in folders
            if f.get("group") and f.get("PK")
        }
    except (httpx.HTTPError, KeyError) as e:
        log.error(f"Failed to list existing folders: {e}")
        return {}


def get_all_existing_rules(profile_id: str) -> Set[str]:
    """Get all existing rules from all folders in the profile."""
    all_rules = set()

    try:
        # Get rules from root folder (no folder_id)
        try:
            data = _api_get(f"{API_BASE}/{profile_id}/rules").json()
            root_rules = data.get("body", {}).get("rules", [])
            for rule in root_rules:
                if rule.get("PK"):
                    all_rules.add(rule["PK"])

            log.debug(f"Found {len(root_rules)} rules in root folder")

        except httpx.HTTPError as e:
            log.warning(f"Failed to get root folder rules: {e}")

        # Get all folders (including ones we're not managing)
        folders = list_existing_folders(profile_id)

        # Get rules from each folder
        for folder_name, folder_id in folders.items():
            try:
                data = _api_get(f"{API_BASE}/{profile_id}/rules/{folder_id}").json()
                folder_rules = data.get("body", {}).get("rules", [])
                for rule in folder_rules:
                    if rule.get("PK"):
                        all_rules.add(rule["PK"])

                log.debug(f"Found {len(folder_rules)} rules in folder '{folder_name}'")

            except httpx.HTTPError as e:
                log.warning(f"Failed to get rules from folder '{folder_name}': {e}")
                continue

        log.info(f"Total existing rules across all folders: {len(all_rules)}")
        return all_rules

    except Exception as e:
        log.error(f"Failed to get existing rules: {e}")
        return set()


def fetch_folder_data(url: str) -> Dict[str, Any]:
    """Return folder data from GitHub JSON."""
    js = _gh_get(url)
    return js


def delete_folder(profile_id: str, name: str, folder_id: str) -> bool:
    """Delete a single folder by its ID. Returns True if successful."""
    try:
        _api_delete(f"{API_BASE}/{profile_id}/groups/{folder_id}")
        log.info("Deleted folder '%s' (ID %s)", name, folder_id)
        return True
    except httpx.HTTPError as e:
        log.error(f"Failed to delete folder '{name}' (ID {folder_id}): {e}")
        return False


def create_folder(profile_id: str, name: str, do: int, status: int) -> Optional[str]:
    """
    Create a new folder and return its ID.
    The API returns the full list of groups, so we look for the one we just added.
    """
    try:
        _api_post(
            f"{API_BASE}/{profile_id}/groups",
            data={"name": name, "do": do, "status": status},
        )

        # Re-fetch the list and pick the folder we just created
        data = _api_get(f"{API_BASE}/{profile_id}/groups").json()
        for grp in data["body"]["groups"]:
            if grp["group"].strip() == name.strip():
                log.info("Created folder '%s' (ID %s)", name, grp["PK"])
                time.sleep(FOLDER_CREATION_DELAY)
                return str(grp["PK"])

        log.error(f"Folder '{name}' was not found after creation")
        return None
    except (httpx.HTTPError, KeyError) as e:
        log.error(f"Failed to create folder '{name}': {e}")
        return None


def push_rules(
    profile_id: str,
    folder_name: str,
    folder_id: str,
    do: int,
    status: int,
    hostnames: List[str],
    existing_rules: Set[str],
) -> bool:
    """Push hostnames in batches to the given folder, skipping duplicates. Returns True if successful."""
    if not hostnames:
        log.info("Folder '%s' - no rules to push", folder_name)
        return True

    # Filter out duplicates
    original_count = len(hostnames)
    filtered_hostnames = [h for h in hostnames if h not in existing_rules]
    duplicates_count = original_count - len(filtered_hostnames)

    if duplicates_count > 0:
        log.info(f"Folder '{folder_name}': skipping {duplicates_count} duplicate rules")

    if not filtered_hostnames:
        log.info(
            f"Folder '{folder_name}' - no new rules to push after filtering duplicates"
        )
        return True

    successful_batches = 0
    total_batches = len(range(0, len(filtered_hostnames), BATCH_SIZE))

    for i, start in enumerate(range(0, len(filtered_hostnames), BATCH_SIZE), 1):
        batch = filtered_hostnames[start : start + BATCH_SIZE]

        data = {
            "do": str(do),
            "status": str(status),
            "group": str(folder_id),
        }

        for j, hostname in enumerate(batch):
            data[f"hostnames[{j}]"] = hostname

        try:
            _api_post_form(
                f"{API_BASE}/{profile_id}/rules",
                data=data,
            )
            log.info(
                "Folder '%s' – batch %d: added %d rules",
                folder_name,
                i,
                len(batch),
            )
            successful_batches += 1

            # Update existing_rules set with the newly added rules
            existing_rules.update(batch)

        except httpx.HTTPError as e:
            log.error(f"Failed to push batch {i} for folder '{folder_name}': {e}")
            if hasattr(e, "response") and e.response is not None:
                log.error(f"Response content: {e.response.text}")

    if successful_batches == total_batches:
        log.info(
            "Folder '%s' – finished (%d new rules added)",
            folder_name,
            len(filtered_hostnames),
        )
        return True
    else:
        log.error(
            f"Folder '%s' – only {successful_batches}/{total_batches} batches succeeded"
        )
        return False


# --------------------------------------------------------------------------- #
# 4. Main workflow
# --------------------------------------------------------------------------- #
def sync_profile(profile_id: str) -> bool:
    """One-shot sync: delete old, create new, push rules. Returns True if successful."""
    try:
        # Get folder URLs for this specific profile using the cached lookup
        folder_urls = PROFILE_FOLDERS.get(profile_id, DEFAULT_FOLDER_URLS)
        log.info(f"Profile: syncing {len(folder_urls)} folders")

        # Fetch all folder data first
        folder_data_list = []
        for url in folder_urls:
            try:
                log.debug(f"Fetching folder data from: '{url}'")
                folder_data_list.append(fetch_folder_data(url))
            except (httpx.HTTPError, KeyError) as e:
                log.error(f"Failed to fetch folder data from '{url}': {e}")
                continue

        if not folder_data_list:
            log.error("No valid folder data found")
            return False

        # Get existing folders and delete target folders
        existing_folders = list_existing_folders(profile_id)
        for folder_data in folder_data_list:
            name = folder_data["group"]["group"].strip()
            if name in existing_folders:
                delete_folder(profile_id, name, existing_folders[name])

        # Get all existing rules AFTER deleting target folders
        existing_rules = get_all_existing_rules(profile_id)

        # Create new folders and push rules
        success_count = 0
        for folder_data in folder_data_list:
            grp = folder_data["group"]
            name = grp["group"].strip()
            do = grp["action"]["do"]
            status = grp["action"]["status"]
            hostnames = [r["PK"] for r in folder_data.get("rules", []) if r.get("PK")]

            folder_id = create_folder(profile_id, name, do, status)
            if folder_id and push_rules(
                profile_id, name, folder_id, do, status, hostnames, existing_rules
            ):
                success_count += 1
                # Note: existing_rules is updated within push_rules function

            # Optional: Refresh existing rules after each folder (more thorough but slower)
            # existing_rules = get_all_existing_rules(profile_id)

        log.info(
            f"Sync complete: {success_count}/{len(folder_data_list)} folders processed successfully"
        )
        return success_count == len(folder_data_list)

    except Exception as e:
        log.error(f"Unexpected error during sync for profile {profile_id}: {e}")
        return False


# --------------------------------------------------------------------------- #
# 5. Entry-point
# --------------------------------------------------------------------------- #
def main():
    if not TOKEN or not PROFILE_IDS:
        log.error("TOKEN and/or PROFILE missing - check your .env file")
        exit(1)

    log.info(f"Found {len(PROFILE_IDS)} profiles")

    # Debug: Show configuration for each profile
    for i, profile_id in enumerate(PROFILE_IDS):
        env_key = f"PROFILE_{i}_FOLDERS"
        env_value = os.getenv(env_key)
        if env_value:
            log.info(
                f"Profile {i + 1}: Found custom configuration with {len(env_value.split(','))} folders"
            )
        else:
            log.info(
                f"Profile {i + 1}: No custom configuration found, will use defaults"
            )

    success_count = 0
    for profile_id in PROFILE_IDS:
        log.info("Starting sync for profile")
        if sync_profile(profile_id):
            success_count += 1

    log.info(f"All profiles processed: {success_count}/{len(PROFILE_IDS)} successful")
    exit(0 if success_count == len(PROFILE_IDS) else 1)


if __name__ == "__main__":
    main()
