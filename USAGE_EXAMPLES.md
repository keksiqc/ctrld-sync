# Example Usage Scenarios

## Scenario 1: All profiles use the same default folders
```bash
TOKEN=your_token_here
PROFILE=profile1,profile2
# No PROFILE_*_FOLDERS variables needed - all profiles will use default folders
```

## Scenario 2: Each profile has different folder sets (indexed approach)
```bash
TOKEN=your_token_here
PROFILE=profile1,profile2

# Profile 0 (first profile) only syncs allow-lists
PROFILE_0_FOLDERS=https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/ultimate-known_issues-allow-folder.json,https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/apple-private-relay-allow-folder.json

# Profile 1 (second profile) only syncs block-lists
PROFILE_1_FOLDERS=https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/badware-hoster-folder.json,https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/native-tracker-amazon-folder.json
```

## Scenario 3: Mixed configuration
```bash
TOKEN=your_token_here
PROFILE=profile1,profile2,profile3

# Profile 0 has custom folders
PROFILE_0_FOLDERS=https://example.com/custom1.json,https://example.com/custom2.json

# Profile 1 has different custom folders  
PROFILE_1_FOLDERS=https://example.com/custom3.json

# Profile 2 has no PROFILE_2_FOLDERS variable, so it uses default folders
```
