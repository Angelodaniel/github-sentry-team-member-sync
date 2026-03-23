# GitHub → Sentry Team Sync

Keeps Sentry teams in sync with your GitHub org teams.

- Creates Sentry teams that exist in GitHub but not yet in Sentry
- Adds members to Sentry teams to match GitHub team membership
- Optionally filters to only teams referenced in specified repos' **CODEOWNERS** files
- Optionally links synced teams to their corresponding **Sentry projects**
- Optionally invites GitHub members who are not yet in the Sentry org
- Optionally removes members and deletes teams when they leave GitHub
- Resolves user emails via **GitHub GraphQL** (`organizationVerifiedDomainEmails`) — works even when users have personal GitHub accounts with no public email
- Safe by default: only adds and creates, never deletes unless explicitly enabled

## Coverage

| Scenario | Covered | Notes |
|---|---|---|
| Team created in GitHub | ✅ | Created automatically in Sentry |
| Team deleted in GitHub | ✅ | Use `--delete-removed` |
| Team renamed in GitHub | ✅ | Renamed in place via `state.json`, preserves issue ownership |
| Member added to a team | ✅ | Added to the Sentry team |
| Member removed from a team | ✅ | Use `--remove-departed` |
| Member moved to another team | ✅ | Removed from old team, added to new team |
| User has personal GitHub account (no public email) | ✅ | Resolved via GraphQL `organizationVerifiedDomainEmails` |
| Sync only CODEOWNERS teams | ✅ | Set `CODEOWNERS_REPOS` |
| Link teams to Sentry projects | ✅ | Set `PROJECT_MAP_FILE` |
| SCIM enabled | ✅ | Users already provisioned; script manages team membership only |
| SCIM disabled | ✅ | Use `--invite-missing` to invite users not yet in Sentry |

## Setup

```bash
cd github-sentry-sync
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in .env with your tokens
```

## Required tokens

### GitHub token
Create a **classic** token at: GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)

Required scopes:
- **`read:org`** — read org teams, members, and verified domain emails (GraphQL)

> If your org uses SAML SSO, after creating the token click **Configure SSO** on the token page and authorise it for your organisation.

### Sentry auth token
Create at: Sentry → Settings → Auth Tokens

Required scopes: `org:read`, `team:read`, `team:write`, `team:admin` (for deletions), `member:read`, `member:write` (for invites), `project:write` (for project linking)

## Usage

```bash
# Preview what would change (no writes)
python sync.py --dry-run

# Apply changes
python sync.py

# Also invite GitHub members who are not yet in the Sentry org
python sync.py --invite-missing

# Also remove members from Sentry teams if they left the GitHub team
python sync.py --remove-departed

# Also delete Sentry teams that no longer exist in GitHub
python sync.py --delete-removed

# Full sync — all options enabled
python sync.py --invite-missing --remove-departed --delete-removed

# Verbose output for debugging
python sync.py --dry-run --verbose
```

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `GITHUB_TOKEN` | ✅ | GitHub personal access token (`read:org` scope) |
| `GITHUB_ORG` | ✅ | GitHub organisation slug |
| `SENTRY_TOKEN` | ✅ | Sentry auth token |
| `SENTRY_ORG` | ✅ | Sentry organisation slug |
| `CODEOWNERS_REPOS` | — | Comma-separated list of repos to scan CODEOWNERS from. If set, only teams referenced in those files are synced. |
| `PROJECT_MAP_FILE` | — | Path to a JSON file mapping repo name → Sentry project slugs. Teams found in a repo's CODEOWNERS will be linked to those projects. Requires `CODEOWNERS_REPOS`. |
| `USERNAME_MAP_FILE` | — | Path to a JSON file mapping GitHub username → work email. Used as an explicit override before the GraphQL lookup. |

### CODEOWNERS_REPOS

Scans the CODEOWNERS file of each listed repo and only syncs teams that are referenced. Useful when you have many teams in GitHub but only want to manage a subset in Sentry.

```bash
CODEOWNERS_REPOS=ios-app,android-app1,android-app2,android-shared
```

### PROJECT_MAP_FILE

A JSON file mapping repo name → list of Sentry project slugs. Teams found in a repo's CODEOWNERS will automatically be linked to the listed projects.

```json
{
  "ios-app": ["ios-app-production", "ios-app-staging"],
  "android-app1": ["android-production", "android-staging"],
  "android-app2": ["android-production", "android-staging"],
  "android-shared": ["android-production", "android-staging"]
}
```

### USERNAME_MAP_FILE

An optional JSON file for cases where the GraphQL lookup doesn't return an email (e.g. a user whose account is not linked to the org's verified domain). Maps GitHub username → work email.

```json
{
  "some-contractor": "contractor@company.com"
}
```

## How email matching works

Users are matched between GitHub and Sentry by email address. The script tries three sources in order, using the first match it finds:

1. **`USERNAME_MAP_FILE`** — explicit override, checked first
2. **GitHub GraphQL `organizationVerifiedDomainEmails`** — returns the user's verified org-domain email (e.g. `user@company.com`) even when their public GitHub profile email is empty. This covers the vast majority of org members, including those using personal GitHub accounts.
3. **Public GitHub profile email** — least reliable, used as a last resort

Users for whom no email can be resolved are logged as `[SKIP]` and left unchanged.

## How team renames are handled

GitHub team IDs are permanent and never change, even when a team is renamed. The script persists a `state.json` file mapping each GitHub team ID to its current Sentry slug. On each run it compares the stored slug against the current GitHub slug — if they differ, it **renames** the existing Sentry team rather than deleting and recreating it. This preserves issue ownership and project assignments in Sentry.

The `state.json` file should be committed to your repository so it persists between CI runs (see the GitHub Actions example below).

## Running on a schedule (GitHub Actions)

Create `.github/workflows/sync-teams.yml` in your repo:

```yaml
name: Sync GitHub teams to Sentry

on:
  schedule:
    - cron: "0 6 * * 1"  # Every Monday at 06:00 UTC
  workflow_dispatch:       # Allow manual trigger

jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: pip install -r github-sentry-sync/requirements.txt

      - name: Run sync
        env:
          GITHUB_TOKEN: ${{ secrets.SYNC_GITHUB_TOKEN }}
          GITHUB_ORG: ${{ vars.GITHUB_ORG }}
          SENTRY_TOKEN: ${{ secrets.SENTRY_TOKEN }}
          SENTRY_ORG: ${{ vars.SENTRY_ORG }}
          CODEOWNERS_REPOS: ${{ vars.CODEOWNERS_REPOS }}
          PROJECT_MAP_FILE: project_map.json
        run: python sync.py --invite-missing --remove-departed
        working-directory: github-sentry-sync

      - name: Commit updated state
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add github-sentry-sync/state.json
          git diff --cached --quiet || git commit -m "chore: update team sync state"
          git push
```

Add `SYNC_GITHUB_TOKEN` and `SENTRY_TOKEN` as repository secrets, and `GITHUB_ORG`, `SENTRY_ORG`, `CODEOWNERS_REPOS` as repository variables.

> Note: use a dedicated service account token rather than a personal token for CI runs. The token also needs `contents: write` permission to commit the state file back.
