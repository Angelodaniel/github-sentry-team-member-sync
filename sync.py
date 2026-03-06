#!/usr/bin/env python3
"""
GitHub → Sentry Team Sync

Syncs GitHub org teams and their members to Sentry.
Users are matched across platforms by email address.

Usage:
    python sync.py                        # apply changes
    python sync.py --dry-run              # preview changes without applying
    python sync.py --invite-missing       # invite GitHub members not yet in Sentry
    python sync.py --verbose              # show debug output
"""

import argparse
import logging
import os
import sys
from typing import Optional

import requests
from dotenv import load_dotenv

log = logging.getLogger("sync")


# ---------------------------------------------------------------------------
# GitHub client
# ---------------------------------------------------------------------------

class GitHubClient:
    BASE = "https://api.github.com"

    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    def _paginate(self, url: str, params: dict = None) -> list[dict]:
        results = []
        params = {**(params or {}), "per_page": 100}
        while url:
            resp = self.session.get(url, params=params)
            resp.raise_for_status()
            results.extend(resp.json())
            url = resp.links.get("next", {}).get("url")
            params = {}  # params are already encoded in the next URL
        return results

    def get_teams(self, org: str) -> list[dict]:
        return self._paginate(f"{self.BASE}/orgs/{org}/teams")

    def get_team_members(self, org: str, team_slug: str) -> list[dict]:
        return self._paginate(f"{self.BASE}/orgs/{org}/teams/{team_slug}/members")

    def get_user_email(self, username: str) -> Optional[str]:
        resp = self.session.get(f"{self.BASE}/users/{username}")
        resp.raise_for_status()
        return resp.json().get("email") or None


# ---------------------------------------------------------------------------
# Sentry client
# ---------------------------------------------------------------------------

class SentryClient:
    BASE = "https://sentry.io/api/0"

    def __init__(self, token: str, org: str):
        self.org = org
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })

    def _paginate(self, url: str) -> list[dict]:
        results = []
        while url:
            resp = self.session.get(url, params={"per_page": 100})
            resp.raise_for_status()
            results.extend(resp.json())
            url = self._next_url(resp)
        return results

    @staticmethod
    def _next_url(resp: requests.Response) -> Optional[str]:
        link = resp.headers.get("Link", "")
        for part in link.split(","):
            part = part.strip()
            if 'rel="next"' in part and 'results="true"' in part:
                return part.split(";")[0].strip().strip("<>")
        return None

    def get_teams(self) -> list[dict]:
        return self._paginate(f"{self.BASE}/organizations/{self.org}/teams/")

    def create_team(self, name: str, slug: str) -> dict:
        resp = self.session.post(
            f"{self.BASE}/organizations/{self.org}/teams/",
            json={"name": name, "slug": slug},
        )
        resp.raise_for_status()
        return resp.json()

    def get_org_members(self) -> list[dict]:
        return self._paginate(f"{self.BASE}/organizations/{self.org}/members/")

    def get_team_members(self, team_slug: str) -> list[dict]:
        return self._paginate(f"{self.BASE}/teams/{self.org}/{team_slug}/members/")

    def add_member_to_team(self, member_id: str, team_slug: str) -> None:
        resp = self.session.post(
            f"{self.BASE}/organizations/{self.org}/members/{member_id}/teams/{team_slug}/"
        )
        resp.raise_for_status()

    def invite_member(self, email: str, team_slug: str, role: str = "member") -> dict:
        resp = self.session.post(
            f"{self.BASE}/organizations/{self.org}/members/",
            json={"email": email, "role": role, "teams": [team_slug]},
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------

def sync(github_token: str, github_org: str, sentry_token: str, sentry_org: str, dry_run: bool = False, invite_missing: bool = False) -> None:
    if dry_run:
        log.info("DRY RUN — no changes will be applied")
    if invite_missing:
        log.info("INVITE MODE — members not in Sentry will be sent an invitation")

    gh = GitHubClient(github_token)
    sentry = SentryClient(sentry_token, sentry_org)

    # --- Fetch everything upfront ---
    log.info("Fetching GitHub teams...")
    gh_teams = gh.get_teams(github_org)
    log.info(f"  {len(gh_teams)} teams found")

    log.info("Fetching Sentry teams...")
    sentry_teams = sentry.get_teams()
    sentry_team_by_slug = {t["slug"]: t for t in sentry_teams}
    log.info(f"  {len(sentry_teams)} teams found")

    log.info("Fetching Sentry org members...")
    sentry_members = sentry.get_org_members()
    sentry_member_by_email = {m["email"].lower(): m for m in sentry_members}
    log.info(f"  {len(sentry_members)} members found")

    stats = {
        "teams_created": 0,
        "teams_already_exist": 0,
        "members_added": 0,
        "members_already_in_team": 0,
        "members_invited": 0,
        "members_skipped_no_email": 0,
        "members_skipped_not_in_sentry": 0,
        "errors": 0,
    }

    # Track invited emails within this run so we don't send duplicate invites
    # if the same person appears in multiple GitHub teams
    invited_this_run: dict[str, dict] = {}  # email -> sentry member object returned by invite

    # --- Process each GitHub team ---
    for gh_team in gh_teams:
        team_slug = gh_team["slug"]
        team_name = gh_team["name"]

        log.info(f"\nTeam: {team_name} ({team_slug})")

        # Create team in Sentry if missing
        if team_slug not in sentry_team_by_slug:
            log.info(f"  [CREATE] Team does not exist in Sentry — creating")
            if not dry_run:
                try:
                    sentry.create_team(team_name, team_slug)
                    stats["teams_created"] += 1
                except requests.HTTPError as e:
                    log.error(f"  [ERROR] Failed to create team: {e.response.text}")
                    stats["errors"] += 1
                    continue
            else:
                stats["teams_created"] += 1
        else:
            log.debug(f"  Team already exists in Sentry")
            stats["teams_already_exist"] += 1

        # Fetch GitHub team members
        gh_members = gh.get_team_members(github_org, team_slug)
        log.debug(f"  {len(gh_members)} members in GitHub team")

        # Fetch current Sentry team members
        try:
            sentry_team_member_ids = {
                m["id"] for m in sentry.get_team_members(team_slug)
            }
        except requests.HTTPError as e:
            log.error(f"  [ERROR] Could not fetch Sentry team members: {e.response.text}")
            sentry_team_member_ids = set()

        # Add missing members
        for gh_member in gh_members:
            username = gh_member["login"]

            try:
                email = gh.get_user_email(username)
            except requests.HTTPError as e:
                log.warning(f"  [SKIP] {username}: could not fetch GitHub user — {e}")
                stats["errors"] += 1
                continue

            if not email:
                log.warning(f"  [SKIP] {username}: no public email on GitHub")
                stats["members_skipped_no_email"] += 1
                continue

            sentry_member = sentry_member_by_email.get(email.lower())
            if not sentry_member:
                if not invite_missing:
                    log.warning(f"  [SKIP] {username} ({email}): not found in Sentry org")
                    stats["members_skipped_not_in_sentry"] += 1
                    continue

                # Already invited this person in an earlier team — just add to this team
                if email.lower() in invited_this_run:
                    sentry_member = invited_this_run[email.lower()]
                    log.info(f"  [ADD] {username} ({email}) → {team_slug} (pending invite)")
                    if not dry_run:
                        try:
                            sentry.add_member_to_team(sentry_member["id"], team_slug)
                        except requests.HTTPError as e:
                            log.error(f"  [ERROR] Failed to add {username} to {team_slug}: {e.response.text}")
                            stats["errors"] += 1
                    continue

                log.info(f"  [INVITE] {username} ({email}) → invite sent, added to {team_slug}")
                if not dry_run:
                    try:
                        invited_member = sentry.invite_member(email, team_slug)
                        invited_this_run[email.lower()] = invited_member
                        sentry_member_by_email[email.lower()] = invited_member
                        stats["members_invited"] += 1
                    except requests.HTTPError as e:
                        log.error(f"  [ERROR] Failed to invite {username}: {e.response.text}")
                        stats["errors"] += 1
                else:
                    stats["members_invited"] += 1
                continue

            if sentry_member["id"] in sentry_team_member_ids:
                log.debug(f"  {username}: already in team")
                stats["members_already_in_team"] += 1
                continue

            log.info(f"  [ADD] {username} ({email}) → {team_slug}")
            if not dry_run:
                try:
                    sentry.add_member_to_team(sentry_member["id"], team_slug)
                    stats["members_added"] += 1
                except requests.HTTPError as e:
                    log.error(f"  [ERROR] Failed to add {username}: {e.response.text}")
                    stats["errors"] += 1
            else:
                stats["members_added"] += 1

    # --- Summary ---
    log.info("\n" + "=" * 50)
    log.info("Sync complete")
    log.info(f"  Teams created:              {stats['teams_created']}")
    log.info(f"  Teams already in Sentry:    {stats['teams_already_exist']}")
    log.info(f"  Members added:              {stats['members_added']}")
    log.info(f"  Members already in team:    {stats['members_already_in_team']}")
    log.info(f"  Members invited to Sentry:  {stats['members_invited']}")
    log.info(f"  Skipped (no GitHub email):  {stats['members_skipped_no_email']}")
    log.info(f"  Skipped (not in Sentry):    {stats['members_skipped_not_in_sentry']}")
    log.info(f"  Errors:                     {stats['errors']}")
    if dry_run:
        log.info("  (DRY RUN — nothing was changed)")
    log.info("=" * 50)

    if stats["errors"] > 0:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Sync GitHub org teams and members to Sentry"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would change without applying anything",
    )
    parser.add_argument(
        "--invite-missing",
        action="store_true",
        help="Invite GitHub members who are not yet in the Sentry org",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show debug-level output",
    )
    args = parser.parse_args()

    load_dotenv()

    github_token = os.getenv("GITHUB_TOKEN")
    github_org = os.getenv("GITHUB_ORG")
    sentry_token = os.getenv("SENTRY_TOKEN")
    sentry_org = os.getenv("SENTRY_ORG")

    missing = [
        name for name, val in [
            ("GITHUB_TOKEN", github_token),
            ("GITHUB_ORG", github_org),
            ("SENTRY_TOKEN", sentry_token),
            ("SENTRY_ORG", sentry_org),
        ]
        if not val
    ]
    if missing:
        print(f"Error: missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    sync(github_token, github_org, sentry_token, sentry_org, dry_run=args.dry_run, invite_missing=args.invite_missing)


if __name__ == "__main__":
    main()
