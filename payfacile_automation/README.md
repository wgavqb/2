# PayFacile "Share by email" automation

Automates the flow shown in the screenshots:

1. Opens the product page in PayFacile.
2. Clicks **Share by email**.
3. Fills the email (and optional note).
4. Clicks **Confirm**.
5. Captures **every** HTTP request/response the page makes.

Output goes to `payfacile_automation/output/`:

| File                | What's in it                                                             |
|---------------------|--------------------------------------------------------------------------|
| `network.har`       | Standard HAR — import into Chrome DevTools → Network → right-click → "Import HAR". |
| `requests_log.json` | Flat JSON log of every request + response (headers, body, status).       |
| `error.png`         | Full-page screenshot, only written if the flow crashed.                  |

The console also prints every request live.

## Install

```bash
pip install -r requirements.txt
# Chromium is already at /opt/pw-browsers/chromium in this cloud env.
# On a local machine: python -m playwright install chromium
```

## Run

```bash
# Minimal — assumes you provide a pre-authenticated storage_state.json
PRODUCT_URL="https://www.payfacile.com/products/monthly-subscription" \
SHARE_EMAIL="buyer@example.com" \
python share_by_email.py --storage storage_state.json
```

```bash
# With login credentials (the script will log in first)
PF_EMAIL="you@example.com" \
PF_PASSWORD="…" \
SHARE_EMAIL="buyer@example.com" \
SHARE_NOTE="Here is the subscription link." \
python share_by_email.py --save-storage storage_state.json
```

```bash
# Watch it live
python share_by_email.py --headful --slow-mo 400
```

## Environment variables

| Var           | Default                                                                 |
|---------------|-------------------------------------------------------------------------|
| `PF_EMAIL`    | *(unset)* — if set, triggers login                                      |
| `PF_PASSWORD` | *(unset)*                                                               |
| `PRODUCT_URL` | `https://www.payfacile.com/products/monthly-subscription`               |
| `SHARE_EMAIL` | `test@example.com`                                                      |
| `SHARE_NOTE`  | *empty*                                                                 |

## CLI flags

| Flag             | Effect                                                                |
|------------------|-----------------------------------------------------------------------|
| `--headful`      | Launch a visible browser window.                                      |
| `--slow-mo MS`   | Pause each action by MS milliseconds.                                 |
| `--storage FILE` | Load a saved `storage_state.json` (skips login).                      |
| `--save-storage FILE` | After login, dump `storage_state.json` so later runs skip login. |
| `--quiet`        | Only print `xhr`/`fetch`/`document` in the console (HAR + JSON still capture everything). |

## Reading the output

- `requests_log.json` has one object per request **and** one per response, in order.
- `network.har` opens in Chrome DevTools, Firefox DevTools, or [har-viewer](https://har-viewer.sh/).
- The share API call is the `POST` or `PUT` fired at the moment you click **Confirm** — grep the log for the email you passed in or for `share`/`email` in the URL.
