# IVA — WhatsApp channel

Conversational food ordering for Ivar on WhatsApp. Customers chat in English, Telugu or
Hindi (native script or romanized) to browse the menu, search products, build a cart,
check out and track an order, or paste a meal plan from the AI Planner on the website.

This repository holds **only the WhatsApp channel** (webhook, conversation engine,
intent layer). Catalog, customers, cart, orders, payments and delivery belong to the
organization's backend and are reached **only** through the Backend Port
(`apps/conversations/backend_port.py`). Until the real backend exists, a deterministic
in-memory Fake Backend stands in for it (development and tests only; production refuses it).

**Stack:** Python 3.13, Django 5.2, PostgreSQL (psycopg 3), Redis, Celery, Meta WhatsApp
Cloud API.

## How it works

```
WhatsApp user ─► Meta Cloud API ─► POST /webhooks/whatsapp/   (signature-verified)
                                        │
                        Celery task (or inline in dev)       claim → per-customer lock
                                        │                    → rate limit → at-most-once
                     intent layer (apps/ai): rules → fuzzy → optional LLM proposal
                                        │
                     conversation engine (apps/conversations) ─► Backend Port ─► backend
                                        │
                              renderers ─► WhatsApp Cloud API reply
```

- The LLM (none is configured yet) may only *propose* an intent; it never writes data or
  decides price, stock, payment or order status.
- Message text is never stored or logged; phone numbers are masked in logs and
  pseudonymized (HMAC) in records.

## Layout

| Path | Delivered? | Purpose |
|---|---|---|
| `apps/iva_common/` | yes | Shared helpers: log masking, pseudonyms, money formatting, HTTP/TLS transport |
| `apps/conversations/` | yes | Backend Port contract, Fake Backend, conversation engine, renderers, session state, retention |
| `apps/whatsapp/` | yes | Webhook, signature check, parser, Cloud API client, Celery tasks, locks, rate limiting |
| `apps/ai/` | yes | Intent layer: rules, fuzzy matching, language detection, quantities, plan-message parser, LLM port |
| `config/`, `host/` | **no** | Disposable host project: settings, health checks |
| `Reference/` | no | Original specification PDF |

The delivered packages never import `config` or `host`
(`apps/iva_common/tests/test_portability.py` enforces this). Their only integration
points are Django settings and the Backend Port. There is no admin panel of any kind.

## Documentation

| File | For |
|---|---|
| `ARCHITECTURE.md` | Design, contract decisions, open requests for the backend team |
| `HANDOVER.md` | Backend engineer wiring the real backend in place of the Fake Backend |
| `PLANNER_INTEGRATION.md` | Website team: the `IVA PLAN v1` message format from the AI Planner |
| `CLAUDE.md` | Project rules and the detailed stage/status log |

## Stage status

S1–S4 (host project, WhatsApp webhook and Cloud API client, conversation engine, intent
layer) are built and verified against PostgreSQL and Redis. The LLM provider is not yet
chosen, so the intent layer runs on rules and fuzzy matching only. S5 (hardening:
per-customer ordering, rate limiting, deploy check, end-to-end and failure tests) is in
progress. See `CLAUDE.md`'s status table for the current detail.

## Security

- **Never commit `.env`.** It is git-ignored; use `.env.example` (placeholders only) as
  the template. If a real secret is ever committed, rotate it.
- Production (`config.settings.prod`) refuses to start with the Fake Backend, inline
  processing, wildcard hosts/origins, missing credentials, or an `IVA_PSEUDONYM_KEY`
  that is short or equal to `DJANGO_SECRET_KEY`.
- Inbound webhooks are verified with `X-Hub-Signature-256` using `WHATSAPP_APP_SECRET`.
- Do not rotate `IVA_PSEUDONYM_KEY` casually: it resets all conversation records and sessions.

## Setup (Windows, PowerShell)

```powershell
cd iva-whatsapp
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements\dev.txt
pip check
copy .env.example .env     # then fill in the values below
```

`.env` values (never commit `.env`):

- `DJANGO_SECRET_KEY`: generate with
  `python -c "import secrets; print(secrets.token_urlsafe(64))"`.
- `DATABASE_URL`: the organization's **development** PostgreSQL, e.g.
  `postgres://USER:PASSWORD@HOST:5432/DBNAME?sslmode=require`. The user needs
  `CREATEDB` so pytest can create and drop `test_<DBNAME>`.
- `REDIS_URL`, `CELERY_BROKER_URL`, `CONVERSATION_REDIS_URL`: provided by the
  organization. Leave empty until then (cache falls back to local memory and
  readiness reports Redis as `not_configured`).
- `IVA_BACKEND_ADAPTER`: the Fake Backend for development. Production refuses it.
- `IVA_PLANNER_URL`: the AI Planner page (built on the website, not this repo). Unset
  or non-`https` is treated as "not configured": the welcome CTA and menu link are
  simply skipped.
- `IVA_WEBSITE_SHOP_URL`: the website's full-menu page (e.g.
  `https://ivar-eyw.vercel.app/shop`). Unset or non-`https` hides the "See full menu on
  website" main-menu option.
- `IVA_INLINE_PROCESSING`: `true` for day-to-day development (no Celery worker
  needed — see Run below). Production refuses it; `.env.example` defaults it to `false`.
- `IVA_PRODUCT_IMAGE_BASE_URL`: base `https://` URL for product card images (e.g.
  `https://ivar-eyw.vercel.app/assets/products/`). Unset or non-`https` is treated as
  "not configured": product cards render text-only.
- `IVA_ORDER_WAIT_SECONDS` (default 5, `0` = off) and `IVA_ORDER_WAIT_STATUS` (`sent`, or
  `delivered`): a "Show more / Menu" message waits this long for the preceding carousel's
  or image card's WhatsApp status callback, so it cannot arrive first. Needs the Meta
  webhook to deliver status callbacks.

## Run

Two processing modes, both going through the exact same claim/lock/rate-limit/
at-most-once code (`apps.whatsapp.tasks._process_once`):

- **Inline** (`IVA_INLINE_PROCESSING=true`) — **development only.** The webhook
  processes each message synchronously in the request itself; no Celery worker or
  broker needed at all. Production refuses to start with this on
  (`config/settings/prod.py`). Idle Redis usage is ~0 (nothing is running when no
  request is in flight) and one message costs ~6 Redis commands (measured: a fresh
  customer's first message — per-customer lock acquire/release, the rate-limit
  counter, and the conversation session save). This is the default for local
  day-to-day development.
- **Worker** (`IVA_INLINE_PROCESSING=false`, the default) — **production**, and also
  used for the pre-handover check below. The webhook enqueues a Celery task; a
  separate worker process picks it up.

```powershell
python manage.py migrate
python manage.py runserver                 # dev settings
# http://127.0.0.1:8000/health/live/   -> 200 {"status": "ok"}
# http://127.0.0.1:8000/health/ready/  -> 200 or 503 with per-check status

# Only needed when IVA_INLINE_PROCESSING=false (Windows needs the solo pool); needs
# CELERY_BROKER_URL.
# --without-gossip/--without-mingle/--without-heartbeat: this is a single-worker
# deployment, so multi-worker coordination and `celery inspect` visibility are not
# needed, and dropping them cuts idle Redis command volume (see CLAUDE.md's
# 2026-09-25 Upstash free-tier tuning note). Add them back if a second worker joins.
celery -A config worker --pool=solo --loglevel=INFO --without-gossip --without-mingle --without-heartbeat
# On Upstash's free tier, idle usage in worker mode is ~74 commands/5min (~21,300/day),
# mostly Kombu's own unacked-message restore scan — investigated whether disabling
# CELERY_TASK_ACKS_LATE would stop it (it does not; see CLAUDE.md for the measurement
# and the trade-off, since acks_late also protects real crash-recovery). Tune further
# via IVA_CELERY_POLLING_INTERVAL_SECONDS / IVA_CELERY_HEALTH_CHECK_INTERVAL_SECONDS,
# or switch to inline mode for local development instead.
```

### Pre-handover real-worker check

Inline mode is convenient for day-to-day development, but the lock-retry path
(`CustomerLockTimeout` → release claim → retry) is only automatically tested under
Celery's eager mode. Before handing this off, verify it once against a real worker.
**This needs a real phone and has not been run as part of this work — do it yourself
before handover:**

1. Set `IVA_INLINE_PROCESSING=false` in `.env`, start `python manage.py runserver` and
   the Celery worker (the command above, with the low-usage flags).
2. Send three messages rapidly, back to back, from one phone. All three should get
   replies, in order (proves the per-customer lock's retry-then-report path holds up
   against a real worker, not just the eager-mode test suite).
3. Run one full journey: onboarding → add to cart → checkout → order status.
4. Stop the worker afterwards and set `IVA_INLINE_PROCESSING` back to `true` for
   day-to-day development.

`manage.py` defaults to `config.settings.dev`. `wsgi.py` / `asgi.py` default to
`config.settings.prod`, which refuses to start without `DJANGO_SECRET_KEY`,
`DATABASE_URL`, `REDIS_URL`, `CELERY_BROKER_URL` and a real `IVA_BACKEND_ADAPTER`,
or with wildcard hosts or origins.

## Tests and checks

```powershell
pytest -v                          # everything (needs DATABASE_URL)
pytest -v -m "not django_db"       # everything that needs no database
pytest --cov=apps --cov=host
ruff check .
ruff format --check .
pip check
```

Tests always use `config.settings.test` (PostgreSQL only, eager Celery, local-memory cache).

### Backend Port contract suite (for the backend team)

`apps/conversations/tests/contract/` runs the same tests against any adapter. To
run it against the real backend, write a harness class shaped like `ContractHarness`
in `tests/contract/harness.py`, then:

```powershell
$env:IVA_CONTRACT_HARNESS = "your_package.tests.iva_harness.RealHarness"
pytest apps/conversations/tests/contract -v
```

## Manual test checklist (Meta's test number)

Automated tests never call the real Meta API. Use this checklist for a one-off manual
check with a real WhatsApp client and Meta's test phone number. Do not automate this.

**Setup**

1. Fill in `.env`: `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`,
   `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_API_VERSION` from the Meta App Dashboard's
   test number (WhatsApp > API Setup). To also try the AI Planner welcome link, set
   `IVA_PLANNER_URL` to a real `https://` URL (leave it empty to confirm the
   unset/skip-the-link behavior instead).
2. Start the server: `python manage.py runserver` (and, in another terminal, the
   Celery worker: `celery -A config worker --pool=solo --loglevel=INFO --without-gossip
   --without-mingle --without-heartbeat`).
3. Start a tunnel to `127.0.0.1:8000`, e.g. `ngrok http 8000`. Add the tunnel's
   hostname to `DJANGO_ALLOWED_HOSTS` and its `https://` origin to
   `DJANGO_CSRF_TRUSTED_ORIGINS`, then restart the server.
4. In the Meta App Dashboard (WhatsApp > Configuration): set the callback URL to
   `https://<tunnel-host>/webhooks/whatsapp/`, the verify token to
   `WHATSAPP_VERIFY_TOKEN`'s value, click **Verify and save**, then subscribe the
   `messages` field.
5. On your phone, message the test number (add it as a contact first if WhatsApp
   requires it).

**Conversations to try**

- [ ] First message ("hi"): the very first reply greets you by your WhatsApp profile
      name (e.g. "Hi [Name]! 👋 Welcome to Ivar!") if you have one set, or a
      name-free "Welcome to Ivar!" if not, followed by the trilingual "Please choose
      your language" line and the three language buttons; pick English.
- [ ] Onboarding: with `IVA_PLANNER_URL` set, a planner message ("Our AI Planner can
      build you a personalised food plan.", no repeated greeting) with a "Plan my
      meals" link button appears, followed by a "Browse menu" button; with it unset,
      you land straight on the main menu instead.
- [ ] Send the same "hi" again (or restart the app): onboarding is skipped, you land
      on/near the main menu (returning customer).
- [ ] Tap "Plan my meals" from the main menu; the same link button reappears.
- [ ] With `IVA_WEBSITE_SHOP_URL` set, the main menu shows "See full menu on website"
      (and still fits, with "Plan my meals" too); tapping it sends an "Open full menu"
      link button that opens the shop page. With it unset, the option is absent.
- [ ] Type a search with exactly one match (e.g. "makhana"): that product's card
      appears (image, name, price, "Add to cart"/"Order now"), not a list. "add 2 quinoa"
      still adds 2.
- [ ] Type a product search with several matches (e.g. "protein"): one list with
      name and price per row ("Show more" if there are more than 10); tapping a row
      sends that product's card, whose "Add to cart"/"Order now" work. Category
      browsing still sends cards.
- [ ] Paste a plan message into the chat (first line exactly `IVA PLAN v1`, then
      `key: value` lines — see `PLANNER_INTEGRATION.md` for the exact format): a summary
      and the resolved products with real prices appear, with "Add all to cart" and
      "Browse menu" buttons; tapping "Add all to cart" adds them and shows the cart.
- [ ] Discovery (carousel): with `IVA_PRODUCT_IMAGE_BASE_URL` set, open "Café Favourites"
      (7 products): ONE swipeable carousel arrives ("Here are our Café Favourites"), each
      card with an image, name, price and the same "Add to cart"/"Order now" buttons,
      followed by a message with only "Menu" (nothing more to show). Check all 7 cards
      scroll and every image loads. **This is the first real-Meta test of the carousel
      payload; if Meta rejects it you should instead see the plain cards.**
- [ ] Ordering: in every carousel test above the "Want to see more, or head back to the
      menu?" message arrives AFTER the carousel (never before), on Desktop and Android.
      It may appear a second or two later than before; with the webhook not delivering
      status callbacks it appears after the 5-second timeout. If it is still first, set
      `IVA_ORDER_WAIT_STATUS=delivered`. Same check for a single image card followed by a
      buttons message.
- [ ] Open "Nuts, Seeds & Snacks" (10 products): one carousel of 10 cards. Tap "Add to
      cart" and "Order now" on a carousel card: same replies as on a plain card.
- [ ] Unset `IVA_PRODUCT_IMAGE_BASE_URL` (or point it at a bad host) and reopen a
      category: plain cards appear instead (4 per page, a "Show more"/Menu message
      after them; "Show more" shows the next 4 with no repeats). Nothing is hidden.
- [ ] Set your diet to vegetarian and open a category that has non-veg items: only the
      allowed items show, and a message says some are hidden (with "Show anyway").
- [ ] Tap "Add to cart" on a card: a short confirmation appears with "View cart"/
      "Continue shopping" buttons; "Continue shopping" shows the category list for the
      group you were browsing (Fresh & Ready to Eat / Packaged Foods) — it does not
      re-send the product cards you already have in the chat.
- [ ] Tap "Order now" on a card (with something else already in the cart): you go
      straight into checkout, and the order summary shows the *whole* cart, not just
      that one item.
- [ ] Add to cart, then "show my cart"; the item and total appear.
- [ ] Checkout: type a new address, confirm; a payment link and order number arrive.
      (The link goes nowhere real — the Fake Backend never talks to a real payment
      provider.)
- [ ] At the address prompt type "Hi": you get "Welcome back, <name>! You were in the middle of
      checkout." with "Continue checkout" and "Menu" (not an address error); "Continue checkout"
      asks for the address again and the cart is unchanged. "menu" and "my cart" do their normal
      thing. A real address like "Cart Road, Kondapur, Hyderabad" is still accepted.
- [ ] "where is my order" / tap the order-status button: shows `PENDING` payment.
- [ ] Change language mid-conversation (menu > change language) to Telugu, then Hindi;
      replies switch immediately and stay in that language on the next message.
- [ ] Type a message in Telugu script, then Hindi script, then a romanized/mixed
      message (e.g. "naaku snacks kavali", "mujhe protein chahiye"); each is understood
      without switching your saved language.
- [ ] Send two messages back to back within a second or two (e.g. "add makhana" then
      immediately "show my cart"); both get replies, in order, with no interleaving or
      dropped reply (per-customer ordering, S5).
- [ ] Send messages quickly past the per-minute limit (`IVA_RATE_LIMIT_PER_MINUTE`,
      default 20); after the cap, one "please slow down" reply arrives and then
      nothing else until the next minute.
- [ ] Tap "Help"; a help message appears.
- [ ] Send an emoji-only or nonsense message; you get a fallback/clarifying reply, not
      an error or silence.

**What to watch**

- Server and worker logs: only masked phone numbers/WhatsApp IDs, no message text (this
  includes your WhatsApp profile name and anything in a plan message), no tokens.
- `GET /health/ready/`: `200` with `database` and `redis` both `ok` throughout.
- `python manage.py report_stalled_messages`: `0` unless you deliberately kill the
  worker mid-message.

## Fake Backend

`apps/conversations/fake_backend.py` is in-memory and deterministic. It loads
`fixtures/ivar_catalog.json` (55 products; **prices and nutrition unverified**) and
logs every call with `backend=fake`. Each product's `image_path` (relative, e.g.
`eat/seed-mix.jpg`) is joined with `IVA_PRODUCT_IMAGE_BASE_URL` to build `image_url`;
all 55 have a path (each verified with one HEAD request: 45 on 2026-09-29, the last 10
on 2026-10-01); a product with none still renders as a text-only card, and a category
page containing one is sent as plain cards instead of a carousel. Its test-only helpers
(`simulate_payment_confirmed`, `simulate_payment_failed`, `simulate_status`,
`set_stock`, `reset`) are not part of the port.

## Keeping dev customer data across restarts

Set `IVA_FAKE_BACKEND_STATE_FILE` (the dev `.env` uses `.local-data/fake_backend.json`) and the Fake
Backend keeps customer profiles, carts, addresses and orders in that JSON file, so they survive server
restarts and autoreloads. The product catalog still comes from the fixture. Leave it empty for
in-memory only (the default; always the case in tests). Production refuses to start with it set. The
file is git-ignored (`.local-data/`) and may contain test customers' phone numbers, so do not share it.
A missing file starts empty; a corrupt one starts empty with a warning and is kept as `<file>.corrupt`.

## Resetting dev data

`python manage.py reset_dev_data` deletes all IVA test data: conversation rows in
PostgreSQL, IVA's own Redis keys (sessions, per-customer locks, rate-limit counters —
found by key prefix, never `FLUSHDB`/`FLUSHALL`), and any queued Celery tasks, and (if `IVA_FAKE_BACKEND_STATE_FILE` is set) empties that state file. It never
touches a non-IVA table or a non-IVA Redis key.

- **Dry run by default** — prints row/key/task counts and deletes nothing.
- **`--confirm`** actually deletes.
- **Refuses to run** against production settings, or if `IVA_BACKEND_ADAPTER` isn't the
  Fake Backend — either way, nothing is touched.
- Output is counts only: it never prints a key, a pseudonym, message text or a row's
  field values.

```powershell
python manage.py reset_dev_data              # dry run: shows counts, deletes nothing
python manage.py reset_dev_data --confirm    # actually deletes
```
