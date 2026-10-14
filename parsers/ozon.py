"""Парсер карточек товаров Ozon на базе долгоживущего пула Playwright."""

import asyncio
import json
import logging
import random
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from playwright.async_api import Browser, Playwright, Route, async_playwright

from config.settings import settings
from parsers.base import BaseParser
from schemas.tracker import ParsedProduct

logger = logging.getLogger(__name__)


class OzonParserError(Exception):
    """Исключение при ошибках работы парсера Ozon."""

    pass


def format_playwright_proxy(proxy_url: Optional[str]) -> Optional[Dict[str, str]]:
    """Разделение строки прокси на параметры для Chromium."""
    if not proxy_url:
        return None
    try:
        parsed = urlsplit(proxy_url)
        res = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
        if parsed.username:
            res["username"] = parsed.username
        if parsed.password:
            res["password"] = parsed.password
        return res
    except Exception:
        return {"server": proxy_url}


class OzonParser(BaseParser):
    """Клиент сбора данных Ozon с долгоживущим браузером, проксированием и каскадным парсингом цен."""

    BASE_URL: str = "https://www.ozon.ru/product/"

    def __init__(self) -> None:
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Инициализация и запуск процесса Chromium."""
        async with self._lock:
            if self._browser is None:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--blink-settings=imagesEnabled=false",
                        "--disable-gpu",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                )

    async def close(self) -> None:
        """Остановка браузера и освобождение ресурсов."""
        async with self._lock:
            if self._browser:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None

    @staticmethod
    def _clean_price(raw_text: Any) -> Optional[Decimal]:
        """Нормализация строки или числа к объекту Decimal."""
        if raw_text is None:
            return None
        digits = re.sub(r"[^\d]", "", str(raw_text))
        if digits:
            val = Decimal(digits)
            return val if val >= Decimal("10") else None
        return None

    def _extract_from_json_ld(self, html: str) -> Optional[Decimal]:
        """Извлечение цены из микроразметки Schema.org (JSON-LD)."""
        pattern = r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>'
        matches = re.findall(pattern, html, re.DOTALL)
        for raw_json in matches:
            raw_json = raw_json.strip()
            if '"offers"' in raw_json or '"price"' in raw_json:
                try:
                    data = json.loads(raw_json)
                    items = data if isinstance(data, list) else [data]
                    for item in items:
                        offers = item.get("offers", {})
                        if isinstance(offers, dict) and "price" in offers:
                            return self._clean_price(offers["price"])
                        if isinstance(offers, list) and len(offers) > 0:
                            p = offers[0].get("price")
                            if p:
                                return self._clean_price(p)
                except Exception:
                    continue
        return None

    async def fetch_product_by_sku(self, sku: str) -> ParsedProduct:
        """Сбор параметров карточки товара по артикулу SKU."""
        clean_sku = "".join(filter(str.isdigit, str(sku)))
        if not clean_sku:
            raise OzonParserError(f"Некорректный артикул SKU: '{sku}'")

        if self._browser is None:
            await self.start()

        if self._browser is None:
            raise OzonParserError("Не удалось инициализировать пул браузера Playwright")

        target_url = f"{self.BASE_URL}{clean_sku}/"

        context_options: Dict[str, Any] = {
            "viewport": {"width": 1920, "height": 1080},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "locale": "ru-RU",
        }

        if settings.PROXY_SERVER:
            pw_proxy = format_playwright_proxy(settings.PROXY_SERVER)
            if pw_proxy:
                context_options["proxy"] = pw_proxy

        context = await self._browser.new_context(**context_options)

        try:
            page = await context.new_page()

            async def intercept_route(route: Route) -> None:
                req = route.request
                if req.resource_type in ["image", "media"] or any(
                    tracker in req.url
                    for tracker in ["metrika", "google-analytics", "advertising"]
                ):
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", intercept_route)

            await page.goto(
                target_url,
                wait_until="commit",
                timeout=settings.REQUEST_TIMEOUT_SECONDS * 1000,
            )
            await page.wait_for_load_state("domcontentloaded", timeout=20000)
            await asyncio.sleep(random.uniform(2.0, 3.5))

            raw_title = await page.title()
            logger.info("Загружена карточка SKU %s: '%s'", clean_sku, raw_title)

            title = (
                raw_title.replace(" - купить в интернет-магазине OZON", "")
                .split(" - купить")[0]
                .strip()
            )

            body_text = await page.inner_text("body")
            html_content = await page.content()

            is_out_of_stock = (
                "Этот товар закончился" in body_text
                or "Товар закончился" in body_text
                or "Узнать о поступлении" in body_text
            )

            if is_out_of_stock:
                return ParsedProduct(
                    sku=clean_sku,
                    url=target_url,
                    title=title,
                    card_price=None,
                    current_price=Decimal("0"),
                    original_price=None,
                    is_in_stock=False,
                    stock_count=0,
                )

            card_price: Optional[Decimal] = None
            current_price: Optional[Decimal] = None
            original_price: Optional[Decimal] = None

            ld_price = self._extract_from_json_ld(html_content)
            if ld_price:
                current_price = ld_price

            state_data_raw: Optional[str] = await page.evaluate("""
                () => {
                    const el = document.querySelector('[id*="state-webPrice"]');
                    return el ? el.getAttribute('data-state') : null;
                }
                """)
            if state_data_raw:
                try:
                    price_state = json.loads(state_data_raw)
                    cp = self._clean_price(price_state.get("cardPrice"))
                    p = self._clean_price(price_state.get("price"))
                    op = self._clean_price(
                        price_state.get("originalPrice") or price_state.get("oldPrice")
                    )
                    if cp:
                        card_price = cp
                    if p:
                        current_price = p
                    if op:
                        original_price = op
                except Exception:
                    pass

            if original_price is None:
                orig_text: Optional[str] = await page.evaluate("""
                    () => {
                        const el = document.querySelector('s, del, [class*="strike"], [class*="original"], [style*="line-through"]');
                        return el && el.innerText.includes('₽') ? el.innerText : null;
                    }
                    """)
                if orig_text:
                    original_price = self._clean_price(orig_text)

            price_box_text: Optional[str] = await page.evaluate("""
                () => {
                    const selectors = [
                        '[data-widget="webPrice"]',
                        '[data-widget="webBuyBox"]',
                        '[id*="webPrice"]',
                        'div[class*="price"]'
                    ];
                    for (const s of selectors) {
                        const el = document.querySelector(s);
                        if (el && el.innerText.includes('₽')) {
                            return el.innerText;
                        }
                    }
                    return null;
                }
                """)

            if price_box_text:
                extracted: List[Decimal] = []
                for line in price_box_text.split("\n"):
                    if "₽" in line:
                        cleaned = self._clean_price(line)
                        if cleaned and cleaned not in extracted:
                            extracted.append(cleaned)

                if extracted:
                    sorted_nums = sorted(extracted)
                    if len(sorted_nums) >= 3:
                        card_price = card_price or sorted_nums[0]
                        current_price = current_price or sorted_nums[1]
                        original_price = original_price or sorted_nums[-1]
                    elif len(sorted_nums) == 2:
                        card_price = card_price or sorted_nums[0]
                        current_price = current_price or sorted_nums[1]
                    elif len(sorted_nums) == 1:
                        current_price = current_price or sorted_nums[0]

            if current_price is None:
                matches = re.findall(r"(\d+[\d\s]*)\s*₽", body_text)
                valid_nums = [
                    self._clean_price(m) for m in matches if self._clean_price(m)
                ]
                unique_nums = [
                    n for n in valid_nums if n is not None and n > Decimal("50")
                ]
                if unique_nums:
                    current_price = unique_nums[0]

            if current_price is None:
                raise OzonParserError(f"Не удалось определить цену для SKU {clean_sku}")

            # Фиксация реального остатка: только при наличии явной надписи дефицита
            stock_count: Optional[int] = None
            stock_match = re.search(
                r"Осталось\s+(?:всего\s+)?(\d+)\s*(?:шт|штук)", body_text
            )
            if stock_match:
                try:
                    stock_count = int(stock_match.group(1))
                except Exception:
                    pass

            return ParsedProduct(
                sku=clean_sku,
                url=target_url,
                title=title,
                card_price=card_price,
                current_price=current_price,
                original_price=original_price,
                is_in_stock=True,
                stock_count=stock_count,
            )

        except Exception as exc:
            if isinstance(exc, OzonParserError):
                raise exc
            raise OzonParserError(f"Сбой парсинга SKU {clean_sku}: {exc}") from exc
        finally:
            await context.close()


_shared_parser: Optional[OzonParser] = None


def get_shared_parser() -> OzonParser:
    """Получение глобального разделяемого экземпляра OzonParser."""
    global _shared_parser
    if _shared_parser is None:
        _shared_parser = OzonParser()
    return _shared_parser
