from __future__ import annotations

import base64
import html
import json
import re
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

OUT = Path("walmart_location_probe_output")
OUT.mkdir(parents=True, exist_ok=True)
TARGET_STORE = "5224"
TARGET_ZIP = "78249"
BASE = "https://www.walmart.com/search"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"

location_obj = {
    "pickupStore": TARGET_STORE,
    "deliveryStore": TARGET_STORE,
    "intent": "PICKUP",
    "postalCode": TARGET_ZIP,
    "stateOrProvinceCode": "TX",
    "city": "San Antonio",
    "storeId": TARGET_STORE,
    "intentStrength": "EXPLICIT",
}
location_json = json.dumps(location_obj, separators=(",", ":"))
location_enc = urllib.parse.quote(location_json, safe="")
location_b64 = base64.b64encode(location_json.encode()).decode()
location_b64url = base64.urlsafe_b64encode(location_json.encode()).decode().rstrip("=")

BASE_PARAMS = {
    "q": "milk",
    "facet": "fulfillment_method:In-store",
    "affinityOverride": "store_led",
}

VARIANTS: list[dict[str, Any]] = [
    {"name": "baseline"},
    {"name": "store_id_param", "params": {"storeId": TARGET_STORE}},
    {"name": "stores_param", "params": {"stores": TARGET_STORE}},
    {"name": "pickup_store_param", "params": {"pickupStore": TARGET_STORE}},
    {"name": "delivery_store_param", "params": {"deliveryStore": TARGET_STORE}},
    {"name": "all_store_params", "params": {"storeId": TARGET_STORE, "stores": TARGET_STORE, "pickupStore": TARGET_STORE, "deliveryStore": TARGET_STORE}},
    {"name": "zip_param", "params": {"zip": TARGET_ZIP}},
    {"name": "zipcode_param", "params": {"zipcode": TARGET_ZIP}},
    {"name": "postal_code_param", "params": {"postalCode": TARGET_ZIP}},
    {"name": "all_location_params", "params": {"storeId": TARGET_STORE, "stores": TARGET_STORE, "pickupStore": TARGET_STORE, "deliveryStore": TARGET_STORE, "zip": TARGET_ZIP, "zipcode": TARGET_ZIP, "postalCode": TARGET_ZIP, "stateOrProvinceCode": "TX", "latitude": "29.575", "longitude": "-98.600"}},
    {"name": "headers_store", "headers": {"x-o-store-id": TARGET_STORE, "x-store-id": TARGET_STORE, "wm-store-id": TARGET_STORE, "x-o-zip-code": TARGET_ZIP}},
    {"name": "cookie_locDataV3_raw", "cookies": {"locDataV3": location_json}},
    {"name": "cookie_locDataV3_urlencoded", "cookies": {"locDataV3": location_enc}},
    {"name": "cookie_locDataV3_b64", "cookies": {"locDataV3": location_b64}},
    {"name": "cookie_locDataV3_b64url", "cookies": {"locDataV3": location_b64url}},
    {"name": "cookie_location_data_raw", "cookies": {"location-data": location_json}},
    {"name": "cookie_location_data_encoded", "cookies": {"location-data": location_enc}},
    {"name": "cookie_store_ids", "cookies": {"pickupStore": TARGET_STORE, "deliveryStore": TARGET_STORE, "storeId": TARGET_STORE, "postalCode": TARGET_ZIP}},
    {"name": "warm_store_page", "warmup": "https://www.walmart.com/store/5224-san-antonio-tx"},
    {"name": "warm_store_services", "warmup": "https://www.walmart.com/store/5224-san-antonio-tx/shopping-services"},
    {"name": "warm_store_page_plus_params", "warmup": "https://www.walmart.com/store/5224-san-antonio-tx", "params": {"storeId": TARGET_STORE, "stores": TARGET_STORE, "pickupStore": TARGET_STORE, "zip": TARGET_ZIP}},
    {"name": "store_path_search", "url": "https://www.walmart.com/store/5224-san-antonio-tx/search", "params": {"q": "milk"}},
]


def extract_next_data(text: str) -> dict[str, Any] | None:
    match = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', text, re.S | re.I)
    if not match:
        return None
    raw = html.unescape(match.group(1))
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def get_path(obj: Any, path: list[Any], default: Any = None) -> Any:
    cur = obj
    try:
        for part in path:
            cur = cur[part]
        return cur
    except (KeyError, IndexError, TypeError):
        return default


def summarize(data: dict[str, Any] | None) -> dict[str, Any]:
    if not data:
        return {"has_next_data": False}
    initial = get_path(data, ["props", "pageProps", "initialData"], {}) or {}
    location = get_path(initial, ["pageMetadata", "location"], {}) or {}
    search = initial.get("searchResult") or {}
    stacks = search.get("itemStacks") or []
    items: list[dict[str, Any]] = []
    for stack in stacks:
        for item in stack.get("items") or []:
            if isinstance(item, dict) and item.get("__typename") == "Product":
                items.append(item)
    fulfillment_store_ids: set[str] = set()
    prices = 0
    samples = []
    for item in items:
        pinfo = item.get("priceInfo") or {}
        price = pinfo.get("linePrice") or pinfo.get("linePriceDisplay") or pinfo.get("itemPrice")
        if price not in (None, ""):
            prices += 1
        for summary in item.get("fulfillmentSummary") or []:
            sid = summary.get("storeId")
            if sid is not None:
                fulfillment_store_ids.add(str(sid))
        if len(samples) < 5:
            samples.append({
                "id": item.get("usItemId"),
                "name": item.get("name"),
                "price": price,
                "unit_price": pinfo.get("unitPrice"),
                "fulfillment_store_ids": sorted({str(x.get("storeId")) for x in item.get("fulfillmentSummary") or [] if x.get("storeId") is not None}),
            })
    pickup_store = str(location.get("pickupStore") or location.get("storeId") or "")
    exact = pickup_store == TARGET_STORE and TARGET_STORE in fulfillment_store_ids and prices > 0
    return {
        "has_next_data": True,
        "page": data.get("page"),
        "pickup_store": pickup_store,
        "delivery_store": str(location.get("deliveryStore") or ""),
        "postal_code": str(location.get("postalCode") or ""),
        "city": location.get("city"),
        "state": location.get("stateOrProvinceCode"),
        "intent_strength": location.get("intentStrength"),
        "product_items": len(items),
        "priced_items": prices,
        "fulfillment_store_ids": sorted(fulfillment_store_ids),
        "exact_de_zavala_binding": exact,
        "samples": samples,
    }


def run_variant(variant: dict[str, Any]) -> dict[str, Any]:
    headers = {
        "user-agent": UA,
        "accept-language": "en-US,en;q=0.9",
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    }
    headers.update(variant.get("headers") or {})
    cookies = variant.get("cookies") or {}
    params = dict(BASE_PARAMS)
    params.update(variant.get("params") or {})
    url = variant.get("url") or BASE
    name = variant["name"]
    result: dict[str, Any] = {"name": name, "url": url, "params": params, "cookies": list(cookies), "headers_added": variant.get("headers") or {}}
    with httpx.Client(headers=headers, cookies=cookies, follow_redirects=True, timeout=45) as client:
        try:
            if variant.get("warmup"):
                warm = client.get(variant["warmup"])
                result["warmup_status"] = warm.status_code
                result["warmup_final_url"] = str(warm.url)
                result["warmup_set_cookie_names"] = sorted(client.cookies.keys())
            response = client.get(url, params=params)
            body = response.text
            result.update({
                "status": response.status_code,
                "final_url": str(response.url),
                "bytes": len(response.content),
                "title": (re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S).group(1).strip() if re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S) else ""),
                "response_cookie_names": sorted(client.cookies.keys()),
            })
            data = extract_next_data(body)
            result.update(summarize(data))
            (OUT / f"{name}.html").write_text(body, encoding="utf-8")
            if data:
                (OUT / f"{name}_next_data.json").write_text(json.dumps(data), encoding="utf-8")
        except Exception as exc:
            result["error"] = repr(exc)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


results = [run_variant(variant) for variant in VARIANTS]
(OUT / "summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
exact = [row for row in results if row.get("exact_de_zavala_binding")]
print("EXACT_VARIANTS", json.dumps([row["name"] for row in exact]), flush=True)
if not exact:
    raise SystemExit("No exact Walmart De Zavala store binding found")
