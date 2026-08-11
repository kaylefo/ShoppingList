from __future__ import annotations

import hashlib
import html
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

OUT = Path("walmart_nearby_modules_output")
RAW = OUT / "raw_html"
OUT.mkdir(parents=True, exist_ok=True)
RAW.mkdir(parents=True, exist_ok=True)

STORES = [
    {"store_id": "5224", "slug": "san-antonio-tx", "expected_zip": "78249", "label": "De Zavala Neighborhood Market"},
    {"store_id": "2599", "slug": "san-antonio-tx", "expected_zip": "78249", "label": "De Zavala Supercenter"},
    {"store_id": "5146", "slug": "san-antonio-tx", "expected_zip": "78250", "label": "Bandera Supercenter"},
    {"store_id": "2835", "slug": "san-antonio-tx", "expected_zip": "78250", "label": "Guilbeau Neighborhood Market"},
    {"store_id": "5490", "slug": "san-antonio-tx", "expected_zip": "78254", "label": "FM 1560 Neighborhood Market"},
    {"store_id": "5226", "slug": "san-antonio-tx", "expected_zip": "78251", "label": "Military Drive W Neighborhood Market"},
    {"store_id": "3057", "slug": "helotes-tx", "expected_zip": "78023", "label": "Helotes Supercenter"},
    {"store_id": "3107", "slug": "san-antonio-tx", "expected_zip": "78257", "label": "Leon Springs Supercenter"},
    {"store_id": "5290", "slug": "san-antonio-tx", "expected_zip": "78245", "label": "S Ellison Neighborhood Market"},
    {"store_id": "5145", "slug": "san-antonio-tx", "expected_zip": "78251", "label": "Culebra Supercenter"},
]

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def extract_next_data(text: str) -> dict[str, Any] | None:
    match = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', text, re.S | re.I)
    if not match:
        return None
    try:
        return json.loads(html.unescape(match.group(1)))
    except json.JSONDecodeError:
        return None


def numeric_current_price(item: dict[str, Any]) -> float | None:
    value = (((item.get("priceInfo") or {}).get("currentPrice") or {}).get("price"))
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return None


def parse_store(next_data: dict[str, Any], expected_id: str, expected_zip: str) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    page_props = (next_data.get("props") or {}).get("pageProps") or {}
    node = (((page_props.get("initialDataNodeDetails") or {}).get("data") or {}).get("nodeDetail") or {})
    actual_id = str(node.get("id") or "")
    address = node.get("address") or {}
    actual_zip = str(address.get("postalCode") or "")
    if actual_id != expected_id:
        raise ValueError(f"node ID mismatch: requested {expected_id}, embedded {actual_id}")
    if actual_zip != expected_zip:
        raise ValueError(f"ZIP mismatch for {expected_id}: expected {expected_zip}, embedded {actual_zip}")
    rollback = page_props.get("initialDataRollbackItems") or {}
    search_result = ((((rollback.get("data") or {}).get("search") or {}).get("searchResult")) or {})
    title = str(search_result.get("title") or "")
    stacks = search_result.get("itemStacks") or []
    products: list[dict[str, Any]] = []
    for stack in stacks:
        for item in (stack.get("itemsV2") or stack.get("items") or []):
            if isinstance(item, dict) and item.get("__typename") == "Product":
                products.append(item)
    if not products:
        raise ValueError("no Product rows in official local rollback module")
    return node, products, title


def candidate_urls(store: dict[str, str]) -> list[str]:
    base = f"https://www.walmart.com/store/{store['store_id']}-{store['slug']}"
    return [base + "/shopping-services", base]


def fetch_store(store: dict[str, str]) -> dict[str, Any]:
    headers = {
        "user-agent": UA,
        "accept-language": "en-US,en;q=0.9",
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "cache-control": "no-cache",
    }
    attempts: list[dict[str, Any]] = []
    with httpx.Client(headers=headers, follow_redirects=True, timeout=60) as client:
        for url in candidate_urls(store):
            for retry in range(1, 4):
                captured_at = now_utc()
                try:
                    response = client.get(url)
                    body = response.content
                    attempt = {
                        "url": url,
                        "retry": retry,
                        "status": response.status_code,
                        "final_url": str(response.url),
                        "bytes": len(body),
                        "captured_at_utc": captured_at,
                        "content_type": response.headers.get("content-type", ""),
                    }
                    attempts.append(attempt)
                    if response.status_code != 200:
                        time.sleep(retry)
                        continue
                    text = body.decode("utf-8", errors="ignore")
                    data = extract_next_data(text)
                    if not data:
                        attempt["parse_error"] = "missing __NEXT_DATA__"
                        time.sleep(retry)
                        continue
                    try:
                        node, products, module_title = parse_store(
                            data,
                            store["store_id"],
                            store["expected_zip"],
                        )
                    except Exception as exc:
                        attempt["parse_error"] = repr(exc)
                        time.sleep(retry)
                        continue
                    digest = hashlib.sha256(body).hexdigest()
                    html_name = f"walmart_{store['store_id']}.html"
                    json_name = f"walmart_{store['store_id']}_next_data.json"
                    (RAW / html_name).write_bytes(body)
                    (OUT / json_name).write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
                    priced = sum(numeric_current_price(item) is not None for item in products)
                    return {
                        "ok": True,
                        "requested": store,
                        "source_url": str(response.url),
                        "captured_at_utc": captured_at,
                        "source_response_sha256": digest,
                        "raw_html_file": f"raw_html/{html_name}",
                        "next_data_file": json_name,
                        "node": node,
                        "module_title": module_title,
                        "product_rows": products,
                        "product_count": len(products),
                        "numeric_price_count": priced,
                        "attempts": attempts,
                    }
                except Exception as exc:
                    attempts.append({
                        "url": url,
                        "retry": retry,
                        "captured_at_utc": captured_at,
                        "error": repr(exc),
                    })
                    time.sleep(retry)
    return {"ok": False, "requested": store, "attempts": attempts}


results = []
for store in STORES:
    result = fetch_store(store)
    results.append(result)
    printable = {
        "store_id": store["store_id"],
        "label": store["label"],
        "ok": result.get("ok"),
        "source_url": result.get("source_url"),
        "module_title": result.get("module_title"),
        "product_count": result.get("product_count", 0),
        "numeric_price_count": result.get("numeric_price_count", 0),
        "embedded_store_id": str((result.get("node") or {}).get("id") or ""),
        "embedded_zip": str(((result.get("node") or {}).get("address") or {}).get("postalCode") or ""),
    }
    print(json.dumps(printable), flush=True)

successful = [result for result in results if result.get("ok")]
serializable = []
for result in results:
    copy = dict(result)
    copy.pop("product_rows", None)
    serializable.append(copy)
(OUT / "summary.json").write_text(json.dumps(serializable, indent=2), encoding="utf-8")
(OUT / "walmart_nearby_modules.json").write_text(json.dumps(successful, separators=(",", ":")), encoding="utf-8")
manifest = {
    "built_at_utc": now_utc(),
    "requested_stores": len(STORES),
    "validated_stores": len(successful),
    "validated_store_ids": [result["requested"]["store_id"] for result in successful],
    "total_product_module_rows": sum(result["product_count"] for result in successful),
    "total_numeric_price_rows": sum(result["numeric_price_count"] for result in successful),
    "validation_rule": "official Walmart store route; embedded nodeDetail ID and postal code match requested store; local rollback module contains Product rows",
}
(OUT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
print("FINAL", json.dumps(manifest), flush=True)
if len(successful) < 4:
    raise SystemExit(f"Only {len(successful)} nearby stores passed exact embedded-ID validation")
