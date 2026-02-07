import asyncio
import base64
import logging

import httpx
from lxml import html
from playwright.async_api import async_playwright

from app.config import RUCAPTCHA_API_KEY, REQUEST_DELAY
from app.models import ArbitrationCase, CaseParty

logger = logging.getLogger(__name__)

KAD_BASE = "https://kad.arbitr.ru"
RUCAPTCHA_IN = "https://rucaptcha.com/in.php"
RUCAPTCHA_RES = "https://rucaptcha.com/res.php"

MAX_CAPTCHA_RETRIES = 5
MAX_POLL_ATTEMPTS = 24
POLL_INTERVAL = 5


async def _solve_via_rucaptcha(image_b64: str) -> str | None:
    """Submit CAPTCHA image to rucaptcha.com and poll for result."""
    if not RUCAPTCHA_API_KEY:
        logger.error("RUCAPTCHA_API_KEY not configured")
        return None

    async with httpx.AsyncClient(timeout=30) as rc:
        resp = await rc.post(RUCAPTCHA_IN, data={
            "key": RUCAPTCHA_API_KEY,
            "method": "base64",
            "body": image_b64,
            "lang": "ru",
            "json": "1",
            "regsense": "1",
        })
        result = resp.json()
        if result.get("status") != 1:
            logger.warning("rucaptcha submit failed: %s", result)
            return None

        request_id = result["request"]
        logger.info("CAPTCHA submitted to rucaptcha, id=%s", request_id)

        for _ in range(MAX_POLL_ATTEMPTS):
            await asyncio.sleep(POLL_INTERVAL)
            resp = await rc.get(RUCAPTCHA_RES, params={
                "key": RUCAPTCHA_API_KEY,
                "action": "get",
                "id": request_id,
                "json": "1",
            })
            result = resp.json()
            if result.get("status") == 1:
                text = result["request"]
                logger.info("CAPTCHA solved: %s", text)
                return text
            if result.get("request") != "CAPCHA_NOT_READY":
                logger.warning("rucaptcha error: %s", result)
                return None

    logger.warning("CAPTCHA solve timed out")
    return None


def _parse_case_type(class_attr: str) -> str | None:
    types = {"civil": "civil", "adm": "administrative", "bankrupt": "bankruptcy"}
    for key, val in types.items():
        if key in class_attr.lower():
            return val
    return None


def _parse_party(td_element) -> list[CaseParty]:
    parties = []
    spans = td_element.xpath('.//span[@class="js-rolloverHtml"]')
    if not spans:
        divs = td_element.xpath('.//div')
        for div in divs:
            name = div.text_content().strip()
            if name:
                parties.append(CaseParty(name=name))
        return parties

    for span in spans:
        name = None
        inn = None
        address = None

        name_el = span.xpath('.//b | .//strong | .//a')
        if name_el:
            name = name_el[0].text_content().strip()
        else:
            name = span.text_content().strip().split("\n")[0].strip()

        rollover = span.get("data-rollover", "")
        if rollover:
            try:
                roll_tree = html.fromstring(rollover)
                text = roll_tree.text_content()
                for line in text.split("\n"):
                    line = line.strip()
                    if line.isdigit() and len(line) in (10, 12):
                        inn = line
                        break
                addr_spans = roll_tree.xpath('.//span[@class="js-rollover-address"]')
                if addr_spans:
                    address = addr_spans[0].text_content().strip()
            except Exception:
                pass

        if name:
            parties.append(CaseParty(name=name, inn=inn, address=address))

    return parties


def _parse_search_results(html_content: str) -> tuple[list[ArbitrationCase], int]:
    cases = []
    total_pages = 1

    try:
        tree = html.fromstring(html_content)
    except Exception as e:
        logger.error("Failed to parse HTML: %s", e)
        return cases, total_pages

    pages_el = tree.xpath('//input[@id="documentsPagesCount"]/@value')
    if pages_el:
        try:
            total_pages = int(pages_el[0])
        except (ValueError, IndexError):
            pass

    rows = tree.xpath('//tr[contains(@class, "b-container")]')
    for row in rows:
        try:
            url_el = row.xpath('.//td[1]//a/@href')
            if not url_el:
                continue
            case_path = url_el[0]
            case_id = case_path.split("/")[-1] if "/" in case_path else case_path
            case_url = f"{KAD_BASE}{case_path}"

            num_el = row.xpath('.//td[1]//a/text()')
            case_number = num_el[0].strip() if num_el else ""

            type_el = row.xpath('.//td[1]//span[contains(@class,"type")]/@class')
            case_type = _parse_case_type(type_el[0]) if type_el else None

            court_el = row.xpath('.//td[2]//text()')
            court = " ".join(t.strip() for t in court_el).strip() or None

            judge_el = row.xpath('.//td[3]//text()')
            judge = " ".join(t.strip() for t in judge_el).strip() or None

            date_el = row.xpath('.//td[1]//span[contains(@class,"num_case")]//span/text()')
            entry_date = date_el[0].strip() if date_el else None

            tds = row.xpath('.//td')
            plaintiffs = []
            respondents = []
            if len(tds) >= 5:
                plaintiffs = _parse_party(tds[3])
                respondents = _parse_party(tds[4])

            cases.append(ArbitrationCase(
                case_id=case_id,
                case_number=case_number,
                case_type=case_type,
                case_url=case_url,
                court=court,
                judge=judge,
                entry_date=entry_date,
                plaintiffs=plaintiffs,
                respondents=respondents,
            ))
        except Exception as e:
            logger.warning("Failed to parse row: %s", e)
            continue

    return cases, total_pages


async def _solve_captcha_popup(page) -> bool:
    """Detect captcha popup in the page, solve it via rucaptcha, and submit."""
    for attempt in range(1, MAX_CAPTCHA_RETRIES + 1):
        logger.info("Captcha popup attempt %d/%d", attempt, MAX_CAPTCHA_RETRIES)

        # Find captcha image in the popup
        captcha_img = await page.query_selector(
            ".b-pravo-popup img, "
            ".js-pravo-popup img, "
            ".b-modal img[src*='Recaptcha'], "
            ".b-modal img[src*='GetImage'], "
            "img[src*='Recaptcha/GetImage']"
        )

        if not captcha_img:
            # Try broader: any visible popup with an image
            captcha_img = await page.query_selector(
                ".b-pravo-popup img, .modal img, .popup img, "
                "[class*='captcha'] img, [id*='captcha'] img"
            )

        if not captcha_img:
            logger.warning("Could not find captcha image in popup")
            # Take screenshot for debugging
            try:
                await page.screenshot(path="/tmp/captcha_debug.png")
                logger.info("Debug screenshot saved to /tmp/captcha_debug.png")
            except Exception:
                pass
            return False

        # Screenshot the captcha image and convert to base64
        img_bytes = await captcha_img.screenshot()
        img_b64 = base64.b64encode(img_bytes).decode()
        logger.info("Captcha image captured, size=%d bytes", len(img_b64))

        # Solve via rucaptcha
        solved_text = await _solve_via_rucaptcha(img_b64)
        if not solved_text:
            continue

        # Find the input field in the captcha popup
        captcha_input = await page.query_selector(
            ".b-pravo-popup input[type='text'], "
            ".js-pravo-popup input[type='text'], "
            ".b-modal input[type='text'], "
            "[class*='captcha'] input[type='text'], "
            "[id*='captcha'] input[type='text']"
        )

        if not captcha_input:
            # Broader search
            captcha_input = await page.query_selector(
                "input.b-pravo-popup__input, input[name*='captcha'], "
                ".modal input:not([type='hidden'])"
            )

        if not captcha_input:
            logger.warning("Could not find captcha input field")
            return False

        # Type the answer
        await captcha_input.fill(solved_text)
        logger.info("Typed captcha answer: %s", solved_text)

        # Find and click submit button
        submit_btn = await page.query_selector(
            ".b-pravo-popup button, "
            ".js-pravo-popup button, "
            ".b-modal button, "
            "[class*='captcha'] button, "
            ".b-pravo-popup input[type='submit'], "
            ".b-modal input[type='submit']"
        )

        if submit_btn:
            await submit_btn.click()
            logger.info("Clicked captcha submit button")
        else:
            # Try pressing Enter
            await captcha_input.press("Enter")
            logger.info("Pressed Enter to submit captcha")

        # Wait for popup to disappear or results to load
        await asyncio.sleep(2)

        # Check if popup is gone
        popup_visible = await page.evaluate("""() => {
            const popups = document.querySelectorAll(
                '.b-pravo-popup, .js-pravo-popup, .b-modal, [class*="captcha-popup"]'
            );
            for (const p of popups) {
                if (p.offsetParent !== null && p.style.display !== 'none') return true;
            }
            return false;
        }""")

        if not popup_visible:
            logger.info("Captcha popup dismissed successfully")
            return True

        logger.warning("Captcha popup still visible, retrying...")

    logger.error("Failed to solve captcha popup after %d attempts", MAX_CAPTCHA_RETRIES)
    return False


async def _wait_for_search_result(page, timeout: int = 60000) -> str:
    """Wait for search to complete. Returns: 'results', 'empty', 'captcha', 'error'."""
    try:
        state = await page.evaluate("""(timeout) => {
            return new Promise((resolve) => {
                const check = () => {
                    // Check for case rows
                    const rows = document.querySelectorAll('#b-cases tr.b-container');
                    if (rows.length > 0) { resolve('results'); return true; }

                    // Check for "no results" message
                    const noRes = document.querySelector('.b-noResults');
                    if (noRes && !noRes.classList.contains('g-hidden') &&
                        noRes.offsetParent !== null) { resolve('empty'); return true; }

                    // Check for captcha popup
                    const popups = document.querySelectorAll(
                        '.b-pravo-popup, .js-pravo-popup, [class*="captcha"]'
                    );
                    for (const p of popups) {
                        const img = p.querySelector('img');
                        const input = p.querySelector('input[type="text"]');
                        if (img && input && p.offsetParent !== null) {
                            resolve('captcha');
                            return true;
                        }
                    }

                    return false;
                };

                if (check()) return;

                const interval = setInterval(() => {
                    if (check()) clearInterval(interval);
                }, 500);

                setTimeout(() => {
                    clearInterval(interval);
                    resolve('timeout');
                }, timeout);
            });
        }""", timeout)
        logger.info("Search result state: %s", state)
        return state
    except Exception as e:
        logger.error("Error waiting for search result: %s", e)
        return "error"


async def search_cases_by_inn(inn: str) -> list[ArbitrationCase]:
    """Search arbitration cases by INN using headless browser with UI interaction."""
    all_cases = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="ru-RU",
        )
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)
        page = await context.new_page()

        # Log browser console messages and errors
        page.on("console", lambda msg: logger.info("CONSOLE [%s]: %s", msg.type, msg.text))
        page.on("pageerror", lambda exc: logger.error("PAGE ERROR: %s", exc.message))

        # Monitor network responses for SearchInstances
        async def _on_response(response):
            url = response.url
            if "SearchInstances" in url or "Recaptcha" in url:
                try:
                    body = await response.text()
                    logger.info("NETWORK %s %s body_len=%d first200=%s",
                                response.status, url, len(body), body[:200])
                except Exception:
                    logger.info("NETWORK %s %s (no body)", response.status, url)
        page.on("response", _on_response)

        try:
            # Step 1: Navigate to kad.arbitr.ru
            logger.info("Opening kad.arbitr.ru...")
            await page.goto(KAD_BASE, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(REQUEST_DELAY)

            # Log cookies
            cookies = await context.cookies()
            cookie_names = [c["name"] for c in cookies]
            logger.info("Cookies after page load: %s", cookie_names)

            # Step 2: Fill INN in the participant search field
            logger.info("Filling INN %s into search form...", inn)
            textarea = page.locator("#sug-participants textarea")
            await textarea.wait_for(timeout=10000)
            await textarea.fill(inn)
            await asyncio.sleep(0.5)

            # Dismiss any notification popups that block the search button
            await page.evaluate("""() => {
                document.querySelectorAll(
                    '.b-promo_notification, .b-promo_notification-popup_wrapper'
                ).forEach(el => el.remove());
            }""")

            # Step 3: Click search button
            logger.info("Clicking search button...")
            await page.click("#b-form-submit button[type=submit]")

            # Wait a moment and check if pravocaptcha triggered the search
            await asyncio.sleep(5)

            # Check pravocaptcha state and wasm cookie
            debug_info = await page.evaluate("""() => {
                return {
                    hasPravocaptcha: typeof pravocaptcha !== 'undefined',
                    hasWasmCookie: document.cookie.includes('wasm='),
                    hasJQuery: typeof $ !== 'undefined' || typeof jQuery !== 'undefined',
                    pageTitle: document.title,
                    loadingVisible: !!document.querySelector('#b-loader:not(.g-hidden)'),
                    blindVisible: !!document.querySelector('.b-blind--loader:not(.g-hidden)'),
                };
            }""")
            logger.info("Debug info after click: %s", debug_info)

            # If pravocaptcha exists but WASM cookie not set, try to trigger search directly
            if debug_info.get("hasPravocaptcha") and not debug_info.get("hasWasmCookie"):
                logger.info("WASM cookie not set, trying to trigger search via JS...")
                # Try calling the site's internal search function directly
                try:
                    await page.evaluate("""async () => {
                        // Try to trigger the form submit event directly
                        const form = document.querySelector('#b-form');
                        if (form) {
                            const event = new Event('submit', { cancelable: true });
                            form.dispatchEvent(event);
                        }
                    }""")
                    await asyncio.sleep(3)
                except Exception as e:
                    logger.warning("Direct form submit failed: %s", e)

            # Step 4: Wait for result (results, captcha, or empty)
            captcha_retries = MAX_CAPTCHA_RETRIES
            while captcha_retries > 0:
                state = await _wait_for_search_result(page, timeout=45000)

                if state == "captcha":
                    logger.info("Captcha popup detected, solving...")
                    if await _solve_captcha_popup(page):
                        continue
                    else:
                        logger.error("Failed to solve captcha popup")
                        return []
                elif state == "results":
                    break
                elif state == "empty":
                    logger.info("No results found for INN %s", inn)
                    return []
                else:
                    logger.warning("Search state: %s", state)
                    # Take debug screenshot
                    try:
                        await page.screenshot(path="/tmp/kad_debug.png")
                        logger.info("Debug screenshot saved")
                    except Exception:
                        pass
                    # Try clicking search again
                    captcha_retries -= 1
                    if captcha_retries > 0:
                        await asyncio.sleep(REQUEST_DELAY)
                        await page.click("#b-form-submit button[type=submit]")
                        continue
                    break

            # Step 5: Parse results from DOM
            dom_html = await page.inner_html("#b-cases")
            if dom_html and dom_html.strip():
                # Wrap in table tags for lxml parser
                full_html = f"<table>{dom_html}</table>"
                cases, total_pages = _parse_search_results(full_html)
                all_cases.extend(cases)
                logger.info("Page 1/%d: %d cases from DOM", total_pages, len(cases))

                # Step 6: Pagination
                for page_num in range(2, total_pages + 1):
                    await asyncio.sleep(REQUEST_DELAY)

                    next_btn = page.locator("#pages li.rarr a")
                    if await next_btn.count() == 0:
                        logger.info("No next page button, stopping")
                        break

                    await next_btn.click()
                    state = await _wait_for_search_result(page, timeout=30000)

                    if state == "captcha":
                        if not await _solve_captcha_popup(page):
                            break
                        state = await _wait_for_search_result(page, timeout=30000)

                    if state in ("results", "empty"):
                        next_html = await page.inner_html("#b-cases")
                        if next_html:
                            more_cases, _ = _parse_search_results(
                                f"<table>{next_html}</table>"
                            )
                            all_cases.extend(more_cases)
                            logger.info("Page %d/%d: %d cases",
                                        page_num, total_pages, len(more_cases))
                    else:
                        logger.warning("Failed to get page %d, state=%s", page_num, state)
                        break

            # Log final cookies
            cookies = await context.cookies()
            cookie_names = [c["name"] for c in cookies]
            logger.info("Final cookies: %s", cookie_names)

        except Exception as e:
            logger.error("Browser error: %s", e)
        finally:
            await browser.close()

    logger.info("Total cases for INN %s: %d", inn, len(all_cases))
    return all_cases
