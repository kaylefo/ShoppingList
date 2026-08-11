from __future__ import annotations

import asyncio
import base64
import html
import json
import re
import urllib.parse
from pathlib import Path
from typing import Any

import httpx
from playwright.async_api import BrowserContext, Page, Response, async_playwright

OUT = Path("sams_location_probe_output")
OUT.mkdir(parents=True, exist_ok=True)
TARGET_CLUB = "6262"
TARGET_ZIP = "78249"
BASE = "https://www.samsclub.com/search"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"

location_obj = {
    "pickupStore": TARGET_CLUB,
    "deliveryStore": TARGET_CLUB,
    "intent": "PICKUP",
    "postalCode": TARGET_ZIP,
    "stateOrProvinceCode": "TX",
    "city": "San Antonio",
    "storeId": TARGET_CLUB,
    "intentStrength": "EXPLICIT",
}
location_json = json.dumps(location_obj, separators=(",", ":"))
location_enc = urllib.parse.quote(location_json, safe="")
location_b64 = base64.b64encode(location_json.encode()).decode()

VARIANTS: list[dict[str, Any]] = [
    {"name": "baseline"},
    {"name": "club_id", "params": {"clubId": TARGET_CLUB}},
    {"name": "club_number", "params": {"clubNumber": TARGET_CLUB}},
    {"name": "store_id", "params": {"storeId": TARGET_CLUB}},
    {"name": "pickup_store", "params": {"pickupStore": TARGET_CLUB}},
    {"name": "all_params", "params": {"clubId": TARGET_CLUB, "clubNumber": TARGET_CLUB, "storeId": TARGET_CLUB, "pickupStore": TARGET_CLUB, "deliveryStore": TARGET_CLUB, "zip": TARGET_ZIP, "postalCode": TARGET_ZIP}},
    {"name": "cookie_assortment", "cookies": {"assortmentStoreId": TARGET_CLUB, "hasLocData": "1"}},
    {"name": "cookie_store_fields", "cookies": {"pickupStore": TARGET_CLUB, "deliveryStore": TARGET_CLUB, "storeId": TARGET_CLUB, "postalCode": TARGET_ZIP, "assortmentStoreId": TARGET_CLUB, "hasLocData": "1"}},
    {"name": "cookie_loc_raw", "cookies": {"locDataV3": location_json, "assortmentStoreId": TARGET_CLUB, "hasLocData": "1"}},
    {"name": "cookie_loc_encoded", "cookies": {"locDataV3": location_enc, "assortmentStoreId": TARGET_CLUB, "hasLocData": "1"}},
    {"name": "cookie_loc_b64", "cookies": {"locDataV3": location_b64, "assortmentStoreId": TARGET_CLUB, "hasLocData": "1"}},
    {"name": "warm_club", "warmup": f"https://www.samsclub.com/club/{TARGET_CLUB}"},
    {"name": "warm_club_search", "warmup": f"https://www.samsclub.com/club/{TARGET_CLUB}", "params": {"clubId": TARGET_CLUB}},
]


def extract_next_data(text: str) -> dict[str, Any] | None:
    match = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', text, re.S | re.I)
    if not match:
        return None
    try:
        return json.loads(html.unescape(match.group(1)))
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
    items: list[dict[str, Any]] = []
    for stack in search.get("itemStacks") or []:
        for item in stack.get("items") or []:
            if isinstance(item, dict) and item.get("__typename") == "Product":
                items.append(item)
    fulfillment_ids: set[str] = set()
    prices = 0
    samples = []
    for item in items:
        price_info = item.get("priceInfo") or {}
        price = item.get("price") or price_info.get("linePrice") or price_info.get("linePriceDisplay")
        if price not in (None, "", 0):
            prices += 1
        product_ids = set()
        for fulfillment in item.get("fulfillmentSummary") or []:
            sid = fulfillment.get("storeId")
            if sid is not None:
                fulfillment_ids.add(str(sid))
                product_ids.add(str(sid))
        if len(samples) < 6:
            samples.append({
                "id": item.get("usItemId") or item.get("id"),
                "name": item.get("name"),
                "price": price,
                "unit_price": price_info.get("unitPrice"),
                "fulfillment_club_ids": sorted(product_ids),
            })
    pickup = str(location.get("pickupStore") or location.get("storeId") or "")
    exact = pickup == TARGET_CLUB and TARGET_CLUB in fulfillment_ids and prices > 0
    return {
        "has_next_data": True,
        "page": data.get("page"),
        "pickup_club": pickup,
        "delivery_club": str(location.get("deliveryStore") or ""),
        "postal_code": str(location.get("postalCode") or ""),
        "city": location.get("city"),
        "state": location.get("stateOrProvinceCode"),
        "intent": location.get("intent"),
        "intent_strength": location.get("intentStrength"),
        "product_items": len(items),
        "priced_items": prices,
        "fulfillment_club_ids": sorted(fulfillment_ids),
        "exact_club_binding": exact,
        "samples": samples,
    }


def cookie_snapshot(client: httpx.Client) -> dict[str, str]:
    return {cookie.name: cookie.value for cookie in client.cookies.jar}


def direct_variant(variant: dict[str, Any]) -> dict[str, Any]:
    headers = {
        "user-agent": UA,
        "accept-language": "en-US,en;q=0.9",
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    }
    cookies = variant.get("cookies") or {}
    params = {"q": "milk", **(variant.get("params") or {})}
    name = variant["name"]
    result: dict[str, Any] = {"name": name, "params": params, "input_cookies": cookies}
    with httpx.Client(headers=headers, cookies=cookies, follow_redirects=True, timeout=50) as client:
        try:
            if variant.get("warmup"):
                warm = client.get(variant["warmup"])
                result.update({
                    "warmup_status": warm.status_code,
                    "warmup_url": str(warm.url),
                    "warmup_cookies": cookie_snapshot(client),
                })
            response = client.get(BASE, params=params)
            body = response.text
            result.update({
                "status": response.status_code,
                "final_url": str(response.url),
                "bytes": len(response.content),
                "response_cookies": cookie_snapshot(client),
            })
            data = extract_next_data(body)
            result.update(summarize(data))
            (OUT / f"direct_{name}.html").write_text(body, encoding="utf-8")
            if data:
                (OUT / f"direct_{name}_next.json").write_text(json.dumps(data), encoding="utf-8")
        except Exception as exc:
            result["error"] = repr(exc)
    print("DIRECT", json.dumps(result, ensure_ascii=False), flush=True)
    return result


async def save_response(response: Response, records: list[dict[str, Any]]) -> None:
    url = response.url
    if not re.search(r"location|club|graphql|orchestration|search|store|postal|zipcode", url, re.I):
        return
    rec = {"url": url, "status": response.status, "content_type": response.headers.get("content-type", ""), "resource_type": response.request.resource_type}
    try:
        body = await response.body()
        rec["bytes"] = len(body)
        if len(body) <= 5_000_000:
            suffix = ".json" if b"json" in rec["content_type"].encode() or body[:1] in (b"{", b"[") else ".bin"
            filename = f"browser_network_{len(records):03d}{suffix}"
            (OUT / filename).write_bytes(body)
            rec["file"] = filename
    except Exception as exc:
        rec["error"] = repr(exc)
    records.append(rec)


async def visible_text(page: Page) -> str:
    try:
        return await page.locator("body").inner_text(timeout=10_000)
    except Exception:
        return ""


async def browser_probe(context: BrowserContext) -> dict[str, Any]:
    page = await context.new_page()
    records: list[dict[str, Any]] = []
    tasks: list[asyncio.Task[Any]] = []
    page.on("response", lambda response: tasks.append(asyncio.create_task(save_response(response, records))))
    result: dict[str, Any] = {}
    try:
        await page.goto("https://www.samsclub.com/search?q=milk", wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(8_000)
        (OUT / "browser_before.html").write_text(await page.content(), encoding="utf-8")
        (OUT / "browser_before.txt").write_text(await visible_text(page), encoding="utf-8")
        await page.screenshot(path=str(OUT / "browser_before.png"), full_page=False)
        before_data = extract_next_data(await page.content())
        result["before"] = summarize(before_data)
        result["before_cookies"] = await context.cookies()

        trigger = page.get_by_text(re.compile(r"How do you want your items", re.I)).first
        if await trigger.count():
            await trigger.click(timeout=15_000)
        else:
            buttons = page.get_by_role("button", name=re.compile(r"How do you want|location|club", re.I))
            if await buttons.count():
                await buttons.first.click(timeout=15_000)
        await page.wait_for_timeout(2_500)
        dialog_text = await visible_text(page)
        (OUT / "browser_dialog_open.txt").write_text(dialog_text, encoding="utf-8")
        (OUT / "browser_dialog_open.html").write_text(await page.content(), encoding="utf-8")
        await page.screenshot(path=str(OUT / "browser_dialog_open.png"), full_page=False)

        inputs = page.locator("input")
        input_meta = []
        for i in range(min(await inputs.count(), 30)):
            el = inputs.nth(i)
            input_meta.append({
                "i": i,
                "placeholder": await el.get_attribute("placeholder"),
                "aria_label": await el.get_attribute("aria-label"),
                "name": await el.get_attribute("name"),
                "type": await el.get_attribute("type"),
                "visible": await el.is_visible(),
            })
        result["input_meta"] = input_meta

        candidate = page.locator('input[placeholder*="ZIP" i], input[aria-label*="ZIP" i], input[name*="zip" i]').filter(visible=True).first
        if not await candidate.count():
            for i in range(await inputs.count()):
                if await inputs.nth(i).is_visible():
                    candidate = inputs.nth(i)
                    break
        await candidate.fill(TARGET_ZIP)
        await page.wait_for_timeout(500)
        search_buttons = page.get_by_role("button", name=re.compile(r"search|submit|find", re.I))
        if await search_buttons.count():
            await search_buttons.last.click(timeout=10_000)
        else:
            await candidate.press("Enter")
        await page.wait_for_timeout(4_000)
        after_zip_text = await visible_text(page)
        (OUT / "browser_after_zip.txt").write_text(after_zip_text, encoding="utf-8")
        (OUT / "browser_after_zip.html").write_text(await page.content(), encoding="utf-8")
        await page.screenshot(path=str(OUT / "browser_after_zip.png"), full_page=False)

        # Prefer a result containing the target club number; otherwise De Zavala text.
        choices = [
            page.get_by_text(re.compile(r"6262", re.I)),
            page.get_by_text(re.compile(r"De Zavala", re.I)),
            page.get_by_text(re.compile(r"San Antonio", re.I)),
        ]
        clicked = False
        for choice in choices:
            if await choice.count():
                for i in range(await choice.count()):
                    el = choice.nth(i)
                    if await el.is_visible():
                        try:
                            await el.click(timeout=10_000)
                            clicked = True
                            break
                        except Exception:
                            pass
            if clicked:
                break
        await page.wait_for_timeout(1_500)
        save_buttons = page.get_by_role("button", name=re.compile(r"set|save|select|make.*club|use.*club|choose", re.I))
        if await save_buttons.count():
            for i in range(await save_buttons.count()):
                el = save_buttons.nth(i)
                if await el.is_visible():
                    try:
                        await el.click(timeout=10_000)
                        clicked = True
                        break
                    except Exception:
                        pass
        result["clicked_choice"] = clicked
        await page.wait_for_timeout(5_000)
        await page.goto("https://www.samsclub.com/search?q=milk", wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(7_000)
        final_html = await page.content()
        (OUT / "browser_final.html").write_text(final_html, encoding="utf-8")
        (OUT / "browser_final.txt").write_text(await visible_text(page), encoding="utf-8")
        await page.screenshot(path=str(OUT / "browser_final.png"), full_page=False)
        result["after"] = summarize(extract_next_data(final_html))
        result["after_cookies"] = await context.cookies()
    except Exception as exc:
        result["error"] = repr(exc)
        try:
            (OUT / "browser_error.html").write_text(await page.content(), encoding="utf-8")
            (OUT / "browser_error.txt").write_text(await visible_text(page), encoding="utf-8")
            await page.screenshot(path=str(OUT / "browser_error.png"), full_page=False)
        except Exception:
            pass
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    (OUT / "browser_network_index.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    (OUT / "browser_summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("BROWSER", json.dumps(result, ensure_ascii=False), flush=True)
    await page.close()
    return result


async def main() -> None:
    direct_results = [direct_variant(v) for v in VARIANTS]
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
        context = await browser.new_context(
            user_agent=UA,
            locale="en-US",
            timezone_id="America/Chicago",
            viewport={"width": 1440, "height": 1100},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        browser_result = await browser_probe(context)
        await context.close()
        await browser.close()
    exact_direct = [r["name"] for r in direct_results if r.get("exact_club_binding")]
    exact_browser = bool((browser_result.get("after") or {}).get("exact_club_binding"))
    summary = {"exact_direct_variants": exact_direct, "exact_browser": exact_browser}
    (OUT / "final_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("FINAL", json.dumps(summary), flush=True)
    if not exact_direct and not exact_browser:
        raise SystemExit("No exact Sam's Club 6262 binding found")


if __name__ == "__main__":
    asyncio.run(main())
