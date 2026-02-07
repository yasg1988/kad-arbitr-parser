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
    """Parse plaintiff/respondent parties from a table cell."""
    parties = []
    # Parties are inside span.js-rolloverHtml (hidden rollover content)
    rollover_spans = td_element.xpath('.//span[@class="js-rolloverHtml"]')

    for span in rollover_spans:
        name = None
        inn = None
        address = None

        # Name is in <strong> tag
        name_el = span.xpath('.//strong')
        if name_el:
            name = name_el[0].text_content().strip()

        # INN is in "ИНН: XXXX" text inside a div
        inn_divs = span.xpath('.//div')
        for div in inn_divs:
            text = div.text_content().strip()
            if text.startswith("ИНН:"):
                inn = text.replace("ИНН:", "").strip()
                break

        # Address is plain text between <br/> and the INN div
        # Get all text content, remove name and INN parts
        full_text = span.text_content()
        lines = [l.strip() for l in full_text.split("\n") if l.strip()]
        # Address is usually the second non-empty line (after name)
        for line in lines:
            if line != name and not line.startswith("ИНН:") and len(line) > 10:
                address = line
                break

        if name:
            parties.append(CaseParty(name=name, inn=inn, address=address))

    # Fallback: if no rollover spans, try to get names from visible text
    if not rollover_spans:
        visible_spans = td_element.xpath('.//span[@class="js-rollover b-newRollover"]')
        for vs in visible_spans:
            # Get only direct text, not children
            text = vs.text_content().strip().split("\n")[0].strip()
            if text:
                parties.append(CaseParty(name=text))

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

    # Find rows: either by class or by presence of td.num
    rows = tree.xpath('//tr[td[@class="num"]]')
    for row in rows:
        try:
            # Case number and URL from td.num
            num_td = row.xpath('.//td[@class="num"]')
            if not num_td:
                continue

            url_el = num_td[0].xpath('.//a[@class="num_case"]/@href')
            if not url_el:
                url_el = num_td[0].xpath('.//a/@href')
            if not url_el:
                continue

            case_path = url_el[0]
            case_id = case_path.split("/")[-1] if "/" in case_path else case_path
            if case_path.startswith("http"):
                case_url = case_path
            else:
                case_url = f"{KAD_BASE}{case_path}"

            num_el = num_td[0].xpath('.//a[@class="num_case"]/text()')
            if not num_el:
                num_el = num_td[0].xpath('.//a/text()')
            case_number = num_el[0].strip() if num_el else ""

            # Case type from div class (civil/bankrupt/adm)
            type_div = num_td[0].xpath('.//div[@class="b-container"]/div/@class')
            case_type = _parse_case_type(type_div[0]) if type_div else None

            # Entry date from the type div's span or title
            entry_date = None
            date_span = num_td[0].xpath('.//div[@class="b-container"]/div/span/text()')
            if date_span:
                entry_date = date_span[0].strip()

            # Court and judge from td.court
            court = None
            judge = None
            court_td = row.xpath('.//td[@class="court"]')
            if court_td:
                judge_el = court_td[0].xpath('.//div[@class="judge"]/text()')
                judge = judge_el[0].strip() if judge_el else None
                # Court name is in a div without the "judge" class
                court_divs = court_td[0].xpath(
                    './/div[@class="b-container"]/div[not(@class="judge")]/@title'
                )
                court = court_divs[0].strip() if court_divs else None

            # Plaintiffs from td.plaintiff
            plaintiffs = []
            plaintiff_td = row.xpath('.//td[@class="plaintiff"]')
            if plaintiff_td:
                plaintiffs = _parse_party(plaintiff_td[0])

            # Respondents from td.respondent (might also be just next td)
            respondents = []
            respondent_td = row.xpath(
                './/td[@class="respondent"] | .//td[@class="defendant"]'
            )
            if respondent_td:
                respondents = _parse_party(respondent_td[0])

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
    """Search arbitration cases by INN using browser with network response capture."""
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

        # Capture SearchInstances responses directly from network
        search_responses: list[str] = []
        captcha_needed = asyncio.Event()
        response_received = asyncio.Event()

        async def _on_response(response):
            url = response.url
            if "Kad/SearchInstances" in url:
                try:
                    body = await response.text()
                    if response.status == 200 and len(body) > 500:
                        search_responses.append(body)
                        response_received.set()
                        logger.info("Captured SearchInstances response: %d bytes", len(body))
                    else:
                        logger.warning("SearchInstances %s, body_len=%d",
                                       response.status, len(body))
                except Exception as e:
                    logger.warning("Failed to read SearchInstances body: %s", e)
            elif "Recaptcha/IsNeedShowCaptcha" in url:
                try:
                    body = await response.text()
                    logger.info("IsNeedShowCaptcha: %s", body[:200])
                    if '"Result":true' in body:
                        captcha_needed.set()
                except Exception:
                    pass
        page.on("response", _on_response)

        page.on("console", lambda msg: (
            logger.info("CONSOLE [%s]: %s", msg.type, msg.text)
            if msg.type == "error" else None
        ))
        page.on("pageerror", lambda exc: logger.error("PAGE ERROR: %s", exc.message))

        try:
            # Step 1: Navigate to kad.arbitr.ru
            logger.info("Opening kad.arbitr.ru...")
            await page.goto(KAD_BASE, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(REQUEST_DELAY)

            # Step 2: Fill INN in the participant search field
            logger.info("Filling INN %s into search form...", inn)
            textarea = page.locator("#sug-participants textarea")
            await textarea.wait_for(timeout=10000)
            await textarea.fill(inn)
            await asyncio.sleep(0.5)

            # Dismiss notification popups that block the search button
            await page.evaluate("""() => {
                document.querySelectorAll(
                    '.b-promo_notification, .b-promo_notification-popup_wrapper'
                ).forEach(el => el.remove());
            }""")

            # Step 3: Click search button and wait for network response
            logger.info("Clicking search button...")
            response_received.clear()
            captcha_needed.clear()
            await page.click("#b-form-submit button[type=submit]")

            # Wait for either SearchInstances response or captcha
            for attempt in range(MAX_CAPTCHA_RETRIES):
                try:
                    await asyncio.wait_for(response_received.wait(), timeout=30)
                    logger.info("Search response captured on attempt %d", attempt + 1)
                    break
                except asyncio.TimeoutError:
                    if captcha_needed.is_set():
                        logger.info("CAPTCHA required, solving...")
                        if await _solve_captcha_popup(page):
                            captcha_needed.clear()
                            response_received.clear()
                            # After solving captcha, click search again
                            await page.click("#b-form-submit button[type=submit]")
                            continue
                        else:
                            logger.error("Failed to solve captcha")
                            return []
                    logger.warning("Timeout waiting for response, attempt %d", attempt + 1)
                    # Retry click
                    response_received.clear()
                    await page.click("#b-form-submit button[type=submit]")

            # Step 4: Parse captured response
            if search_responses:
                html_content = search_responses[-1]  # Use last response
                cases, total_pages = _parse_search_results(html_content)
                all_cases.extend(cases)
                logger.info("Page 1/%d: parsed %d cases from network response",
                            total_pages, len(cases))

                # Step 5: Pagination - click next page buttons via JS
                for page_num in range(2, min(total_pages + 1, 20)):
                    await asyncio.sleep(REQUEST_DELAY)

                    response_received.clear()
                    search_responses.clear()

                    # Click next page via JS (the site's pagination)
                    has_next = await page.evaluate("""() => {
                        const nextLink = document.querySelector('#pages li.rarr a');
                        if (nextLink) { nextLink.click(); return true; }
                        return false;
                    }""")
                    if not has_next:
                        logger.info("No next page button at page %d", page_num)
                        break

                    try:
                        await asyncio.wait_for(response_received.wait(), timeout=30)
                    except asyncio.TimeoutError:
                        logger.warning("Timeout on page %d", page_num)
                        break

                    if search_responses:
                        more_cases, _ = _parse_search_results(search_responses[-1])
                        all_cases.extend(more_cases)
                        logger.info("Page %d/%d: %d cases",
                                    page_num, total_pages, len(more_cases))
            else:
                logger.warning("No SearchInstances responses captured")

        except Exception as e:
            logger.error("Browser error: %s", e)
        finally:
            await browser.close()

    logger.info("Total cases for INN %s: %d", inn, len(all_cases))
    return all_cases
