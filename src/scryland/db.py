"""SQLite inventory database for tracking listings and detecting sales."""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from scryland.models import Listing, SyncReport

logger = logging.getLogger("scryland")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_name TEXT NOT NULL,
    set_name TEXT DEFAULT '',
    condition TEXT DEFAULT '',
    finish TEXT DEFAULT '',
    quantity INTEGER DEFAULT 0,
    current_price REAL,
    tcg_low_price REAL,
    market_price REAL,
    tcgplayer_id TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    last_price_change TEXT,
    status TEXT DEFAULT 'active',
    UNIQUE(product_name, condition, finish)
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    total_items INTEGER DEFAULT 0,
    added INTEGER DEFAULT 0,
    removed INTEGER DEFAULT 0,
    price_changes INTEGER DEFAULT 0,
    quantity_changes INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_name TEXT NOT NULL,
    condition TEXT DEFAULT '',
    our_price REAL,
    tcg_low REAL,
    market_price REAL,
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_price_history_product
    ON price_history(product_name, condition, recorded_at);

CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_number TEXT NOT NULL,
    order_date TEXT NOT NULL,
    buyer_name TEXT DEFAULT '',
    status TEXT DEFAULT '',
    product_name TEXT NOT NULL,
    condition TEXT DEFAULT '',
    quantity INTEGER DEFAULT 1,
    sale_price REAL DEFAULT 0,
    shipping_amt REAL DEFAULT 0,
    total_amount REAL DEFAULT 0,
    fee_amount REAL DEFAULT 0,
    net_amount REAL DEFAULT 0,
    recorded_at TEXT NOT NULL,
    marketplace TEXT DEFAULT 'tcgplayer',  -- tcgplayer | ebay
    UNIQUE(order_number, product_name, condition, marketplace)
);

CREATE TABLE IF NOT EXISTS ebay_listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_key TEXT NOT NULL,        -- shared with inventory for cross-marketplace match
    sku TEXT NOT NULL UNIQUE,
    offer_id TEXT,
    listing_id TEXT,
    product_name TEXT NOT NULL,
    set_name TEXT DEFAULT '',
    collector_number TEXT DEFAULT '',
    condition TEXT DEFAULT '',
    is_foil INTEGER DEFAULT 0,
    price REAL,
    quantity INTEGER DEFAULT 0,
    status TEXT DEFAULT 'draft',        -- draft | active | ended | sold
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_synced TEXT
);

CREATE INDEX IF NOT EXISTS idx_ebay_canonical
    ON ebay_listings(canonical_key);

CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class InventoryDB:
    """SQLite-backed inventory tracker."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        """Open database connection and initialize schema."""
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._migrate()
        self._reclassify_false_sold()
        logger.debug("Database opened: %s", self._db_path)

    def _migrate(self) -> None:
        """Apply additive migrations that `CREATE TABLE IF NOT EXISTS` misses.

        SQLite skips column changes on existing tables, so we manually
        ADD COLUMN for anything added after v1.
        """
        existing_sales_cols = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(sales)").fetchall()
        }
        if "marketplace" not in existing_sales_cols:
            self.conn.execute("ALTER TABLE sales ADD COLUMN marketplace TEXT DEFAULT 'tcgplayer'")
            logger.info("DB migration: added sales.marketplace column")
        # Backfill: rows inserted before the marketplace column existed,
        # or eBay rows written before `_marketplace` was plumbed through,
        # can be identified by the EBAY- prefix `order_to_sales_rows` adds.
        fixed = self.conn.execute(
            "UPDATE sales SET marketplace='ebay' "
            "WHERE marketplace='tcgplayer' AND order_number LIKE 'EBAY-%'"
        ).rowcount
        if fixed:
            logger.info("DB migration: reclassified %d EBAY-* rows as marketplace=ebay", fixed)
        existing_ph_cols = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(price_history)").fetchall()
        }
        if "marketplace" not in existing_ph_cols:
            self.conn.execute(
                "ALTER TABLE price_history ADD COLUMN marketplace TEXT DEFAULT 'tcgplayer'"
            )
            logger.info("DB migration: added price_history.marketplace column")
        if "cross_delist_done" not in existing_sales_cols:
            # Tracks whether we've already fired the cross-marketplace
            # withdraw for this sale. Without it the withdraw sweep
            # re-processes every historical sale each watch pass.
            self.conn.execute("ALTER TABLE sales ADD COLUMN cross_delist_done INTEGER DEFAULT 0")
            # Seed legacy rows as done so we don't flood withdraws for
            # long-past sales on first run after the migration.
            self.conn.execute(
                "UPDATE sales SET cross_delist_done = 1 "
                "WHERE cross_delist_done IS NULL OR cross_delist_done = 0"
            )
            logger.info("DB migration: added sales.cross_delist_done column")
        self.conn.commit()
        self._recompute_ebay_canonical_keys()

    # Version of the canonical_key formula. Bumped when we change the
    # canonical_key composition. Stored as PRAGMA user_version.
    _CANONICAL_KEY_VERSION = 3

    def _recompute_ebay_canonical_keys(self) -> None:
        """Re-derive canonical_key on ebay_listings rows when the formula bumped.

        Guarded by PRAGMA user_version so it only runs once per version
        change — avoids churning keys on every db.open().
        """
        row = self.conn.execute("PRAGMA user_version").fetchone()
        current_version = int(row[0]) if row else 0
        if current_version >= self._CANONICAL_KEY_VERSION:
            return

        rows = self.conn.execute(
            "SELECT id, product_name, set_name, collector_number, condition, "
            "is_foil, canonical_key FROM ebay_listings"
        ).fetchall()
        updated = 0
        for row in rows:
            new_key = canonical_key(
                row["product_name"],
                row["condition"] or "",
                bool(row["is_foil"]),
                set_name=row["set_name"] or "",
                collector_number=row["collector_number"] or "",
            )
            if new_key != (row["canonical_key"] or ""):
                self.conn.execute(
                    "UPDATE ebay_listings SET canonical_key = ? WHERE id = ?",
                    (new_key, row["id"]),
                )
                updated += 1
        self.conn.execute(f"PRAGMA user_version = {self._CANONICAL_KEY_VERSION}")
        self.conn.commit()
        if updated:
            logger.info(
                "DB migration v%d: recomputed canonical_key on %d eBay listing(s)",
                self._CANONICAL_KEY_VERSION,
                updated,
            )

    def _reclassify_false_sold(self) -> None:
        """Fix historical 'sold' rows that have no matching sale record.

        Earlier versions of `sync` marked any listing missing from a scrape
        as 'sold', which produced false positives whenever the scrape had a
        pagination or rendering glitch. We now only mark 'sold' when an
        actual order row exists. Reclassify legacy bad rows to 'removed'.
        """
        # Fuzzy match inventory.product_name ↔ sales.product_name because
        # TCG names include parentheticals that the orders page strips.
        cur = self.conn.execute(
            "UPDATE inventory SET status = 'removed' "
            "WHERE status = 'sold' "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM sales "
            "  WHERE sales.product_name = inventory.product_name "
            "     OR sales.product_name LIKE '%' || inventory.product_name || '%' "
            "     OR inventory.product_name LIKE '%' || sales.product_name || '%' "
            ")"
        )
        if cur.rowcount:
            logger.info(
                "Reclassified %d legacy 'sold' rows with no matching sale as 'removed'",
                cur.rowcount,
            )
        self.conn.commit()

    def close(self) -> None:
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database not opened. Call open() first.")
        return self._conn

    def upsert_listing(self, listing: Listing, finish: str = "") -> bool:
        """Insert or update a listing. Returns True if this is a new listing."""
        now = datetime.now(UTC).isoformat()

        # Check if it already exists
        existing = self.conn.execute(
            "SELECT id, current_price, quantity, status, product_name FROM inventory "
            "WHERE product_name = ? AND condition = ? AND finish = ?",
            (listing.product_name, listing.condition, finish),
        ).fetchone()

        # DFC fallback: TCG sometimes scrapes the front-face only
        # ("Grave Researcher") and sometimes the full form ("Grave
        # Researcher // Reanimate"). Without this, two rows accumulate for
        # the same physical listing, breaking find_inventory_by_canonical
        # (which then refuses to auto-delist as ambiguous). Only collapses
        # rows that match by DFC front-face — parentheticals like
        # "(Borderless)" stay distinct, since they really are separate
        # printings.
        if not existing:
            target_front = _dfc_front_key(listing.product_name)
            if target_front:
                candidates = self.conn.execute(
                    "SELECT id, current_price, quantity, status, product_name "
                    "FROM inventory WHERE condition = ? AND finish = ?",
                    (listing.condition, finish),
                ).fetchall()
                for cand in candidates:
                    if _dfc_front_key(cand["product_name"]) == target_front:
                        existing = cand
                        break

        if existing:
            # Update existing
            updates = {
                "quantity": listing.quantity,
                "current_price": float(listing.current_price),
                "tcg_low_price": float(listing.tcg_low_price) if listing.tcg_low_price else None,
                "market_price": float(listing.market_price) if listing.market_price else None,
                "last_seen": now,
                "status": "active",
                "set_name": listing.set_name,
            }
            # Track price changes
            old_price = existing["current_price"]
            if old_price is not None and abs(old_price - float(listing.current_price)) > 0.001:
                updates["last_price_change"] = now

            # If the new scrape carries the full DFC form ("X // Y") and
            # the existing row only has the front face, upgrade the name —
            # downstream canonical-key lookups match better against the
            # full form (which is what eBay/Scryfall use). Never downgrade
            # a richer name to a shorter one.
            new_name = existing["product_name"]
            if "//" in listing.product_name and "//" not in (existing["product_name"] or ""):
                new_name = listing.product_name

            self.conn.execute(
                "UPDATE inventory SET "
                "product_name=?, quantity=?, current_price=?, tcg_low_price=?, market_price=?, "
                "last_seen=?, status=?, set_name=?, last_price_change=COALESCE(?, last_price_change) "
                "WHERE id=?",
                (
                    new_name,
                    updates["quantity"],
                    updates["current_price"],
                    updates["tcg_low_price"],
                    updates["market_price"],
                    updates["last_seen"],
                    updates["status"],
                    updates["set_name"],
                    updates.get("last_price_change"),
                    existing["id"],
                ),
            )
            return False
        else:
            # Insert new
            self.conn.execute(
                "INSERT INTO inventory "
                "(product_name, set_name, condition, finish, quantity, current_price, "
                "tcg_low_price, market_price, tcgplayer_id, first_seen, last_seen, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')",
                (
                    listing.product_name,
                    listing.set_name,
                    listing.condition,
                    finish,
                    listing.quantity,
                    float(listing.current_price),
                    float(listing.tcg_low_price) if listing.tcg_low_price else None,
                    float(listing.market_price) if listing.market_price else None,
                    listing.tcgplayer_id,
                    now,
                    now,
                ),
            )
            return True

    def is_listed(self, product_name: str, condition: str, finish: str = "") -> bool:
        """Check if a card is already actively listed in our inventory.

        Condition is compared with any embedded 'Foil' token stripped on
        both sides — see `_bare_condition`.
        """
        row = self.conn.execute(
            "SELECT id FROM inventory "
            "WHERE product_name = ? AND TRIM(REPLACE(condition, 'Foil', '')) = ? "
            "AND finish = ? AND status = 'active'",
            (product_name, _bare_condition(condition), finish),
        ).fetchone()
        return row is not None

    def is_sold(self, product_name: str, condition: str, finish: str = "") -> bool:
        """Check if a card was sold/removed."""
        row = self.conn.execute(
            "SELECT id FROM inventory "
            "WHERE product_name = ? AND condition = ? AND finish = ? AND status = 'sold'",
            (product_name, condition, finish),
        ).fetchone()
        return row is not None

    def is_known(self, product_name: str, condition: str, finish: str = "") -> str | None:
        """Check if a card exists in the DB at all. Returns status or None.

        Condition is compared with any embedded 'Foil' token stripped on
        both sides so a bare condition ("Near Mint") matches a row that
        `sync()` stored with the finish embedded ("Near Mint Foil") — see
        `_bare_condition`. Without this, add-inventory's foil dedup check
        never hits.
        """
        row = self.conn.execute(
            "SELECT status FROM inventory WHERE product_name = ? "
            "AND TRIM(REPLACE(condition, 'Foil', '')) = ? AND finish = ?",
            (product_name, _bare_condition(condition), finish),
        ).fetchone()
        return row["status"] if row else None

    def is_listed_fuzzy(self, card_name: str, condition: str, finish: str = "") -> bool:
        """Check if a card is listed, using fuzzy name matching.

        Handles cases where TCGPlayer names differ slightly from CSV names
        (e.g., collector numbers in parentheses, double-faced card names).
        """
        # Exact match first
        if self.is_listed(card_name, condition, finish):
            return True

        bare_condition = _bare_condition(condition)

        # Check front face only for double-faced cards
        front_face = card_name.split("//")[0].strip()
        if front_face != card_name:
            row = self.conn.execute(
                "SELECT id FROM inventory "
                "WHERE product_name LIKE ? ESCAPE '\\' "
                "AND TRIM(REPLACE(condition, 'Foil', '')) = ? AND finish = ? AND status = 'active'",
                (f"%{_escape_like(front_face)}%", bare_condition, finish),
            ).fetchone()
            if row:
                return True

        # Check if any active listing contains this card name
        row = self.conn.execute(
            "SELECT id FROM inventory "
            "WHERE product_name LIKE ? ESCAPE '\\' "
            "AND TRIM(REPLACE(condition, 'Foil', '')) = ? AND finish = ? AND status = 'active'",
            (f"%{_escape_like(card_name)}%", bare_condition, finish),
        ).fetchone()
        return row is not None

    def record_price(self, listing) -> None:
        """Record a price snapshot for a listing."""
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO price_history (product_name, condition, our_price, tcg_low, market_price, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                listing.product_name,
                listing.condition,
                float(listing.current_price),
                float(listing.tcg_low_price) if listing.tcg_low_price else None,
                float(listing.market_price) if listing.market_price else None,
                now,
            ),
        )

    def record_prices(self, listings: list) -> None:
        """Record price snapshots for a batch of listings."""
        for listing in listings:
            self.record_price(listing)
        self.conn.commit()

    def record_ebay_price(
        self,
        product_name: str,
        condition: str,
        our_price: float,
        competitor_low: float | None = None,
    ) -> None:
        """Snapshot one eBay listing's price + current competitor lowest."""
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO price_history "
            "(product_name, condition, our_price, tcg_low, market_price, "
            " recorded_at, marketplace) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                product_name,
                condition,
                our_price,
                competitor_low,
                None,
                now,
                "ebay",
            ),
        )
        self.conn.commit()

    def get_price_history(
        self, product_name: str, condition: str = "", limit: int = 30
    ) -> list[dict]:
        """Get price history for a specific card."""
        if condition:
            rows = self.conn.execute(
                "SELECT * FROM price_history WHERE product_name = ? AND condition = ? "
                "ORDER BY recorded_at DESC LIMIT ?",
                (product_name, condition, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM price_history WHERE product_name = ? "
                "ORDER BY recorded_at DESC LIMIT ?",
                (product_name, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_price_extremes(self) -> list[dict]:
        """Get the highest and lowest prices seen for each card, plus current price."""
        rows = self.conn.execute("""
            SELECT ph.product_name, ph.condition,
                MIN(ph.tcg_low) as lowest_seen,
                MAX(ph.tcg_low) as highest_seen,
                MIN(ph.our_price) as our_lowest,
                MAX(ph.our_price) as our_highest,
                COUNT(*) as data_points,
                MIN(ph.recorded_at) as first_seen,
                MAX(ph.recorded_at) as last_seen,
                i.current_price as current_price
            FROM price_history ph
            LEFT JOIN inventory i
                ON ph.product_name = i.product_name
                AND ph.condition = i.condition
                AND i.status = 'active'
            GROUP BY ph.product_name, ph.condition
            ORDER BY ph.product_name
        """).fetchall()
        return [dict(row) for row in rows]

    def get_all_active(self) -> list[dict]:
        """Get all active inventory items."""
        rows = self.conn.execute(
            "SELECT * FROM inventory WHERE status = 'active' ORDER BY product_name"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_active_at_or_below_price(self, threshold: float) -> list[dict]:
        """Active inventory rows with current_price <= threshold.

        Used by the TCG floor sweep — picks up cards that already sit at
        the bottom of the market (no differential to flag them in TCG's
        report, so the standard optimize pass misses them).
        """
        rows = self.conn.execute(
            "SELECT * FROM inventory "
            "WHERE status = 'active' AND current_price IS NOT NULL "
            "AND current_price <= ? ORDER BY current_price",
            (threshold,),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_inventory_removed(self, product_name: str, condition: str, finish: str) -> None:
        """Mark a single inventory row as removed (after a successful delist)."""
        self.conn.execute(
            "UPDATE inventory SET status = 'removed', "
            "last_seen = strftime('%Y-%m-%dT%H:%M:%S','now') "
            "WHERE product_name = ? AND condition = ? AND finish = ?",
            (product_name, condition, finish),
        )
        self.conn.commit()

    def clear_sold(self) -> int:
        """Delete all sold/removed items from the database. Returns count deleted."""
        cursor = self.conn.execute("DELETE FROM inventory WHERE status = 'sold'")
        self.conn.commit()
        return cursor.rowcount

    def get_all_sold(self) -> list[dict]:
        """Get all sold/removed items."""
        rows = self.conn.execute(
            "SELECT * FROM inventory WHERE status = 'sold' ORDER BY last_seen DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_summary(self) -> dict:
        """Get inventory summary stats."""
        active = self.conn.execute(
            "SELECT COUNT(*) as count, SUM(current_price * quantity) as total_value, "
            "SUM(quantity) as total_qty FROM inventory WHERE status = 'active'"
        ).fetchone()

        sold = self.conn.execute(
            "SELECT COUNT(*) as count FROM inventory WHERE status = 'sold'"
        ).fetchone()

        last_sync = self.conn.execute(
            "SELECT timestamp FROM sync_log ORDER BY id DESC LIMIT 1"
        ).fetchone()

        return {
            "active_listings": active["count"] or 0,
            "total_value": active["total_value"] or 0.0,
            "total_quantity": active["total_qty"] or 0,
            "sold_count": sold["count"] or 0,
            "last_sync": last_sync["timestamp"] if last_sync else "Never",
        }

    def sync(self, current_listings: list[Listing]) -> SyncReport:
        """Sync scraped listings against the database.

        1. Mark all active items as 'checking'
        2. Upsert each scraped listing (marks as 'active')
        3. Any still 'checking' → 'removed' (not on TCGPlayer right now)
        4. Return a report of changes

        Note: 'removed' means "we didn't see it this run" — the cause could
        be a genuine sale, a manual delist, or (worryingly) a scrape hiccup.
        True 'sold' status is only set by `record_order_sales` when we find
        a matching order. Keeping the two separate avoids false-positive
        "sold" marks when the scrape misses a healthy listing.
        """
        now = datetime.now(UTC)
        added: list[str] = []
        removed: list[str] = []
        price_changed: list[dict] = []
        quantity_changed: list[dict] = []

        # Record price history for all listings (separate operation, not rolled back with sync)
        self.record_prices(current_listings)

        try:
            self.conn.execute("SAVEPOINT sync")

            # Step 1: Mark all active as 'checking'
            self.conn.execute("UPDATE inventory SET status = 'checking' WHERE status = 'active'")

            # Step 2: Upsert each listing
            for listing in current_listings:
                # Detect finish from condition name (e.g., "Near Mint Foil")
                finish = ""
                condition = listing.condition
                if "Foil" in condition:
                    finish = "Foil"

                # Check old values before upserting
                old = self.conn.execute(
                    "SELECT current_price, quantity FROM inventory "
                    "WHERE product_name = ? AND condition = ? AND finish = ?",
                    (listing.product_name, listing.condition, finish),
                ).fetchone()

                is_new = self.upsert_listing(listing, finish)

                if is_new:
                    added.append(f"{listing.product_name} ({listing.condition})")
                elif old:
                    old_price = old["current_price"]
                    old_qty = old["quantity"]
                    if (
                        old_price is not None
                        and abs(old_price - float(listing.current_price)) > 0.001
                    ):
                        price_changed.append(
                            {
                                "name": f"{listing.product_name} ({listing.condition})",
                                "old_price": old_price,
                                "new_price": float(listing.current_price),
                            }
                        )
                    if old_qty != listing.quantity:
                        quantity_changed.append(
                            {
                                "name": f"{listing.product_name} ({listing.condition})",
                                "old_qty": old_qty,
                                "new_qty": listing.quantity,
                            }
                        )

            # Step 3: Any still 'checking' → removed
            still_checking = self.conn.execute(
                "SELECT product_name, condition FROM inventory WHERE status = 'checking'"
            ).fetchall()
            for row in still_checking:
                removed.append(f"{row['product_name']} ({row['condition']})")
            self.conn.execute(
                "UPDATE inventory SET status = 'removed', last_seen = ? WHERE status = 'checking'",
                (now.isoformat(),),
            )

            # Step 4: Log the sync
            active_count = self.conn.execute(
                "SELECT COUNT(*) as c FROM inventory WHERE status = 'active'"
            ).fetchone()["c"]

            self.conn.execute(
                "INSERT INTO sync_log (timestamp, total_items, added, removed, price_changes, quantity_changes) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    now.isoformat(),
                    active_count,
                    len(added),
                    len(removed),
                    len(price_changed),
                    len(quantity_changed),
                ),
            )

            self.conn.execute("RELEASE sync")
            self.conn.commit()
        except Exception:
            self.conn.execute("ROLLBACK TO sync")
            self.conn.execute("RELEASE sync")
            raise

        return SyncReport(
            timestamp=now,
            total_active=active_count,
            added=added,
            removed=removed,
            price_changed=price_changed,
            quantity_changed=quantity_changed,
        )

    # --- Sales tracking ---

    def record_sale(self, sale: dict) -> bool:
        """Record a sale from the orders page. Returns True if new."""
        now = datetime.now(UTC).isoformat()
        marketplace = sale.get("_marketplace") or "tcgplayer"

        existing = self.conn.execute(
            "SELECT id FROM sales WHERE order_number = ? AND product_name = ? AND condition = ? AND marketplace = ?",
            (sale["order_number"], sale["product_name"], sale.get("condition", ""), marketplace),
        ).fetchone()

        if existing:
            return False

        self.conn.execute(
            "INSERT INTO sales "
            "(order_number, order_date, buyer_name, status, product_name, condition, "
            "quantity, sale_price, shipping_amt, total_amount, fee_amount, net_amount, recorded_at, marketplace) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sale["order_number"],
                sale.get("order_date", ""),
                sale.get("buyer_name", ""),
                sale.get("status", ""),
                sale["product_name"],
                sale.get("condition", ""),
                sale.get("quantity", 1),
                sale.get("sale_price", 0),
                sale.get("shipping_amt", 0),
                sale.get("total_amount", 0),
                sale.get("fee_amount", 0),
                sale.get("net_amount", 0),
                now,
                marketplace,
            ),
        )

        self._mark_inventory_sold(sale["product_name"], now, sale.get("condition", ""))

        self.conn.commit()
        return True

    def is_sale_recorded(
        self, order_number: str, product_name: str, condition: str = "", marketplace: str = "tcgplayer"
    ) -> bool:
        """Return True if this order+product+condition+marketplace sale is
        already stored. Lets a caller distinguish "just recorded this sweep"
        from "already known" before doing a one-time side effect (e.g.
        marking a listing sold) that shouldn't repeat on every sweep."""
        row = self.conn.execute(
            "SELECT id FROM sales WHERE order_number = ? AND product_name = ? "
            "AND condition = ? AND marketplace = ?",
            (order_number, product_name, condition, marketplace),
        ).fetchone()
        return row is not None

    def record_order_sales(self, sales: list[dict]) -> int:
        """Atomically record every product sold in a single order.

        Uses a SAVEPOINT so this is safe to call whether or not an outer
        transaction is already open (avoids BEGIN-within-BEGIN errors with
        sqlite3's implicit transaction management). Returns the count of new
        sale rows inserted (zero if the order was already known).
        """
        if not sales:
            return 0

        new_count = 0
        try:
            self.conn.execute("SAVEPOINT record_order_sales")
            for sale in sales:
                if self._insert_sale_row(sale):
                    new_count += 1
            self.conn.execute("RELEASE record_order_sales")
        except Exception:
            self.conn.execute("ROLLBACK TO record_order_sales")
            self.conn.execute("RELEASE record_order_sales")
            raise
        return new_count

    def _insert_sale_row(self, sale: dict) -> bool:
        """Insert one sale row (no commit). Returns True if new."""
        now = datetime.now(UTC).isoformat()
        marketplace = sale.get("_marketplace") or "tcgplayer"
        existing = self.conn.execute(
            "SELECT id FROM sales WHERE order_number = ? AND product_name = ? AND condition = ? AND marketplace = ?",
            (sale["order_number"], sale["product_name"], sale.get("condition", ""), marketplace),
        ).fetchone()
        if existing:
            return False
        self.conn.execute(
            "INSERT INTO sales "
            "(order_number, order_date, buyer_name, status, product_name, condition, "
            "quantity, sale_price, shipping_amt, total_amount, fee_amount, net_amount, "
            "recorded_at, marketplace) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sale["order_number"],
                sale.get("order_date", ""),
                sale.get("buyer_name", ""),
                sale.get("status", ""),
                sale["product_name"],
                sale.get("condition", ""),
                sale.get("quantity", 1),
                sale.get("sale_price", 0),
                sale.get("shipping_amt", 0),
                sale.get("total_amount", 0),
                sale.get("fee_amount", 0),
                sale.get("net_amount", 0),
                now,
                marketplace,
            ),
        )
        self._mark_inventory_sold(sale["product_name"], now, sale.get("condition", ""))
        return True

    def _mark_inventory_sold(self, product_name: str, now_iso: str, condition: str = "") -> None:
        """Mark active inventory rows matching `product_name` as sold.

        Matches product_name by case-insensitive EQUALITY, not a `%name%`
        substring scan — the old substring match marked unrelated cards sold
        too (e.g. "Island" matching "Island of Wak-Wak"). When the sale's
        condition is known, also constrains on it (and the finish embedded
        in it, e.g. "Near Mint Foil") so selling one condition/finish
        doesn't mark every other condition/finish of the same card sold.
        """
        if condition:
            finish = "Foil" if "Foil" in condition else ""
            self.conn.execute(
                "UPDATE inventory SET status = 'sold', last_seen = ? "
                "WHERE LOWER(product_name) = LOWER(?) AND condition = ? AND finish = ? "
                "AND status = 'active'",
                (now_iso, product_name, condition, finish),
            )
        else:
            self.conn.execute(
                "UPDATE inventory SET status = 'sold', last_seen = ? "
                "WHERE LOWER(product_name) = LOWER(?) AND status = 'active'",
                (now_iso, product_name),
            )

    def get_known_order_numbers(self) -> set[str]:
        """Get all order numbers already recorded."""
        rows = self.conn.execute("SELECT DISTINCT order_number FROM sales").fetchall()
        return {row["order_number"] for row in rows}

    def get_all_sales(self) -> list[dict]:
        """Get all sales ordered by date."""
        rows = self.conn.execute("SELECT * FROM sales ORDER BY order_date DESC").fetchall()
        return [dict(row) for row in rows]

    def get_sales_summary(self, marketplace: str | None = None) -> dict:
        """Get sales statistics. Pass `marketplace` to scope to one side
        (e.g. 'tcgplayer' or 'ebay'). None = all sales combined."""
        if marketplace is None:
            where = ""
            params: tuple = ()
        else:
            where = "WHERE marketplace = ?"
            params = (marketplace,)
        # nosec B608 — `where` is one of two hardcoded literals above
        # ("" or "WHERE marketplace = ?"), and the `marketplace` value is
        # bound via `params`. No user input ever flows into the SQL string.
        stats = self.conn.execute(
            "SELECT COUNT(*) as total_sales, "
            "SUM(quantity) as total_items_sold, "
            "SUM(sale_price * quantity) as total_revenue, "
            "SUM(fee_amount) as total_fees, "
            "SUM(net_amount) as total_net, "
            "AVG(sale_price) as avg_sale_price, "
            "COUNT(DISTINCT order_number) as total_orders "
            f"FROM sales {where}",  # nosec B608
            params,
        ).fetchone()

        return {
            "marketplace": marketplace or "all",
            "total_sales": stats["total_sales"] or 0,
            "total_items_sold": stats["total_items_sold"] or 0,
            "total_revenue": stats["total_revenue"] or 0.0,
            "total_fees": stats["total_fees"] or 0.0,
            "total_net": stats["total_net"] or 0.0,
            "avg_sale_price": stats["avg_sale_price"] or 0.0,
            "total_orders": stats["total_orders"] or 0,
        }

    def get_sales_summary_by_marketplace(self) -> list[dict]:
        """Return one summary dict per marketplace with sales present."""
        rows = self.conn.execute(
            "SELECT DISTINCT COALESCE(marketplace, 'tcgplayer') as m FROM sales"
        ).fetchall()
        return [self.get_sales_summary(r["m"]) for r in rows]

    # ------------------------------------------------------------------
    # Cross-marketplace support (canonical key + eBay listing persistence)
    # ------------------------------------------------------------------

    def upsert_ebay_listing(
        self,
        *,
        sku: str,
        offer_id: str | None,
        listing_id: str | None,
        product_name: str,
        set_name: str,
        collector_number: str,
        condition: str,
        is_foil: bool,
        price: float,
        quantity: int,
        status: str,
    ) -> None:
        """Insert/update an eBay listing row. Called after each publish."""
        now = datetime.now(UTC).isoformat()
        canonical = canonical_key(
            product_name,
            condition,
            is_foil,
            set_name=set_name,
            collector_number=collector_number,
        )
        existing = self.conn.execute(
            "SELECT id FROM ebay_listings WHERE sku = ?", (sku,)
        ).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE ebay_listings SET offer_id = ?, listing_id = ?, "
                "product_name = ?, set_name = ?, collector_number = ?, "
                "condition = ?, is_foil = ?, price = ?, quantity = ?, "
                "status = ?, updated_at = ?, last_synced = ?, "
                "canonical_key = ? WHERE sku = ?",
                (
                    offer_id,
                    listing_id,
                    product_name,
                    set_name,
                    collector_number,
                    condition,
                    1 if is_foil else 0,
                    price,
                    quantity,
                    status,
                    now,
                    now,
                    canonical,
                    sku,
                ),
            )
        else:
            self.conn.execute(
                "INSERT INTO ebay_listings "
                "(canonical_key, sku, offer_id, listing_id, product_name, "
                "set_name, collector_number, condition, is_foil, price, "
                "quantity, status, created_at, updated_at, last_synced) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    canonical,
                    sku,
                    offer_id,
                    listing_id,
                    product_name,
                    set_name,
                    collector_number,
                    condition,
                    1 if is_foil else 0,
                    price,
                    quantity,
                    status,
                    now,
                    now,
                    now,
                ),
            )
        self.conn.commit()

    def get_ebay_listings(self, *, status: str | None = "active") -> list[dict]:
        """Return eBay listings filtered by status (None = all)."""
        if status is None:
            rows = self.conn.execute("SELECT * FROM ebay_listings ORDER BY product_name").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM ebay_listings WHERE status = ? ORDER BY product_name",
                (status,),
            ).fetchall()
        return [dict(r) for r in rows]

    def find_ebay_listing_by_canonical(self, key: str) -> dict | None:
        """Find an active/draft eBay listing matching the canonical key.

        Tries strict match first. Falls back to matching only on
        (name, condition, foil) if the key has no set/collector — useful
        when the caller only has TCG-side info (no set/collector metadata).
        If multiple candidates exist under the loose match, returns None to
        avoid delisting the wrong printing.
        """
        row = self.conn.execute(
            "SELECT * FROM ebay_listings WHERE canonical_key = ? "
            "AND status IN ('active', 'draft') LIMIT 1",
            (key,),
        ).fetchone()
        if row:
            return dict(row)

        parts = key.split("|")
        if len(parts) != 5:
            return None
        k_name, k_set, k_num, k_cond, k_foil = parts
        if k_set or k_num:
            # Caller supplied set or collector — strict-only.
            return None

        # Loose lookup: all active listings with matching name + cond + foil.
        candidates = self.conn.execute(
            "SELECT * FROM ebay_listings WHERE status IN ('active', 'draft')"
        ).fetchall()
        matches = []
        for r in candidates:
            r_name = _norm_name(r["product_name"])
            r_cond = (r["condition"] or "").replace("Foil", "").strip().lower()
            r_foil = "F" if r["is_foil"] else "N"
            if r_name == k_name and r_cond == k_cond and r_foil == k_foil:
                matches.append(r)
        if len(matches) == 1:
            return dict(matches[0])
        if len(matches) > 1:
            logger.warning(
                "Canonical loose-match ambiguous for '%s' (%d candidates) — "
                "refusing to act to avoid wrong-printing delist",
                k_name,
                len(matches),
            )
        return None

    def mark_ebay_listing_status(self, sku: str, status: str) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "UPDATE ebay_listings SET status = ?, updated_at = ? WHERE sku = ?",
            (status, now, sku),
        )
        self.conn.commit()

    def get_metadata(self, key: str) -> str | None:
        """Return the stored metadata string for key, or None if unset."""
        row = self.conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_metadata(self, key: str, value: str) -> None:
        """Upsert a metadata key/value pair. Used for last_tcg_scrape timestamps."""
        self.conn.execute(
            "INSERT INTO metadata(key, value) VALUES(?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def update_tcg_price(self, product_name: str, condition: str, price: float) -> int:
        """Write a fresh TCG price to all active inventory rows matching name+condition.

        Called by the optimizer after each successful price match so
        current_price stays fresh for the eBay uncompetitive-gap delist check.
        Returns the number of rows updated (0 = no active match found).
        """
        cur = self.conn.execute(
            "UPDATE inventory SET current_price = ?"
            " WHERE product_name = ? AND condition = ? AND status = 'active'",
            (price, product_name, condition),
        )
        self.conn.commit()
        return cur.rowcount

    def find_inventory_by_canonical(
        self, key: str, *, include_removed: bool = False
    ) -> dict | None:
        """Return the inventory row whose canonical key matches.

        Inventory often has empty set/collector, so we match in tiers:
          1. Strict key equality.
          2. Loose key (name + cond + foil, no set/collector).
          3. (name + cond + foil) with set-name match where either side
             may be empty.

        If the fuzziest tier yields multiple candidates we refuse to pick
        one (avoids wrong-printing delists). The caller sees None and
        should manually reconcile.

        include_removed: also search 'removed' rows. Useful for detecting
        a listing that was already successfully delisted in a prior run.
        """
        status_clause = (
            "status IN ('active', 'removed')" if include_removed else "status = 'active'"
        )
        rows = self.conn.execute(
            f"SELECT * FROM inventory WHERE {status_clause}"  # noqa: S608
        ).fetchall()
        # Phase 1 + 2: exact/loose match — return immediately on hit.
        fallback_matches: list[dict] = []
        parts_cache = key.split("|") if "|" in key else []
        for row in rows:
            is_foil = "foil" in (row["finish"] or "").lower()
            row_condition = row["condition"] or ""
            strict = canonical_key(
                row["product_name"],
                row_condition,
                is_foil,
                set_name=row["set_name"] or "",
                collector_number="",
            )
            loose = canonical_key(row["product_name"], row_condition, is_foil)
            if key == strict or key == loose:
                return dict(row)
            # Phase 3: collect fuzzy candidates, then disambiguate.
            if len(parts_cache) == 5:
                k_name, k_set, _k_num, k_cond, k_foil = parts_cache
                r_name = _norm_name(row["product_name"])
                r_set = _norm_name(row["set_name"] or "")
                r_cond = row_condition.replace("Foil", "").strip().lower()
                r_foil = "F" if is_foil else "N"
                set_ok = (not k_set) or (not r_set) or (k_set == r_set)
                if k_name == r_name and k_cond == r_cond and k_foil == r_foil and set_ok:
                    fallback_matches.append(dict(row))
        if len(fallback_matches) == 1:
            return fallback_matches[0]
        if len(fallback_matches) > 1:
            logger.warning(
                "find_inventory_by_canonical: %d ambiguous TCG candidates "
                "for key '%s' — refusing to auto-delist any. Resolve "
                "manually in TCG Seller Hub.",
                len(fallback_matches),
                key,
            )
        return None


def canonical_key(
    product_name: str,
    condition: str,
    is_foil: bool,
    *,
    set_name: str = "",
    collector_number: str = "",
) -> str:
    """Stable key identifying the same physical *printing* across marketplaces.

    Composition: normalized(name) | normalized(set) | collector# |
    condition | finish. Set + collector are included so different
    printings (reprints across sets, numbered variants) don't collide.

    Names have punctuation and parenthetical treatments stripped so
    "Hop to It" matches "Hop to It (Borderless)" *only* when set +
    collector also match.
    """
    name = _norm_name(product_name)
    set_part = _norm_name(set_name) if set_name else ""
    num = (collector_number or "").lstrip("#").lstrip("0") or ""
    cond = condition.replace("Foil", "").strip().lower()
    return f"{name}|{set_part}|{num}|{cond}|{'F' if is_foil else 'N'}"


def _bare_condition(condition: str) -> str:
    """Strip an embedded 'Foil' finish token from a condition string.

    `sync()` stores TCG's scraped condition with the finish embedded
    ("Near Mint Foil"), while callers like add-inventory pass the bare
    condition ("Near Mint") plus a separate finish. Mirrors the SQL-side
    `TRIM(REPLACE(condition, 'Foil', ''))` so both sides normalize the
    same way.
    """
    return condition.replace("Foil", "").strip()


def _escape_like(s: str) -> str:
    """Escape SQL LIKE-wildcard chars so user input can't expand the match.
    Use with `LIKE ? ESCAPE '\\\\'`."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _dfc_front_key(s: str) -> str:
    """DFC-only collapse for upsert dedup: strip the "// Back" half but
    KEEP parentheticals like "(Borderless)" — those mark genuinely
    different printings that must remain distinct rows. Narrower than
    _norm_name on purpose."""
    if not s:
        return ""
    return " ".join(s.split("//")[0].strip().lower().split())


def _norm_name(s: str) -> str:
    # Collapse DFCs to the front face — TCG scrape returns just the front
    # ("Grave Researcher") while eBay listings + Scryfall use the full
    # "Front // Back" form, which would otherwise prevent cross-match.
    front = s.split("//")[0]
    # Strip parenthetical treatments ("(Borderless)", "(Extended Art)") and
    # drop non-alphanumeric, collapse whitespace.
    core = front.split("(")[0].strip().lower()
    allowed = []
    for ch in core:
        if ch.isalnum() or ch.isspace():
            allowed.append(ch)
        else:
            allowed.append(" ")
    return " ".join("".join(allowed).split())
