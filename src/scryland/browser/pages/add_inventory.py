"""Add inventory flow — search for a card and add it to TCGPlayer inventory."""

from __future__ import annotations

import logging

from playwright.async_api import Page

from scryland.browser.flaky import retry_on_flaky
from scryland.config import ScrylandConfig
from scryland.exceptions import NavigationError, SelectorNotFoundError
from scryland.mythic_csv import MythicCard

logger = logging.getLogger("scryland")


def _resolve_best_match(all_matches: list[dict]) -> tuple[dict, int]:
    """Pick the best-scoring candidate row from `all_matches` (non-empty).

    A lone candidate is NOT promoted to score 3 ("safe to auto-add") just
    for being the only match on the page — only an actual collector-number
    match or exact-name match earns that tier. A single partial-name match
    (score 1) must still fall through to the caller's non-safe path
    (skip/manual), not get auto-added.
    """
    ordered = sorted(all_matches, key=lambda m: m["score"], reverse=True)
    best = ordered[0]
    return best, best["score"]


def _is_plausible_price(text: str | None) -> bool:
    """True if `text` (a price input's raw value) parses as a positive number.

    Used to verify a Match-button click actually populated the price input,
    since TCGPlayer's Match button unreliably updates it — an empty or
    unchanged input after clicking Match must not be reported as success.
    """
    if not text:
        return False
    try:
        value = float(text.replace("$", "").replace(",", "").strip())
    except ValueError:
        return False
    return value > 0


class AddInventoryPage:
    """Handles searching for cards and adding them to TCGPlayer inventory."""

    def __init__(self, page: Page, config: ScrylandConfig) -> None:
        self._page = page
        self._config = config

    async def search_for_card(self, card: MythicCard, apply_set_filter: bool = True) -> None:
        """Search the product catalog for a specific card.

        When `apply_set_filter` is False, the set dropdown is left at its
        default (typically "All Sets"). Used as a fallback by callers when
        the strict set-filtered search returned 0 results — Mythic Tools
        set names occasionally don't line up with TCG's dropdown labels
        (e.g. promo sets, special-edition variants), so a name-only search
        within Magic gives the row-level matcher a chance to use collector #.
        """
        logger.info(
            "Searching for '%s' from '%s'%s",
            card.card_name,
            card.set_name,
            "" if apply_set_filter else " [no set filter]",
        )

        # Always navigate to the catalog page fresh. Prior in-flight
        # navigations (e.g. a previous save redirect) can abort this one —
        # the retry helper handles ERR_ABORTED and context-destroyed.
        await retry_on_flaky(
            lambda: self._page.goto(
                self._config.inventory_url,
                wait_until="domcontentloaded",
            ),
            page=self._page,
            label="search_for_card goto catalog",
        )
        # Wait for the CategoryId dropdown to populate (Knockout.js is async)
        try:
            await self._page.wait_for_function(
                "() => document.querySelector('#CategoryId') && "
                "document.querySelector('#CategoryId').options.length > 1",
                timeout=15000,
            )
        except Exception:
            logger.debug("CategoryId dropdown did not populate in time")

        # Debug: log current URL and take screenshot
        logger.debug("On page: %s", self._page.url)

        # Set Product Line filter using JS (Knockout.js populates these dynamically)
        try:
            selected = await self._page.evaluate(
                """(targetText) => {
                const sel = document.querySelector('#CategoryId');
                if (!sel) return null;
                const options = Array.from(sel.options);
                for (const opt of options) {
                    if (opt.text.toLowerCase().includes(targetText.toLowerCase())) {
                        sel.value = opt.value;
                        sel.dispatchEvent(new Event('change', { bubbles: true }));
                        return opt.text;
                    }
                }
                return null;
            }""",
                "Magic",
            )
            if selected:
                logger.debug("Set product line to '%s'", selected)
                # Wait for Set dropdown to repopulate in response to category change
                try:
                    await self._page.wait_for_function(
                        "() => document.querySelector('#SetNameId') && "
                        "document.querySelector('#SetNameId').options.length > 1",
                        timeout=15000,
                    )
                except Exception:
                    logger.debug("SetNameId did not repopulate after category change")
            else:
                logger.debug("Could not find Magic product line in dropdown")
        except Exception:
            logger.debug("Could not set product line filter", exc_info=True)

        # Set the Set filter using JS. Matches are made on a normalized form
        # (lowercase, punctuation stripped, whitespace collapsed) because
        # Mythic Tools names don't always match TCG's exactly — e.g. TCG has
        # "Secrets of Strixhaven: Mystical Archive" vs Mythic "Secrets of
        # Strixhaven Mystical Archive".
        if not apply_set_filter:
            logger.debug("Skipping set filter (caller requested name-only search)")
        try:
            if not apply_set_filter:
                # Leftover dropdown state from a previous search would
                # silently re-restrict the retry to the wrong set.
                await self._page.evaluate(
                    """() => {
                        const sel = document.querySelector('#SetNameId');
                        if (!sel || sel.options.length === 0) return;
                        sel.value = sel.options[0].value;
                        sel.dispatchEvent(new Event('change', { bubbles: true }));
                    }"""
                )
                selected_set = None
            else:
                selected_set = await self._page.evaluate(
                    """(targetSet) => {
                    const sel = document.querySelector('#SetNameId');
                    if (!sel) return null;
                    const options = Array.from(sel.options);
                    const norm = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
                    const target = norm(targetSet);
                    const targetWords = new Set(target.split(' ').filter(w => w.length >= 3));

                    // 1. Exact (normalized) match
                    for (const opt of options) {
                        if (norm(opt.text) === target) {
                            sel.value = opt.value;
                            sel.dispatchEvent(new Event('change', { bubbles: true }));
                            return opt.text;
                        }
                    }
                    // 2. Substring match (either direction) on normalized form
                    for (const opt of options) {
                        const o = norm(opt.text);
                        if (o.includes(target) || target.includes(o)) {
                            sel.value = opt.value;
                            sel.dispatchEvent(new Event('change', { bubbles: true }));
                            return opt.text;
                        }
                    }
                    // 3. Word-overlap fallback: 2+ significant shared words
                    let best = null, bestScore = 0;
                    for (const opt of options) {
                        const words = new Set(norm(opt.text).split(' ').filter(w => w.length >= 3));
                        let common = 0;
                        for (const w of targetWords) if (words.has(w)) common++;
                        if (common > bestScore && common >= 2) {
                            bestScore = common;
                            best = opt;
                        }
                    }
                    if (best) {
                        sel.value = best.value;
                        sel.dispatchEvent(new Event('change', { bubbles: true }));
                        return best.text;
                    }
                    return null;
                }""",
                    card.set_name,
                )
            if selected_set:
                logger.debug("Set filter to '%s'", selected_set)
                try:
                    await self._page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
            else:
                logger.debug("Could not find '%s' in set dropdown", card.set_name)
        except Exception:
            logger.debug("Could not set filter for '%s'", card.set_name, exc_info=True)

        # Fill the search box — for double-faced cards, use only the front face name
        search_name = card.card_name.split("//")[0].strip()

        # Ensure any ajax from the set-filter change has fully settled before we
        # fill, otherwise Knockout can clobber the input value we just typed.
        try:
            await self._page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

        # Set the value via JS and dispatch the events that Knockout listens for
        # (input + change + keyup). Playwright's fill() alone has been observed
        # to lose the value to Knockout's own re-binding during ajax churn.
        await self._page.evaluate(
            """(val) => {
                const el = document.querySelector('#SearchValue');
                if (!el) return;
                el.focus();
                el.value = val;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: 'a' }));
            }""",
            search_name,
        )

        # Verify value stuck, retry up to 3× if Knockout clobbered it.
        actual = ""
        for _ in range(3):
            actual = await self._page.evaluate(
                "() => (document.querySelector('#SearchValue') || {}).value"
            )
            if actual == search_name:
                break
            logger.debug("Search input value is '%s' — re-setting", actual)
            await self._page.evaluate(
                """(val) => {
                    const el = document.querySelector('#SearchValue');
                    if (!el) return;
                    el.value = val;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                search_name,
            )
        if actual != search_name:
            # Submitting with wrong text would return unrelated results. Refuse.
            raise NavigationError(
                f"Search input would not accept '{search_name}' (actual='{actual}')"
            )

        # Click the Search button.
        search_btn = self._page.locator("input[value='Search'], button:has-text('Search')").first
        await search_btn.click()
        try:
            await self._page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        # Log how many results we got
        try:
            pagination = await self._page.query_selector("text=/Viewing \\d+/")
            if pagination:
                logger.debug("Search results: %s", await pagination.inner_text())
        except Exception:
            pass

    async def find_and_click_add(self, card: MythicCard) -> tuple[bool, int]:
        """Find the matching card in search results and click Add.

        Returns (found, score):
        - (True, 3): exact collector number match — safe to auto-add
        - (True, 2): exact name match — needs manual verification
        - (True, 1): partial name match — needs manual verification
        - (False, 0): not found
        """
        # The search may auto-navigate to a manage page if there's only one match.
        # Give that navigation a chance to settle before deciding what page we're on.
        try:
            await self._page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        current_url = self._page.url
        if "/admin/product/manage" in current_url:
            back_link = await self._page.query_selector("a:has-text('Back to Inventory')")
            if back_link:
                # TCG auto-navigated here because search resolved to a single product —
                # treat as confirmed. The search was already filtered by set + name.
                score = 3
                if card.collector_number:
                    page_text = await self._page.evaluate("() => document.body.innerText")
                    import re

                    num = card.collector_number.lstrip("0") or "0"
                    pattern = re.compile(
                        r"(?:^|[#/\-(\s])0*" + re.escape(num) + r"(?:$|[)\s,])",
                        re.MULTILINE,
                    )
                    if pattern.search(page_text):
                        logger.info(
                            "Confirmed collector # %s on manage page", card.collector_number
                        )
                    else:
                        logger.warning(
                            "Collector # %s for '%s' was NOT found on the manage page — "
                            "scoring 3 anyway because search auto-navigated (single match). "
                            "If TCG ever broadens auto-nav, this could Add the wrong product.",
                            card.collector_number,
                            card.card_name,
                        )

                logger.info("Already on manage page (score %d)", score)
                return True, score

        # Wait for the results table to render. Only result rows contain an
        # Add/Manage control — sidebar filter rows do not. If no such row
        # appears, the search returned zero products (or auto-navigated).
        # Use a custom JS wait because TCG renders the control as either
        # <input value='Add'> or <a>Add</a> depending on product state.
        try:
            await self._page.wait_for_function(
                """() => {
                    const trs = document.querySelectorAll('tr');
                    for (const tr of trs) {
                        const els = tr.querySelectorAll('a, input, button');
                        for (const el of els) {
                            const t = (el.textContent || el.value || '').trim();
                            if (t === 'Add' || t === 'Manage') return true;
                        }
                    }
                    return false;
                }""",
                timeout=10000,
            )
        except Exception:
            logger.debug("No Add/Manage rows appeared — likely zero results")

        # Re-check URL — TCG may have auto-navigated to a manage page while we
        # waited, which means the single-match code path above should apply.
        if "/admin/product/manage" in self._page.url and self._page.url != current_url:
            logger.info("Auto-navigated to manage page after wait — single-result match")
            return True, 3

        # Extract all result rows in a single JS call — avoids stale
        # ElementHandles when the page re-renders between queries. Result
        # rows are identified by having either an Add or Manage control
        # (can be <input>, <a>, or <button> depending on TCG's render).
        row_data = await retry_on_flaky(
            lambda: self._page.evaluate("""() => {
            const hasAction = (tr) => {
                const els = tr.querySelectorAll('a, input, button');
                for (const el of els) {
                    const t = (el.textContent || el.value || '').trim();
                    if (t === 'Add') return 'add';
                    if (t === 'Manage') return 'manage';
                }
                return null;
            };
            const out = [];
            const trs = document.querySelectorAll('tr');
            let idx = 0;
            for (const tr of trs) {
                const action = hasAction(tr);
                if (!action) continue;
                const tds = tr.querySelectorAll('td');
                if (tds.length < 3) continue;
                const name = (tds[2] ? tds[2].innerText : '').trim();
                const setName = (tds[3] ? tds[3].innerText : '').trim();
                const number = (tds[5] ? tds[5].innerText : '').trim();
                out.push({ idx, name, setName, number, hasManage: action === 'manage' });
                idx++;
            }
            return out;
        }"""),
            page=self._page,
            label="find_and_click_add row extract",
        )

        logger.debug("Found %d data rows in search results", len(row_data))

        all_matches: list[dict] = []  # row data dicts with added 'score'
        target_num = card.collector_number.lstrip("0") if card.collector_number else ""
        for r in row_data:
            name = r["name"]
            set_name = r["setName"]
            number = r["number"]
            number_clean = number.lstrip("#").lstrip("0").strip() if number else ""

            logger.debug("Row: name='%s', set='%s', number='%s'", name, set_name, number)

            # Highest-priority check: set + collector # column alone is
            # decisive. Name fuzzy-matching often misses when TCG decorates
            # the name differently than Mythic Tools ("(Borderless)",
            # "(Showcase)", reordered DFC faces), but the Number column is
            # stable. If set matches and #s match, trust it and skip the
            # name-based fallback entirely.
            if (
                target_num
                and number_clean
                and number_clean == target_num
                and self._set_matches(set_name, card)
            ):
                logger.debug("Collector # + set match: '%s' #%s", name, number)
                all_matches.append({**r, "score": 3})
                continue

            matches, score = self._matches_card(name, set_name, card)
            if not (matches and score > 0):
                continue
            all_matches.append({**r, "score": score})

        if all_matches:
            best, best_score = _resolve_best_match(all_matches)
            best_name = best["name"]

            if len(all_matches) > 1 and best_score < 3:
                logger.info(
                    "Multiple matches for '%s' but no collector # match. Matches: %s",
                    card.card_name,
                    [(m["name"], m["score"]) for m in all_matches],
                )

            # Click by re-selecting the row via its position among action
            # rows. A fresh query avoids stale handles after any re-render —
            # but a re-render between the row-extract evaluate above and
            # this click evaluate could still shuffle rows, so re-verify the
            # row's product name matches what we scored before clicking.
            target_action = "Manage" if best["hasManage"] else "Add"
            clicked = await retry_on_flaky(
                lambda: self._page.evaluate(
                    """(args) => {
                        const actionOf = (tr) => {
                            const els = tr.querySelectorAll('a, input, button');
                            for (const el of els) {
                                const t = (el.textContent || el.value || '').trim();
                                if (t === 'Add' || t === 'Manage') return {el, t};
                            }
                            return null;
                        };
                        let counter = 0;
                        for (const tr of document.querySelectorAll('tr')) {
                            const action = actionOf(tr);
                            if (!action) continue;
                            if (counter === args.idx) {
                                if (action.t !== args.target) return false;
                                const tds = tr.querySelectorAll('td');
                                const name = (tds[2] ? tds[2].innerText : '').trim();
                                if (name !== args.name) return false;
                                action.el.click();
                                return true;
                            }
                            counter++;
                        }
                        return false;
                    }""",
                    {"idx": best["idx"], "target": target_action, "name": best_name},
                ),
                page=self._page,
                label=f"click {target_action} row {best['idx']}",
            )
            if clicked:
                action = "Manage" if best["hasManage"] else "Add"
                logger.info("Found '%s' (score %d) — clicked %s", best_name, best_score, action)
                try:
                    await self._page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                return True, best_score
            logger.warning("Could not click button for row %d", best["idx"])

        # Log all rows for debugging when not found
        for r in row_data[:10]:
            logger.debug("  Available: '%s' — '%s'", r["name"], r["setName"])

        logger.warning(
            "Could not find '%s' from '%s' in search results",
            card.card_name,
            card.set_name,
        )
        return False, 0

    def _set_matches(self, tcg_set: str, card: MythicCard) -> bool:
        """Flexible set-name match between Mythic Tools and TCGPlayer.

        Mythic Tools and TCGPlayer name sets differently (e.g., "Teenage
        Mutant Ninja Turtles Eternal" vs "Commander: Teenage Mutant Ninja
        Turtles") so we accept exact, substring (either direction), or 2+
        shared significant-word overlap.
        """
        tcg_set_lower = tcg_set.lower().strip()
        card_set_lower = card.set_name.lower().strip()

        if (
            tcg_set_lower == card_set_lower
            or card_set_lower in tcg_set_lower
            or tcg_set_lower in card_set_lower
        ):
            return True

        tcg_words = {w for w in tcg_set_lower.split() if len(w) >= 3}
        card_words = {w for w in card_set_lower.split() if len(w) >= 3}
        return len(tcg_words & card_words) >= 2

    def _matches_card(self, tcg_name: str, tcg_set: str, card: MythicCard) -> tuple[bool, int]:
        """Check if a TCGPlayer catalog row matches a Mythic Tools card.

        Returns (matches, score) where score indicates match quality:
        - 3: exact collector number match (best)
        - 2: exact name match
        - 1: partial name match
        - 0: no match
        """
        tcg_name_lower = tcg_name.lower().strip()
        card_name_lower = card.card_name.lower().strip()
        front_face = card_name_lower.split("//")[0].strip()

        if not self._set_matches(tcg_set, card):
            return False, 0

        # Best: exact collector number in TCGPlayer name like "(2376)"
        if card.collector_number and f"({card.collector_number})" in tcg_name_lower:
            return True, 3

        # Exact name match
        if tcg_name_lower == card_name_lower or tcg_name_lower == front_face:
            return True, 2

        # Partial name match (card name contained in TCGPlayer name)
        if front_face in tcg_name_lower or card_name_lower in tcg_name_lower:
            return True, 1

        return False, 0

    async def set_price_and_quantity(
        self, card: MythicCard, price_strategy: str = "lowest"
    ) -> bool:
        """On the manage page, set the price and quantity for the right condition row.

        Args:
            card: The card to set price/qty for.
            price_strategy: Which Match button to click:
                - "lowest" (default): TCG Lowest Listing (1st Match button)
                - "market": TCG Market Price (3rd Match button)
                - "last-sold": TCG Last Sold Listing (2nd Match button)
                - "csv": Use the price from the Mythic Tools CSV

        Returns True if successful.
        """
        # Check "If me, show next lowest"
        try:
            show_next = self._page.locator("text=If me, show next lowest")
            if await show_next.count() > 0:
                await show_next.click()
                await self._page.wait_for_timeout(1000)
        except Exception:
            pass

        # Find the pricing table
        pricing_table = None
        all_tables = await self._page.query_selector_all("table")
        for table in all_tables:
            header_text = await table.evaluate("el => el.innerText.substring(0, 200)")
            if "Condition" in header_text and "TCG" in header_text:
                pricing_table = table
                break

        if not pricing_table:
            logger.warning("Pricing table not found on manage page")
            return False

        rows = await pricing_table.query_selector_all("tr")
        target_condition = card.tcg_condition
        if card.is_foil:
            target_condition += " Foil"

        logger.info(
            "Looking for condition '%s', qty %d (strategy=%s)",
            target_condition,
            card.quantity,
            price_strategy,
        )

        # Collect all available conditions for debugging
        available_conditions = []
        for row in rows:
            cond = await row.evaluate("""el => {
                const cells = el.querySelectorAll('td');
                return cells.length > 0 ? cells[0].innerText.trim() : '';
            }""")
            if cond and cond not in ("", "Condition") and "If me" not in cond:
                available_conditions.append(cond)

        logger.debug("Available conditions: %s", available_conditions)

        # Try exact match first, then fallback to partial match
        target_row = None
        for row in rows:
            condition_text = await row.evaluate("""el => {
                const cells = el.querySelectorAll('td');
                return cells.length > 0 ? cells[0].innerText.trim() : '';
            }""")

            if condition_text == target_condition:
                target_row = row
                break

        # Fallback: if "Near Mint" not found but "Near Mint Foil" exists (foil-only product)
        if target_row is None and not card.is_foil:
            foil_target = target_condition + " Foil"
            for row in rows:
                condition_text = await row.evaluate("""el => {
                    const cells = el.querySelectorAll('td');
                    return cells.length > 0 ? cells[0].innerText.trim() : '';
                }""")
                if condition_text == foil_target:
                    logger.info(
                        "Condition '%s' not found, using '%s' instead (foil-only product?)",
                        target_condition,
                        foil_target,
                    )
                    target_row = row
                    break

        if target_row is None:
            logger.warning(
                "Condition '%s' not found in pricing table. Available: %s",
                target_condition,
                available_conditions,
            )
            return False

        row = target_row

        # Set quantity first
        qty_handle = await row.evaluate_handle("""el => {
            const inputs = el.querySelectorAll('input[type="text"], input[type="number"]');
            let found = 0;
            for (const inp of inputs) {
                if (inp.value === 'Match' || inp.type === 'submit') continue;
                found++;
                if (found === 2) return inp;
            }
            return null;
        }""")
        qty_el = qty_handle.as_element()
        if qty_el:
            await self._page.wait_for_timeout(300)
            await qty_el.click(click_count=3)
            await qty_el.fill(str(card.quantity))
            logger.debug("Set quantity to %d", card.quantity)
            await self._page.wait_for_timeout(500)

        # Set price via Match button or CSV price
        # Match button indices: 0 = TCG Lowest, 1 = TCG Last Sold, 2 = TCG Market
        match_index = {"lowest": 0, "last-sold": 1, "market": 2}.get(price_strategy)
        strategy_names = {
            "lowest": "TCG Lowest",
            "last-sold": "TCG Last Sold",
            "market": "TCG Market",
        }

        if match_index is not None:
            # ElementHandle.evaluate_handle passes the element as first JS arg
            match_handle = await row.evaluate_handle(
                """(el, idx) => {
                    const buttons = el.querySelectorAll('input[value="Match"]');
                    return buttons.length > idx ? buttons[idx] : null;
                }""",
                match_index,
            )

            match_el = match_handle.as_element()
            if match_el:
                price_handle = await row.evaluate_handle("""el => {
                    const inputs = el.querySelectorAll('input[type="text"], input[type="number"]');
                    for (const inp of inputs) {
                        if (inp.value !== 'Match' && inp.type !== 'submit') return inp;
                    }
                    return null;
                }""")
                price_el = price_handle.as_element()

                await match_el.click()
                await self._page.wait_for_timeout(500)

                if price_el:
                    new_val = await price_el.input_value()
                    if _is_plausible_price(new_val):
                        logger.info(
                            "Clicked %s Match → price set to $%s",
                            strategy_names.get(price_strategy, price_strategy),
                            new_val,
                        )
                        return True
                    logger.warning(
                        "%s Match click did not produce a plausible price (value='%s') — "
                        "falling back to manual fill",
                        strategy_names.get(price_strategy, price_strategy),
                        new_val,
                    )
                else:
                    logger.warning(
                        "No price input found after clicking %s Match — "
                        "falling back to manual fill",
                        strategy_names.get(price_strategy, price_strategy),
                    )
            else:
                logger.debug(
                    "No %s Match button found, falling back to CSV price",
                    strategy_names.get(price_strategy, price_strategy),
                )

        # Fallback: manually fill CSV price (also reached when the Match
        # click above didn't verifiably set a plausible price).
        price_handle = await row.evaluate_handle("""el => {
            const inputs = el.querySelectorAll('input[type="text"], input[type="number"]');
            for (const inp of inputs) {
                if (inp.value !== 'Match' && inp.type !== 'submit') return inp;
            }
            return null;
        }""")
        price_el = price_handle.as_element()
        if not price_el:
            logger.warning("No price input found for '%s'", target_condition)
            return False

        await price_el.click(click_count=3)
        await price_el.fill(f"{card.effective_price:.2f}")
        confirmed = await price_el.input_value()
        if not _is_plausible_price(confirmed):
            logger.warning(
                "Manual fill did not stick for '%s' (value='%s')", target_condition, confirmed
            )
            return False
        logger.info("Set price to $%.2f (from CSV)", card.effective_price)
        return True

    async def is_already_listed(self, card: MythicCard) -> bool:
        """Check if this card already has quantity > 0 on the manage page."""
        target_condition = card.tcg_condition
        if card.is_foil:
            target_condition += " Foil"

        # Wrapped in retry_on_flaky because the caller often invokes this
        # immediately after clicking Add, which navigates to the manage
        # page — Playwright's networkidle wait can return before the new
        # context is installed, and the evaluate then dies with
        # "Execution context was destroyed".
        result = await retry_on_flaky(
            lambda: self._page.evaluate(
                """(targetCond) => {
            const tables = document.querySelectorAll('table');
            for (const table of tables) {
                const header = table.innerText.substring(0, 200);
                if (!header.includes('Condition') || !header.includes('TCG')) continue;
                const rows = table.querySelectorAll('tr');
                for (const row of rows) {
                    const cells = row.querySelectorAll('td');
                    if (cells.length < 2) continue;
                    if (cells[0].innerText.trim() !== targetCond) continue;
                    // Find quantity input (second text input in the row)
                    const inputs = row.querySelectorAll('input[type="text"], input[type="number"]');
                    let count = 0;
                    for (const inp of inputs) {
                        if (inp.value === 'Match' || inp.type === 'submit') continue;
                        count++;
                        if (count === 2) {
                            const qty = parseInt(inp.value || '0', 10);
                            return qty > 0;
                        }
                    }
                }
            }
            return false;
        }""",
                target_condition,
            ),
            page=self._page,
            label="is_already_listed evaluate",
        )

        return result or False

    async def get_tcg_lowest_price(self, card: MythicCard) -> float | None:
        """Read the TCG Lowest price for the card's condition from the manage page.

        Returns the price as a float, or None if not found.
        """
        target_condition = card.tcg_condition
        if card.is_foil:
            target_condition += " Foil"

        # See is_already_listed: this runs right after click-Add navigation,
        # so the evaluate races the new manage page's context being
        # installed. retry_on_flaky settles the page and re-runs.
        result = await retry_on_flaky(
            lambda: self._page.evaluate(
                """(targetCond) => {
            const tables = document.querySelectorAll('table');
            for (const table of tables) {
                const header = table.innerText.substring(0, 200);
                if (!header.includes('Condition') || !header.includes('TCG')) continue;
                const rows = table.querySelectorAll('tr');
                for (const row of rows) {
                    const cells = row.querySelectorAll('td');
                    if (cells.length < 2) continue;
                    if (cells[0].innerText.trim() !== targetCond) continue;
                    const text = cells[1].innerText.trim();
                    const match = text.match(/\\$([\\d,]+\\.\\d+)/);
                    return match ? parseFloat(match[1].replace(/,/g, '')) : null;
                }
            }
            return null;
        }""",
                target_condition,
            ),
            page=self._page,
            label="get_tcg_lowest_price evaluate",
        )

        return result

    async def save(self) -> None:
        """Click Save on the manage page."""
        save_selectors = [
            "input[value='Save']",
            "a:has-text('Save')",
            "button:has-text('Save')",
        ]
        for selector in save_selectors:
            save_btn = self._page.locator(selector).first
            if await save_btn.count() > 0:
                await save_btn.click()
                await self._page.wait_for_load_state("networkidle")
                await self._page.wait_for_timeout(1000)
                logger.info("Saved")
                return

        raise SelectorNotFoundError("Save button not found")
