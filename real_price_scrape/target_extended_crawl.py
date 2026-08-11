from __future__ import annotations

import asyncio
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import httpx

import target_crawl as tc
from target_queries import QUERIES

PACKAGE_NAME = f"utsa_madera_real_food_prices_extended_{tc.CAPTURE_DATE}"
tc.PACKAGE_NAME = PACKAGE_NAME
tc.PACKAGE_DIR = tc.OUT_ROOT / PACKAGE_NAME
tc.RAW_DIR = tc.PACKAGE_DIR / "raw_evidence_samples"
tc.ZIP_PATH = tc.OUT_ROOT / f"{PACKAGE_NAME}.zip"
tc.PREFERRED_UNIQUE_PRODUCTS = 10_000

# Target merchandising departments observed in the official product payloads.
# These are grocery, produce, meat/seafood, dairy, bakery, pantry, snacks,
# beverages, frozen food, baby food, candy, and cooking-supply departments.
ALLOWED_DEPARTMENTS = {
    "7", "55", "71", "94", "203", "210", "211", "212", "213", "216",
    "218", "224", "226", "231", "253", "261", "266", "267", "268",
    "270", "271", "284", "288",
}

# Department 94 mixes edible sports nutrition with pills/remedies. Keep actual
# foods, powders, drinks, bars, chips, shakes and hydration mixes; reject medicine.
SUPPLEMENT_MEDICINE = re.compile(
    r"\b(?:multivitamin|vitamin\s+[a-k]\b|fish oil supplements?|supplements?|"
    r"capsules?|tablets?|softgels?|cold remedy|homeopathic|antacid|pain relief|"
    r"sleep aid|laxative|probiotic gummies|mushroom gummies|shilajit|medicine|"
    r"medication|cough drops?|lozenges?|fiber gummies|iron chewable|zinc remedy)\b",
    re.I,
)
EDIBLE_NUTRITION = re.compile(
    r"\b(?:protein (?:powder|shake|bar|chips?|drink|smoothie)|nutrition(?:al)? shake|"
    r"meal replacement|sports drink|electrolyte(?:s| drink| powder| mix)?|hydration|"
    r"collagen (?:powder|peptides|drink|tea|latte)|greens powder|pre[- ]workout powder|"
    r"energy drink|protein balls?|protein donuts?|protein cereal|protein oatmeal|"
    r"protein pancake|whey protein|plant[- ]based protein|casein protein)\b",
    re.I,
)
COOKING_REQUISITE = re.compile(
    r"\b(?:aluminum foil|aluminium foil|parchment paper|wax paper|plastic wrap|"
    r"cling wrap|press.n seal|food wrap|storage bags?|sandwich bags?|freezer bags?|"
    r"food storage|oven bags?|roasting bags?|baking cups?|cupcake liners?|"
    r"foil pans?|pasta pan|baking pan|reheat pan)\b",
    re.I,
)

_base_product_to_rows = tc.product_to_rows


def food_filtered_product_to_rows(*args: Any, **kwargs: Any):
    product_row, price_row, audit = _base_product_to_rows(*args, **kwargs)
    if product_row is None or price_row is None:
        return product_row, price_row, audit
    department = str(product_row.get("merch_department_id") or "")
    title = str(product_row.get("title") or "")
    if department not in ALLOWED_DEPARTMENTS:
        audit["excluded_reason"] = "nonfood_merchandising_department"
        audit["merch_department_id"] = department
        return None, None, audit
    if department == "94" and SUPPLEMENT_MEDICINE.search(title) and not EDIBLE_NUTRITION.search(title):
        audit["excluded_reason"] = "nonfood_supplement_or_medicine"
        audit["merch_department_id"] = department
        return None, None, audit
    if department == "253" and not COOKING_REQUISITE.search(title):
        audit["excluded_reason"] = "nonfood_household_item_in_cooking_supply_department"
        audit["merch_department_id"] = department
        return None, None, audit
    product_row["catalog_scope"] = "food_beverage_or_cooking_requisite"
    price_row["catalog_scope"] = "food_beverage_or_cooking_requisite"
    price_row["age_restricted_alcohol_candidate"] = bool(
        department == "213"
        and re.search(r"\b(?:wine|beer|vodka|whiskey|bourbon|tequila|rum|gin|liqueur|cocktail)\b", title, re.I)
        and not re.search(r"\bnon[- ]alcoholic\b", title, re.I)
    )
    return product_row, price_row, audit


tc.product_to_rows = food_filtered_product_to_rows

STORES = [
    {
        "retailer": "Target",
        "store_id": "2187",
        "store_name": "Target La Cantera",
        "address": "17502 La Cantera Pkwy, San Antonio, TX 78257-8202",
        "zip": "78249",
        "state": "TX",
        "latitude": "29.5850",
        "longitude": "-98.6210",
        "target_exact_rows": 15_000,
        "max_requests": 4_000,
    },
    {
        "retailer": "Target",
        "store_id": "1785",
        "store_name": "Target San Antonio West",
        "address": "11311 Bandera Rd, San Antonio, TX 78250-6812",
        "zip": "78249",
        "state": "TX",
        "latitude": "29.5500",
        "longitude": "-98.6670",
        "target_exact_rows": 9_000,
        "max_requests": 1_800,
    },
]
tc.STORES = STORES


async def crawl_store_extended(
    collector: tc.Collector,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    store: dict[str, Any],
) -> None:
    import secrets

    visitor_id = secrets.token_hex(16).upper()
    request_count = 0
    first_page_stats: list[dict[str, Any]] = []
    print(
        f"START_EXTENDED store={store['store_id']} target_exact={store['target_exact_rows']} "
        f"max_requests={store['max_requests']} queries={len(QUERIES)}",
        flush=True,
    )

    for start in range(0, len(QUERIES), tc.BATCH_SIZE):
        if collector.exact_count_for_store(store["store_id"]) >= store["target_exact_rows"]:
            break
        if request_count >= store["max_requests"]:
            break
        jobs = [(group, query, 0) for group, query in QUERIES[start : start + tc.BATCH_SIZE]]
        jobs = jobs[: store["max_requests"] - request_count]
        stats = await tc.run_request_batch(collector, client, semaphore, store, visitor_id, jobs)
        first_page_stats.extend(stats)
        request_count += len(jobs)
        print(
            f"EXTENDED_PASS1 store={store['store_id']} requests={request_count} "
            f"exact={collector.exact_count_for_store(store['store_id'])} "
            f"unique={collector.unique_exact_products()}",
            flush=True,
        )

    candidates = sorted(
        [s for s in first_page_stats if s.get("total_pages", 0) > 1 and s.get("new_exact", 0) > 0],
        key=lambda s: (s.get("new_exact", 0), s.get("total_pages", 0), s.get("total_results", 0)),
        reverse=True,
    )
    stale_rounds = 0
    for page_number in range(2, 16):
        if collector.exact_count_for_store(store["store_id"]) >= store["target_exact_rows"]:
            break
        if request_count >= store["max_requests"]:
            break
        offset = 24 * (page_number - 1)
        eligible = [
            (s["group"], s["query"], offset)
            for s in candidates
            if s.get("total_pages", 0) >= page_number
        ]
        page_new = 0
        for start in range(0, len(eligible), tc.BATCH_SIZE):
            if collector.exact_count_for_store(store["store_id"]) >= store["target_exact_rows"]:
                break
            if request_count >= store["max_requests"]:
                break
            jobs = eligible[start : start + tc.BATCH_SIZE]
            jobs = jobs[: store["max_requests"] - request_count]
            stats = await tc.run_request_batch(collector, client, semaphore, store, visitor_id, jobs)
            page_new += sum(s.get("new_exact", 0) for s in stats)
            request_count += len(jobs)
            print(
                f"EXTENDED_PAGE store={store['store_id']} page={page_number} requests={request_count} "
                f"page_new={page_new} exact={collector.exact_count_for_store(store['store_id'])} "
                f"unique={collector.unique_exact_products()}",
                flush=True,
            )
        stale_rounds = stale_rounds + 1 if page_new == 0 else 0
        if stale_rounds >= 2:
            break

    print(
        f"DONE_EXTENDED store={store['store_id']} requests={request_count} "
        f"exact={collector.exact_count_for_store(store['store_id'])} unique={collector.unique_exact_products()}",
        flush=True,
    )


async def main() -> None:
    if tc.OUT_ROOT.exists():
        shutil.rmtree(tc.OUT_ROOT)
    tc.PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
    tc.RAW_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(__file__), tc.PACKAGE_DIR / "target_extended_crawl.py")
    collector = tc.Collector()
    semaphore = asyncio.Semaphore(tc.CONCURRENCY)
    limits = httpx.Limits(max_connections=tc.CONCURRENCY + 4, max_keepalive_connections=tc.CONCURRENCY)
    timeout = httpx.Timeout(60.0, connect=30.0)
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, limits=limits) as client:
        for store in STORES:
            await crawl_store_extended(collector, client, semaphore, store)
    tc.validate_and_export(collector)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
