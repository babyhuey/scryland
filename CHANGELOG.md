# Changelog

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
- 341 tests, pytest + pytest-asyncio + pytest-cov.
- Ruff lint + format.
- Mypy type-check (soft).
- Pre-commit hooks: trailing whitespace, EOF, YAML/TOML validation, ruff lint + format, mypy, pytest on push.
- CI on Python 3.11 / 3.12 / 3.13 with coverage.
- Dependabot for Python, GitHub Actions, and pre-commit.
