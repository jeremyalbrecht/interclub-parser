"""Tests for the interclub-parser -> Strapi function.

Grouped by the thing under test: Knative contract, pure parsing/merge
helpers, network calls in isolation, then the orchestrated pipeline.
"""

import copy
import json

import pytest
from bs4 import BeautifulSoup

from conftest import FakeClient, FakeResponse, fixture_json, fixture_text
from function.func import (
    GITHUB_DISPATCH_URL,
    InterclubUpdate,
    apply_leaderboard,
    find_league_link,
    leaderboard_from_group,
    new,
    parse_competition,
    parse_group,
    strip_ids,
)

SAISON_SWITCH = fixture_text("saison_switch.html")
POULE_UNIQUE = fixture_text("classement_poule_unique.html")
POULE_MULTI = fixture_text("classement_poule_multi.html")
STRAPI_TEAMS = fixture_json("strapi_teams.json")

COMPETITION_URL = "https://icbad.ffbad.org/competition/9000003"


def make_updater(**kwargs):
    updater = InterclubUpdate(**kwargs)
    updater.base_url = "https://icbad.ffbad.org/"
    updater.api_url = "https://api.example.org/api/vie-du-club"
    updater.api_token = "s3cret-token"
    updater.github_dispatch_token = "gh-pat"
    return updater


def teams_fixture():
    """A fresh, unshared copy — tests mutate/compare freely."""
    return copy.deepcopy(STRAPI_TEAMS["data"]["teams"])


async def drive_handle(updater):
    sent = []

    async def send(message):
        sent.append(message)

    await updater.handle({"type": "http", "path": "/"}, None, send)
    return sent


def group_from(html):
    table = BeautifulSoup(html, "lxml").find("table", class_="classement-poule")
    return parse_group(table)


# --------------------------------------------------------------------------
# Knative contract
# --------------------------------------------------------------------------


def test_new_returns_an_instance_that_reports_not_ready_until_started():
    updater = new()
    assert isinstance(updater, InterclubUpdate)
    assert updater.alive() == (True, "Alive")
    assert updater.ready() == (False, "Not initialized")


def test_start_reads_config():
    updater = InterclubUpdate()
    updater.start({
        "BASE_URL": "https://icbad.ffbad.org/",
        "API_URL": "https://api.example.org/api/vie-du-club",
        "API_TOKEN": "s3cret-token",
        "GITHUB_DISPATCH_TOKEN": "gh-pat",
    })

    assert updater.base_url == "https://icbad.ffbad.org/"
    assert updater.api_url == "https://api.example.org/api/vie-du-club"
    assert updater.api_token == "s3cret-token"
    assert updater.github_dispatch_token == "gh-pat"
    assert updater.ready() == (True, "Ready")


def test_start_rejects_missing_required_config():
    updater = InterclubUpdate()
    with pytest.raises(KeyError):
        updater.start({"BASE_URL": "https://icbad.ffbad.org/"})


@pytest.mark.asyncio
async def test_handle_returns_the_summary_as_json():
    updater = make_updater()
    summary = {"updated": ["Comité 00 D1"], "skipped": [], "failed": [], "dispatched": True}

    async def run():
        return summary

    updater._run = run
    sent = await drive_handle(updater)

    assert sent[0]["status"] == 200
    assert [b"content-type", b"application/json"] in sent[0]["headers"]
    assert json.loads(sent[1]["body"]) == summary


@pytest.mark.asyncio
async def test_handle_reports_failures_as_500_with_the_message():
    updater = make_updater()

    async def boom():
        raise ValueError("Strapi returned a 500")

    updater._run = boom
    sent = await drive_handle(updater)

    assert sent[0]["status"] == 500
    assert b"Strapi returned a 500" in sent[1]["body"]


# --------------------------------------------------------------------------
# HTML parsing
# --------------------------------------------------------------------------


def test_parse_group_reads_the_right_cell_indices():
    group = group_from(POULE_UNIQUE)

    assert len(group.rows) == 6
    first = group.rows[0]
    assert first.team.name == "Badminton Club Alpha (00-ALPHA-1)"
    assert (first.played, first.won, first.draw, first.lost) == ("10", "9", "1", "0")
    assert (first.bonus, first.penalties, first.points) == ("2", "0", "50")


def test_parse_competition_handles_the_poule_unique_case():
    competition = parse_competition(COMPETITION_URL, POULE_UNIQUE)

    assert competition.url == COMPETITION_URL
    assert len(competition.groups) == 1
    assert len(competition.groups[0].rows) == 6


def test_parse_competition_handles_multiple_groups():
    competition = parse_competition(COMPETITION_URL, POULE_MULTI)

    assert len(competition.groups) == 2
    assert competition.groups[0].rows[0].team.name == "Badminton Club Eta 1 (00-ETA-1)"
    assert competition.groups[1].rows[0].team.name == "Badminton Club Delta 3 (00-DELTA-3)"


def test_find_league_link_matches_case_insensitively():
    assert find_league_link(SAISON_SWITCH, "comité 00 d1") == \
        "https://icbad.ffbad.org/competition/9000003"


def test_find_league_link_returns_none_when_no_link_matches():
    assert find_league_link(SAISON_SWITCH, "Comité 999") is None


def test_leaderboard_from_group_builds_the_strapi_shape():
    leaderboard = leaderboard_from_group(COMPETITION_URL, group_from(POULE_UNIQUE))

    assert leaderboard["competition_id"] == COMPETITION_URL
    assert leaderboard["rows"][0] == {
        "club": "Badminton Club Alpha (00-ALPHA-1)", "won": "9", "lost": "0", "total": "50",
    }


# --------------------------------------------------------------------------
# Merging a scraped leaderboard into the Strapi team list (pure, no I/O)
# --------------------------------------------------------------------------


def test_apply_leaderboard_replaces_only_the_matching_team():
    teams = teams_fixture()
    d1 = next(t for t in teams if t["ranking"] == "D1")
    competition = parse_competition(COMPETITION_URL, POULE_UNIQUE)

    result = apply_leaderboard(teams, d1, competition)

    updated = next(t for t in result if t["ranking"] == "D1")
    untouched = next(t for t in result if t["ranking"] == "D2")
    assert updated["leaderboard"]["competition_id"] == COMPETITION_URL
    assert updated["leaderboard"]["rows"][0]["club"] == "Badminton Club Alpha (00-ALPHA-1)"
    assert untouched["leaderboard"] == {
        "competition_id": "https://icbad.ffbad.org/competition/9000004", "rows": []
    }


def test_apply_leaderboard_does_not_mutate_its_inputs():
    teams = teams_fixture()
    original = copy.deepcopy(teams)
    d1 = next(t for t in teams if t["ranking"] == "D1")
    competition = parse_competition(COMPETITION_URL, POULE_UNIQUE)

    apply_leaderboard(teams, d1, competition)

    assert teams == original


def test_apply_leaderboard_raises_when_the_group_index_is_out_of_range():
    teams = teams_fixture()
    d1 = next(t for t in teams if t["ranking"] == "D1")
    d1["group"] = "9"  # competition only has 1 parsed group
    competition = parse_competition(COMPETITION_URL, POULE_UNIQUE)

    with pytest.raises(IndexError):
        apply_leaderboard(teams, d1, competition)


def test_strip_ids_drops_id_and_leaves_everything_else():
    teams = [{"id": 1, "ranking": "D1"}, {"id": 2, "ranking": "D2"}]
    assert strip_ids(teams) == [{"ranking": "D1"}, {"ranking": "D2"}]


# --------------------------------------------------------------------------
# Network calls in isolation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_teams_sends_the_populate_params_and_bearer_auth():
    updater = make_updater()
    client = FakeClient(get_responses=[FakeResponse(json_body=STRAPI_TEAMS)])

    teams = await updater._fetch_teams(client)

    assert [t["ranking"] for t in teams] == ["D1", "D2", "D3"]
    call = client.get_calls[0]
    assert call["url"] == updater.api_url
    assert call["params"] == {
        "populate[0]": "teams", "populate[1]": "teams.leaderboard",
        "populate[2]": "teams.leaderboard.rows", "populate[3]": "teams.season",
        "populate[4]": "teams.images",
    }
    assert call["headers"] == {"Authorization": "Bearer s3cret-token"}


@pytest.mark.asyncio
async def test_push_teams_puts_the_list_verbatim_with_json_headers():
    updater = make_updater()
    client = FakeClient(put_responses=[FakeResponse(status_code=200)])
    teams = [{"ranking": "D1"}]

    await updater._push_teams(client, teams)

    call = client.put_calls[0]
    assert call["url"] == updater.api_url
    assert call["json"] == {"data": {"teams": teams}}
    assert call["headers"] == {
        "Authorization": "Bearer s3cret-token", "Content-Type": "application/json",
    }


@pytest.mark.asyncio
async def test_dispatch_rebuild_posts_to_the_workflow_dispatch_endpoint():
    updater = make_updater()
    client = FakeClient(post_responses=[FakeResponse(status_code=204)])

    assert await updater._dispatch_rebuild(client) is True

    call = client.post_calls[0]
    assert call["url"] == GITHUB_DISPATCH_URL
    assert call["json"] == {"ref": "main"}
    assert call["headers"] == {
        "Authorization": "Bearer gh-pat",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


@pytest.mark.asyncio
async def test_dispatch_rebuild_returns_false_instead_of_raising_on_failure():
    updater = make_updater()
    client = FakeClient(post_responses=[FakeResponse(status_code=401)])

    assert await updater._dispatch_rebuild(client) is False


@pytest.mark.asyncio
async def test_resolve_competition_switches_season_and_parses_the_league_page():
    updater = make_updater()
    client = FakeClient(get_responses=[FakeResponse(text=SAISON_SWITCH), FakeResponse(text=POULE_UNIQUE)])
    team = {"icbad_id": "Comité 00 D1", "season": {"name": "2025-2026"}}

    competition = await updater._resolve_competition(client, team)

    assert competition.url == COMPETITION_URL
    assert client.get_calls[0]["params"] == {"switchSaison": "2025"}
    assert client.get_calls[1]["url"] == COMPETITION_URL


@pytest.mark.asyncio
async def test_resolve_competition_returns_none_when_the_league_is_not_listed():
    updater = make_updater()
    client = FakeClient(get_responses=[FakeResponse(text=SAISON_SWITCH)])
    team = {"icbad_id": "Comité 999", "season": {"name": "2025-2026"}}

    assert await updater._resolve_competition(client, team) is None
    assert len(client.get_calls) == 1  # no competition page fetched


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_updates_major_teams_in_a_single_put_and_dispatches_the_rebuild():
    client = FakeClient(
        get_responses=[
            FakeResponse(json_body=STRAPI_TEAMS),  # _fetch_teams
            FakeResponse(text=SAISON_SWITCH),        # D1 saison switch
            FakeResponse(text=POULE_UNIQUE),          # D1 competition page
            FakeResponse(text=SAISON_SWITCH),         # D2 saison switch
            FakeResponse(text=POULE_MULTI),           # D2 competition page
        ],
        put_responses=[FakeResponse(status_code=200)],
        post_responses=[FakeResponse(status_code=204)],
    )
    updater = make_updater(client_factory=lambda: client)

    result = await updater._run()

    assert result == {
        "updated": ["Comité 00 D1", "Comité 00 D2"], "skipped": [], "failed": [],
        "dispatched": True,
    }
    # One PUT for both updates, not one per team.
    assert len(client.put_calls) == 1
    pushed = client.put_calls[0]["json"]["data"]["teams"]
    assert all("id" not in t for t in pushed)
    assert next(t for t in pushed if t["ranking"] == "D1")["leaderboard"]["rows"]
    assert next(t for t in pushed if t["ranking"] == "D2")["leaderboard"]["rows"]
    # The dispatch happens after the push, not before or in parallel.
    assert len(client.post_calls) == 1
    assert client.closed is True


@pytest.mark.asyncio
async def test_run_reports_a_failed_dispatch_without_failing_the_request():
    client = FakeClient(
        get_responses=[
            FakeResponse(json_body=STRAPI_TEAMS),
            FakeResponse(text=SAISON_SWITCH),
            FakeResponse(text=POULE_UNIQUE),
            FakeResponse(text=SAISON_SWITCH),
            FakeResponse(text=POULE_MULTI),
        ],
        put_responses=[FakeResponse(status_code=200)],
        post_responses=[FakeResponse(status_code=401)],
    )
    updater = make_updater(client_factory=lambda: client)

    result = await updater._run()

    assert result["dispatched"] is False
    assert result["updated"] == ["Comité 00 D1", "Comité 00 D2"]


@pytest.mark.asyncio
async def test_run_skips_a_team_whose_league_link_is_not_found():
    teams = {"data": {"teams": [{**teams_fixture()[0], "icbad_id": "Comité 999"}]}}
    client = FakeClient(
        get_responses=[FakeResponse(json_body=teams), FakeResponse(text=SAISON_SWITCH)],
    )
    updater = make_updater(client_factory=lambda: client)

    result = await updater._run()

    assert result == {"updated": [], "skipped": ["Comité 999"], "failed": [], "dispatched": False}
    assert client.put_calls == []
    assert client.post_calls == []


@pytest.mark.asyncio
async def test_run_records_a_team_as_failed_without_aborting_the_others():
    # D1's Strapi "group" points past the single parsed group -> IndexError,
    # caught and recorded, while D2 still succeeds and still gets PUT.
    teams = teams_fixture()
    for t in teams:
        if t["ranking"] == "D1":
            t["group"] = "9"
    payload = {"data": {"teams": teams}}
    client = FakeClient(
        get_responses=[
            FakeResponse(json_body=payload),
            FakeResponse(text=SAISON_SWITCH),
            FakeResponse(text=POULE_UNIQUE),
            FakeResponse(text=SAISON_SWITCH),
            FakeResponse(text=POULE_MULTI),
        ],
        put_responses=[FakeResponse(status_code=200)],
        post_responses=[FakeResponse(status_code=204)],
    )
    updater = make_updater(client_factory=lambda: client)

    result = await updater._run()

    assert result == {
        "updated": ["Comité 00 D2"], "skipped": [], "failed": ["Comité 00 D1"],
        "dispatched": True,
    }
    assert len(client.put_calls) == 1


@pytest.mark.asyncio
async def test_run_makes_no_put_or_dispatch_when_nothing_was_updated():
    teams = {"data": {"teams": [{**teams_fixture()[0], "major": False}]}}
    client = FakeClient(get_responses=[FakeResponse(json_body=teams)])
    updater = make_updater(client_factory=lambda: client)

    result = await updater._run()

    assert result == {"updated": [], "skipped": [], "failed": [], "dispatched": False}
    assert client.put_calls == []
    assert client.post_calls == []
