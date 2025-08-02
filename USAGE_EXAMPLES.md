# Example Usage Scenarios

## Scenario 1: All profiles use the same default folders
```bash
TOKEN=your_token_here
PROFILE=123456,789012
# No PROFILE_*_FOLDERS variables needed - all profiles will use default folders
```

## Scenario 2: Each profile has different folder sets
```bash
TOKEN=your_token_here
PROFILE=123456,789012

# Profile 123456 only syncs allow-lists
PROFILE_123456_FOLDERS=https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/ultimate-known_issues-allow-folder.json,https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/apple-private-relay-allow-folder.json

# Profile 789012 only syncs block-lists
PROFILE_789012_FOLDERS=https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/badware-hoster-folder.json,https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/native-tracker-amazon-folder.json
```

## Scenario 3: Mixed configuration
```bash
TOKEN=your_token_here
PROFILE=123456,789012,345678

# Profile 123456 has custom folders
PROFILE_123456_FOLDERS=https://example.com/custom1.json,https://example.com/custom2.json

# Profile 789012 has different custom folders  
PROFILE_789012_FOLDERS=https://example.com/custom3.json

# Profile 345678 has no PROFILE_*_FOLDERS variable, so it uses default folders
```
