# interclub-parser

Knative Python HTTP function that scrapes interclub standings from
[icbad.ffbad.org](https://icbad.ffbad.org) and pushes them into the Strapi CMS
for [Augny Badminton](https://augny-badminton.fr).

## How it works

An HTTP request to the function triggers one full pass:

1. **Fetch teams** — `GET` the Strapi `vie-du-club` entry with a deep
   `populate` of `teams`, `teams.leaderboard`, `teams.leaderboard.rows`,
   `teams.season` and `teams.images`.
2. **Find the league** — for each team flagged `major`, switch icbad.ffbad.org
   to the team's season (`?switchSaison=<first year>`) and find the
   `<a class="link">` whose visible text contains the team's `icbad_id`
   (e.g. `"Comité 00 D1"` matches `"Interclubs Comité 00 D1 - 2025/2026"`).
   A team whose league link can't be found is skipped, not failed.
3. **Parse standings** — the league page has either a single table under a
   "Poule Unique" heading, or several `table.classement-poule` elements (one
   per pool). Team name, played/won/draw/lost, bonus, penalties and points
   are read from fixed `<td>` positions.
4. **Push to Strapi** — once every major team has been scraped, the full team
   list from step 1 is updated (each team's `id` stripped, since it isn't
   writable on `PUT`; each successfully-scraped team's `leaderboard`
   replaced) and `PUT` back to Strapi **once**, not once per team.
5. **Dispatch a site rebuild** — if anything was updated, trigger the
   [`azure-augny-badminton-fr.yml`](https://github.com/jeremyalbrecht/csjbad/actions/workflows/azure-augny-badminton-fr.yml)
   workflow on `jeremyalbrecht/csjbad` via GitHub's `workflow_dispatch` REST
   API, so the public site picks up the new standings. Nothing is dispatched
   if there was nothing to update.

A team whose league link can't be found is skipped; a team whose scrape
succeeds but whose data doesn't line up with Strapi (e.g. a `group` number
that doesn't match any parsed pool) is recorded as failed rather than
aborting the whole run — earlier teams that already merged successfully are
still included in the single PUT. Likewise, a failed dispatch (bad token,
GitHub API down) is logged and reported but doesn't fail the request —
Strapi was already updated by that point, which is the part that matters.

The response body is a JSON summary:
`{"updated": [...], "skipped": [...], "failed": [...], "dispatched": bool}`,
one `icbad_id` per major team in whichever bucket applies.

## Environment variables

Read in `start(cfg)`; configured via `func.yaml` `run.envs`.

| Variable | Required | Purpose |
|---|---|---|
| `BASE_URL` | yes | icbad.ffbad.org base URL (season-switch entry point) |
| `API_URL` | yes | Strapi `vie-du-club` entry endpoint |
| `API_TOKEN` | yes | Strapi API bearer token |
| `GITHUB_DISPATCH_TOKEN` | yes | GitHub PAT with `actions: write` on `jeremyalbrecht/csjbad`, used to dispatch the site rebuild |

At runtime the secret values are injected as plain environment variables at
`func deploy` / CI time (see `.github/workflows/image-build.yml`).

## Development

```bash
# Install dependencies (runtime + test)
pip install -e '.[dev]'

# Run tests — no network access required
pytest -q

# Run locally, outside the container
func run --builder=host
curl -i -X POST localhost:8080/
```

### Tests

`tests/test_func.py` runs under `pytest-asyncio` in strict mode and never
touches the network. The HTTP client is injected through a factory on
`InterclubUpdate.__init__`, with the fake supplied by `tests/conftest.py`.

`tests/fixtures/` holds real, trimmed pages captured from icbad.ffbad.org (the
season/region link listing and both the single-pool and multi-pool standings
layouts) plus a sample Strapi response, so the parsing tests are pinned
against actual site markup rather than an idealised shape.

## Deployment

```bash
func deploy --registry docker.io/jeremyalbrecht
```

CI builds and pushes the image on every push to `main` that touches
`function/`, `tests/`, `pyproject.toml`, or `func.yaml`, gated on the test
suite passing (`.github/workflows/image-build.yml`).

## License

MIT
