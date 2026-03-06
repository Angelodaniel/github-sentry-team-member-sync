#!/usr/bin/env python3
"""
GitHub → Sentry Team Sync

Syncs GitHub org teams and their members to Sentry.
Users are matched across platforms by email address.
Team renames are detected via a state.json file that maps
persistent GitHub team IDs to their last-known Sentry slug.

Usage:
    python sync.py                        # apply changes
    python sync.py --dry-run              # preview changes without applying
    python sync.py --invite-missing       # invite GitHub members not yet in Sentry
    python sync.py --remove-departed      # remove members no longer in the GitHub team
    python sync.py --delete-removed       # delete Sentry teams no longer in GitHub
    python sync.py --verbose              # show debug output
"""

import argparse
import json
import logging
import os
import sys
from typing import Optional

import requests
from dotenv import load_dotenv

STATE_FILE = "state.json"

log = logging.getLogger("sync")


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state() -> dict:
    """Load persisted mapping of GitHub team ID → Sentry team slug."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"teams": {}}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    log.debug(f"State saved to {STATE_FILE}")


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
            params = {}  # already encoded in the next URL
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

    def update_team(self, old_slug: str, new_name: str, new_slug: str) -> dict:
        resp = self.session.put(
            f"{self.BASE}/teams/{self.org}/{old_slug}/",
            json={"name": new_name, "slug": new_slug},
        )
        resp.raise_for_status()
        return resp.json()

    def delete_team(self, team_slug: str) -> None:
        resp = self.session.delete(f"{self.BASE}/teams/{self.org}/{team_slug}/")
        resp.raise_for_status()

    def get_org_members(self) -> list[dict]:
        return self._paginate(f"{self.BASE}/organizations/{self.org}/members/")

    def get_team_members(self, team_slug: str) -> list[dict]:
        return self._paginate(f"{self.BASE}/teams/{self.org}/{team_slug}/members/")

    def add_member_to_team(self, member_id: str, team_slug: str) -> None:
        resp = self.session.post(
            f"{self.BASE}/organizations/{self.org}/members/{member_id}/teams/{team_slug}/"
        )
        resp.raise_for_status()

    def remove_member_from_team(self, member_id: str, team_slug: str) -> None:
        resp = self.session.delete(
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

def sync(
    github_token: str,
    github_org: str,
    sentry_token: str,
    sentry_org: str,
    dry_run: bool = False,
    invite_missing: bool = False,
    remove_departed: bool = False,
    delete_removed: bool = False,
) -> None:
    if dry_run:
        log.info("DRY RUN — no changes will be applied")
    if invite_missing:
        log.info("INVITE MODE — members not in Sentry will be sent an invitation")
    if remove_departed:
        log.info("REMOVE MODE — members no longer in GitHub team will be removed from Sentry team")
    if delete_removed:
        log.info("DELETE MODE — Sentry teams not in GitHub will be deleted")

    gh = GitHubClient(github_token)
    sentry = SentryClient(sentry_token, sentry_org)

    # --- Load persisted state ---
    state = load_state()
    state_teams: dict[str, str] = state.get("teams", {})  # github_id (str) → sentry_slug
    log.debug(f"Loaded state: {len(state_teams)} tracked teams")

    # --- Fetch everything upfront ---
    log.info("Fetching GitHub teams...")
    gh_teams = gh.get_teams(github_org)
    gh_team_by_id = {str(t["id"]): t for t in gh_teams}
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
        "teams_renamed": 0,
        "teams_already_exist": 0,
        "teams_deleted": 0,
        "members_added": 0,
        "members_removed": 0,
        "members_already_in_team": 0,
        "members_invited": 0,
        "members_skipped_no_email": 0,
        "members_skipped_not_in_sentry": 0,
        "errors": 0,
    }

    # Track emails invited this run to avoid duplicate invitations across teams
    invited_this_run: dict[str, dict] = {}  # email → sentry member object

    # --- Process each GitHub team ---
    for gh_team in gh_teams:
        gh_id = str(gh_team["id"])
        current_slug = gh_team["slug"]
        current_name = gh_team["name"]

        log.info(f"\nTeam: {current_name} ({current_slug})")

        # Determine the active Sentry slug for this team, handling renames
        old_sentry_slug = state_teams.get(gh_id)
        active_slug = current_slug

        if old_sentry_slug and old_sentry_slug != current_slug:
            # Team was renamed in GitHub
            if old_sentry_slug in sentry_team_by_slug:
                log.info(f"  [RENAME] {old_sentry_slug} → {current_slug}")
                if not dry_run:
                    try:
                        sentry.update_team(old_sentry_slug, current_name, current_slug)
                        sentry_team_by_slug[current_slug] = sentry_team_by_slug.pop(old_sentry_slug)
                        stats["teams_renamed"] += 1
                    except requests.HTTPError as e:
                        log.error(f"  [ERROR] Failed to rename team: {e.response.text}")
                        stats["errors"] += 1
                        active_slug = old_sentry_slug  # fall back for member sync
                else:
                    stats["teams_renamed"] += 1
            else:
                # Old slug gone from Sentry (deleted manually) — create fresh
                log.info(f"  [CREATE] Old slug '{old_sentry_slug}' not found in Sentry — creating as {current_slug}")
                if not dry_run:
                    try:
                        sentry.create_team(current_name, current_slug)
                        stats["teams_created"] += 1
                    except requests.HTTPError as e:
                        log.error(f"  [ERROR] Failed to create team: {e.response.text}")
                        stats["errors"] += 1
                        continue
                else:
                    stats["teams_created"] += 1

        elif current_slug not in sentry_team_by_slug:
            log.info(f"  [CREATE] Team does not exist in Sentry — creating")
            if not dry_run:
                try:
                    sentry.create_team(current_name, current_slug)
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

        # Update state (written to disk after the full loop)
        state_teams[gh_id] = current_slug

        # --- Resolve GitHub team members and their emails ---
        gh_members_raw = gh.get_team_members(github_org, current_slug)
        log.debug(f"  {len(gh_members_raw)} members in GitHub team")

        gh_member_emails: set[str] = set()
        gh_members_resolved: list[tuple[str, str]] = []  # (username, email)

        for gh_member in gh_members_raw:
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
            gh_member_emails.add(email.lower())
            gh_members_resolved.append((username, email))

        # --- Fetch current Sentry team members ---
        try:
            sentry_team_members = sentry.get_team_members(active_slug)
            sentry_team_member_ids = {m["id"] for m in sentry_team_members}
        except requests.HTTPError as e:
            log.error(f"  [ERROR] Could not fetch Sentry team members: {e.response.text}")
            sentry_team_members = []
            sentry_team_member_ids = set()

        # --- Remove departed members (in Sentry team but no longer in GitHub team) ---
        if remove_departed:
            for sentry_tm in sentry_team_members:
                tm_email = sentry_tm.get("email", "").lower()
                if not tm_email or tm_email in gh_member_emails:
                    continue
                log.info(f"  [REMOVE] {tm_email} is no longer in the GitHub team")
                if not dry_run:
                    try:
                        sentry.remove_member_from_team(sentry_tm["id"], active_slug)
                        stats["members_removed"] += 1
                    except requests.HTTPError as e:
                        log.error(f"  [ERROR] Failed to remove {tm_email}: {e.response.text}")
                        stats["errors"] += 1
                else:
                    stats["members_removed"] += 1

        # --- Add members present in GitHub but missing from Sentry team ---
        for username, email in gh_members_resolved:
            sentry_member = sentry_member_by_email.get(email.lower())

            if not sentry_member:
                if not invite_missing:
                    log.warning(f"  [SKIP] {username} ({email}): not found in Sentry org")
                    stats["members_skipped_not_in_sentry"] += 1
                    continue

                # Already invited this person earlier in this run — add to this team too
                if email.lower() in invited_this_run:
                    sentry_member = invited_this_run[email.lower()]
                    log.info(f"  [ADD] {username} ({email}) → {active_slug} (pending invite)")
                    if not dry_run:
                        try:
                            sentry.add_member_to_team(sentry_member["id"], active_slug)
                        except requests.HTTPError as e:
                            log.error(f"  [ERROR] Failed to add {username} to {active_slug}: {e.response.text}")
                            stats["errors"] += 1
                    continue

                log.info(f"  [INVITE] {username} ({email}) → invite sent, added to {active_slug}")
                if not dry_run:
                    try:
                        invited_member = sentry.invite_member(email, active_slug)
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

            log.info(f"  [ADD] {username} ({email}) → {active_slug}")
            if not dry_run:
                try:
                    sentry.add_member_to_team(sentry_member["id"], active_slug)
                    stats["members_added"] += 1
                except requests.HTTPError as e:
                    log.error(f"  [ERROR] Failed to add {username}: {e.response.text}")
                    stats["errors"] += 1
            else:
                stats["members_added"] += 1

    # --- Delete Sentry teams whose GitHub team no longer exists ---
    if delete_removed:
        removed_gh_ids = set(state_teams.keys()) - set(gh_team_by_id.keys())
        removed_teams = [(gh_id, state_teams[gh_id]) for gh_id in removed_gh_ids]

        if removed_teams:
            log.info(f"\nTeams removed from GitHub ({len(removed_teams)}):")
            for gh_id, sentry_slug in removed_teams:
                if sentry_slug in sentry_team_by_slug:
                    log.info(f"  [DELETE] {sentry_slug}")
                    if not dry_run:
                        try:
                            sentry.delete_team(sentry_slug)
                            stats["teams_deleted"] += 1
                        except requests.HTTPError as e:
                            log.error(f"  [ERROR] Failed to delete {sentry_slug}: {e.response.text}")
                            stats["errors"] += 1
                            continue
                    else:
                        stats["teams_deleted"] += 1
                else:
                    log.debug(f"  {sentry_slug} already gone from Sentry, skipping")

                if not dry_run:
                    del state_teams[gh_id]
        else:
            log.info("\nNo teams removed from GitHub")

    # --- Persist state ---
    if not dry_run:
        save_state({"teams": state_teams})

    # --- Summary ---
    log.info("\n" + "=" * 50)
    log.info("Sync complete")
    log.info(f"  Teams created:              {stats['teams_created']}")
    log.info(f"  Teams renamed:              {stats['teams_renamed']}")
    log.info(f"  Teams already in Sentry:    {stats['teams_already_exist']}")
    log.info(f"  Teams deleted:              {stats['teams_deleted']}")
    log.info(f"  Members added:              {stats['members_added']}")
    log.info(f"  Members removed:            {stats['members_removed']}")
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
        "--remove-departed",
        action="store_true",
        help="Remove members from Sentry teams if they are no longer in the GitHub team",
    )
    parser.add_argument(
        "--delete-removed",
        action="store_true",
        help="Delete Sentry teams that no longer exist in GitHub",
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

    sync(
        github_token,
        github_org,
        sentry_token,
        sentry_org,
        dry_run=args.dry_run,
        invite_missing=args.invite_missing,
        remove_departed=args.remove_departed,
        delete_removed=args.delete_removed,
    )


if __name__ == "__main__":
    main()
