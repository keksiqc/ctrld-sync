# Example Usage Scenarios

This file shows how to configure your `.env` file or GitHub secrets for different use cases.

## Scenario 1: All profiles use the same default folders

In this setup, all profiles listed in the `PROFILE` variable will be synced with the `DEFAULT_FOLDER_URLS` defined in `main.py`.

```bash
# .env file or GitHub repository secrets
TOKEN=your_token_here
PROFILE=abc123def,xyz789ghi

# No PROFILE_*_FOLDERS variables are set.
# Both profiles will use the default folder URLs.
```

## Scenario 2: All profiles use custom folder lists

In this case, you want to define a specific list of folders for **every** profile. You must provide a `PROFILE_X_FOLDERS` variable for each profile listed in `PROFILE`.

```bash
# .env file or GitHub repository secrets
TOKEN=your_token_here
PROFILE=abc123def,xyz789ghi

# For profile 'abc123def' (index 0)
PROFILE_0_FOLDERS=https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/ultimate-known_issues-allow-folder.json,https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/apple-private-relay-allow-folder.json

# For profile 'xyz789ghi' (index 1)
PROFILE_1_FOLDERS=https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/badware-hoster-folder.json,https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/native-tracker-amazon-folder.json
```

## Scenario 3: Some profiles use custom lists, others use defaults

This is a hybrid approach. You can define custom folder lists for specific profiles, while letting any others that are not explicitly defined fall back to the `DEFAULT_FOLDER_URLS`.

```bash
# .env file or GitHub repository secrets
TOKEN=your_token_here
PROFILE=abc123def,xyz789ghi,jkl456mno

# For profile 'abc123def' (index 0)
PROFILE_0_FOLDERS=https://example.com/custom1.json,https://example.com/custom2.json

# For profile 'xyz789ghi' (index 1)
PROFILE_1_FOLDERS=https://example.com/custom3.json

# Profile 'jkl456mno' (index 2) has no PROFILE_2_FOLDERS variable,
# so it will fall back to using the default folder URLs.
```

