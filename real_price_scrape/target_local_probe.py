from __future__ import annotations

import json
import secrets
from pathlib import Path
from urllib.parse import urlencode

import httpx

OUT = Path("target_local_probe_output")
OUT.mkdir(parents=True, exist_ok=True)

BASE = "https://cdui-orchestrations.target.com/cdui_orchestrations/v1/pages/slp"
KEY = "9f36aeafbe60771e321a7cc95a78140772ab3e96"
STORE_ID = "2187"
ZIP = "78249"
LAT = "29.5850"
LON = "-98.6210"
VISITOR = secrets.token_hex(16).upper()

params = {
    "key": KEY,
    "platform": "WEB",
    "privacy_do_not_sell": "false",
    "targeted_advertising_opt_out": "false",
    "device_type": "desktop",
    "sapphire_channel": "WEB",
    "sapphire_page": "/s/milk",
    "channel": "WEB",
    "page": "/s/milk",
    "visitor_id": VISITOR,
    "purchasable_store_ids": STORE_ID,
    "latitude": LAT,
    "longitude": LON,
    "scheduled_delivery_store_id": STORE_ID,
    "scheduled_delivery_zip_code": ZIP,
    "state": "TX",
    "store_id": STORE_ID,
    "zip": ZIP,
    "country": "US",
    "has_pending_inputs": "false",
    "count": "24",
    "default_purchasability_filter": "true",
    "include_sponsored": "true",
    "new_search": "true",
    "offset": "0",
    "spellcheck": "true",
    "store_ids": STORE_ID,
    "keyword": "milk",
    "is_seo_bot": "false",
    "include_data_source_modules": "true",
    "query_string": "searchTerm=milk&storeId=2187",
    "timezone": "America/Chicago",
}
headers = {
    "accept": "application/json",
    "accept-language": "en-US,en;q=0.9",
    "referer": "https://www.target.com/s?searchTerm=milk&storeId=2187",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
}

with httpx.Client(headers=headers, follow_redirects=True, timeout=60) as client:
    response = client.get(BASE, params=params)

(OUT / "response.json").write_bytes(response.content)
(OUT / "request_url.txt").write_text(str(response.request.url), encoding="utf-8")

summary = {
    "status": response.status_code,
    "content_type": response.headers.get("content-type"),
    "bytes": len(response.content),
    "request_url": str(response.request.url),
    "visitor_id": VISITOR,
}

if response.status_code == 200:
    data = response.json()
    products = []
    for module in data.get("data_source_modules", []):
        candidate = (((module.get("module_data") or {}).get("search_response") or {}).get("products") or [])
        if candidate:
            products.extend(candidate)
    rows = []
    for product in products:
        price = product.get("price") or {}
        item = product.get("item") or {}
        desc = item.get("product_description") or {}
        fulfillment = product.get("fulfillment") or {}
        store_options = fulfillment.get("store_options") or []
        rows.append({
            "tcin": product.get("tcin"),
            "title": desc.get("title"),
            "price": price.get("current_retail"),
            "price_location_id": price.get("location_id"),
            "store_option_location_ids": [str(x.get("location_id")) for x in store_options],
            "store_names": [((x.get("store") or {}).get("location_name")) for x in store_options],
        })
    summary.update({
        "products": len(rows),
        "priced_products": sum(r["price"] is not None for r in rows),
        "price_location_ids": sorted({str(r["price_location_id"]) for r in rows if r["price_location_id"] is not None}),
        "store_option_location_ids": sorted({x for r in rows for x in r["store_option_location_ids"]}),
        "store_names": sorted({x for r in rows for x in r["store_names"] if x}),
        "sample": rows[:8],
    })
    (OUT / "parsed_rows.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")

(OUT / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
print(json.dumps(summary, indent=2), flush=True)

if response.status_code != 200:
    raise SystemExit(2)
if summary.get("priced_products", 0) < 10:
    raise SystemExit("too few priced products")
if summary.get("price_location_ids") != [STORE_ID]:
    raise SystemExit(f"wrong price location IDs: {summary.get('price_location_ids')}")
