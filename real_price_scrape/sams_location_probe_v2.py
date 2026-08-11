from __future__ import annotations

import asyncio
import html
import json
import re
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page, Response, async_playwright

OUT = Path("sams_location_probe_v2_output")
OUT.mkdir(parents=True, exist_ok=True)
TARGET_CLUB = "6262"
TARGET_ZIP = "78249"
CLUB_URL = "https://www.samsclub.com/club/6262-san-antonio-tx"
SEARCH_URL = "https://www.samsclub.com/search?q=milk"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"


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


def summarize_search(data: dict[str, Any] | None) -> dict[str, Any]:
    if not data:
        return {"has_next_data": False}
    initial = get_path(data, ["props", "pageProps", "initialData"], {}) or {}
    location = get_path(initial, ["pageMetadata", "location"], {}) or {}
    search = initial.get("searchResult") or {}
    products: list[dict[str, Any]] = []
    for stack in search.get("itemStacks") or []:
        for item in stack.get("items") or []:
            if isinstance(item, dict) and item.get("__typename") == "Product":
                products.append(item)
    store_ids: set[str] = set()
    priced = 0
    pickup_eligible = 0
    samples = []
    for item in products:
        price_info = item.get("priceInfo") or {}
        price = item.get("price") or price_info.get("linePrice") or price_info.get("linePriceDisplay")
        if price not in (None, "", 0):
            priced += 1
        item_ids: set[str] = set()
        for fulfillment in item.get("fulfillmentSummary") or []:
            sid = fulfillment.get("storeId")
            if sid is not None:
                store_ids.add(str(sid))
                item_ids.add(str(sid))
            if str(sid) == TARGET_CLUB and fulfillment.get("fulfillment") == "PICKUP":
                pickup_eligible += 1
        if len(samples) < 8:
            samples.append({
                "id": item.get("usItemId") or item.get("id"),
                "name": item.get("name"),
                "price": price,
                "unit_price": price_info.get("unitPrice"),
                "fulfillment_club_ids": sorted(item_ids),
                "canonical_url": item.get("canonicalUrl"),
            })
    pickup_club = str(location.get("pickupStore") or location.get("storeId") or "")
    exact = pickup_club == TARGET_CLUB and TARGET_CLUB in store_ids and priced > 0
    return {
        "has_next_data": True,
        "page": data.get("page"),
        "pickup_club": pickup_club,
        "delivery_club": str(location.get("deliveryStore") or ""),
        "postal_code": str(location.get("postalCode") or ""),
        "city": location.get("city"),
        "state": location.get("stateOrProvinceCode"),
        "intent": location.get("intent"),
        "intent_strength": location.get("intentStrength"),
        "product_items": len(products),
        "priced_items": priced,
        "pickup_eligible_6262": pickup_eligible,
        "fulfillment_club_ids": sorted(store_ids),
        "exact_club_binding": exact,
        "samples": samples,
    }


async def body_text(page: Page) -> str:
    try:
        return await page.locator("body").inner_text(timeout=15_000)
    except Exception:
        return ""


async def capture_response(response: Response, records: list[dict[str, Any]]) -> None:
    url = response.url
    if not re.search(r"club|location|store|search|graphql|orchestration|fulfillment", url, re.I):
        return
    rec: dict[str, Any] = {
        "url": url,
        "status": response.status,
        "resource_type": response.request.resource_type,
        "content_type": response.headers.get("content-type", ""),
    }
    try:
        body = await response.body()
        rec["bytes"] = len(body)
        if len(body) <= 8_000_000:
            suffix = ".json" if "json" in rec["content_type"].lower() or body[:1] in (b"{", b"[") else ".bin"
            filename = f"network_{len(records):03d}{suffix}"
            (OUT / filename).write_bytes(body)
            rec["file"] = filename
    except Exception as exc:
        rec["error"] = repr(exc)
    records.append(rec)


async def browser_probe(context: BrowserContext) -> dict[str, Any]:
    page = await context.new_page()
    records: list[dict[str, Any]] = []
    tasks: list[asyncio.Task[Any]] = []
    page.on("response", lambda response: tasks.append(asyncio.create_task(capture_response(response, records))))
    result: dict[str, Any] = {"club_url": CLUB_URL, "search_url": SEARCH_URL}
    try:
        club_response = await page.goto(CLUB_URL, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(7_000)
        club_html = await page.content()
        club_text = await body_text(page)
        (OUT / "club_before.html").write_text(club_html, encoding="utf-8")
        (OUT / "club_before.txt").write_text(club_text, encoding="utf-8")
        await page.screenshot(path=str(OUT / "club_before.png"), full_page=False)
        result["club_status"] = club_response.status if club_response else None
        result["club_final_url"] = page.url
        result["club_title"] = await page.title()
        result["club_text_has_6262"] = "6262" in club_text
        result["club_text_has_address"] = "5565 De Zavala" in club_text
        result["cookies_before"] = await context.cookies()

        button = page.get_by_role("button", name=re.compile(r"Make this my club", re.I))
        if not await button.count():
            button = page.get_by_text(re.compile(r"Make this my club", re.I))
        result["make_club_candidates"] = await button.count()
        clicked = False
        for idx in range(await button.count()):
            candidate = button.nth(idx)
            if await candidate.is_visible():
                await candidate.scroll_into_view_if_needed()
                await candidate.click(timeout=20_000)
                clicked = True
                break
        result["make_club_clicked"] = clicked
        if not clicked:
            raise RuntimeError("Visible Make this my club control not found")

        await page.wait_for_timeout(6_000)
        result["cookies_after_click"] = await context.cookies()
        (OUT / "club_after_click.html").write_text(await page.content(), encoding="utf-8")
        (OUT / "club_after_click.txt").write_text(await body_text(page), encoding="utf-8")
        await page.screenshot(path=str(OUT / "club_after_click.png"), full_page=False)

        # Use the context request API first so we do not trigger an unnecessary second
        # browser navigation. It carries all first-party cookies created by the club action.
        api_response = await context.request.get(
            SEARCH_URL,
            headers={
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "accept-language": "en-US,en;q=0.9",
                "referer": CLUB_URL,
                "user-agent": UA,
            },
            timeout=90_000,
        )
        search_body = await api_response.body()
        (OUT / "search_context_request.html").write_bytes(search_body)
        result["context_request_status"] = api_response.status
        result["context_request_url"] = api_response.url
        context_data = extract_next_data(search_body.decode("utf-8", errors="ignore"))
        result["context_request_summary"] = summarize_search(context_data)
        if context_data:
            (OUT / "search_context_request_next.json").write_text(json.dumps(context_data), encoding="utf-8")

        # If the request API did not return an exact SSR payload, make one browser navigation.
        if not result["context_request_summary"].get("exact_club_binding"):
            search_response = await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=90_000)
            await page.wait_for_timeout(7_000)
            search_html = await page.content()
            (OUT / "search_browser.html").write_text(search_html, encoding="utf-8")
            (OUT / "search_browser.txt").write_text(await body_text(page), encoding="utf-8")
            await page.screenshot(path=str(OUT / "search_browser.png"), full_page=False)
            result["browser_search_status"] = search_response.status if search_response else None
            result["browser_search_url"] = page.url
            browser_data = extract_next_data(search_html)
            result["browser_search_summary"] = summarize_search(browser_data)
            if browser_data:
                (OUT / "search_browser_next.json").write_text(json.dumps(browser_data), encoding="utf-8")
        else:
            result["browser_search_summary"] = {"skipped": True}

        result["cookies_final"] = await context.cookies()
    except Exception as exc:
        result["error"] = repr(exc)
        try:
            (OUT / "error.html").write_text(await page.content(), encoding="utf-8")
            (OUT / "error.txt").write_text(await body_text(page), encoding="utf-8")
            await page.screenshot(path=str(OUT / "error.png"), full_page=False)
        except Exception:
            pass
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    (OUT / "network_index.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
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
        result = await browser_probe(context)
        await context.close()
        await browser.close()
    exact = bool((result.get("context_request_summary") or {}).get("exact_club_binding")) or bool(
        (result.get("browser_search_summary") or {}).get("exact_club_binding")
    )
    if not exact:
        raise SystemExit("Sam's Club 6262 did not survive into exact search metadata and product fulfillment")


if __name__ == "__main__":
    asyncio.run(main())
