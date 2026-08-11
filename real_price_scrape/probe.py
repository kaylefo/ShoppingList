from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from playwright.async_api import BrowserContext, Page, Response, async_playwright

OUT = Path("probe_output")
OUT.mkdir(parents=True, exist_ok=True)

SITES = {
    "target_search": "https://www.target.com/s?searchTerm=milk&storeId=2187",
    "target_grocery": "https://www.target.com/c/grocery/-/N-5xt1a?storeId=2187",
    "walmart_store": "https://www.walmart.com/store/5224-san-antonio-tx/shopping-services",
    "walmart_search": "https://www.walmart.com/search?q=milk&facet=fulfillment_method_in_store%3AIn-store",
    "heb_search": "https://www.heb.com/search/?q=milk",
    "heb_store": "https://www.heb.com/heb-store/tx/san-antonio/college-park-h-e-b-799",
    "costco_same_day": "https://sameday.costco.com/store/costco/search_v3/milk",
    "sams_search": "https://www.samsclub.com/s/milk",
    "sprouts_instacart": "https://shop.sprouts.com/store/sprouts/search_v3/milk",
}

INTERESTING = re.compile(r"graphql|redsky|search|browse|product|catalog|storefront|plp|items|api", re.I)


def safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", value)[:160]


async def direct_probe() -> None:
    headers = {
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "accept-language": "en-US,en;q=0.9",
    }
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=35) as client:
        for name, url in SITES.items():
            row: dict[str, Any] = {"name": name, "url": url}
            try:
                r = await client.get(url)
                row.update({
                    "status": r.status_code,
                    "final_url": str(r.url),
                    "content_type": r.headers.get("content-type"),
                    "length": len(r.content),
                    "title": (re.search(r"<title[^>]*>(.*?)</title>", r.text, re.I | re.S).group(1).strip() if re.search(r"<title[^>]*>(.*?)</title>", r.text, re.I | re.S) else None),
                })
                (OUT / f"direct_{name}.html").write_bytes(r.content)
            except Exception as exc:
                row["error"] = repr(exc)
            print("DIRECT", json.dumps(row, ensure_ascii=False), flush=True)
            results.append(row)
    (OUT / "direct_summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")


async def capture_response(site: str, response: Response, records: list[dict[str, Any]]) -> None:
    url = response.url
    content_type = (response.headers.get("content-type") or "").lower()
    if not (INTERESTING.search(url) or "json" in content_type):
        return
    rec: dict[str, Any] = {
        "url": url,
        "status": response.status,
        "content_type": content_type,
        "resource_type": response.request.resource_type,
    }
    try:
        body = await response.body()
        rec["length"] = len(body)
        if len(body) <= 12_000_000:
            ext = ".json" if "json" in content_type or body[:1] in (b"{", b"[") else ".bin"
            fname = f"network_{site}_{len(records):04d}_{safe_name(url)}{ext}"
            (OUT / fname).write_bytes(body)
            rec["file"] = fname
            if ext == ".json":
                try:
                    data = json.loads(body)
                    rec["top_keys"] = list(data)[:30] if isinstance(data, dict) else [f"list[{len(data)}]"]
                except Exception:
                    pass
    except Exception as exc:
        rec["body_error"] = repr(exc)
    records.append(rec)


async def browser_probe(context: BrowserContext, name: str, url: str) -> dict[str, Any]:
    page: Page = await context.new_page()
    network: list[dict[str, Any]] = []
    tasks: list[asyncio.Task[Any]] = []

    def on_response(response: Response) -> None:
        tasks.append(asyncio.create_task(capture_response(name, response, network)))

    page.on("response", on_response)
    result: dict[str, Any] = {"name": name, "requested_url": url}
    started = time.time()
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(12_000)
        result.update({
            "status": response.status if response else None,
            "final_url": page.url,
            "title": await page.title(),
            "body_text_length": len(await page.locator("body").inner_text(timeout=15_000)),
            "html_length": len(await page.content()),
        })
        html = await page.content()
        text = await page.locator("body").inner_text(timeout=15_000)
        (OUT / f"browser_{name}.html").write_text(html, encoding="utf-8")
        (OUT / f"browser_{name}.txt").write_text(text, encoding="utf-8")
        await page.screenshot(path=str(OUT / f"browser_{name}.png"), full_page=False)
    except Exception as exc:
        result["error"] = repr(exc)
        try:
            (OUT / f"browser_{name}_error.html").write_text(await page.content(), encoding="utf-8")
            await page.screenshot(path=str(OUT / f"browser_{name}_error.png"), full_page=False)
        except Exception:
            pass
    await page.wait_for_timeout(1500)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    result["network_records"] = len(network)
    result["elapsed_seconds"] = round(time.time() - started, 2)
    (OUT / f"network_{name}_index.json").write_text(json.dumps(network, indent=2), encoding="utf-8")
    print("BROWSER", json.dumps(result, ensure_ascii=False), flush=True)
    await page.close()
    return result


async def main() -> None:
    await direct_probe()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
            locale="en-US",
            timezone_id="America/Chicago",
            viewport={"width": 1440, "height": 1100},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        summaries = []
        for name, url in SITES.items():
            summaries.append(await browser_probe(context, name, url))
        (OUT / "browser_summary.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
        await context.close()
        await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
