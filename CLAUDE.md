# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies (runtime + test)
pip install -e '.[dev]'

# Run tests
pytest -q

# Run a single test
pytest tests/test_func.py::test_run_updates_major_teams_in_a_single_put_and_dispatches_the_rebuild

# Run locally (outside container, recommended for development)
func run --builder=host

# Deploy to Knative cluster
func deploy --registry docker.io/jeremyalbrecht
```

## Architecture

A **Knative Python HTTP function** that scrapes interclub league standings from
icbad.ffbad.org and full-replaces the leaderboard of every `major` team in a
Strapi CMS entry.

### Knative lifecycle

`function/func.py` exposes `new()` returning an `InterclubUpdate`. The runtime calls:

- `start(cfg)` — receives `os.environ.copy()` as a dict on ASGI lifespan
  startup; reads `BASE_URL`/`API_URL`/`API_TOKEN`/`GITHUB_DISPATCH_TOKEN`.
- `handle(scope, receive, send)` — a **raw ASGI handler**, not a convenience
  wrapper. It must `await send(...)` a `http.response.start` then a
  `http.response.body`. Every request runs the full pipeline unconditionally.
- `stop()` / `alive()` / `ready()` — lifecycle and health hooks.

### Pipeline

`handle()` → `_run()` orchestrates three kinds of code kept deliberately
separate:

- **Network I/O** (`_fetch_teams`, `_push_teams`, `_fetch_page`,
  `_dispatch_rebuild`, `_resolve_competition`) — thin async methods on
  `InterclubUpdate`, each doing exactly one HTTP call (or, for
  `_resolve_competition`, chaining two: the season-switch page then the
  league page). Easy to fake in tests via `FakeClient`.
- **Pure logic** (module-level functions: `parse_group`, `parse_competition`,
  `find_league_link`, `leaderboard_from_group`, `apply_leaderboard`,
  `strip_ids`) — no I/O, take/return plain data, and are tested directly
  without touching `InterclubUpdate` at all.
- **Orchestration** (`_run`) — loops over major teams, calls the above, and
  sorts each team into `updated` / `skipped` / `failed`.

`_run`'s loop, per major team:

1. `_resolve_competition` switches icbad.ffbad.org to the team's season
   (`?switchSaison=<first year of "2025-2026">`), matches `icbad_id` against
   the visible text of the page's `<a class="link">` elements (the site has
   no stable per-team league id, only this text-matched link), and parses the
   league page if found. Returns `None` if no link matched — that team is
   `skipped`, not an error.
2. `apply_leaderboard` merges the parsed standings into a **working copy** of
   the team list (returned, not mutated) — it's a pure dict transform, not a
   network call, so a bad merge (`IndexError` from a `group` number that
   doesn't match any parsed pool) is caught around just that call and the
   team is recorded as `failed` without discarding the working list built up
   from earlier teams.
3. Once the loop finishes, if anything was `updated`, `_push_teams` sends
   **one** `PUT` with the whole working list (ids stripped via `strip_ids`) —
   not one PUT per team. A team with nothing to update triggers no PUT call
   at all.
4. If (and only if) the PUT happened, `_dispatch_rebuild` fires a
   `workflow_dispatch` at `jeremyalbrecht/csjbad`'s
   `azure-augny-badminton-fr.yml` so the public site rebuilds with the new
   standings. It swallows its own exceptions and returns `False` on failure
   rather than raising — by the time it runs, Strapi has already been
   updated successfully, and a rebuild dispatch failing shouldn't turn that
   into a 500. Its outcome is surfaced as `"dispatched": bool` in the
   response instead.

### Constraints worth knowing

- **The `<td>` cell indices in `parse_group` are load-bearing**: 2=team name,
  3=played, 4=won, 5=draw, 6=lost, 7=forfeits (skipped — not part of Strapi's
  schema), 8=bonus, 9=penalties, 10=points. Verified against a live page on
  2026-08-12; don't "simplify" this into a header-driven lookup without
  re-checking the site still emits the same column order.
- **Strapi's update is a whole-list PUT, not a per-team PATCH**, and it
  happens once per run, after the loop — not once per team like the original
  script. `apply_leaderboard` is pure and non-mutating specifically so the
  working list can be threaded through the loop and built up incrementally
  without earlier teams' updates being at risk from a later team's failure.
- **Team matching is by `ranking` + `season.name`**, not by Strapi `id` — the
  Strapi payload is handled as plain dicts (no schema dataclasses), so a
  missing key surfaces as a `KeyError`, caught per-team around
  `_resolve_competition`/`apply_leaderboard` and recorded as `failed` rather
  than aborting the run. Only a failure in `_fetch_teams` itself (e.g. Strapi
  auth rejected) propagates up to `handle()`'s top-level try/except as a 500.

### Environment variables

See the table in `README.md`. Configured via `func.yaml` `run.envs`.

### Tests

`tests/test_func.py` uses `pytest-asyncio` in strict mode and makes **no
network calls**. The HTTP client arrives through a `client_factory` on
`InterclubUpdate.__init__`. Fakes live in `tests/conftest.py`; HTML/JSON
fixtures — real, trimmed pages from icbad.ffbad.org and a sample Strapi
payload — live in `tests/fixtures/`.
