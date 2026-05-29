# Changelog

## [Unreleased]

### `scryland sales`
- Now auto-withdraws matching eBay offers at the end of every run (same logic as the `watch` cross-delist). Run it standalone after a TCG sale to sync both marketplaces immediately without waiting for the next watch cycle.

### `scryland watch`
- New `--tcg-refresh-days FLOAT` option (default 3.0): periodically runs a full TCG inventory scrape before the optimize step so `inventory.current_price` stays fresh for the `--ebay-delist-uncompetitive-gap` check. Pass `0` to disable.
- Optimizer now writes the matched price back to `inventory.current_price` immediately after each successful price update (point-of-change freshness between periodic scrapes).

### Browser reliability
- `_apply_my_inventory_filter` now verifies the filter actually applied by comparing Manage vs Add button counts, with up to 3 hard-reload retries. Silent failures previously caused the floor sweep to scan the global TCGPlayer catalog instead of the user's inventory.

### `add-inventory` improvements
- `--include-sold` flag re-lists cards that the DB shows as previously sold (use when the CSV is your full current inventory and you've re-acquired things).
- Auto-writes `<input>_priced.csv` at the end of every run with the real TCG-found prices substituted into the Price columns. Subsequent runs against that file skip floor cards via the existing `--csv-min-price` pre-filter without a second TCG search.
- Collector-#-first matching: when set + collector # both match a search row, accept it without requiring a name fuzzy-match (rescues cards TCG decorates as `(Borderless)` / `(Showcase)` or that have DFC name reorderings).
- Fallback search retry without the set filter when the first search returns 0 results — Mythic Tools set names sometimes don't line up with TCG's dropdown labels (promos, special editions).
- New "Not Added (N)" focused table at the end of each run, listing every card we couldn't add (excluding intentional too-cheap drops).
- Manual deferred-review prompts now show the card quantity inline.

### `list-on-ebay` improvements
- End-of-run "List on eBay Summary" + "Not Listed" focused tables, mirroring `add-inventory`'s shape.
- Retry on `errorId 25001` ("Internal Server Error" / "Core Inventory Service") for the inventory PUT — these are transient and were previously failing the card outright.
- Retry on `errorId 25604` ("Availability not found") for the publish step — eventual-consistency in eBay's inventory service that previously failed cards on first try.
- Aspect-rename diagnostic: when eBay flags a "Some item specifics were renamed" warning, we now GET the inventory item back and log a `dropped: [...] / appeared: [...]` diff so the actual rename is visible instead of hidden.

### `scryland watch`
- New `--ebay-delist-uncompetitive-gap FLOAT` mode: withdraw active eBay listings whose price exceeds the matching TCG inventory price by more than `$gap`. Targets the "no buyer would ever pick eBay $0.99 over TCG $0.10" case.

### Fixes
- Wrap post-save `goto(inventory_url)` in `add-inventory` with `retry_on_flaky` to handle the `net::ERR_ABORTED` race against the save's redirect.
- Wrap `is_already_listed` and `get_tcg_lowest_price` evaluations with `retry_on_flaky` so the click-Add → manage-page navigation race no longer raises "Execution context was destroyed."
- `add-inventory` priced-CSV path now correctly handles input filenames without a `.csv` extension (e.g. `Mythic Tools List Export (all.ards)`).

## [0.1.0] — 2026-04-19

Initial public release.

### Features
- **TCGPlayer automation** via Playwright: price optimize using the Price Differential Report, inventory sync, orders/sales scraping, CSV import from Mythic Tools.
- **eBay Sell API integration**: list cards via the Inventory API, undercut pricing via Browse API, atomic per-order sales recording via Fulfillment API, Scryfall-powered images + condition descriptors.
- **Cross-marketplace automation**: auto-delist on TCG when sold on eBay (with lazy browser spin-up in `--ebay-only` mode), withdraw eBay offers when sold on TCG.
- **`scryland watch`** recurring loop with parallel Browse queries (~5–7× faster than serial), total-price undercut math that accounts for your shipping, per-run + cumulative summary tables.
- **`scryland doctor`** health check — config, DB schema, eBay auth, Scryfall reachability, shipping-policy consistency.
- **`scryland compare`** side-by-side TCG vs eBay prices with colored delta rendering.
- **`scryland ebay-refresh-titles`** re-pushes titles/aspects/images to live listings.
- **Encrypted credential storage** (Fernet + PBKDF2 @ 600k iterations) for both TCGPlayer and eBay.
- **Scryfall disk cache** with 7-day TTL — repeat refreshes are ~5× faster.
- **Rate-limited eBay client** (token bucket, 300/min default).
- **Retry helpers** for flaky Playwright errors (stale handles, context destroyed, ERR_ABORTED, timeouts with page-reload recovery).

### Tooling
- 361 tests, pytest + pytest-asyncio + pytest-cov.
- Ruff lint + format.
- Mypy type-check (soft).
- Pre-commit hooks: trailing whitespace, EOF, YAML/TOML validation, ruff lint + format, mypy, pytest on push.
- CI on Python 3.11 / 3.12 / 3.13 with coverage.
- Dependabot for Python, GitHub Actions, and pre-commit.
