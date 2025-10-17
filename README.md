# Control D Sync

A tiny Python script that keeps your Control D Folders in sync with a set of
remote block-lists.

## What it does

1. Downloads the current JSON block-lists.
2. Deletes any existing folders with the same names.
3. Re-creates the folders and pushes all rules in batches.

## Quick start

### Obtain Control D API token

1. Log in to your Control D account.
2. Navigate to the "Preferences > API" section.
3. Click the "+" button to create a new API token.
4. Copy the token value.

### Obtain Control D profile ID

1. Log in to your Control D account.
2. Open the Profile you want to sync.
3. Copy the profile ID from the URL.

```
https://controld.com/dashboard/profiles/741861frakbm/filters
                                        ^^^^^^^^^^^^
```

### Configure the script

1. **Clone & install**

   ```bash
   git clone https://github.com/your-username/ctrld-sync.git
   cd ctrld-sync
   uv sync
   ```

2. **Configure secrets & variables**

   Create a `.env` file for local runs or configure GitHub for CI. Use Secrets for sensitive values and Repository/Organization Variables for non-sensitive configuration:
   - Sensitive (store as GitHub Secrets):

     ```text
     TOKEN=your_control_d_api_token
     ```

   - Non-sensitive (store as GitHub Repository/Organization Variables or in `.env` for local runs):
     ```text
     PROFILE=your_profile_id  # or comma-separated list of profile ids (e.g. your_id_1,your_id_2)
     PROFILE_0_FOLDERS=https://example.com/folder1.json,https://example.com/folder2.json
     PROFILE_1_FOLDERS=https://example.com/folder3.json,https://example.com/folder4.json
     ```

   Notes:
   - The workflow reads `${{ secrets.TOKEN }}` for the API token and `${{ vars.PROFILE }}`, `${{ vars.PROFILE_0_FOLDERS }}`, etc. for non-sensitive variables.
   - If you prefer all-local configuration, a `.env` file with the same names will work for local runs (but do NOT commit `.env` to the repo).

3. **Configure Folders**

   **Option 1: Use default folders for all profiles**  
   No additional configuration needed. All profiles will use the default folder URLs defined in `main.py`.

   **Option 2: Configure different folders for each profile**  
   Add profile-specific environment variables to your `.env` file using the profile's index (starting from 0). For the first profile ID in your `PROFILE` list, use `PROFILE_0_FOLDERS`, for the second use `PROFILE_1_FOLDERS`, and so on.

   Example:

   ```py
   # .env
   PROFILE=first_profile_id,second_profile_id

   # Corresponds to first_profile_id
   PROFILE_0_FOLDERS=https://example.com/folder1.json,https://example.com/folder2.json
   # Corresponds to second_profile_id
   PROFILE_1_FOLDERS=https://example.com/folder3.json,https://example.com/folder4.json
   ```

   **Option 3: Edit default folders**  
   Edit the `DEFAULT_FOLDER_URLS` list in `main.py` to change the default folders used when no profile-specific configuration is provided.

> [!NOTE]
> Currently only Folders with one action are supported.
> Either "Block" or "Allow" actions are supported.

4. **Run locally**

   ```bash
   uv run python main.py
   ```

5. **Run in CI**  
   The included GitHub Actions workflow runs daily at 02:00 UTC and on demand.

### Configure GitHub Actions

1. Fork this repo.
2. Go to the "Actions" tab and enable actions.
3. Go to the repository Settings → "Secrets and variables" → "Actions".
4. Under the "Secrets" tab, add:
   - `TOKEN` (your Control D API token)

5. Under the "Variables" tab, add non-sensitive configuration values:
   - `PROFILE` (one or more profile IDs, comma-separated)
   - `PROFILE_0_FOLDERS`, `PROFILE_1_FOLDERS`, ... (comma-separated folder URLs for each profile index)

6. The workflow will read `TOKEN` from secrets and the others from variables. This prevents accidental leakage of tokens while keeping configuration editable.

## Requirements

- Python 3.12+
- `uv` (for dependency management)
