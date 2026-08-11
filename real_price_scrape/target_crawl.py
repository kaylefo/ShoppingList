from __future__ import annotations

import asyncio
import csv
import hashlib
import html
import json
import random
import re
import secrets
import shutil
import sqlite3
import sys
import time
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode

import httpx

from target_queries import QUERIES

CAPTURE_DATE = "2026-08-11"
BASE_ENDPOINT = "https://cdui-orchestrations.target.com/cdui_orchestrations/v1/pages/slp"
API_KEY = "9f36aeafbe60771e321a7cc95a78140772ab3e96"
OUT_ROOT = Path("real_food_price_output")
PACKAGE_NAME = f"utsa_madera_real_food_prices_{CAPTURE_DATE}"
PACKAGE_DIR = OUT_ROOT / PACKAGE_NAME
RAW_DIR = PACKAGE_DIR / "raw_evidence_samples"
ZIP_PATH = OUT_ROOT / f"{PACKAGE_NAME}.zip"

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
        "target_exact_rows": 12000,
        "max_requests": 1500,
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
        "target_exact_rows": 7000,
        "max_requests": 1000,
    },
]

CONCURRENCY = 8
BATCH_SIZE = 32
MAX_RETRIES = 6
RAW_SAMPLE_LIMIT_PER_STORE = 24
MIN_REQUIRED_UNIQUE_PRODUCTS = 3000
PREFERRED_UNIQUE_PRODUCTS = 10000

BANNED_TITLE_PATTERNS = re.compile(
    r"\b(?:dog food|cat food|pet food|bird food|fish food|dog treats|cat treats|pet treats|"
    r"body wash|shampoo|conditioner|lotion|skin care|skincare|face mask|lip balm|"
    r"candle|air freshener|cleaner|detergent|dish soap|trash bags|paper towels|"
    r"toy|shirt|dress|shoe|notebook|pencil|marker|vitamin supplement|dietary supplement)\b",
    re.IGNORECASE,
)

PACKAGE_SIZE_RE = re.compile(
    r"(?P<size>(?:\d+(?:\.\d+)?\s*(?:fl\.?\s*oz|oz|lb|lbs|g|kg|ml|l|gal|qt|pt)|"
    r"\d+\s*(?:ct|count|pk|pack)|\d+(?:\.\d+)?\s*%))\b",
    re.IGNORECASE,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    return re.sub(r"\s+", " ", text).strip()


def parse_money(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        amount = float(value)
        return amount if amount > 0 else None
    if not value:
        return None
    match = re.search(r"\$?\s*(\d+(?:\.\d{1,2})?)", str(value).replace(",", ""))
    if not match:
        return None
    amount = float(match.group(1))
    return amount if amount > 0 else None


def package_size_from_title(title: str) -> str:
    matches = list(PACKAGE_SIZE_RE.finditer(title))
    return clean_text(matches[-1].group("size")) if matches else ""


def safe_filename(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", value)[:120].strip("_") or "response"


def build_params(store: dict[str, Any], keyword: str, offset: int, visitor_id: str) -> dict[str, str]:
    page_path = f"/s/{keyword.replace(' ', '-')}"
    return {
        "key": API_KEY,
        "platform": "WEB",
        "privacy_do_not_sell": "false",
        "targeted_advertising_opt_out": "false",
        "device_type": "desktop",
        "sapphire_channel": "WEB",
        "sapphire_page": page_path,
        "channel": "WEB",
        "page": page_path,
        "visitor_id": visitor_id,
        "purchasable_store_ids": store["store_id"],
        "latitude": store["latitude"],
        "longitude": store["longitude"],
        "scheduled_delivery_store_id": store["store_id"],
        "scheduled_delivery_zip_code": store["zip"],
        "state": store["state"],
        "store_id": store["store_id"],
        "zip": store["zip"],
        "country": "US",
        "has_pending_inputs": "false",
        "count": "24",
        "default_purchasability_filter": "true",
        "include_sponsored": "true",
        "new_search": "true" if offset == 0 else "false",
        "offset": str(offset),
        "spellcheck": "true",
        "store_ids": store["store_id"],
        "keyword": keyword,
        "is_seo_bot": "false",
        "include_data_source_modules": "true",
        "query_string": urlencode({"searchTerm": keyword, "storeId": store["store_id"], "Nao": offset}),
        "timezone": "America/Chicago",
    }


def extract_search_payload(data: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    products: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {}
    for module in data.get("data_source_modules", []):
        module_data = module.get("module_data") or {}
        search_wrapper = module_data.get("search_response") or {}
        candidate_products = search_wrapper.get("products") or []
        if candidate_products:
            products.extend(x for x in candidate_products if isinstance(x, dict))
        inner = search_wrapper.get("search_response") or {}
        if inner.get("metadata"):
            metadata = inner["metadata"]
    return products, metadata


def local_store_option(product: dict[str, Any], store_id: str) -> dict[str, Any] | None:
    fulfillment = product.get("fulfillment") or {}
    for option in fulfillment.get("store_options") or []:
        if str(option.get("location_id")) == store_id:
            return option
    item_fulfillment = ((product.get("item") or {}).get("fulfillment") or {})
    for option in item_fulfillment.get("store_options") or []:
        if str(option.get("location_id")) == store_id:
            return option
    return None


def product_to_rows(
    product: dict[str, Any],
    store: dict[str, Any],
    query_group: str,
    query: str,
    offset: int,
    source_url: str,
    captured_at: str,
    response_hash: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any]]:
    tcin = clean_text(product.get("tcin"))
    item = product.get("item") or {}
    description = item.get("product_description") or {}
    title = clean_text(description.get("title"))
    price = product.get("price") or {}
    current_price = parse_money(price.get("current_retail") or price.get("formatted_current_price"))
    price_location_id = clean_text(price.get("location_id"))
    option = local_store_option(product, store["store_id"])
    exact = bool(
        tcin
        and title
        and current_price is not None
        and price_location_id == store["store_id"]
        and option is not None
    )

    audit = {
        "retailer": "Target",
        "store_id": store["store_id"],
        "tcin": tcin,
        "title": title,
        "current_price": current_price,
        "price_location_id": price_location_id,
        "has_local_store_option": option is not None,
        "exact_store_price": exact,
        "query_group": query_group,
        "query": query,
        "offset": offset,
        "source_api_url": source_url,
        "captured_at_utc": captured_at,
    }

    if not tcin or not title or current_price is None:
        return None, None, audit
    if BANNED_TITLE_PATTERNS.search(title):
        audit["excluded_reason"] = "non_food_title_filter"
        return None, None, audit

    brand = clean_text((item.get("primary_brand") or {}).get("name"))
    enrichment = item.get("enrichment") or {}
    product_url = clean_text(enrichment.get("buy_url"))
    if product_url.startswith("/"):
        product_url = "https://www.target.com" + product_url
    image_info = enrichment.get("image_info") or {}
    primary_image = image_info.get("primary_image") or {}
    image_url = clean_text(primary_image.get("url"))
    compliance = item.get("compliance") or {}
    classification = item.get("product_classification") or {}
    merchandise = item.get("merchandise_classification") or {}
    category = product.get("category") or {}
    ratings = product.get("ratings_and_reviews") or {}
    fulfillment = product.get("fulfillment") or {}
    shipping = fulfillment.get("shipping_options") or {}
    option = option or {}
    option_store = option.get("store") or {}
    pickup = option.get("order_pickup") or {}
    in_store = option.get("in_store_only") or {}
    ship_to_store = option.get("ship_to_store") or {}
    regular_price = parse_money(
        price.get("formatted_comparison_price")
        or price.get("reg_retail")
        or price.get("formatted_current_price")
    )
    unit_price_text = clean_text(
        f"{price.get('formatted_unit_price') or ''}{price.get('formatted_unit_price_suffix') or ''}"
    )
    wellness = [clean_text(x.get("value_name")) for x in item.get("wellness_merchandise_attributes") or []]
    wellness = sorted({x for x in wellness if x})

    product_row = {
        "retailer": "Target",
        "product_id": tcin,
        "title": title,
        "brand": brand,
        "package_size_text": package_size_from_title(title),
        "product_url": product_url,
        "image_url": image_url,
        "dpci": clean_text(item.get("dpci")),
        "product_type": clean_text(classification.get("product_type")),
        "merch_department_id": clean_text(merchandise.get("department_id")),
        "merch_class_id": clean_text(merchandise.get("class_id")),
        "category_id": clean_text(category.get("category_id")),
        "parent_category_id": clean_text(category.get("parent_category_id")),
        "snap_eligible": bool(compliance.get("is_snap_eligible")),
        "wellness_attributes": "|".join(wellness),
        "rating_average": ratings.get("average"),
        "rating_count": ratings.get("count"),
        "first_seen_utc": captured_at,
        "source_product_url": product_url,
    }

    price_row = {
        "retailer": "Target",
        "store_id": store["store_id"],
        "store_name": store["store_name"],
        "store_address": store["address"],
        "product_id": tcin,
        "title": title,
        "brand": brand,
        "price_usd": current_price,
        "regular_price_usd": regular_price,
        "unit_price_text": unit_price_text,
        "price_location_id": price_location_id,
        "exact_store_price": exact,
        "store_option_location_id": clean_text(option.get("location_id")),
        "store_option_name": clean_text(option_store.get("location_name")),
        "pickup_status": clean_text(pickup.get("availability_status")),
        "in_store_status": clean_text(in_store.get("availability_status")),
        "ship_to_store_status": clean_text(ship_to_store.get("availability_status")),
        "shipping_status": clean_text(shipping.get("availability_status")),
        "sold_out": bool(fulfillment.get("sold_out")),
        "available_locally": (
            clean_text(pickup.get("availability_status")) == "IN_STOCK"
            or clean_text(in_store.get("availability_status")) == "IN_STOCK"
        ),
        "query_group": query_group,
        "discovery_query": query,
        "query_offset": offset,
        "captured_at_utc": captured_at,
        "source_product_url": product_url,
        "source_api_url": source_url,
        "source_response_sha256": response_hash,
        "provenance": (
            "official_retailer_exact_store_context"
            if exact
            else "official_retailer_store_request_other_pricing_location"
        ),
        "modeled_price": False,
    }
    return product_row, price_row, audit


class Collector:
    def __init__(self) -> None:
        self.products: dict[str, dict[str, Any]] = {}
        self.exact_prices: dict[tuple[str, str], dict[str, Any]] = {}
        self.nonexact_prices: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.query_map: set[tuple[str, str, str, str, int]] = set()
        self.requests: list[dict[str, Any]] = []
        self.audit_exclusions: list[dict[str, Any]] = []
        self.query_stats: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.raw_samples: defaultdict[str, int] = defaultdict(int)
        self.started_at = utc_now()

    def exact_count_for_store(self, store_id: str) -> int:
        return sum(1 for (sid, _), _row in self.exact_prices.items() if sid == store_id)

    def unique_exact_products(self) -> int:
        return len({pid for (_sid, pid) in self.exact_prices})

    def consume(
        self,
        store: dict[str, Any],
        query_group: str,
        query: str,
        offset: int,
        source_url: str,
        captured_at: str,
        response_hash: str,
        products: list[dict[str, Any]],
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        before = self.exact_count_for_store(store["store_id"])
        exact_seen_in_response = 0
        parsed_seen = 0
        for product in products:
            product_row, price_row, audit = product_to_rows(
                product,
                store,
                query_group,
                query,
                offset,
                source_url,
                captured_at,
                response_hash,
            )
            if product_row is None or price_row is None:
                if audit.get("excluded_reason"):
                    self.audit_exclusions.append(audit)
                continue
            parsed_seen += 1
            pid = product_row["product_id"]
            if pid not in self.products:
                self.products[pid] = product_row
            self.query_map.add((store["store_id"], pid, query_group, query, offset))
            if price_row["exact_store_price"]:
                exact_seen_in_response += 1
                key = (store["store_id"], pid)
                if key not in self.exact_prices:
                    self.exact_prices[key] = price_row
            else:
                key = (store["store_id"], pid, price_row["price_location_id"])
                if key not in self.nonexact_prices:
                    self.nonexact_prices[key] = price_row
        after = self.exact_count_for_store(store["store_id"])
        result = {
            "new_exact": after - before,
            "exact_in_response": exact_seen_in_response,
            "parsed_in_response": parsed_seen,
            "products_in_response": len(products),
            "total_pages": int(metadata.get("total_pages") or 0),
            "total_results": int(metadata.get("total_results") or 0),
        }
        self.query_stats[(store["store_id"], query, str(offset))] = result
        return result


async def fetch_one(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    store: dict[str, Any],
    visitor_id: str,
    query_group: str,
    query: str,
    offset: int,
) -> dict[str, Any]:
    params = build_params(store, query, offset, visitor_id)
    source_url = BASE_ENDPOINT + "?" + urlencode(params)
    headers = {
        "accept": "application/json",
        "accept-language": "en-US,en;q=0.9",
        "referer": f"https://www.target.com/s?{urlencode({'searchTerm': query, 'storeId': store['store_id'], 'Nao': offset})}",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    }
    last_error = ""
    async with semaphore:
        for attempt in range(1, MAX_RETRIES + 1):
            started = time.monotonic()
            captured_at = utc_now()
            try:
                if attempt > 1:
                    await asyncio.sleep(min(20, 1.5 ** attempt) + random.random())
                response = await client.get(BASE_ENDPOINT, params=params, headers=headers)
                elapsed = round(time.monotonic() - started, 3)
                body = response.content
                digest = hashlib.sha256(body).hexdigest()
                record = {
                    "retailer": "Target",
                    "store_id": store["store_id"],
                    "query_group": query_group,
                    "query": query,
                    "offset": offset,
                    "captured_at_utc": captured_at,
                    "status_code": response.status_code,
                    "content_type": response.headers.get("content-type", ""),
                    "response_bytes": len(body),
                    "response_sha256": digest,
                    "elapsed_seconds": elapsed,
                    "attempt": attempt,
                    "source_api_url": str(response.request.url),
                    "error": "",
                }
                if response.status_code == 200:
                    try:
                        data = response.json()
                        products, metadata = extract_search_payload(data)
                        if products:
                            record["products_in_response"] = len(products)
                            record["total_pages"] = metadata.get("total_pages")
                            record["total_results"] = metadata.get("total_results")
                            return {
                                "ok": True,
                                "record": record,
                                "data": data,
                                "products": products,
                                "metadata": metadata,
                                "captured_at": captured_at,
                                "response_hash": digest,
                                "source_url": str(response.request.url),
                            }
                        last_error = "200 response contained no product list"
                        record["error"] = last_error
                    except Exception as exc:  # malformed response is retained in logs
                        last_error = f"JSON parse/extract error: {exc!r}"
                        record["error"] = last_error
                else:
                    last_error = f"HTTP {response.status_code}"
                    record["error"] = last_error
                if response.status_code not in {408, 409, 425, 429, 500, 502, 503, 504}:
                    return {"ok": False, "record": record, "body": body}
            except Exception as exc:
                elapsed = round(time.monotonic() - started, 3)
                last_error = repr(exc)
                record = {
                    "retailer": "Target",
                    "store_id": store["store_id"],
                    "query_group": query_group,
                    "query": query,
                    "offset": offset,
                    "captured_at_utc": captured_at,
                    "status_code": 0,
                    "content_type": "",
                    "response_bytes": 0,
                    "response_sha256": "",
                    "elapsed_seconds": elapsed,
                    "attempt": attempt,
                    "source_api_url": source_url,
                    "error": last_error,
                }
        return {"ok": False, "record": record, "body": last_error.encode("utf-8")}


def save_raw_sample(
    collector: Collector,
    store: dict[str, Any],
    query_group: str,
    query: str,
    offset: int,
    result: dict[str, Any],
) -> None:
    store_id = store["store_id"]
    if collector.raw_samples[store_id] >= RAW_SAMPLE_LIMIT_PER_STORE:
        return
    if offset != 0:
        return
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"target_{store_id}_{collector.raw_samples[store_id]:03d}_{safe_filename(query_group)}_{safe_filename(query)}.json"
    path = RAW_DIR / filename
    path.write_text(json.dumps(result["data"], ensure_ascii=False), encoding="utf-8")
    collector.raw_samples[store_id] += 1


async def run_request_batch(
    collector: Collector,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    store: dict[str, Any],
    visitor_id: str,
    jobs: list[tuple[str, str, int]],
) -> list[dict[str, Any]]:
    tasks = [
        fetch_one(client, semaphore, store, visitor_id, group, query, offset)
        for group, query, offset in jobs
    ]
    results = await asyncio.gather(*tasks)
    summaries: list[dict[str, Any]] = []
    for (group, query, offset), result in zip(jobs, results):
        record = result["record"]
        collector.requests.append(record)
        if not result.get("ok"):
            body = result.get("body") or b""
            if body and collector.raw_samples[store["store_id"]] < RAW_SAMPLE_LIMIT_PER_STORE:
                RAW_DIR.mkdir(parents=True, exist_ok=True)
                (RAW_DIR / f"error_{store['store_id']}_{safe_filename(query)}_{offset}.bin").write_bytes(body[:2_000_000])
            summaries.append({"group": group, "query": query, "offset": offset, "new_exact": 0, "total_pages": 0})
            continue
        save_raw_sample(collector, store, group, query, offset, result)
        consumed = collector.consume(
            store,
            group,
            query,
            offset,
            result["source_url"],
            result["captured_at"],
            result["response_hash"],
            result["products"],
            result["metadata"],
        )
        summaries.append({"group": group, "query": query, "offset": offset, **consumed})
    return summaries


async def crawl_store(
    collector: Collector,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    store: dict[str, Any],
) -> None:
    visitor_id = secrets.token_hex(16).upper()
    request_count = 0
    first_page_stats: list[dict[str, Any]] = []
    print(
        f"START store={store['store_id']} {store['store_name']} target_exact={store['target_exact_rows']} queries={len(QUERIES)}",
        flush=True,
    )

    # Pass 1: one page per interleaved aisle query.
    for start in range(0, len(QUERIES), BATCH_SIZE):
        if collector.exact_count_for_store(store["store_id"]) >= store["target_exact_rows"]:
            break
        if request_count >= store["max_requests"]:
            break
        slice_queries = QUERIES[start : start + BATCH_SIZE]
        jobs = [(group, query, 0) for group, query in slice_queries]
        remaining = store["max_requests"] - request_count
        jobs = jobs[:remaining]
        stats = await run_request_batch(collector, client, semaphore, store, visitor_id, jobs)
        first_page_stats.extend(stats)
        request_count += len(jobs)
        exact = collector.exact_count_for_store(store["store_id"])
        print(
            f"PROGRESS store={store['store_id']} requests={request_count} exact_rows={exact} unique_exact_products={collector.unique_exact_products()} failures={sum(1 for r in collector.requests if r['store_id']==store['store_id'] and r['error'])}",
            flush=True,
        )

    # Pass 2+: paginate the queries that yielded the most new exact products.
    page_candidates = sorted(
        [s for s in first_page_stats if s.get("total_pages", 0) > 1 and s.get("new_exact", 0) > 0],
        key=lambda x: (x.get("new_exact", 0), x.get("total_pages", 0), x.get("total_results", 0)),
        reverse=True,
    )
    for page_number, offset in [(2, 24), (3, 48), (4, 72), (5, 96)]:
        if collector.exact_count_for_store(store["store_id"]) >= store["target_exact_rows"]:
            break
        eligible = [
            (s["group"], s["query"], offset)
            for s in page_candidates
            if s.get("total_pages", 0) >= page_number
        ]
        for start in range(0, len(eligible), BATCH_SIZE):
            if collector.exact_count_for_store(store["store_id"]) >= store["target_exact_rows"]:
                break
            if request_count >= store["max_requests"]:
                break
            jobs = eligible[start : start + BATCH_SIZE]
            remaining = store["max_requests"] - request_count
            jobs = jobs[:remaining]
            await run_request_batch(collector, client, semaphore, store, visitor_id, jobs)
            request_count += len(jobs)
            exact = collector.exact_count_for_store(store["store_id"])
            print(
                f"PAGINATE store={store['store_id']} page={page_number} requests={request_count} exact_rows={exact} unique_exact_products={collector.unique_exact_products()}",
                flush=True,
            )
        if request_count >= store["max_requests"]:
            break

    print(
        f"DONE store={store['store_id']} requests={request_count} exact_rows={collector.exact_count_for_store(store['store_id'])}",
        flush=True,
    )


def write_csv(path: Path, rows: Iterable[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def sqlite_type(values: list[Any]) -> str:
    non_null = [v for v in values if v not in (None, "")]
    if non_null and all(isinstance(v, bool) for v in non_null):
        return "INTEGER"
    if non_null and all(isinstance(v, int) and not isinstance(v, bool) for v in non_null):
        return "INTEGER"
    if non_null and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in non_null):
        return "REAL"
    return "TEXT"


def create_sqlite(
    path: Path,
    tables: dict[str, list[dict[str, Any]]],
) -> None:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        for table, rows in tables.items():
            if not rows:
                continue
            columns = list(rows[0].keys())
            column_types = {c: sqlite_type([r.get(c) for r in rows[:1000]]) for c in columns}
            defs = ", ".join(f'"{c}" {column_types[c]}' for c in columns)
            conn.execute(f'CREATE TABLE "{table}" ({defs})')
            placeholders = ",".join("?" for _ in columns)
            quoted_columns = ",".join(f'"{c}"' for c in columns)
            values = []
            for row in rows:
                converted = []
                for c in columns:
                    value = row.get(c)
                    if isinstance(value, bool):
                        value = int(value)
                    elif isinstance(value, (dict, list)):
                        value = json.dumps(value, ensure_ascii=False)
                    converted.append(value)
                values.append(converted)
            conn.executemany(
                f'INSERT INTO "{table}" ({quoted_columns}) VALUES ({placeholders})',
                values,
            )
        if tables.get("products"):
            conn.execute('CREATE UNIQUE INDEX idx_products_id ON products(retailer, product_id)')
        if tables.get("price_observations"):
            conn.execute('CREATE UNIQUE INDEX idx_price_store_product ON price_observations(store_id, product_id)')
            conn.execute('CREATE INDEX idx_price_title ON price_observations(title)')
            conn.execute('CREATE INDEX idx_price_store ON price_observations(store_id)')
        conn.commit()
    finally:
        conn.close()


def validate_and_export(collector: Collector) -> dict[str, Any]:
    PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
    exact_prices = sorted(
        collector.exact_prices.values(),
        key=lambda r: (r["store_id"], r["title"].lower(), r["product_id"]),
    )
    nonexact_prices = sorted(
        collector.nonexact_prices.values(),
        key=lambda r: (r["store_id"], r["title"].lower(), r["product_id"]),
    )
    exact_product_ids = {row["product_id"] for row in exact_prices}
    products = sorted(
        [row for pid, row in collector.products.items() if pid in exact_product_ids],
        key=lambda r: (r["title"].lower(), r["product_id"]),
    )
    query_map = [
        {
            "store_id": store_id,
            "product_id": product_id,
            "query_group": group,
            "query": query,
            "offset": offset,
        }
        for store_id, product_id, group, query, offset in sorted(collector.query_map)
        if product_id in exact_product_ids
    ]
    stores_rows = [
        {
            "retailer": s["retailer"],
            "store_id": s["store_id"],
            "store_name": s["store_name"],
            "address": s["address"],
            "source_store_url": (
                "https://www.target.com/sl/la-cantera/2187"
                if s["store_id"] == "2187"
                else "https://www.target.com/sl/san-antonio-west/1785"
            ),
            "requested_zip": s["zip"],
            "requested_latitude": s["latitude"],
            "requested_longitude": s["longitude"],
        }
        for s in STORES
    ]

    validation_errors: list[str] = []
    if len(products) < MIN_REQUIRED_UNIQUE_PRODUCTS:
        validation_errors.append(
            f"Only {len(products)} unique exact-store products; minimum is {MIN_REQUIRED_UNIQUE_PRODUCTS}."
        )
    if not exact_prices:
        validation_errors.append("No exact store price rows were collected.")
    if any(row.get("modeled_price") for row in exact_prices):
        validation_errors.append("A modeled price flag was found in exact price rows.")
    if any(str(row["price_location_id"]) != str(row["store_id"]) for row in exact_prices):
        validation_errors.append("An exact row has a mismatched Target price location ID.")
    if any(not row.get("source_api_url") or not row.get("source_product_url") for row in exact_prices):
        validation_errors.append("An exact row is missing a source URL.")
    if any(not isinstance(row.get("price_usd"), (int, float)) or row["price_usd"] <= 0 for row in exact_prices):
        validation_errors.append("An exact row has an invalid numeric price.")
    if len({(r["store_id"], r["product_id"]) for r in exact_prices}) != len(exact_prices):
        validation_errors.append("Duplicate store/product rows remain after deduplication.")

    product_columns = [
        "retailer", "product_id", "title", "brand", "package_size_text", "product_url",
        "image_url", "dpci", "product_type", "merch_department_id", "merch_class_id",
        "category_id", "parent_category_id", "snap_eligible", "wellness_attributes",
        "rating_average", "rating_count", "first_seen_utc", "source_product_url",
    ]
    price_columns = [
        "retailer", "store_id", "store_name", "store_address", "product_id", "title", "brand",
        "price_usd", "regular_price_usd", "unit_price_text", "price_location_id",
        "exact_store_price", "store_option_location_id", "store_option_name", "pickup_status",
        "in_store_status", "ship_to_store_status", "shipping_status", "sold_out",
        "available_locally", "query_group", "discovery_query", "query_offset",
        "captured_at_utc", "source_product_url", "source_api_url", "source_response_sha256",
        "provenance", "modeled_price",
    ]
    request_columns = [
        "retailer", "store_id", "query_group", "query", "offset", "captured_at_utc",
        "status_code", "content_type", "response_bytes", "response_sha256", "elapsed_seconds",
        "attempt", "source_api_url", "products_in_response", "total_pages", "total_results", "error",
    ]

    write_csv(PACKAGE_DIR / "products.csv", products, product_columns)
    write_csv(PACKAGE_DIR / "price_observations.csv", exact_prices, price_columns)
    write_csv(PACKAGE_DIR / "excluded_nonexact_official_prices.csv", nonexact_prices, price_columns)
    write_csv(PACKAGE_DIR / "stores.csv", stores_rows, list(stores_rows[0].keys()))
    write_csv(PACKAGE_DIR / "query_product_map.csv", query_map, ["store_id", "product_id", "query_group", "query", "offset"])
    write_csv(PACKAGE_DIR / "source_requests.csv", collector.requests, request_columns)
    if collector.audit_exclusions:
        audit_cols = sorted({k for row in collector.audit_exclusions for k in row})
        write_csv(PACKAGE_DIR / "excluded_nonfood_rows.csv", collector.audit_exclusions, audit_cols)
    write_jsonl(PACKAGE_DIR / "products.jsonl", products)
    write_jsonl(PACKAGE_DIR / "price_observations.jsonl", exact_prices)

    store_counts = {
        store["store_id"]: {
            "store_name": store["store_name"],
            "exact_price_rows": sum(r["store_id"] == store["store_id"] for r in exact_prices),
            "nonexact_official_rows_excluded": sum(r["store_id"] == store["store_id"] for r in nonexact_prices),
            "successful_requests": sum(r["store_id"] == store["store_id"] and not r["error"] for r in collector.requests),
            "failed_requests": sum(r["store_id"] == store["store_id"] and bool(r["error"]) for r in collector.requests),
        }
        for store in STORES
    }
    captured_times = [r["captured_at_utc"] for r in exact_prices]
    manifest = {
        "dataset_name": PACKAGE_NAME,
        "created_at_utc": utc_now(),
        "capture_started_at_utc": collector.started_at,
        "capture_first_price_utc": min(captured_times) if captured_times else None,
        "capture_last_price_utc": max(captured_times) if captured_times else None,
        "origin_address": "8102 W Hausman Rd, San Antonio, TX 78249",
        "unique_exact_store_products": len(products),
        "exact_store_price_observations": len(exact_prices),
        "official_nonexact_price_rows_excluded_from_primary_table": len(nonexact_prices),
        "modeled_or_generated_price_rows": 0,
        "retailers": ["Target"],
        "stores": store_counts,
        "preferred_10000_product_goal_met": len(products) >= PREFERRED_UNIQUE_PRODUCTS,
        "minimum_3000_product_requirement_met": len(products) >= MIN_REQUIRED_UNIQUE_PRODUCTS,
        "validation_passed": not validation_errors,
        "validation_errors": validation_errors,
        "exact_row_rule": (
            "price_usd is numeric; Target price.location_id equals requested store_id; "
            "the same requested store appears in fulfillment.store_options; title, TCIN, "
            "capture timestamp, product URL, API URL, and response SHA-256 are present"
        ),
        "price_caveat": (
            "These are Target web/pickup/store-context prices captured at the stated timestamps. "
            "Retail prices can change after capture and may differ from a physical shelf tag, delivery price, tax, or promotion eligibility."
        ),
    }
    (PACKAGE_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    schema = {
        "products.csv": {column: "canonical product field" for column in product_columns},
        "price_observations.csv": {column: "exact store-context price observation field" for column in price_columns},
        "source_requests.csv": {column: "source request audit field" for column in request_columns},
    }
    (PACKAGE_DIR / "schema.json").write_text(json.dumps(schema, indent=2), encoding="utf-8")

    readme = f"""# UTSA / Madera real local food-price corpus\n\nCapture date: {CAPTURE_DATE}\nOrigin: 8102 W Hausman Rd, San Antonio, TX 78249\n\n## What is in the primary tables\n\n- `{len(products):,}` unique grocery/food products with an exact local Target price at one or more requested stores.\n- `{len(exact_prices):,}` exact store/product price observations.\n- `0` modeled, inferred, generated, interpolated, or chain-multiplied prices.\n- Every primary price row contains the Target product ID, requested store ID, Target pricing location ID, current numeric price, product URL, API URL, capture timestamp, fulfillment state, and source-response SHA-256.\n\n## Exact-store rule\n\nA row enters `price_observations.csv` only when the official Target response contains a numeric price whose `price.location_id` equals the requested store and the same store is present in the product's local fulfillment options. Any official response using another pricing location is kept only in `excluded_nonexact_official_prices.csv`.\n\n## Files\n\n- `products.csv` / `products.jsonl`: canonical products.\n- `price_observations.csv` / `.jsonl`: exact local store prices only.\n- `excluded_nonexact_official_prices.csv`: real official responses that failed the exact-store-ID rule.\n- `query_product_map.csv`: discovery terms and aisle groups.\n- `source_requests.csv`: request URLs, timestamps, response sizes, hashes, and failures.\n- `stores.csv`: requested local stores.\n- `food_prices.sqlite`: query-ready SQLite database.\n- `raw_evidence_samples/`: representative unmodified official JSON responses.\n- `manifest.json`: counts, validation, and provenance.\n\n## Price semantics\n\nPrices are current Target web/pickup/store-context observations at capture time, not promises of a permanent price. Shelf tags, delivery pricing, taxes, deposits, Target Circle offers, weighted produce totals, and later price changes can differ.\n"""
    (PACKAGE_DIR / "README.md").write_text(readme, encoding="utf-8")

    shutil.copy2(Path(__file__), PACKAGE_DIR / "target_crawl.py")
    shutil.copy2(Path(__file__).with_name("target_queries.py"), PACKAGE_DIR / "target_queries.py")

    create_sqlite(
        PACKAGE_DIR / "food_prices.sqlite",
        {
            "stores": stores_rows,
            "products": products,
            "price_observations": exact_prices,
            "excluded_nonexact_prices": nonexact_prices,
            "query_product_map": query_map,
            "source_requests": collector.requests,
        },
    )

    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(PACKAGE_DIR.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(OUT_ROOT))

    print("FINAL_MANIFEST " + json.dumps(manifest, separators=(",", ":")), flush=True)
    print(f"ZIP_PATH {ZIP_PATH} bytes={ZIP_PATH.stat().st_size}", flush=True)
    if validation_errors:
        raise RuntimeError("; ".join(validation_errors))
    return manifest


async def main() -> None:
    if OUT_ROOT.exists():
        shutil.rmtree(OUT_ROOT)
    PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    collector = Collector()
    semaphore = asyncio.Semaphore(CONCURRENCY)
    limits = httpx.Limits(max_connections=CONCURRENCY + 4, max_keepalive_connections=CONCURRENCY)
    timeout = httpx.Timeout(60.0, connect=30.0)
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, limits=limits) as client:
        for store in STORES:
            await crawl_store(collector, client, semaphore, store)
    validate_and_export(collector)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
