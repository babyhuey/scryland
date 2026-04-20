# Contributing to Scryland

Thanks for considering a contribution. Scryland is a personal project that got thorough enough to share; PRs are welcome but the scope stays focused on MTG seller workflows (TCGPlayer + eBay).

## Development setup

```bash
git clone https://github.com/babyhuey/scryland
cd scryland

# uv handles everything: venv, deps, python version
uv sync --extra dev
uv run playwright install chromium

# Set up pre-commit (runs on every commit)
uv run pre-commit install
uv run pre-commit install --hook-type pre-push
```

## Running tests

```bash
uv run pytest                 # full suite with coverage
uv run pytest -x              # stop on first failure
uv run pytest tests/test_db.py  # single file
```

Aim for tests on new library code. CLI command bodies are harder to unit-test (lots of I/O) and are OK to cover via `CliRunner` with mocked deps where possible.

## Style

Ruff enforces formatting and linting. Pre-commit runs it automatically. If you want to run manually:

```bash
uv run ruff check --fix src/ tests/
uv run ruff format src/ tests/
```

## Committing

- Prefer small, single-topic commits.
- First line ≤72 chars, imperative (`Add X`, not `Added X`).
- Mention the user-visible behavior change, not the diff.
- If touching browser-automation code, note that CI can't catch browser-specific regressions — test manually against a live TCG session before merging.

## Before opening a PR

1. `uv run pytest` — all tests pass
2. `uv run ruff check src/ tests/` — no lint errors
3. `uv run ruff format --check src/ tests/` — formatting is clean
4. Update `CHANGELOG.md` under a new heading if your change is user-visible

## Not in scope

- Non-MTG games (Pokémon, Yu-Gi-Oh, etc.)
- Auto-buying or bidding
- Anything that would violate TCGPlayer or eBay ToS (evading rate limits, scraping buyer data, fake listings)
