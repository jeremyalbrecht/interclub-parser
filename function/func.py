"""Knative HTTP function: sync FFBad interclub standings into Strapi.

One request runs the whole pass: fetch every team registered in Strapi, and
for each ``major`` team scrape its interclub league standings from
icbad.ffbad.org, then push the parsed leaderboards back to Strapi in a
single request.

Notes on the two systems this glues together:

* icbad.ffbad.org's season selector is a query string (``?switchSaison=``)
  keyed on the *first* year of the season (``"2025-2026"`` -> ``2025``), and
  the league a team plays in is found by matching ``icbad_id`` against the
  visible text of one of the page's many ``<a class="link">`` entries (e.g.
  ``"Interclubs Comité 00 D1 - 2025/2026"``) rather than a stable id.
* A standings page has either one table under a "Poule Unique" heading, or
  several ``table.classement-poule`` elements (one per pool) with no
  guaranteed 1:1 pairing to their headings — the code that follows mirrors
  what the live site actually returns, not an idealised schema.
* Strapi's update contract here is "PUT the whole team list back": every
  team's ``id`` key is stripped (id is not a writable field on PUT), the
  matching team(s)' ``leaderboard`` are replaced, and the entire list goes
  back in one request — not one PUT per team.
* Once Strapi has new data, the public site needs rebuilding: a successful
  run with at least one update dispatches the ``azure-augny-badminton-fr.yml``
  workflow on the site's own repo via GitHub's ``workflow_dispatch`` REST API.
  A failed dispatch is logged and reported, not raised — Strapi was already
  updated by that point, which is the part that actually matters.
"""

import json
import logging
from dataclasses import dataclass
from typing import List, Optional

import httpx
from bs4 import BeautifulSoup

STRAPI_POPULATE_PARAMS = {
    "populate[0]": "teams",
    "populate[1]": "teams.leaderboard",
    "populate[2]": "teams.leaderboard.rows",
    "populate[3]": "teams.season",
    "populate[4]": "teams.images",
}

GITHUB_DISPATCH_REPO = "jeremyalbrecht/csjbad"
GITHUB_DISPATCH_WORKFLOW = "azure-augny-badminton-fr.yml"
GITHUB_DISPATCH_REF = "main"
GITHUB_DISPATCH_URL = (
    f"https://api.github.com/repos/{GITHUB_DISPATCH_REPO}/actions/"
    f"workflows/{GITHUB_DISPATCH_WORKFLOW}/dispatches"
)


@dataclass
class Team:
    name: str
    code: str
    logo: str


@dataclass
class Row:
    team: Team
    played: int
    won: int
    draw: int
    lost: int
    bonus: int
    penalties: int
    points: int


@dataclass
class Group:
    teams: List[Team]
    rows: List[Row]


@dataclass
class Competition:
    url: str
    name: str
    groups: List[Group]


def new():
    return InterclubUpdate()


# --------------------------------------------------------------------------
# Pure helpers — no I/O, so this is the easy half to test.
# --------------------------------------------------------------------------


def parse_group(table) -> Group:
    """One ``table.classement-poule`` into a Group.

    Cell indices are load-bearing, not incidental: 2=team name, 3=played,
    4=won, 5=draw, 6=lost, 7=forfeits (skipped, not surfaced by Strapi's
    schema), 8=bonus, 9=penalties, 10=points.
    """
    teams = []
    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) == 0:
            continue
        team = Team(name=cells[2].text.strip(), code="", logo="")
        row = Row(
            team=team,
            played=cells[3].text.strip(),
            won=cells[4].text.strip(),
            draw=cells[5].text.strip(),
            lost=cells[6].text.strip(),
            bonus=cells[8].text.strip(),
            penalties=cells[9].text.strip(),
            points=cells[10].text.strip(),
        )
        teams.append(team)
        rows.append(row)
    return Group(teams=teams, rows=rows)


def parse_competition(url, html) -> Competition:
    """A standings page into a Competition.

    A "Poule Unique" heading means a single group even though the page may
    carry other, unrelated tables further down (finals brackets, documents);
    otherwise every ``table.classement-poule`` on the page is one group.
    """
    soup = BeautifulSoup(html, "lxml")
    for title in soup.find_all("h2"):
        if "poule unique" in title.get_text(" ", strip=True).lower():
            table = soup.find("table", class_="classement-poule")
            return Competition(url=url, name="", groups=[parse_group(table)])

    tables = soup.find_all("table", class_="classement-poule")
    groups = [parse_group(table) for table in tables]
    return Competition(url=url, name="", groups=groups)


def find_league_link(html, region) -> Optional[str]:
    """Href of the ``<a class="link">`` whose text contains ``region``
    (case-insensitive substring), or None."""
    soup = BeautifulSoup(html, "lxml")
    for link in soup.find_all("a", class_="link"):
        if region.lower() in link.get_text(" ", strip=True).lower():
            return link.attrs["href"]
    return None


def leaderboard_from_group(competition_url, group: Group) -> dict:
    """Strapi's expected ``{"competition_id": ..., "rows": [...]}`` shape."""
    return {
        "competition_id": competition_url,
        "rows": [
            {"club": row.team.name, "won": row.won, "lost": row.lost, "total": row.points}
            for row in group.rows
        ],
    }


def apply_leaderboard(teams: List[dict], team: dict, competition: Competition) -> List[dict]:
    """``teams`` with the entry matching ``team`` (by ranking + season) given
    a freshly-scraped leaderboard.

    Matched by ranking + season name, not by Strapi id — the id isn't part of
    what a scrape identifies a team by. Returns a new list; does not mutate
    ``teams`` or its dicts, so a failed later team can never corrupt an
    earlier one's already-applied update.

    Raises ``IndexError`` if ``team["group"]`` doesn't correspond to a parsed
    group — that's a real data problem (Strapi's `group` disagreeing with how
    many pools the site shows), not something to paper over here.
    """
    group = competition.groups[int(team["group"]) - 1]
    leaderboard = leaderboard_from_group(competition.url, group)
    return [
        {**t, "leaderboard": leaderboard}
        if t["ranking"] == team["ranking"] and t["season"]["name"] == team["season"]["name"]
        else t
        for t in teams
    ]


def strip_ids(teams: List[dict]) -> List[dict]:
    """``teams`` with each entry's Strapi ``id`` removed — not a writable
    field on PUT."""
    return [{k: v for k, v in t.items() if k != "id"} for t in teams]


# --------------------------------------------------------------------------


class InterclubUpdate:
    """The function instance held by the Knative Python middleware."""

    _initialized = False

    def __init__(self, *, client_factory=None):
        # Outbound HTTP arrives as a factory so tests never touch the network.
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(follow_redirects=True, timeout=60.0)
        )

    # -- Knative lifecycle ---------------------------------------------

    def start(self, cfg):
        self.base_url = cfg["BASE_URL"]
        self.api_url = cfg["API_URL"]
        self.api_token = cfg["API_TOKEN"]
        self.github_dispatch_token = cfg["GITHUB_DISPATCH_TOKEN"]
        self._initialized = True
        logging.info("Interclub Update started for %s -> %s", self.base_url, self.api_url)

    async def handle(self, scope, receive, send):
        try:
            body = json.dumps(await self._run(), ensure_ascii=False).encode()
            status, content_type = 200, b"application/json"
        except Exception as e:
            logging.exception("Failed to update interclub standings")
            status, content_type, body = 500, b"text/plain", str(e).encode()

        await send({"type": "http.response.start", "status": status,
                    "headers": [[b"content-type", content_type]]})
        await send({"type": "http.response.body", "body": body})

    def stop(self):
        logging.info("Function stopping")

    def alive(self):
        return True, "Alive"

    def ready(self):
        return (True, "Ready") if self._initialized else (False, "Not initialized")

    # -- pipeline (orchestration only — the actual work is in the pure
    # helpers above and the network calls below) ------------------------

    async def _run(self):
        client = self._client_factory()
        try:
            teams = await self._fetch_teams(client)
            working = teams
            updated, skipped, failed = [], [], []

            for team in teams:
                if not team.get("major"):
                    continue
                icbad_id = team["icbad_id"]

                try:
                    competition = await self._resolve_competition(client, team)
                except Exception:
                    logging.exception("Failed to resolve the league page for %s", icbad_id)
                    failed.append(icbad_id)
                    continue
                if competition is None:
                    logging.warning("Did not find a league page for %s", icbad_id)
                    skipped.append(icbad_id)
                    continue

                try:
                    working = apply_leaderboard(working, team, competition)
                except (IndexError, ValueError, KeyError):
                    logging.exception("Failed to apply parsed standings for %s", icbad_id)
                    failed.append(icbad_id)
                    continue

                updated.append(icbad_id)

            # One request for every update, not one per team — the working
            # list already carries every successful team's leaderboard.
            dispatched = False
            if updated:
                await self._push_teams(client, strip_ids(working))
                dispatched = await self._dispatch_rebuild(client)

            return {"updated": updated, "skipped": skipped, "failed": failed,
                     "dispatched": dispatched}
        finally:
            await client.aclose()

    async def _resolve_competition(self, client, team):
        season_start = team["season"]["name"].split("-")[0]
        listing_html = await self._fetch_page(client, self.base_url, params={"switchSaison": season_start})
        href = find_league_link(listing_html, team["icbad_id"])
        if href is None:
            return None
        competition_html = await self._fetch_page(client, href)
        return parse_competition(href, competition_html)

    # -- network I/O — thin, single-purpose, easy to fake in tests ------

    async def _fetch_teams(self, client):
        resp = await client.get(self.api_url, params=STRAPI_POPULATE_PARAMS,
                                 headers=self._auth_headers())
        resp.raise_for_status()
        return resp.json()["data"]["teams"]

    async def _push_teams(self, client, teams):
        resp = await client.put(
            self.api_url,
            json={"data": {"teams": teams}},
            headers=self._auth_headers() | {"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        logging.info("Pushed %d team(s) to Strapi", len(teams))

    async def _fetch_page(self, client, url, params=None):
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.text

    async def _dispatch_rebuild(self, client):
        """Trigger the site's rebuild workflow. Never raises — a failed
        dispatch is logged and reflected in the response, not fatal: Strapi
        was already updated by the time this runs."""
        try:
            resp = await client.post(
                GITHUB_DISPATCH_URL,
                json={"ref": GITHUB_DISPATCH_REF},
                headers={
                    "Authorization": f"Bearer {self.github_dispatch_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            resp.raise_for_status()
        except Exception:
            logging.exception("Failed to dispatch %s/%s", GITHUB_DISPATCH_REPO,
                               GITHUB_DISPATCH_WORKFLOW)
            return False
        logging.info("Dispatched %s/%s", GITHUB_DISPATCH_REPO, GITHUB_DISPATCH_WORKFLOW)
        return True

    def _auth_headers(self):
        return {"Authorization": f"Bearer {self.api_token}"}
