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

Nothing fancy, just works.
"""

import os
import logging
from typing import Dict, List

import httpx
from dotenv import load_dotenv

# --------------------------------------------------------------------------- #
# 0. Bootstrap – load secrets and configure logging
# --------------------------------------------------------------------------- #
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
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

# URLs of the JSON block-lists we want to import
FOLDER_URLS = [
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/badware-hoster-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/native-tracker-amazon-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/native-tracker-microsoft-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/native-tracker-tiktok-aggressive-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/referral-allow-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/spam-idns-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/spam-tlds-allow-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/spam-tlds-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/ultimate-known_issues-allow-folder.json",
]

BATCH_SIZE = 500

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
    """GET helper for Control-D API."""
    r = _api.get(url)
    r.raise_for_status()
    return r


def _api_delete(url: str) -> httpx.Response:
    """DELETE helper for Control-D API."""
    r = _api.delete(url)
    r.raise_for_status()
    return r


def _api_post(url: str, data: Dict) -> httpx.Response:
    """POST helper for Control-D API."""
    r = _api.post(url, data=data)
    r.raise_for_status()
    return r


def _gh_get(url: str) -> Dict:
    """Fetch JSON from GitHub (cached)."""
    if url not in _cache:
        r = _gh.get(url)
        r.raise_for_status()
        _cache[url] = r.json()
    return _cache[url]


def list_existing_folders(profile_id: str) -> Dict[str, str]:
    """Return folder-name -> folder-id mapping."""
    data = _api_get(f"{API_BASE}/{profile_id}/groups").json()
    folders = data.get("body", {}).get("groups", [])
    return {
        f["group"].strip(): f["PK"]
        for f in folders
        if f.get("group") and f.get("PK")
    }


def fetch_folder_name(url: str) -> str:
    """Return folder name from GitHub JSON."""
    return _gh_get(url)["group"]["group"].strip()


def delete_folder(profile_id: str, name: str, folder_id: str) -> None:
    """Delete a single folder by its ID."""
    _api_delete(f"{API_BASE}/{profile_id}/groups/{folder_id}")
    log.info("Deleted folder '%s' (ID %s)", name, folder_id)


def create_folder(profile_id: str, name: str, do: int, status: int) -> str:
    """
    Create a new folder and return its ID.
    The API returns the full list of groups, so we look for the one we just added.
    """
    _api_post(
        f"{API_BASE}/{profile_id}/groups",
        data={"name": name, "do": do, "status": status},
    )
    # Re-fetch the list and pick the folder we just created
    data = _api_get(f"{API_BASE}/{profile_id}/groups").json()
    for grp in data["body"]["groups"]:
        if grp["group"].strip() == name.strip():
            log.info("Created folder '%s' (ID %s)", name, grp["PK"])
            return str(grp["PK"])
    raise RuntimeError(f"Folder '{name}' was not found after creation")


def push_rules(
    profile_id: str,
    folder_name: str,
    folder_id: str,
    do: int,
    status: int,
    hostnames: List[str],
) -> None:
    """Push hostnames in batches to the given folder."""
    for i, start in enumerate(range(0, len(hostnames), BATCH_SIZE), 1):
        batch = hostnames[start : start + BATCH_SIZE]
        _api_post(
            f"{API_BASE}/{profile_id}/rules",
            data={
                "do": do,
                "status": status,
                "group": folder_id,
                "hostnames[]": batch,
            },
        )
        log.info(
            "Folder '%s' – batch %d: added %d rules",
            folder_name,
            i,
            len(batch),
        )
    log.info("Folder '%s' – finished (%d total rules)", folder_name, len(hostnames))


# --------------------------------------------------------------------------- #
# 4. Main workflow
# --------------------------------------------------------------------------- #
def sync_profile(profile_id: str) -> None:
    """One-shot sync: delete old, create new, push rules."""
    wanted_names = [fetch_folder_name(u) for u in FOLDER_URLS]

    existing = list_existing_folders(profile_id)
    for name in wanted_names:
        try:
            if name in existing:
                delete_folder(profile_id, name, existing[name])
        except Exception as e:
            log.error("Skipping deletion of folder '%s' due to error: %s", name, str(e))
            continue

    for url in FOLDER_URLS:
        try:
            js = _gh_get(url)
            grp = js["group"]
            folder_name = grp["group"]
            do = grp["action"]["do"]
            status = grp["action"]["status"]
            hostnames = [r["PK"] for r in js.get("rules", []) if r.get("PK")]

            folder_id = create_folder(profile_id, folder_name, do, status)
            if hostnames:
                push_rules(profile_id, folder_name, folder_id, do, status, hostnames)
            else:
                log.info("Folder '%s' - no rules to push", folder_name)
        except Exception as e:
            log.error("Skipping folder from URL '%s' due to error: %s", url, str(e))
            continue

    log.info("Sync complete ✔")

# --------------------------------------------------------------------------- #
# 5. Entry-point
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    if not TOKEN or not PROFILE_IDS:
        log.error("TOKEN and/or PROFILE missing - check your .env file")
        exit(1)

    for profile_id in PROFILE_IDS:
        log.info("Starting sync for profile %s", profile_id)
        sync_profile(profile_id)
