"""
Test script: detect Gorgias signup reCAPTCHA config & attempt signup.
Run: python3 test_signup.py
Requires: pip3 install playwright && python3 -m playwright install chromium
"""
from __future__ import annotations
import json, re, time, sys

def main():
    from playwright.sync_api import sync_playwright

    print("=== Gorgias Signup Detector ===\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()

        # Collect network info
        recaptcha_urls = []
        api_requests = []

        def on_request(req):
            url = req.url
            if "recaptcha" in url:
                recaptcha_urls.append(url)
            if "user/" in url and req.method == "POST":
                try:
                    body = req.post_data or ""
                except:
                    body = ""
                api_requests.append({
                    "url": url, "method": req.method,
                    "body": body[:500] if body else "",
                })

        def on_response(resp):
            url = resp.url
            if any(x in url for x in ["user/init", "user/submit", "user/validation", "user/create"]):
                try:
                    body = resp.text()[:1000]
                except:
                    body = ""
                api_requests.append({
                    "url": url, "status": resp.status,
                    "response": body,
                })

        page.on("request", on_request)
        page.on("response", on_response)

        print("[1] Loading gorgias.com/signup ...")
        page.goto("https://gorgias.com/signup", wait_until="domcontentloaded", timeout=40000)
        time.sleep(3)
        print(f"    URL: {page.url}\n")

        # Dismiss cookie consent popup (Axeptio)
        print("[1.5] Dismissing cookie popup...")
        for btn_text in ["OK!", "OK", "Accept", "Accept all", "No, thanks"]:
            try:
                btn = page.locator(f"text={btn_text}").first
                if btn.is_visible(timeout=2000):
                    btn.click()
                    print(f"    Clicked '{btn_text}'")
                    time.sleep(1)
                    break
            except:
                pass

        # Detect reCAPTCHA
        print("[2] Detecting reCAPTCHA configuration...")
        for url in recaptcha_urls:
            is_ent = "enterprise.js" in url
            print(f"    {'ENTERPRISE' if is_ent else 'Standard'}: {url[:150]}")
            m = re.search(r'render=([^&]+)', url)
            if m:
                print(f"    → sitekey from URL: {m.group(1)}")

        # Extract from page JS
        info = page.evaluate("""() => {
            const result = {sitekey: null, enterprise: false, actions: [], scripts: []};

            // Check for enterprise
            if (window.grecaptcha && window.grecaptcha.enterprise) {
                result.enterprise = true;
            }

            // Check script tags
            document.querySelectorAll('script[src*="recaptcha"]').forEach(s => {
                result.scripts.push(s.src);
                if (s.src.includes('enterprise.js')) result.enterprise = true;
                const m = s.src.match(/render=([^&]+)/);
                if (m && m[1] !== 'explicit') result.sitekey = m[1];
            });

            // Check data-sitekey
            const el = document.querySelector('[data-sitekey]');
            if (el) result.sitekey = el.getAttribute('data-sitekey');

            // Check grecaptcha config
            if (window.___grecaptcha_cfg) {
                const cfg = JSON.stringify(window.___grecaptcha_cfg).substring(0, 2000);
                result.cfg_excerpt = cfg;
                const m = cfg.match(/"sitekey"\\s*:\\s*"([^"]+)"/);
                if (m) result.sitekey = m[1];
            }

            // Look for execute calls in inline scripts
            document.querySelectorAll('script:not([src])').forEach(s => {
                const text = s.textContent || '';
                const matches = text.matchAll(/grecaptcha(?:\\.enterprise)?\\.execute\\([^,]*,\\s*\\{\\s*action:\\s*['"]([^'"]+)['"]/g);
                for (const m of matches) {
                    result.actions.push(m[1]);
                }
            });

            return result;
        }""")

        print(f"\n    Enterprise: {info.get('enterprise', False)}")
        print(f"    Sitekey:    {info.get('sitekey', 'NOT FOUND')}")
        print(f"    Actions:    {info.get('actions', [])}")
        if info.get('scripts'):
            for s in info['scripts']:
                print(f"    Script:     {s[:150]}")
        if info.get('cfg_excerpt'):
            print(f"    Config:     {info['cfg_excerpt'][:300]}")

        # List all form fields
        print("\n[3] Form fields found:")
        fields = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('input, select, textarea, button')).map(el => ({
                tag: el.tagName,
                type: el.type || '',
                name: el.name || '',
                id: el.id || '',
                placeholder: el.placeholder || '',
                visible: el.offsetParent !== null,
                value: el.value || '',
            }));
        }""")
        for f in fields:
            vis = "✓" if f['visible'] else "✗"
            print(f"    [{vis}] <{f['tag'].lower()} type='{f['type']}' name='{f['name']}' "
                  f"id='{f['id']}' placeholder='{f['placeholder']}'>")

        # Fill form with test data
        print("\n[4] Filling form...")
        test_email = "test_" + str(int(time.time())) + "@example.com"
        test_url = "https://testshop123.com"

        email_filled = False
        for sel in ['input[name="email"]', 'input[type="email"]', 'input[placeholder*="email" i]']:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.fill(test_email)
                    email_filled = True
                    print(f"    Email filled via: {sel}")
                    break
            except:
                pass

        url_filled = False
        for sel in ['input[name="website"]', 'input[name="url"]', 'input[type="url"]',
                    'input[placeholder*="website" i]', 'input[placeholder*="url" i]',
                    'input[placeholder*="store" i]']:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.fill(test_url)
                    url_filled = True
                    print(f"    URL filled via: {sel}")
                    break
            except:
                pass

        if not url_filled:
            inputs = page.query_selector_all("input:visible")
            if len(inputs) >= 2:
                inputs[1].fill(test_url)
                print(f"    URL filled via second visible input")

        # Try clicking submit and capture what happens
        print("\n[5] Clicking submit (watching network)...")
        api_requests.clear()

        submit = None
        for sel in ['button[type="submit"]', 'button:has-text("Create")',
                    'button:has-text("Sign up")', 'button:has-text("Get started")']:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    submit = el
                    print(f"    Found submit: {sel} → '{el.text_content()[:50]}'")
                    break
            except:
                pass

        if submit:
            submit.click()
            print("    Clicked! Waiting 8s for network activity...")
            time.sleep(8)

            print(f"\n[6] Captured {len(api_requests)} API requests:")
            for req in api_requests:
                print(f"    {json.dumps(req, indent=2)[:400]}")
        else:
            print("    No submit button found!")

        print(f"\n[7] Current URL: {page.url}")

        # Keep browser open for manual inspection
        print("\n=== Browser stays open for 60s. Check DevTools! ===")
        print("    Press Ctrl+C to exit early.")
        try:
            time.sleep(60)
        except KeyboardInterrupt:
            pass

        browser.close()

if __name__ == "__main__":
    main()
