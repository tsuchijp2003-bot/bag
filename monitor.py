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
    format="%(asctime)s %(message)s",
    handlers=[logging.StreamHandler()] + (
        [] if TEST_MODE else [logging.FileHandler("monitor.log")]
    ),
)
log = logging.getLogger(__name__)


def load_config() -> dict:
    with open(TARGETS_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)


seen: dict[str, bool] = {}
cycle = 0


async def is_purchasable(page: Page, url: str) -> bool:
    try:
        await page.goto(url, timeout=30_000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        for sel in [
            "[class*='AddToCart']:not([disabled])",
            "[class*='add-to-cart']:not([disabled])",
            "button[data-action*='cart']:not([disabled])",
            "button[class*='cart']:not([disabled])",
        ]:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                return True

        for btn in await page.query_selector_all("button:not([disabled])"):
            text = (await btn.inner_text()).strip().lower()
            if any(kw in text for kw in ["カートに追加", "add to cart", "購入", "buy"]):
                return True

        return False
    except Exception as e:
        log.warning(f"    ⚠️  アクセス失敗: {e}")
        return False


def notify(target: dict, url: str):
    if TEST_MODE or not SLACK_WEBHOOK_URL:
        log.info(f"    [TEST] Slack通知スキップ")
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    payload = {
        "text": f"🛒 *在庫アラート* — {target['name']}\n🕐 {now}\n🔗 {url}",
    }
    try:
        requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10).raise_for_status()
        log.info(f"    📨 Slack送信済み")
    except Exception as e:
        log.error(f"    ❌ Slack失敗: {e}")


async def check_all(page: Page, targets: list[dict]):
    global cycle
    cycle += 1
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log.info(f"")
    log.info(f"┌─ サイクル #{cycle}  {now}  ({len(targets)} 件チェック)")

    for i, target in enumerate(targets, 1):
        url = target["url"]
        log.info(f"│  [{i:02d}/{len(targets)}] {target['name']}")
        log.info(f"│       URL: {url}")

        purchasable     = await is_purchasable(page, url)
        was_purchasable = seen.get(url, None)
        status          = "✅ カート可" if purchasable else "❌ 在庫なし"
        log.info(f"│       結果: {status}")

        if purchasable and not was_purchasable:
            log.info(f"│       🆕 → Slack通知!")
            notify(target, url)
        elif was_purchasable is not None and not purchasable and was_purchasable:
            log.info(f"│       📭 在庫切れに変化")

        seen[url] = purchasable

        wait = random.uniform(3.0, 6.0)
        log.info(f"│       次まで {wait:.1f}秒待機...")
        await asyncio.sleep(wait)

    log.info(f"└─ サイクル #{cycle} 完了")


async def main():
    cfg      = load_config()
    targets  = cfg["targets"]
    interval = cfg.get("interval", 60)

    log.info("=" * 55)
    log.info(f"  bag-monitor {'[テストモード]' if TEST_MODE else '起動'}")
    log.info(f"  ターゲット : {len(targets)} 件")
    log.info(f"  チェック間隔: {interval} 秒")
    log.info(f"  Slack      : {'設定済み ✓' if SLACK_WEBHOOK_URL else '未設定 ⚠️'}")
    log.info("=" * 55)

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
        log.info("トップページにアクセス中...")
        await page.goto("https://www.hermes.com/jp/ja/", timeout=30_000)
        await page.wait_for_timeout(3000)
        log.info("準備完了。モニタリング開始。")

        if TEST_MODE:
            await check_all(page, targets)
            log.info("テスト完了。")
        else:
            while True:
                try:
                    await check_all(page, targets)
                except Exception as e:
                    log.error(f"❌ エラー: {e}", exc_info=True)
                    try:
                        page = await ctx.new_page()
                    except Exception:
                        pass
                log.info(f"次のサイクルまで {interval} 秒待機...")
                await asyncio.sleep(interval)

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
