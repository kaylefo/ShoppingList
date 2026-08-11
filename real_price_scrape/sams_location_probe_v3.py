from __future__ import annotations

import asyncio
import html
import json
import re
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page, async_playwright

OUT = Path("sams_location_probe_v3_output")
OUT.mkdir(parents=True, exist_ok=True)
CLUB_ID = "6262"
CLUB_URL = "https://www.samsclub.com/club/6262-san-antonio-tx"
SEARCH_URL = "https://www.samsclub.com/search?q=milk"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"


def next_data(text: str) -> dict[str, Any] | None:
    match = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', text, re.S | re.I)
    if not match:
        return None
    try:
        return json.loads(html.unescape(match.group(1)))
    except Exception:
        return None


def path_get(obj: Any, path: list[Any], default: Any = None) -> Any:
    cur = obj
    try:
        for key in path:
            cur = cur[key]
        return cur
    except (KeyError, IndexError, TypeError):
        return default


def summarize(data: dict[str, Any] | None) -> dict[str, Any]:
    if not data:
        return {"has_next_data": False}
    initial = path_get(data, ["props", "pageProps", "initialData"], {}) or {}
    location = path_get(initial, ["pageMetadata", "location"], {}) or {}
    products: list[dict[str, Any]] = []
    for stack in (initial.get("searchResult") or {}).get("itemStacks") or []:
        for item in stack.get("items") or []:
            if isinstance(item, dict) and item.get("__typename") == "Product":
                products.append(item)
    fulfillment_ids: set[str] = set()
    priced = 0
    samples = []
    for item in products:
        p = item.get("priceInfo") or {}
        price = item.get("price") or p.get("linePrice") or p.get("linePriceDisplay") or p.get("itemPrice")
        if price not in (None, "", 0):
            priced += 1
        item_ids: set[str] = set()
        for f in item.get("fulfillmentSummary") or []:
            sid = f.get("storeId")
            if sid is not None:
                sid = str(sid)
                fulfillment_ids.add(sid)
                item_ids.add(sid)
        if len(samples) < 8:
            samples.append({
                "id": item.get("usItemId") or item.get("id"),
                "name": item.get("name"),
                "price": price,
                "unit_price": p.get("unitPrice"),
                "fulfillment_club_ids": sorted(item_ids),
                "canonical_url": item.get("canonicalUrl"),
            })
    pickup = str(location.get("pickupStore") or location.get("storeId") or "")
    return {
        "pickup_club": pickup,
        "delivery_club": str(location.get("deliveryStore") or ""),
        "postal_code": str(location.get("postalCode") or ""),
        "city": location.get("city"),
        "state": location.get("stateOrProvinceCode"),
        "products": len(products),
        "priced_products": priced,
        "fulfillment_club_ids": sorted(fulfillment_ids),
        "samples": samples,
        "exact_club_binding": pickup == CLUB_ID and CLUB_ID in fulfillment_ids and priced > 0,
    }


async def body_text(page: Page) -> str:
    try:
        return await page.locator("body").inner_text(timeout=15_000)
    except Exception:
        return ""


async def run(context: BrowserContext) -> dict[str, Any]:
    page = await context.new_page()
    result: dict[str, Any] = {}
    try:
        response = await page.goto(CLUB_URL, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(7_000)
        result["club_status"] = response.status if response else None
        result["club_title"] = await page.title()
        result["club_url"] = page.url
        text = await body_text(page)
        result["club_page_verified"] = "6262" in text and "5565 De Zavala" in text
        (OUT / "club_before.html").write_text(await page.content(), encoding="utf-8")
        (OUT / "club_before.txt").write_text(text, encoding="utf-8")
        await page.screenshot(path=str(OUT / "club_before.png"), full_page=False)

        selector = 'button[aria-label="Make this my club"]'
        button = page.locator(selector).first
        await button.wait_for(state="visible", timeout=20_000)
        result["button_count"] = await page.locator(selector).count()
        click_method = ""
        try:
            await button.click(force=True, timeout=10_000)
            click_method = "playwright_force_click"
        except Exception as first_error:
            result["force_click_error"] = repr(first_error)
            await page.evaluate("document.querySelector('button[aria-label=\"Make this my club\"]')?.click()")
            click_method = "dom_click"
        result["click_method"] = click_method
        await page.wait_for_timeout(8_000)
        result["cookies_after_click"] = await context.cookies()
        (OUT / "club_after.html").write_text(await page.content(), encoding="utf-8")
        (OUT / "club_after.txt").write_text(await body_text(page), encoding="utf-8")
        await page.screenshot(path=str(OUT / "club_after.png"), full_page=False)

        api = await context.request.get(
            SEARCH_URL,
            headers={
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "accept-language": "en-US,en;q=0.9",
                "referer": CLUB_URL,
                "user-agent": UA,
            },
            timeout=90_000,
        )
        body = await api.body()
        (OUT / "search_request.html").write_bytes(body)
        data = next_data(body.decode("utf-8", errors="ignore"))
        if data:
            (OUT / "search_request_next.json").write_text(json.dumps(data), encoding="utf-8")
        result["request_status"] = api.status
        result["request_url"] = api.url
        result["request_summary"] = summarize(data)

        if not result["request_summary"].get("exact_club_binding"):
            browser_response = await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=90_000)
            await page.wait_for_timeout(6_000)
            browser_html = await page.content()
            (OUT / "search_browser.html").write_text(browser_html, encoding="utf-8")
            (OUT / "search_browser.txt").write_text(await body_text(page), encoding="utf-8")
            await page.screenshot(path=str(OUT / "search_browser.png"), full_page=False)
            browser_data = next_data(browser_html)
            if browser_data:
                (OUT / "search_browser_next.json").write_text(json.dumps(browser_data), encoding="utf-8")
            result["browser_status"] = browser_response.status if browser_response else None
            result["browser_summary"] = summarize(browser_data)
        else:
            result["browser_summary"] = {"skipped": True}
        result["cookies_final"] = await context.cookies()
    except Exception as exc:
        result["error"] = repr(exc)
        try:
            (OUT / "error.html").write_text(await page.content(), encoding="utf-8")
            (OUT / "error.txt").write_text(await body_text(page), encoding="utf-8")
            await page.screenshot(path=str(OUT / "error.png"), full_page=False)
        except Exception:
            pass
    (OUT / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    await page.close()
    return result


async def main() -> None:
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
        result = await run(context)
        await context.close()
        await browser.close()
    exact = bool((result.get("request_summary") or {}).get("exact_club_binding")) or bool(
        (result.get("browser_summary") or {}).get("exact_club_binding")
    )
    if not exact:
        raise SystemExit("Exact Sam's Club 6262 binding still not proven")


if __name__ == "__main__":
    asyncio.run(main())
