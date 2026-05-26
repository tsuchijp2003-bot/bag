#!/usr/bin/env python3
"""
bag-monitor

使い方:
  python monitor.py          # 本番稼働（ループ）
  python monitor.py --test   # 1回だけ実行（Slack通知なし）

環境変数:
  SLACK_WEBHOOK_URL  Slack Incoming Webhook URL
  TARGETS_FILE       設定ファイルのパス（省略時: targets.yml）
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import sys
from datetime import datetime

import requests
import yaml
from playwright.async_api import async_playwright, Page

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
TARGETS_FILE      = os.environ.get("TARGETS_FILE", "targets.yml")
TEST_MODE         = "--test" in sys.argv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()] + (
        [] if TEST_MODE else [logging.FileHandler("monitor.log")]
    ),
)
log = logging.getLogger(__name__)


def load_config() -> dict:
    with open(TARGETS_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)


seen: dict[str, str] = {}


def item_id(name: str, url: str) -> str:
    return hashlib.md5(f"{name}|{url}".encode()).hexdigest()[:12]


def content_hash(item: dict) -> str:
    return hashlib.md5(json.dumps(item, sort_keys=True).encode()).hexdigest()


async def scrape(page: Page, url: str, keywords: list[str]) -> list[dict]:
    products = []

    await asyncio.sleep(random.uniform(2.0, 4.0))
    await page.goto(url, timeout=30_000, wait_until="domcontentloaded")
    await page.wait_for_timeout(4000)
    await page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
    await page.wait_for_timeout(2000)

    def matches(name: str) -> bool:
        return any(kw.lower() in name.lower() for kw in keywords)

    # 1. JSON-LD
    for tag in await page.query_selector_all('script[type="application/ld+json"]'):
        try:
            data = json.loads(await tag.inner_text() or "")
            for node in (data if isinstance(data, list) else [data]):
                if node.get("@type") == "ItemList":
                    for el in node.get("itemListElement", []):
                        _from_ld(products, el.get("item", {}), url, matches)
                elif node.get("@type") == "Product":
                    _from_ld(products, node, url, matches)
        except Exception:
            continue

    # 2. DOM フォールバック
    if not products:
        for card in await page.query_selector_all(
            "article[data-product-id], [class*='ProductCard'], [class*='product-item']"
        ):
            el = await card.query_selector("[class*='product-name'], [class*='ProductName'], h2, h3")
            name = (await el.inner_text()).strip() if el else ""
            if not name or not matches(name):
                continue
            link = await card.query_selector("a[href]")
            href = (await link.get_attribute("href") or "") if link else ""
            base = url.split("/category")[0]
            purl = (base + href) if href and not href.startswith("http") else (href or url)
            price_el = await card.query_selector("[class*='price']")
            price = (await price_el.inner_text()).strip() if price_el else ""
            products.append({
                "id": item_id(name, purl), "name": name,
                "price": price, "url": purl, "availability": "unknown",
            })

    # 3. テキスト全検索（最終手段）
    if not products:
        html = await page.content()
        for kw in keywords:
            if kw.lower() in html.lower():
                products.append({
                    "id": item_id(kw, url), "name": kw,
                    "price": "", "url": url, "availability": "detected",
                })

    return products


def _from_ld(products, data, src, matches):
    name = data.get("name", "")
    if not name or not matches(name):
        return
    offer = data.get("offers") or {}
    if isinstance(offer, list):
        offer = offer[0] if offer else {}
    products.append({
        "id":           data.get("sku") or item_id(name, src),
        "name":         name,
        "price":        str(offer.get("price", "")),
        "url":          data.get("url") or offer.get("url") or src,
        "availability": offer.get("availability", "unknown"),
    })


def notify(item: dict, event: str):
    if TEST_MODE or not SLACK_WEBHOOK_URL:
        log.info(f"  [TEST] 通知スキップ: {item['name']}")
        return
    emoji = "🆕" if event == "new" else "🔄"
    payload = {
        "text": f"{emoji} *在庫アラート* — {item['name']}",
        "attachments": [{
            "color": "#E8A430",
            "fields": [
                {"title": "商品名",   "value": item["name"],                             "short": True},
                {"title": "価格",     "value": (item["price"] or "—") + " JPY",          "short": True},
                {"title": "在庫状態", "value": item["availability"],                      "short": True},
                {"title": "検出時刻", "value": datetime.now().strftime("%Y-%m-%d %H:%M"), "short": True},
            ],
            "actions": [{"type": "button", "text": "商品ページを開く", "url": item["url"], "style": "primary"}],
        }],
    }
    try:
        requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10).raise_for_status()
        log.info(f"  📨 Slack送信: {item['name']}")
    except Exception as e:
        log.error(f"  Slack失敗: {e}")


async def check_all(page: Page, targets: list[dict]):
    log.info("─── チェック開始 ──────────────────────────")
    for target in targets:
        items = await scrape(page, target["url"], target["keywords"])
        log.info(f"[{target['name']}] {len(items)} 件")
        for item in items:
            h = content_hash(item)
            iid = item["id"]
            if iid not in seen:
                log.info(f"  🆕 NEW: {item['name']} ({item['availability']})")
                notify(item, "new")
                seen[iid] = h
            elif seen[iid] != h:
                log.info(f"  🔄 CHANGED: {item['name']}")
                notify(item, "changed")
                seen[iid] = h
            else:
                log.debug(f"  ✓ {item['name']}")


async def main():
    cfg      = load_config()
    targets  = cfg["targets"]
    interval = cfg.get("interval", 60)

    log.info("=" * 50)
    log.info(f"bag-monitor {'[テストモード]' if TEST_MODE else '起動'}")
    log.info(f"  ターゲット: {len(targets)} 件  間隔: {interval}秒")
    log.info("=" * 50)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--lang=ja-JP"],
        )
        ctx = await browser.new_context(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page = await ctx.new_page()
        await page.goto(targets[0]["url"].split("/category")[0] + "/", timeout=30_000)
        await page.wait_for_timeout(3000)

        if TEST_MODE:
            await check_all(page, targets)
            log.info("テスト完了。")
        else:
            while True:
                try:
                    await check_all(page, targets)
                except Exception as e:
                    log.error(f"エラー: {e}", exc_info=True)
                    try:
                        page = await ctx.new_page()
                    except Exception:
                        pass
                await asyncio.sleep(interval)

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
