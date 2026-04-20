"""Centralized CSS/XPath selectors for TCGPlayer seller portal.

Based on: store.tcgplayer.com/admin/
Verified against screenshots of actual seller portal (Level 1).
"""


class Selectors:
    """TCGPlayer seller portal selectors."""

    # --- Navigation tabs ---
    NAV_INVENTORY = "a:has-text('Inventory')"
    NAV_PRICING = "a:has-text('Pricing')"

    # --- Inventory catalog page (store.tcgplayer.com/admin/product/catalog) ---
    MY_INVENTORY_CHECKBOX = "input[type='checkbox']"
    MY_INVENTORY_LABEL = "text=My Inventory Only"
    SEARCH_INPUT = "#SearchValue"
    SEARCH_BUTTON = "input[value='Search'], button:has-text('Search')"

    # Catalog table
    CATALOG_TABLE = "table"
    CATALOG_ROW = "table tbody tr"
    COL_PRODUCT_LINE = "td:nth-child(1)"
    COL_PRODUCT_NAME = "td:nth-child(3)"
    COL_SET = "td:nth-child(4)"
    COL_RARITY = "td:nth-child(5)"
    COL_NUMBER = "td:nth-child(6)"
    COL_IN_STOCK = "td:nth-child(7)"
    # The Manage button appears as a green button in the last column for items in your inventory
    # Items not in your inventory show "Add" instead
    BTN_MANAGE = "a:has-text('Manage'), button:has-text('Manage'), input[value='Manage'], a.Manage, .manage-btn"

    # Pagination
    PAGINATION_INFO = "text=/Viewing \\d+/"
    BTN_NEXT_PAGE = "a:has-text('Next')"

    # --- Manage/Product page (after clicking Manage) ---
    BACK_TO_INVENTORY = "a:has-text('Back to Inventory')"
    BTN_SAVE = "a:has-text('Save'), button:has-text('Save'), input[value='Save']"

    # Live prices bar at top
    LIVE_PRICES_LOW = ".low-price, text=/Low/"
    LIVE_PRICES_MEDIAN = ".median-price, text=/Median/"
    LIVE_PRICES_HIGH = ".high-price, text=/High/"

    # --- Pricing table on manage page ---
    # The table: "Kodama of the West Tree - Add your prices and quantities below"
    PRICING_TABLE = "table"
    PRICING_ROW = "table tbody tr"

    # Columns within a pricing row (manage page)
    PRICE_COL_CONDITION = "td:nth-child(1)"
    PRICE_COL_TCG_LOWEST = "td:nth-child(2)"  # TCG Lowest Listing + Match button
    PRICE_COL_TCG_LAST_SOLD = "td:nth-child(4)"  # TCG Last Sold Listing + Match button
    PRICE_COL_TCG_MARKET = "td:nth-child(6)"  # TCG Market Price + Match button
    PRICE_COL_MARKETPLACE = "td:nth-child(8)"  # TCG Marketplace Price (editable)
    PRICE_COL_QUANTITY = "td:nth-child(9)"  # Total Qty (editable)

    # Match buttons within price columns
    # Each price column (Lowest, Last Sold, Market) has a Match button
    MATCH_BTN_LOWEST = (
        "td:nth-child(3) input[value='Match'], td:nth-child(3) button:has-text('Match')"
    )
    MATCH_BTN_LAST_SOLD = (
        "td:nth-child(5) input[value='Match'], td:nth-child(5) button:has-text('Match')"
    )
    MATCH_BTN_MARKET = (
        "td:nth-child(7) input[value='Match'], td:nth-child(7) button:has-text('Match')"
    )

    # Editable price input in TCG Marketplace Price column
    PRICE_INPUT = "td:nth-child(8) input[type='text'], td:nth-child(8) input[type='number']"
    QUANTITY_INPUT = "td:nth-child(9) input[type='text'], td:nth-child(9) input[type='number']"

    # Checkbox for "If set, show next lowest"
    SHOW_NEXT_LOWEST = "input[type='checkbox']"

    # --- Sidebar tools ---
    BTN_REVIEW_PRICES = "text=Review Prices"
