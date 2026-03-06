# GitHub → Sentry Team Sync

Keeps Sentry teams in sync with your GitHub org teams.

- Creates Sentry teams that exist in GitHub but not yet in Sentry
- Adds members to Sentry teams to match GitHub team membership
- Optionally invites GitHub members who are not yet in the Sentry org
- Optionally deletes Sentry teams that no longer exist in GitHub
- Users are matched across platforms by **email address**
- Safe by default: only adds and creates, never deletes unless `--delete-removed` is passed

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
- **`read:org`** — read org teams and members
- **`read:user`** — read user profile info
- **`user:email`** — read user email addresses

> If your org uses SAML SSO, after creating the token click **Configure SSO** on the token page and authorise it for your organisation.

### Sentry auth token
Create at: Sentry → Settings → Auth Tokens

Required scopes: `org:read`, `team:read`, `team:write`, `team:admin` (for deletions), `member:read`, `member:write` (for invites)

## Usage

```bash
# Preview what would change (no writes)
python sync.py --dry-run

# Apply changes
python sync.py

# Also invite GitHub members who are not yet in the Sentry org
python sync.py --invite-missing

# Also delete Sentry teams that no longer exist in GitHub
python sync.py --delete-removed

# Combine flags
python sync.py --invite-missing --delete-removed

# Verbose output for debugging
python sync.py --dry-run --verbose
```

> **Note on team renames:** if a team is renamed in GitHub its slug changes, so the script will create a new Sentry team with the new slug. Run with `--delete-removed` to also clean up the old one.

## How team renames are handled

GitHub team IDs are permanent and never change, even when a team is renamed. The script persists a `state.json` file mapping each GitHub team ID to its current Sentry slug. On each run it compares the stored slug against the current GitHub slug — if they differ, it **renames** the existing Sentry team rather than deleting and recreating it. This preserves issue ownership and project assignments in Sentry.

The `state.json` file should be committed to your repository so it persists between CI runs (see the GitHub Actions example below).

## Known limitation: private GitHub emails

GitHub users who hide their email address cannot be matched to a Sentry account.
The script logs a warning for each one and skips them. To fix this at scale:

- Ask team members to set a public email in their GitHub profile, **or**
- Use GitHub's SAML identity API (requires GitHub Enterprise with SSO)

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
        run: python sync.py
        working-directory: github-sentry-sync

      - name: Commit updated state
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add github-sentry-sync/state.json
          git diff --cached --quiet || git commit -m "chore: update team sync state"
          git push
```

Add `SYNC_GITHUB_TOKEN` and `SENTRY_TOKEN` as repository secrets, and `GITHUB_ORG` + `SENTRY_ORG` as repository variables.

> Note: use a dedicated service account token rather than a personal token for CI runs. The token also needs `contents: write` permission to commit the state file back.
