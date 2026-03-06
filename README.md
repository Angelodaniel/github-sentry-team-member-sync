# GitHub → Sentry Team Sync

Keeps Sentry teams in sync with your GitHub org teams.

- Creates Sentry teams that exist in GitHub but not yet in Sentry
- Adds members to Sentry teams to match GitHub team membership
- Users are matched across platforms by **email address**
- Safe by default: never deletes teams or removes members

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
Create at: GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens

Required permissions (repository-independent):
- **Members** — Read-only  (under Organisation permissions)
- **Email addresses** — Read-only (under User permissions)

### Sentry auth token
Create at: Sentry → Settings → Auth Tokens

Required scopes: `org:read`, `team:read`, `team:write`, `member:read`

## Usage

```bash
# Preview what would change (no writes)
python sync.py --dry-run

# Apply changes
python sync.py

# Verbose output for debugging
python sync.py --verbose
python sync.py --dry-run --verbose
```

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
        run: python github-sentry-sync/sync.py
```

Add `SYNC_GITHUB_TOKEN` and `SENTRY_TOKEN` as repository secrets, and `GITHUB_ORG` as a repository variable.

> Note: use a dedicated service account token rather than a personal token for CI runs.
