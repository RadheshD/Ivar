# ARCHITECTURE.md — IVA WhatsApp Channel

This document describes how the WhatsApp channel is built and how it connects to the organization's backend. Keep it current: update it whenever a stage changes the design. The full specification is in `Reference/IVA_WhatsApp_FreshStart_Prompt.pdf`.

## 1. Responsibility split

| Area | Owner |
|---|---|
| WhatsApp webhook, Cloud API client | **This repository** |
| Conversation engine (flows, state, replies) | **This repository** |
| Intent layer (rules, aliases, LLM fallback, validation) | **This repository** |
| Catalog, customers, cart, addresses, checkout, orders, payments, delivery | Organization backend team |
| PostgreSQL, Redis, hosting | Organization |
| AI Planner page (UI, nutrition computation) | Website team — this repo only links out to it and parses its `wa.me` reply message (2026-09-28) |

The only interface between the two sides is the **Backend Port** (section 5).

## 2. System overview

```text
Customer (WhatsApp)
        │
        ▼
Meta WhatsApp Cloud API
        │  HTTPS webhook (signed)
        ▼
apps/whatsapp ── verify signature ── dedupe message ID ── enqueue ──► Celery worker
                                                                         │
                                                                         ▼
                                                         apps/conversations (engine)
                                                           │         │          │
                                                  session state   intent     Backend Port
                                                    (Redis)     (apps/ai)        │
                                                                                 ▼
                                                          Fake Backend (dev/test) │ Real backend (org)
        ▲                                                                         
        │  reply (text / buttons / list / product card)
apps/whatsapp client ◄── renderer ◄── engine response
```

## 3. Repository layout

```text
config/        Host project settings, URLs, Celery app       (host only, not delivered)
host/          Health checks, host-level tests               (host only, not delivered)
apps/
  iva_common/     Shared helpers: log masking, money formatting
  conversations/  Backend Port, Fake Backend, adapters, engine, session state,
                  messages (en/te/hi), renderers, conversation models
  whatsapp/       Webhook views, signature check, payload parser, Cloud API client, tasks
  ai/             Intents, rules, aliases, text normalization, language guess, quantities,
                  fuzzy matching, LLM client, schema validation, pipeline
```

**Portability rule:** only the four packages under `apps/` are delivered. They must not import from `config/` or `host/`. Import direction:

```text
whatsapp → conversations → ai → iva_common
```

All four may use `iva_common`. `ai` never imports `whatsapp`. A static test enforces this.

## 4. Message lifecycle

1. **Receive.** Meta sends `POST /webhooks/whatsapp/`.
2. **Verify.** HMAC-SHA256 of the raw body with the app secret is compared (constant time) to `X-Hub-Signature-256`. Invalid → 403, nothing processed.
3. **Deduplicate.** The WhatsApp message ID is stored with a database unique constraint. Duplicates are acknowledged and ignored.
4. **Acknowledge fast.** Return 200 immediately; processing continues in a Celery task.
5. **Load context.** Resolve the customer through the Backend Port and load the session from Redis.
6. **Understand.** Run the intent pipeline (section 7).
7. **Act.** The engine calls Backend Port operations. Business decisions happen in the backend.
8. **Reply.** The renderer turns the engine's response into WhatsApp text, reply buttons (max 3), a list (max 10 rows) or a product card (image header optional, max 3 buttons) or, for category pages, a media carousel (2-10 image cards, 2 quick-reply buttons each), in the customer's language.
9. **Record.** Inbound and outbound messages are stored; logs mask phone numbers.

Status callbacks from Meta (sent, delivered, read, failed) are recorded and never replied to.

### Webhook, client and records (S2)

**Webhook** (`apps/whatsapp/views.py`, `/webhooks/whatsapp/`, a plain Django view, CSRF-exempt)
- **GET:** requires `hub.mode=subscribe`, a constant-time match of `hub.verify_token` against `WHATSAPP_VERIFY_TOKEN`, and a short alphanumeric `hub.challenge`, which is echoed as `text/plain`. Anything else → 403.
- **POST:**
  1. `X-Hub-Signature-256` is checked over the **raw body** with `WHATSAPP_APP_SECRET` (`security.py`). Invalid or missing → 403, nothing processed.
  2. The body is parsed (`parser.py`). The parser is defensive and never raises. Events for another `phone_number_id` are ignored, and contact profile names are not read.
  3. Each message is recorded; the unique `wa_message_id` drops duplicates. New messages are queued, and the response is 200.
- **Status callbacks** update our outbound row. Statuses only move forward (sent → delivered → read); `failed` records Meta's error code.

**Worker** (`tasks.process_inbound`)
1. Claims the message: sets `processing_started_at` only if it is still empty.
2. Checks the 24-hour window.
3. Runs `deliver()`: engine → `renderers.render` → `WhatsAppClient.send`, one message per reply, in order. It stops at the first failed send.
4. Records each outbound message with its WhatsApp ID or a failure code, then sets `processing_completed_at`.
5. **Crash visibility:** a message claimed but not completed after 5 minutes is treated as crashed processing. `report_stalled_messages` (the management command, or the `whatsapp.report_stalled` Celery task) logs one masked warning for it, e.g. `processing stalled: message=42 to=91******0001 claimed 6 min ago; not retried`, and marks it reported. Stalled messages are never retried, which keeps processing at most once. Run the check every 5 minutes.

### Hardening (S5)

**Per-customer ordering** (`apps/whatsapp/locks.py`). `customer_lock(wa_id)` is a Redis mutex keyed by the customer's pseudonym (`iva:lock:customer:<hash>`), backed by `CONVERSATION_REDIS_URL`. It wraps only the part of `process_inbound` that touches the session (engine call through recording the replies) — the message is claimed for at-most-once dedup *before* the lock, so a lock wait or timeout never causes a double claim. Two near-simultaneous messages from the same customer therefore run one at a time, in the order each worker reaches the lock; messages from different customers are never affected. The lock has two independent timings: `ttl_seconds` (fixed, `locks.DEFAULT_TTL_SECONDS`) is a safety net that frees a crashed holder's lock on its own; `IVA_CUSTOMER_LOCK_WAIT_SECONDS` (default 30) bounds how long a second message waits for its turn. A timeout raises `CustomerLockTimeout`; the message is left claimed-but-incomplete so `report_stalled_messages` reports it exactly like a crashed worker (never retried, same at-most-once guarantee). With no `CONVERSATION_REDIS_URL` configured, the lock is a no-op (no ordering guarantee, matching the in-memory session store's single-process assumption).

**Per-customer rate limit** (`apps/whatsapp/rate_limit.py`). `register_message(wa_id)` counts inbound messages per customer per fixed one-minute window, backed by `settings.CACHES` (`REDIS_URL`, shared across worker processes). Configurable via `IVA_RATE_LIMIT_PER_MINUTE` (default 20). Up to the cap, messages are processed normally. The message that first crosses the cap gets one localized "please slow down" reply (`ConversationEngine.rate_limited_reply`, key `limit.rate_limited`) and nothing else — it never reaches the intent pipeline or touches the session. Every further message in the same window is silently dropped (no reply, no session, no engine) until the next minute's window resets. This check runs inside the Celery task, after the customer lock, so it never delays or affects the webhook's own 200 response to Meta.

**Client** (`client.py`)
- `POST {WHATSAPP_GRAPH_BASE_URL}/{WHATSAPP_API_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages` with `messaging_product`, `recipient_type`, `to` and the rendered message.
- Retries 429, 5xx and transport failures up to 3 attempts, with backoff of 0.5 s then 1 s (honouring `Retry-After`, capped at 8 s). Other 4xx are not retried.
- A 2xx response without a message ID counts as a failure and is not resent, because resending could duplicate the message.

**HTTP** (`apps/iva_common/http_transport.py`, standard library only)
- Uses `http.client`, which `urllib` is built on. `urllib` has only one timeout; `http.client` allows a separate **connect timeout (3 s)** and **read timeout (10 s)**.
- TLS always uses `ssl.create_default_context()`, so certificates are verified (`CERT_REQUIRED`) and hostnames are checked. There is no option, setting or environment variable to turn this off; Python's global opt-outs (`PYTHONHTTPSVERIFY=0`, an unverified default HTTPS context) do not affect it. A test proves this against a local server with a self-signed certificate.
- Responses are capped at 1 MB.
- Tests use a fake transport; nothing ever calls Meta.

**Message text retention decision.** Message text is **not stored**. PostgreSQL keeps only the metadata needed for dedup and auditing (section 9). That metadata is deleted after `IVA_MESSAGE_RETENTION_DAYS` (default 90) by `manage.py purge_conversation_messages`. Text exists only in worker memory and in the Celery broker until the task runs. Celery shows the task arguments masked.

## 5. Backend Port

Defined in `apps/conversations/backend_port.py` as `typing.Protocol` interfaces with frozen dataclasses. Money is `Decimal` plus currency.

| Port | Operations |
|---|---|
| CustomerPort | `get_or_create_customer`, `update_profile` |
| CatalogPort | `list_categories`, `list_products`, `get_product`, `resolve_product_names` |
| CartPort | `get_cart`, `add_item`, `update_quantity`, `remove_item` |
| AddressPort | `list_addresses`, `add_address` |
| CheckoutPort | `checkout(customer_id, address_id, idempotency_key)` |
| OrderPort | `get_latest_order`, `get_order`, `list_recent_orders` |

**Errors:** `BackendError` → `BackendUnavailable`, `NotFound`, `ProductUnavailable`, `InvalidQuantity`, `CartEmpty`, `CheckoutRejected`, `NotAuthorized`. The engine maps each to a friendly localized message.

**Adapters:** `settings.IVA_BACKEND_ADAPTER` names the implementation.

- Development/tests: `FakeBackend` (in-memory, loaded from `fixtures/ivar_catalog.json`, 55 products, marked unverified).
- Production: the organization's adapter. Production settings refuse the Fake Backend.
- A shared **contract test suite** runs against any adapter, so the backend team can prove their implementation matches.

### Contract decisions

Details decided in `backend_port.py` and enforced by `apps/conversations/tests/contract/`. The backend team should confirm or correct each one.

- **Diet filter.** `list_products(diet=...)` filters by the customer's dietary preference: `veg` → veg only; `egg` → veg + egg; `non_veg` → everything.
- **Quantity limit.** Integer 1–20 (`MAX_LINE_QUANTITY = 20`). *Fake Backend default, real limit set by backend.* Non-integers (including `bool`, `float`, `str`, `None`) and out-of-range values raise `InvalidQuantity`. Quantity 0 is not a delete; use `remove_item`.
- **Page limit.** `list_products(limit=...)` is 1–50 (`MAX_PAGE_SIZE = 50`); `offset` must be ≥ 0.
- **Payment event names.** `OrderStatusView.events` are oldest first. Order-status events use `OrderStatus` values; payment events use `PAYMENT_<PaymentStatus>` (`PAYMENT_PENDING`, `PAYMENT_PAID`, `PAYMENT_FAILED`, `PAYMENT_REFUNDED`), built by `payment_event()`.
- **`get_order`** returns `None` for an order number that does not exist; it never raises `NotFound`. An order owned by another customer raises `NotAuthorized`.
- **`NotFound` scope.** NotFound applies to products, variants, categories, customers, addresses and cart lines. It is never used for orders: get_order returns None for an unknown order number. This overrides Reference PDF §5.3.
- **`CheckoutRejected.reason` codes.** `product_unavailable`, `price_changed`, `address_not_serviceable`, `below_minimum_order`, `other` (default). Codes are safe and customer-agnostic.
- **`ValueError` for invalid arguments.** Programming errors, not customer errors, raise `ValueError`: unknown diet or language code, `limit`/`offset` out of range, empty `whatsapp_id`, address text or `idempotency_key`.
- **Quantity limit ownership.** The conversation layer never hardcodes or imports the quantity limit. It passes the customer's quantity to the port and relies on `InvalidQuantity`.
- **`CLARIFY` result.** The intent layer adds `CLARIFY` to the 15 intents in Reference PDF §8 (S4). It is produced only by the pipeline for low confidence and is never accepted from the LLM or a reply ID. This overrides the PDF's intent list.
- **Order numbers in the intent layer.** Extracted only when the intent is `ORDER_STATUS`, `PAYMENT_STATUS` or `TRACKING` (for `CLARIFY`, when one of these is suggested). The current pattern (letters followed by digits, e.g. `IVA1002`) is generic: order-number pattern to be tightened to the backend's real format.
- **Search with a diet.** `resolve_product_names` has no `diet` parameter, so the engine searches with `list_products(search=…, diet=…)` and uses `resolve_product_names` only for customers without a diet preference. The backend team may want to add `diet` to `resolve_product_names`.
- **Clearing a diet.** In `update_profile`, `None` means "unchanged", so a diet cannot be cleared once set. Customers can switch to `non_veg` (shows everything). Changing this needs a port change.
- **Payment link after checkout.** Only `CheckoutResult` carries `payment_url`; `OrderStatusView` does not. The engine keeps the link from `checkout` in the session to show it again. The backend team may want to add `payment_url` to `OrderStatusView`.
- **Checkout retries.** A `CheckoutRejected` checkout is assumed to create no order; the engine uses a new idempotency key for the next attempt. After `BackendUnavailable` (e.g. a lost response) it retries with the same key.
- **Pseudonym key.** WhatsApp IDs are pseudonymized with a dedicated `IVA_PSEUDONYM_KEY`, never `SECRET_KEY`.
  - Production requires it: at least 32 characters, and different from `DJANGO_SECRET_KEY`.
  - Dev and test fall back to a clearly labelled insecure key; the dev `.env` has a generated one.
  - **When integrating into the organization's project, carry this key over unchanged.**
  - **Rotating it resets all conversation records and sessions:** existing rows and Redis sessions can no longer be matched to customers, and every customer starts at the main menu (onboarding data lives in the backend and is kept).
- **Production WhatsApp configuration.** Production settings also require `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` and `WHATSAPP_API_VERSION`. There is no default API version: the team sets the version they have tested (tests use the placeholder `v21.0` and never call Meta).
- **Production session store.** Production settings require `CONVERSATION_REDIS_URL`, because the in-memory store is not shared between worker processes. This adds to the four required values in Reference PDF §4.3.
- **"menu" global intent.** The PDF lists "menu" as a global intent but has no MENU intent. "menu" maps to `CATEGORY_SELECT` with `category_query = null`, meaning "show the top-level groups".
- **AI Planner picks have no variant-choice step (2026-09-28).** `resolve_product_names(name, limit=1)` is called once per pick; a match with more than one variant uses the first in-stock one automatically — "Add all to cart" never asks the customer to choose, unlike the normal add-to-cart flow. The displayed price/name for a pick always come from that resolved `ProductSummary`/variant, never from the plan message's text.
- **Contact profile name reverses a prior decision (2026-09-28).** `apps/whatsapp/parser.py` previously read: "Contact profile names in the payload are deliberately not read." This is now read (`contacts[].profile.name`, matched by `wa_id`), sanitized, and saved via `update_profile(name=...)` only when the customer has no saved name yet — it is never logged, and never overwrites an existing name.
- **Product image URL (2026-09-29).** `ProductSummary`/`ProductDetail` gained `image_url: str | None`. It must be an `https://` URL or `None`; the channel never guesses or constructs one. The Fake Backend builds it by joining `IVA_PRODUCT_IMAGE_BASE_URL` (settings, or the `image_base_url` constructor argument; unset or non-https means no images) with the fixture's per-product relative `image_path` field. `image_path` values were derived by reusing each product's own `id` — already a slug (lowercase, spaces → hyphens, with `&`, commas and apostrophes dropped rather than replaced) — under an `eat/` folder, e.g. `seed-mix` → `eat/seed-mix.jpg`, matching the one example given ("Seed Mix" → `eat/seed-mix.jpg`) exactly. Each of the 55 candidates was checked with a single HEAD request (no retries): 45 returned `200` with an `image/*` content type and a size well under Meta's 5 MB image cap; the other 10 were fixed 2026-10-01 (Open request #6, closed), so **all 55 products now have an `image_path`** (each of the 10 verified once by HEAD: `200`, `image/jpeg`, 24-331 KB).
- **WhatsApp media carousel research (2026-09-29).** Checked whether a non-template, non-catalog interactive media carousel exists on the Cloud API (so product cards could be a real swipeable carousel instead of separate messages). It does: Meta's **Interactive Media Carousel** (`interactive`/`type: carousel`) is a free-form session message, not a template and not tied to a Commerce Catalog — confirmed via two independent fetches of `developers.facebook.com/docs/whatsapp/cloud-api/messages/interactive-media-carousel-messages/`. Limits: 2–10 cards, card body ≤160 characters (≤2 line breaks), main body ≤1024 characters, button labels ≤20 characters, one URL button or 1+ quick-reply buttons per card with the same type/count on every card, image/video-only card headers, no top-level header/footer/buttons. Approved and built 2026-10-01 for category pages (see the carousel entry below); the option A cards remain as the fallback.
- **Product cards vs. the "which one do you mean" list (2026-09-29).** Browsing a category or a plain product search now sends one `OutProductCard` (image header if available, name + price, "Add to cart"/"Order now") per product, `CARD_PAGE_SIZE = 4` per page, paginated the same way as before ("Show more"/Menu). The pre-existing compact `OutList` "which one do you mean" step is **kept, unchanged**, for the one case where a quantity is already known or being collected (typed "add 2 oats" resolving to several matches): switching that step to cards too would silently drop the requested quantity, since a card's quick actions always add 1. `Session.listing["mode"]` (`"cards"` or `"list"`) records which shape produced the current page so "Show more" (`ACT:more`) keeps rendering the same shape across pages. This is a judgment call made without a stop for approval, consistent with "keep the diet filter behaviour" and existing tested quantity handling — flag if a different split was intended.
- **Typed search results are a list; category browsing keeps cards (2026-09-30, supersedes the plain-search half of the entry above).** A typed product search with 2+ matches sends one compact `OutList` (mode `"results"`): name + price (and an out-of-stock tag) taken from the port, up to 10 rows, or 9 products plus a "Show more" row when more remain (`ACT:more`; the next page starts after the rows actually shown, `listing["shown"]`). Tapping a row sends `ACT:showcard:<product>`, which calls `get_product` fresh and then sends that product's existing card ("Add to cart"/"Order now"); if the product is gone the customer gets the "no longer available" message, if it is out of stock the "out of stock" message instead of a card. A typed search with exactly one match (2026-09-30) sends that product's card directly (same `_show_card` re-check: out of stock → out-of-stock message, missing → "no longer available" and the main menu; a diet-hidden lone match → the "hidden" notice, and "Show anyway" then sends the card). A typed request that carries a quantity ("add 2 quinoa") keeps the detail + quantity path, because a card always adds 1. Category browsing uses the carousel when possible (see the carousel entry below), else cards. The "add N of X" disambiguation list (mode `"list"`) is unchanged, and so is the diet filter (a "hidden some/all" notice is always shown).
- **Website menu link (2026-09-30).** New setting `IVA_WEBSITE_SHOP_URL` (https only; unset or invalid hides the option). When valid, the main menu gets a "See full menu on website" row (`ACT:website`) that sends a `cta_url` message, the same pattern as "Plan my meals". The menu is built from 5 fixed rows + up to 2 link rows + as many category groups as still fit within WhatsApp's 10-row list limit (`MENU_FIXED_ROWS`, `MAX_MENU_GROUPS = 4`); with both links configured at most 3 groups are listed. The fixture has 2 groups, so nothing is dropped today.
- **Category pages are a media carousel (2026-10-01, approved option B).** A category page with 2-10 products that all have an `https` image is sent as ONE `OutCarousel` (`channel.py`; rendered as Meta's `interactive`/`type: carousel`): a short localised body (`carousel.body`, e.g. "Here are our Café Favourites"; te/hi need native review like all te/hi text), then per card an image header, a body of name + price (+ out-of-stock tag) taken from the port (<=160 chars, <=2 line breaks, cut by the renderer), and the same two quick-reply buttons on every card ("Add to cart" `ACT:addcard:<ref>`, "Order now" `ACT:ordernow:<ref>` -- the existing card handlers, so every tap re-checks via `get_product`, and stale/out-of-stock handling and session-expiry safety are unchanged). Carousels have no top-level buttons, so each is followed by the existing `cards.footer` reply-buttons message: "Show more" (only if more products exist) and "Menu". The card JSON follows Meta's documented shape (card `type` is `cta_url` even for quick-reply cards, per Meta's doc, fetched 2026-10-01; it has not yet been exercised against a real Meta number -- see the README checklist). Category pages now fetch `CAROUSEL_PAGE_SIZE = 10`. **Fallbacks, never dropping a product:** (1) if the page has fewer than 2 products or any product lacks an image, the page is sent as the previous one-card-per-product shape, trimmed to `CARD_PAGE_SIZE = 4` (`listing["shown"]` records how many were shown so "Show more" continues after them); (2) if WhatsApp rejects the carousel send, `apps/whatsapp/delivery.py::send_replies` replaces it in place with `OutCarousel.fallback` (the intro text + one `OutProductCard` for every product on the page) and the footer still follows. Typed search keeps the list (a single match keeps the card), the "add N of X" path is unchanged, and the diet filter is unchanged (the hidden-items notice follows the footer; "Show anyway" re-lists everything). No Backend Port change and no extra Redis calls.
- **Follow-up ordering after a carousel or image card (2026-10-01).** Live testing showed the "Show more / Menu" message arriving BEFORE the carousel on WhatsApp Desktop and Android. Investigation: sends were already strictly sequential (`delivery.send_replies` sends the next reply only after the previous send call returned successfully), so the cause is not our send order: the Cloud API acknowledges a send once it is validated and then prepares the message (fetching up to 10 images for a carousel) asynchronously, so a light buttons message sent straight afterwards overtakes it. The recorded status callbacks could not be compared: the database held no outbound carousel/footer rows from the live test (it ran against a different database), and `ConversationMessage` keeps only the latest status per message anyway, not a history. **Fix:** a buttons message (`OutButtons`) that follows a carousel, or an image `OutProductCard`, is held until that large message's WhatsApp status callback has arrived (`apps/whatsapp/ordering.py`): the webhook records every `sent`/`delivered`/`read` callback in `settings.CACHES` (Redis in production) under a pseudonym of the message ID, and the worker polls it every 0.5 s, up to `IVA_ORDER_WAIT_SECONDS` (default 5; 0 turns it off), then sends the follow-up regardless -- a timeout only delays it, it is never lost. `IVA_ORDER_WAIT_STATUS` (default `sent`, as requested; `delivered` is stricter but slower) picks the status that ends the wait; later statuses also satisfy it, since callbacks can arrive out of order. Why a cache signal rather than the database row: the outbound row is only written after all replies are sent, and the signal must exist even if the callback beats the row. It applies in `send_replies` itself, so it covers the carousel, the carousel's fallback cards (one wait, after the last card, before the footer), single image cards, and the diet-notice/other buttons messages that follow them; text-only cards, replies to non-buttons messages, a large message that failed to send, and a large message that is last in the turn never wait. Every failure fails open (cache error: no wait). Costs: about 2-10 extra Redis commands per category page (the polling GETs, each 0.5 s) plus one `SET` per status callback; the per-customer lock is held for the wait (max 5 s, well inside its TTL). In inline mode the callback is handled by a concurrent webhook request, so the dev server must be threaded (Django's runserver is). Not yet verified on a real device: whether `sent` is early enough to fix the live ordering -- if the footer still overtakes, set `IVA_ORDER_WAIT_STATUS=delivered`.
- **Greetings and commands at the address step (2026-10-01).** Live testing: a customer who typed "Hi" at the delivery-address prompt had it taken as an address ("Please type your complete address..."). At `State.CHECKOUT_ADDRESS` the engine now checks whether the WHOLE trimmed, case/punctuation-folded message (`ai.text.normalize`) is exactly a greeting or a global command, in en/te/hi and romanized forms (`GREETING_WORDS`, `CART_WORDS`, `ORDER_STATUS_WORDS`, `HELP_WORDS`, `LANGUAGE_WORDS` in `messages/*.py`, plus the existing `ESCAPE_WORDS` menu/cancel/stop/back), before saving it as an address. Exact match only, so "Cart Road, Kondapur, Hyderabad" or "Hi, Flat 12, ..." is still an address. A greeting gets "Welcome back, <name>! You were in the middle of checkout." (`checkout.welcome_back`) with "Continue checkout" (`ACT:resume`, which re-runs `_start_checkout`: saved-address list, or the type-your-address prompt) and "Menu"; cart and checkout state are kept, nothing is saved as an address. The other commands run their normal handler (cart, order status, help, language prompt; menu/cancel to the menu). te/hi wording needs native review. Only the address step is affected.
- **Product card quick actions (2026-09-29).** A card's two buttons encode only the **product** ID (`ACT:addcard:<id>` / `ACT:ordernow:<id>`), never a variant ID: tapping always calls `get_product(id)` fresh (the required re-check), then adds the first in-stock variant (or the first variant at all, if none are in stock, so `add_item` itself raises `ProductUnavailable`) with quantity 1. `NotFound`/`ProductUnavailable` from that re-check are caught locally and answered with "no longer available" plus the same category/search page shown again (`Session.listing`), not just a bare Menu button; with no listing to reopen (e.g. after session expiry) it falls back to the main menu — it never crashes either way. "Add to cart" replies with a short confirmation and "View cart"/"Continue shopping" (`ACT:continue`) instead of the full cart dump the text/list add flow shows, since the customer is mid-browse. "Continue shopping" never re-sends the product cards already visible in the chat (a live-testing fix, 2026-09-29) — it goes up one level, to the category list of the group being browsed (`_continue_shopping`, resolved from `Session.listing`'s category via `_all_categories()`); with no group to resolve (e.g. after session expiry, or a listing with no category context) it falls back to the main menu. "View cart" is unchanged. "Order now" adds the item then calls the existing `_start_checkout` flow unchanged, so the order summary the customer sees next already includes the whole cart, not just the one item. A card image that fails to send is retried exactly once, immediately, with the header stripped (`apps/whatsapp/delivery.py::send_replies`) — the product is never silently dropped from the page. No extra Redis calls: `handle()` still loads and saves the session exactly once per turn regardless of how many card messages that turn produces, so a page of 4 cards costs the same 2 Redis commands (one `GET`, one `SET`) as any other single turn.
- **A plan message mid free-text step is a known limitation (2026-09-28).** `ConversationEngine._dispatch` intercepts free-typed text for two states — `ONBOARD_DIET` (changing the food preference) and `CHECKOUT_ADDRESS` (typing a delivery address) — before the intent layer ever runs. If a customer happens to paste an "IVA PLAN v1" message while in one of those states, it is taken as their diet answer or address, not as a plan. This is a pre-existing pattern (any free text in those states was already consumed the same way) and considered acceptable: the overlap is unlikely, and the customer can simply resend the plan message once done with that step.

### Open contract requests for the backend team

Changes we are asking the backend team to confirm or make. Until then, the channel works around each one as described.

| # | Request | Why | Current workaround |
|---|---|---|---|
| 1 | A way to **clear the diet preference** in `update_profile` (e.g. an explicit "none" value) | `None` means "unchanged", so a preference cannot be removed once set | The customer can switch to `non_veg`, which shows everything |
| 2 | **`payment_url` on `OrderStatusView`** while payment is `PENDING` or `FAILED` | Only `checkout` returns the link, so it is lost once the session expires | The engine keeps the link from `checkout` in the session |
| 3 | The **real order-number format** | The channel recognises typed order numbers with a generic "letters + digits" pattern | Pattern accepts e.g. `IVA1002`; it will be tightened to the real format |
| 4 | The **real quantity limit** per cart line | The Fake Backend uses 1–20 | The channel never hardcodes a limit and relies on `InvalidQuantity` |
| 5 | An optional **`diet` filter on `resolve_product_names`** | Fuzzy name matching cannot respect the saved diet today | Used only for customers without a diet; others use `list_products(search=…, diet=…)` |
| 6 | **CLOSED 2026-10-01** -- correct image file names supplied and stored for these 10 products (they had 404'd under `eat/<product-id>.jpg`): `ivar-oats`, `ivar-active-nutrition-drinks`, `millet-seed-crackers`, `ivar-muesli-bowl`, `fruits-nuts-seeds-bowl`, `morning-boost-shot`, `night-reset-shot`, `immunity-shield-shot`, `digest-ease-shot`, `breath-fresh-shot` | Every other product's image path matches its own `id` exactly; these 10 apparently don't | `image_path` is `null` for these products; they render as text-only cards (never hidden) until given the real file name |
| 7 | **`get_order_detail(customer_id, order_number) -> OrderDetail \| None`** (new `OrderPort` method) with `OrderLine`/`OrderDetail` (lines at order-time names and prices, totals, `placed_at`; no address), and optional `placed_at`, `total`, `currency` on `OrderStatusView`. `NotAuthorized` for another customer's order; `None` for an unknown order number | Order history and one-tap reorder need an order's lines and what was paid; today `OrderStatusView` has neither | None yet: the channel does not use these. The Fake Backend implements them for the contract tests |
| 8 | **`get_customer(customer_id) -> CustomerRef \| None`** (new `CustomerPort` method; never creates a customer) | `get_or_create_customer` needs the WhatsApp ID, so a customer cannot be looked up by backend ID | None yet: the channel does not use it |

## 6. Conversation engine

**States**

```text
NEW → LANGUAGE → MAIN_MENU → GROUP → CATEGORY → PRODUCT → QUANTITY → CART
    → CHECKOUT_ADDRESS → CHECKOUT_CONFIRM → AWAITING_PAYMENT
```

Onboarding is language-only (2026-09-28): name comes from the WhatsApp contact profile,
not a question, and city is no longer collected at all. `ONBOARD_DIET` is not an
onboarding step; it is reused later, from the main menu's "Change preference", to
change the food preference (`ACT:diet`).

**The very first reply greets by name (2026-09-29, live-testing fix).** The language
prompt a brand-new customer sees first (`_language_prompt(c, greet=True)`, keys
`language.prompt_greeting_named`/`_unnamed`) opens with "Hi {name}! 👋 Welcome to
Ivar!" when a WhatsApp profile name was captured on that message (or an earlier one),
or a name-free "Welcome to Ivar!" otherwise — language is unknown at this point, so the
line is language-neutral apart from the name and always rendered in English. `greet` is
true only when the session has not yet moved into `State.LANGUAGE` (i.e. the actual
first prompt, including the effectively-first prompt after a session expired before a
language was chosen); a re-prompt after unclear text in the same still-alive session
uses the plain, ungreeted `language.prompt` key, as does the unrelated "change
language" flow from the main menu (`Intent.LANGUAGE_SELECT`, always ungreeted). The
planner message shown right after language selection (`welcome.planner_intro`) no
longer repeats the greeting — it used to (`welcome.new_named`/`welcome.new_unnamed`,
now removed) — since the name greeting already happened. The name-saving rules are
unchanged: only when no name is already saved, never overwritten, never logged.

**Global intents** work from any state: order status, payment status, view cart, help, change language, menu.

**Menu structure** (from the Backend Port, never hardcoded):

```text
Fresh & Ready to Eat          Packaged Foods
  Café Favourites               Breakfast & Grains
  Protein Bowls                 Protein & Active Nutrition
  Non-Veg Snacks                Nuts, Seeds & Snacks
  Wellness Shots                Cooking Essentials
                                Teas & Beverages
                                Ready Mixes
                                Energy Bites & Cookies
```

A two-level menu is needed because 11 categories exceed WhatsApp's 10-row list limit. Customers can always type instead of tapping.

**Session state** lives in Redis (`CONVERSATION_REDIS_URL`) with a TTL. An expired session resumes at the main menu. Returning customers skip onboarding. A set diet preference filters product lists.

**Language:** English, Telugu, Hindi. Reply text lives in `messages/en.py`, `te.py`, `hi.py`. Telugu and Hindi need native-speaker review before launch.

### Engine implementation (S3)

**Interface.** `ConversationEngine.handle(whatsapp_id, Inbound) -> list[Outbound]` (`apps/conversations/engine.py`). `get_engine()` builds a shared engine from settings: Backend Port adapter, session store and intent pipeline.
- `Inbound` holds typed `text`, or a tapped `reply_id` plus `reply_title`.
- `Outbound` is channel-neutral (`channel.py`): `OutText`, `OutButtons` (1–3 buttons), `OutList` (1–10 rows), `OutCTA` (2026-09-28: one tappable link button, WhatsApp's `cta_url` interactive type — used only for the AI Planner link, since `https://` is enforced in `__post_init__`), `OutCarousel` (2026-10-01: 2-10 `CarouselCard`s plus an ordinary-message `fallback`), or `OutProductCard` (2026-09-29: one product — optional image header, body, 1–3 reply buttons; see Contract decisions for how category/search browsing uses it). Each carries a `key` naming its message template, for tests and logs; the customer never sees it.
- `renderers.render()` turns them into WhatsApp Cloud API message objects and truncates to WhatsApp's field limits: text 4096 characters, interactive body 1024, button title 20, row title 24, row description 72, list button 20.
- S3 makes no WhatsApp API calls and defines no database models. The `Conversation`/`ConversationMessage` models come in S2.

**Session** (`state.py`). A small JSON document holds only navigation state:
- the current step and list page;
- a pending choice (e.g. waiting for a quantity);
- the checkout address and `idempotency_key`;
- the last order number and payment link;
- short tokens for backend IDs that do not fit a reply ID.

Profile, cart and orders are always read from the port.
- `RedisSessionStore` uses `CONVERSATION_REDIS_URL`. Every write sets a TTL of `CONVERSATION_SESSION_TTL_SECONDS` (default 3600).
- `InMemorySessionStore` (same TTL behaviour) is for tests and single-process development.
- Keys are `iva:conv:session:<sha256(whatsapp_id)>`, so no phone number is ever a Redis key.
- Unreadable, expired or unreachable sessions count as "no session".
- A failed turn (backend down, error) is not saved, so the previous state is kept.

**Turn flow**
1. `get_or_create_customer`, then load the session.
2. If onboarding is incomplete: language → name → city → diet.
   - Name, city and address steps take free text and do not go through the intent layer.
   - Diet is one list message with four rows: Veg, Veg + Egg, Non-veg, Skip.
   - The diet can be changed later from the main menu. There, Skip keeps the current value.
3. Engine-owned reply IDs start with `ACT:` (diet, "More…", address choice, confirm/cancel, edit cart) and are handled directly.
4. Everything else goes to `apps.ai`, with the catalog's product and category names passed as hints (cached for 5 minutes, read through the port).
5. A missing or expired session starts at the main menu. A greeting or an unclear message there gets "welcome back" plus the menu; a clear request is answered directly. New customers get onboarding.

**Entity resolution** (only through the Backend Port)

| Input | What the engine does |
|---|---|
| `product_query` | `list_products(search=…, diet=<saved diet>)`. If nothing matches, each word is tried, longest first. If still nothing and the customer has no diet, `resolve_product_names` is tried (it has no diet filter). 0 matches → "couldn't find" plus a Menu button. 1 → product detail. More than 1 → a list to choose from; a pending quantity is kept for the chosen product |
| `category_query` | Matched against the names from `list_categories` (accents and plurals ignored). 0 → "couldn't find". 1 → open that group or category. More than 1 (e.g. "snacks") → a list to choose from |
| `CATEGORY_SELECT` with no category | Main menu: groups from the port, plus cart, order, help, language and diet rows |
| `choice_index` | Taken from the current list page |
| Cart lines (remove / update) | Matched by name against `get_cart` |

**Lists.** 9 items per page plus a "More…" row. **Typed search results:** one list, 10 rows if they fit, else 9 + "More…" (row tap → product card). **Category pages:** up to 10 products as one carousel (`CAROUSEL_PAGE_SIZE`) when 2+ products all have images, otherwise **product cards**: 4 per page (`CARD_PAGE_SIZE`), followed by a "Show more"/Menu message; see Contract decisions for when cards vs. the compact list is used. The saved diet is passed to every `list_products` call either way.

**Quantities.** Always passed to the port. `InvalidQuantity` → "that quantity isn't possible", and the engine keeps waiting for a number. No limit appears in engine code; a test enforces this.

**Checkout**
1. Choose a saved address or type a new one (`add_address`).
2. On entering `CHECKOUT_CONFIRM`, a `uuid4` idempotency key is created once and stored in the session. Every confirm tap or retry reuses it.
3. The key is cleared when the cart changes or the port rejects the checkout.
4. The payment link and payment status are shown exactly as `checkout` returns them. A missing link is reported as "not available yet".
5. Payment is called received only when the port returns `PAID`.

**Rejections and errors**

| Event | Reply |
|---|---|
| `CheckoutRejected: price_changed` | Re-show the cart with current prices from `get_cart` |
| `CheckoutRejected: product_unavailable` / `below_minimum_order` / other | A message, then the cart |
| `CheckoutRejected: address_not_serviceable` | Asks for another address |
| `BackendUnavailable` | "Please try again"; session kept |
| `NotFound` / `NotAuthorized` | "Couldn't find that" (another customer's order is never revealed) |
| `ProductUnavailable` | "Out of stock" |
| `CartEmpty` | "Cart is empty" |
| `ValueError` from the port | A bug: logged by type only, customer gets the generic message |

`handle()` never raises.

**Order status / tracking.** Shows the order status, payment status, ETA (formatted `DD-MM-YYYY HH:MM` in `Asia/Kolkata`) and tracking ID exactly as `get_order`/`get_latest_order` return them. A missing ETA or tracking ID is reported as "not available yet".

**Language.** Replies always use the saved language. The detected message language never changes it; only an explicit language request does.

**The diet filter is never silent.** Whenever the saved diet hides items, the customer is told, with the buttons **Show anyway**, **Change preference** and **Menu**. The engine compares the filtered total with an unfiltered count from the same `list_products` call made with `diet=None`.
- If a search or category has matches only outside the diet, the reply says so instead of "no results".
- If some items are hidden, the list is followed by a note with the hidden count.
- **Show anyway** lists page 1 again with no diet filter. The saved preference is not changed.

**Logging.** Only masked WhatsApp IDs, state names, intents, stages and error types are logged. Customer free text is never logged: not names, cities, addresses or raw messages. `apps/conversations/tests/test_logging_privacy.py` runs a whole conversation, including failure paths, and checks every log record.

## 7. Intent layer

```text
"IVA PLAN v1" header ─────────────────────────────────────────────► PLAN_RECEIVED
Button/List reply ID ─┐
                      ├─► Rule / alias / fuzzy match ─► LLM fallback ─► Structured intent
Free text ────────────┘                                                     │
                                                                     Schema validation
                                                                            │
                                                               Engine → Backend Port
```

1. **The "IVA PLAN v1" header** (`apps/ai/plan_message.py`) is checked first, ahead of everything else, including the reply-ID stage — a deterministic parser, never the LLM or rules. See "AI Planner handoff" below.
2. **Button and list replies** map directly to intents. No AI.
3. **Rules, aliases and fuzzy matching** cover English, Telugu, Hindi, romanized and mixed input (e.g. "naaku snacks kavali", "mujhe protein chahiye"). Standard library first.
4. **LLM fallback** only for what's left. Provider-agnostic client, timeout, strict JSON output.
5. **Schema validation** rejects anything malformed. Low confidence → clarifying question.
6. **Failure mode:** if the LLM is down, stages 2–3 keep the flow working.

Intents: GREETING, LANGUAGE_SELECT, PRODUCT_SEARCH, CATEGORY_SELECT, PRODUCT_SELECT, ADD_TO_CART, REMOVE_FROM_CART, UPDATE_QUANTITY, VIEW_CART, CHECKOUT, ORDER_STATUS, PAYMENT_STATUS, TRACKING, HELP, FALLBACK, PLAN_RECEIVED (deterministic-only, see below).

The LLM never writes data and never decides price, stock, payment status, order status or availability. Product names it returns are always resolved through the Backend Port.

### Intent layer: output schema

`apps/ai` imports only `apps.iva_common`; never `apps.conversations`, `apps.whatsapp`, `config` or `host`. It returns raw entity text; the conversation engine (S3) resolves it through the Backend Port.

```python
from apps.ai.pipeline import pipeline_from_settings

pipeline = pipeline_from_settings()
result = pipeline.understand(
    text,  # customer message, or None
    reply_id=None,  # button/list reply ID, if any
    product_names=(...),  # optional plain-string catalog hints
    category_names=(...),  # optional; the pipeline works without hints
)
```

`understand()` never raises. Any internal failure returns `FALLBACK`.

**`IntentResult`**

| Field | Type | Meaning |
|---|---|---|
| `intent` | `Intent` | One of the 15 intents, or `CLARIFY` |
| `confidence` | `float` 0–1 | 1.0 for reply IDs |
| `stage` | `button` \| `rules` \| `fuzzy` \| `llm` \| `none` | The stage that decided |
| `entities` | `Entities` | Fields irrelevant to the intent are `null`; `FALLBACK` and `CLARIFY` keep all |
| `detected_language` | `en` \| `te` \| `hi` \| `null` | Best guess only; never overrides the customer's saved language |
| `suggested_intent` | `Intent` \| `null` | Set only with `CLARIFY` |
| `llm_outcome` | `not_called` \| `not_configured` \| `ok` \| `low_confidence` \| `timeout` \| `error` \| `invalid_json` \| `invalid_schema` | What happened at the LLM stage |

**`Entities`** (no price, stock, product ID, order status or payment status field exists)

| Field | Type | Source |
|---|---|---|
| `product_query` | `str` | Customer's product words. Native words mapped to English ("मखाना" → "makhana"), typos corrected, or the exact catalog hint name when one matches |
| `category_query` | `str` | Built-in key (`snacks`, `breakfast`, `bowls`, `beverages`, `cookies`, `shots`, `cooking essentials`, `ready mixes`, `energy bites`, `fresh`, `packaged`, `non veg`, `cafe`) or a matched category hint name |
| `quantity` | `int` | Digits in any script, or number words in English, Telugu and Hindi. No maximum: the Backend Port enforces limits |
| `unit` | `packet` \| `piece` \| `box` \| `bottle` \| `unit` | Word after the number. Pack sizes such as "500 g" are not quantities |
| `language_choice` | `en` \| `te` \| `hi` | `LANGUAGE_SELECT` only |
| `order_number` | `str` | Letters followed by digits, e.g. `IVA1002`. Only for order intents (see Contract decisions) |
| `choice_index` | `int` ≥ 1 | "second one", "option 2", "రెండవది", "दूसरा" |
| `reference_id` | `str` | Reply IDs only: echoes an ID the engine put in the button or list row |

**Entities kept per intent:** `LANGUAGE_SELECT` → `language_choice`; `PRODUCT_SEARCH` → `product_query`, `category_query`; `CATEGORY_SELECT` → `category_query`; `PRODUCT_SELECT` → `product_query`, `choice_index`; `ADD_TO_CART` → `product_query`, `quantity`, `unit`; `REMOVE_FROM_CART` → `product_query`, `choice_index`; `UPDATE_QUANTITY` → `product_query`, `quantity`, `unit`, `choice_index`; `ORDER_STATUS`, `PAYMENT_STATUS`, `TRACKING` → `order_number`; `PLAN_RECEIVED` → `goal`, `diet`, `calories`, `protein_g`, `picks`; others → none. `reference_id` is kept whenever a reply ID set it.

### AI Planner handoff (`PLAN_RECEIVED`)

`apps/ai/plan_message.py::parse_plan_message(text)` recognises a message whose first
line is exactly `IVA PLAN v1` and is no longer than its own length cap (`PLAN_MAX_LENGTH`,
independent of the pipeline's general `MAX_TEXT_LENGTH`); anything else (missing header,
oversized) is `None` and falls through to the normal pipeline as ordinary text. On a
match it returns an `Entities` with only these fields, each individually validated —
unknown/invalid values are dropped, never guessed, and nothing else is ever populated:

| Key | Accepted values |
|---|---|
| `goal` | `healthy_eating` only; any other value (or absence) → `None` ("no goal") |
| `diet` | `veg` \| `egg` \| `non_veg` |
| `calories` | integer, `800`–`6000` |
| `protein_g` | integer, `10`–`400` |
| `picks` | `"; "`-separated product names, trimmed, capped at 10 |

`bmi`, `bmr`, `tdee`, `healthy_weight_range` and `nutrition_score` are never accepted:
they are simply not recognised keys, so any line naming them is dropped like any other
unknown key. `Intent.PLAN_RECEIVED` is excluded from `CLASSIFIABLE_INTENTS`, so it can
never be produced by the LLM or carried in a button/list reply ID — only this parser can
produce it.

The engine (`ConversationEngine._plan_received`) resolves each pick through
`resolve_product_names`/`get_product` (never the message text) and shows only the
port's name and price; unresolved and out-of-stock picks are mentioned, not silently
dropped. "Add all to cart" adds each resolved pick with quantity 1 via `add_item`,
re-checking stock at that moment too. If `diet` is present it is saved via
`update_profile`. No BMI/BMR/TDEE/goal computation happens anywhere in this channel —
only the customer's own stated targets are restated. Nothing from the plan message is
ever logged or stored in the database (`apps/whatsapp/services.py::record_inbound`
already only stores `text_length`, never message text, for every message).

**Reply IDs** (`build_reply_id` / `parse_reply_id`, max 200 characters): `INTENT`, `INTENT:ref` or `INTENT:ref:quantity`. `LANGUAGE_SELECT:te` sets `language_choice`; `ORDER_STATUS|PAYMENT_STATUS|TRACKING:<order no>` sets `order_number`; `ADD_TO_CART|UPDATE_QUANTITY:<ref>:<qty>` sets `reference_id` and `quantity`; any other `INTENT:<ref>` sets `reference_id`. A `ref` is 1–64 characters of `A–Z a–z 0–9 _ . -`. `FALLBACK` and `CLARIFY` are not valid. An invalid reply ID is ignored and the text is classified instead.

**How each stage decides**

1. **Reply ID** valid → `button`, confidence 1.0.
2. **Rules** apply a fixed priority: language change → tracking → checkout → payment → order status → remove → update quantity → add → view cart → select → product search / category → help → menu → greeting. A keyword decision scores 0.9. A decision without a keyword (a bare product name, "2 makhana") scores 0.8.
3. **Fuzzy** (`difflib`) corrects unknown Latin words of 4+ letters at a ratio of at least 0.85 ("makana", "protien", "chekout"). It also matches whole phrases against catalog hints at 0.8 or above. If a correction produced the result, the stage is `fuzzy`.
4. **Threshold:** 0.7 by default. Anything below goes to the LLM.
5. **LLM:** phone numbers in the text are masked before sending, and hints are included. The call runs in a worker thread with `LLM_TIMEOUT_SECONDS` (default 5). The output is validated with the standard library only: exact keys, a known intent other than `CLARIFY`, confidence 0–1, typed entities, and no missing, extra or `reference_id` fields. Below the threshold → `CLARIFY` with `suggested_intent`.
6. **No LLM, or LLM timeout / error / invalid output:** a low-confidence rules candidate ("naaku kavali", a bare "order") becomes `CLARIFY` with `suggested_intent`. Otherwise the result is `FALLBACK`. The layer never guesses. Logs record the outcome and error type only, never customer text.

**LLM provider:** not chosen yet. An empty `LLM_PROVIDER` means no LLM (rules and fuzzy only). Any other value raises `ImproperlyConfigured` until an adapter is registered in `apps.ai.llm.PROVIDERS`. `FakeLLMClient` is for tests and development only and cannot be selected through settings.

**For the engine (S3):**
- Free-text states (changing the diet preference, typing an address) should bypass the pipeline.
- Resolve `product_query`, `category_query` and `choice_index` through the Backend Port.
- Ask a question on `CLARIFY`.
- In the QUANTITY state, a bare number arrives as `FALLBACK` with `quantity` set.
- Never change the saved language from `detected_language` alone.

### Native WhatsApp catalog orders (2026-10-06)

A customer who orders from WhatsApp's native catalog cart sends a message of type
`order` (`order.catalog_id`, optional `order.text`, `order.product_items[]` with
`product_retailer_id`, `quantity`, `item_price`, `currency`). `parser.py` normalises it into
`InboundMessage.order` (tolerant of extra/missing fields; a malformed order simply has no
items) and it travels to the engine as `Inbound.order`. `ConversationEngine._catalog_order`
(+ `catalog_order.validate_order`) then, through the Backend Port only: drops unknown,
out-of-stock and invalid-quantity items (customer told which), merges duplicate lines,
clamps a product to `IVA_CATALOG_ORDER_MAX_QTY` (default 10, never above the port's
`MAX_LINE_QUANTITY`), and prices everything from the port. `item_price`/`currency` are never
used as prices, only compared so the customer is told when a price differs. The order is
the customer's authoritative selection, so the IVA cart is REPLACED (existing lines updated
in place, new ones added, the rest removed; never emptied by a partial failure). The normal
cart summary follows, with its Checkout button, so address / confirmation / payment are the
existing state machine. All excluded: cart untouched, offer to browse. If `IVA_CATALOG_ID`
is set, an order from another catalog is ignored with a safe reply. A customer who has not
finished onboarding is asked to finish setup and resend. Redelivery is deduplicated by the
existing WhatsApp message-id unique constraint plus the processing claim. Logs carry counts
and product IDs only. No Backend Port change: a catalog item's `product_retailer_id` is the
port product ID; the default in-stock variant is used, as for product cards.

`manage.py replay_order slug:qty[:price] ... --to <wa_id>` (dev only; refuses unless
`IVA_DEV_RESET_ALLOWED`) builds an order payload, signs it with `WHATSAPP_APP_SECRET` and
posts it to the real webhook view, so signature check, dedupe and inline processing all run.

**Blocked:** linking the live catalog to the WhatsApp number needs the catalog-enabled test
number, which does not exist yet. Everything above is verified only against simulated
payloads. Meta's reference: https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages/order/

### LLM fallback (Groq) (2026-10-06)

Off by default (`IVA_LLM_ENABLED=false`). Pipeline: reply ID -> rules -> fuzzy -> **LLM
fallback** -> engine. The rules and fuzzy stages are unchanged and always run first; the LLM
is asked only when they are not confident AND the engine allows it for this message
(`ConversationEngine._llm_context`): free text, onboarded customer, a state in
`_LLM_STATES` (new / main menu / browsing / cart), no pending question, a non-empty catalog.
Never for button/list taps, language or diet selection, address entry, order confirmation,
payment, catalog orders or "IVA PLAN v1" messages.

`apps/ai/classifier.py` defines the provider-neutral `IntentClassifier` interface plus
redaction (phones, emails, 6+ digit runs masked), the prompt (message is DATA, never
instructions; only the EXISTING intent enum, `unknown`, and a compact `slug|name|category`
allow-list from the Backend Port are sent) and strict validation. `apps/ai/groq.py` is the
first adapter (project HTTP helper, no SDK). `apps/ai/llm_fallback.py` runs guard -> call ->
validate and returns an outcome category; `apps/ai/llm_guard.py` holds the circuit breaker,
per-customer limit and Redis-backed daily cap (counters fail closed). Validation after the
model: unknown intent -> `unknown`; slugs not in the allow-list dropped; quantity outside
`1..MAX_LINE_QUANTITY` -> null; schema violations reject the whole reply. Low confidence
(< `IVA_LLM_MIN_CONFIDENCE`) -> clarifying question; `ambiguous` with a product mention ->
read-only search results; `unknown`/failure -> the existing no-match reply (silent).

Cart safety: an LLM-derived add/remove/update never changes the cart directly. The engine
re-checks each slug through the port, stores a pending `llm_confirm` in the session, and asks
"Add 2 x Classic Oats Bowl?" with Yes / No (`ACT:llmyes` / `ACT:llmno`); only Yes applies it,
and any other message cancels it. Read-only intents use the normal handlers.

Privacy: only the redacted message, the catalog allow-list and the intent list leave the
system. Logs carry outcome, intent, confidence, latency, model, token counts and product
slugs only; never customer text, never the key. Settings: `IVA_LLM_ENABLED`, `GROQ_API_KEY`
(environment only), `IVA_LLM_MODEL` (default `openai/gpt-oss-20b`), `IVA_LLM_TIMEOUT_S` (4),
`IVA_LLM_MIN_CONFIDENCE` (0.7), `IVA_LLM_DAILY_CAP` (500), `IVA_LLM_CUSTOMER_PER_MINUTE` (3),
`IVA_LLM_BREAKER_FAILURES` (5), `IVA_LLM_BREAKER_COOLDOWN_S` (120). The circuit breaker is
per process. The older `LLM_PROVIDER` / `apps.ai.llm.LLMClient` path is untouched.

**Eval harness (2026-10-06):** `eval_intent_llm` works on both golden sets (`--set intents|recommend`). Besides accuracy it records, per entry, the model's raw answer, what our validation kept/dropped, and the FINAL ENGINE ACTION class (NO_ACTION, READ_ONLY_REPLY, CLARIFY, CART_CONFIRMATION_PROMPT, PLANNER_HANDOFF) obtained from the real pipeline/engine code against the Fake Backend (`apps/conversations/intent_eval.py`). A SAFETY section lists every entry that offers an unallowed cart change, lets the recommend flow touch the cart, or shows a missing/out-of-stock product; any finding makes the command exit non-zero. Console output is cp1252-safe; full UTF-8 results go to `--output` (default `exports/eval_intent_results.json`). `--ids`, `--failures-only --from-results FILE` and `--dry-run` (canned replies, no network) are available.

Evaluation: `manage.py eval_intent_llm [--limit N] [--sleep S]` (dev only, live Groq calls)
scores `apps/ai/golden/intent_golden.json` (82 synthetic messages) overall and per language.
All Hindi/Telugu/Hinglish/Tenglish/mixed entries are flagged `needs_native_review`; their
quality is **unverified** until a native speaker has reviewed both the set and real results.

### Grounded product recommendations (2026-10-06)

Off by default (`IVA_LLM_RECOMMEND_ENABLED=false`, and it needs the LLM fallback above).
A customer asks for suggestions ("something high-protein under 300"); the LLM **interprets**,
**code selects**, **templates write**. The model only fills a closed schema
(`apps/ai/recommend.py`): `request_type` (recommend / diet_plan / product_question /
medical / other), `category_slug`, `diet_type`, `max_price`, `min_price`, `sort`
(protein_desc / calories_asc / price_asc / price_desc), `product_mention`, `asked_fact`,
`unsupported_constraint`, `language`, `confidence`, `ambiguous`. It receives only the redacted
message, the category allow-list (slug|name) and the diet/sort value lists: no product data.
Validation drops anything outside the allow-lists, non-positive prices and contradictory
ranges; schema errors reject the whole reply.

Routing (inside the existing LLM stage, after rules/fuzzy): a cue from `apps/ai/recommend_cues.py`
(data only; every non-English cue is flagged for native review; matching is NFKC + lower-case,
whole-word for Latin script, substring for Devanagari/Telugu) goes straight to the
extraction call. Without a cue, the Part 1 classifier runs and, if it names RECOMMEND /
DIET_PLAN / PRODUCT_QUESTION (offered only while the flag is on), the extraction is the
second call. Groq calls per flow: cue = 1 (2 if the extraction says `other` and the
classifier then runs); no cue = 1 for an ordinary classified message, 2 for a recommendation
the classifier recognises. Each call goes through the same circuit breaker, per-customer limit
and daily cap. A failed/skipped extraction shows the normal category menu.

Selection (`apps/conversations/recommend_select.py`, Backend Port only): in-stock products;
hard filters for diet type (exact), price range and category; the saved food preference when
no diet was asked for (hidden matches are counted and disclosed); stable sort by the
requested real number; top `IVA_LLM_RECOMMEND_MAX_PICKS`. "High protein" and "low calorie"
are never defined: the sort is applied and the real figures are shown. A sort no product
record supports, or a dietary label the port has no field for (gluten-free, sugar-free,
vegan, allergen-free) is disclosed ("I can't filter by ... yet"), never applied silently.
Replies (`recommend_flow.py`) are templates in en/te/hi: intro, a facts line built only from
fields that exist ("name - price - g protein - kcal - diet", missing parts omitted), the usual
Add to cart buttons, and the existing "nutrition not verified" note when the backend says so.
diet_plan generates nothing and hands off to the planner (`IVA_PLANNER_URL`; no link if unset);
a single-meal constraint in the same message also gets recommendations. product_question
answers verbatim from the product record or says "I don't have that information for this
product." (allergen, ingredient and suitability questions always add "Please check with the
Ivar team": the port has no such fields). medical gives no advice, no picks, and the menu.
Recommendations are read-only: no confirmation step, the cart is never touched.

Phone-testing fixes (2026-10-06): with `IVA_LLM_RECOMMEND_ENABLED` on, a recommendation cue (or a health/weight-loss phrase from `MEDICAL_CUES`) takes priority over a rules/fuzzy *product-search guess* (PRODUCT_SEARCH, or a fuzzy-corrected ADD_TO_CART: the fuzzy stage once turned "weight" into "eight" and a quantity of 8). A cue loses if the message names a real product or category ("suggest ragi mix" still finds ragi mix); every other rules result keeps its priority; if the flow has no answer the rules' guess is kept. Weight-loss and similar phrases get the fixed medical reply with zero model calls. With the LLM layer on, the product-search no-match reply is neutral and never repeats the customer's words (`search.none_neutral`); the quoted `search.none` remains for the non-LLM path. `GOAL_SORT` (data) maps goal words (gym, workout, muscle, protein...) to `protein_desc` in code after validation, only if the model gave no sort; products without a protein figure come last and the reply says "Sorted by protein, highest first." (ordering only). One INFO line per recommendation message: stage (cue / classifier / cue_medical), kind, constraint names, sort, outcome and pick count; never customer text.

Health-aware free-form food questions (2026-10-07). Principle: understand the whole sentence,
answer only the part the menu data supports, decline the rest with fixed templates; never invent a
fact, benefit or need. The extraction schema gained `health_context` (none | symptom | condition |
weight_loss | pregnancy_or_child | allergy | emergency), `asks_advice`, `meal_time` and a larger
`unsupported_constraint` enum (ingredient, allergen, gluten_free, sugar_free, vegan, popularity,
meal_time, other_label). Health wording is CONTEXT ONLY: it adds fixed safety sentences and never
changes which products are chosen or how they are sorted (a model-chosen sort under health wording is
dropped unless the message itself asks for that ordering; see `EXPLICIT_SORT_WORDS`). Composition
(`recommend_flow._reco_compose`): [safety block] then [picks block]. symptom/condition/pregnancy_or_child
-> "I can't give medical or dietary advice." + "If you feel unwell, please see a doctor."; weight_loss ->
the first sentence only; allergy -> the allergen sentence and the category browse, never picks; none ->
normal flow, with "I can't say what you personally need, but here is what's on our menu." before the picks
when `asks_advice`. Picks after a safety block need explicit food constraints (category, diet, price, an
explicit sort, a mapped meal time); otherwise the category browse. Meal time: `apps/ai/meal_time_map.py`
is EMPTY on purpose (the port has no meal-time field), so a meal-time request is disclosed and the other
constraints are applied; a filled list is a plain category filter. Popularity ("best seller") is declined
with a fixed sentence. Health wording also routes a message to the extraction (`HEALTH_CUES`), so the
safety sentences are always composed. The older fixed medical reply (`recommend.medical`, used for the
deterministic weight-loss/condition phrases and request_type=medical) is kept as is: existing tests pin it.

Emergency check (`apps/ai/safety_keywords.py`, `ConversationEngine._emergency_check`): keyword matching in
code on every free-text message (never on button replies), before onboarding, the rules layer and any
model call. `IVA_SAFETY_KEYWORDS_ENABLED` (default ON) is independent of every `IVA_LLM_*` flag. The reply is
ONLY "This sounds serious. Please contact a doctor or your local emergency services right now." plus the
owner's `IVA_EMERGENCY_CONTACT_TEXT` if set, and a Menu button: no products, no cart change, no model call.
The model's `health_context=emergency` gets the same reply. Keyword coverage is a starter set and is
INCOMPLETE (every non-English list needs native-speaker review).

Single-turn limit: no conversation history is sent to the model. A follow-up ("cheaper ones", "the second
one") is a fresh message; with no usable constraints it gets the neutral "couldn't find that" reply and the
category browse (`FOLLOWUP_CUES`). A bare request with no constraints that is not a recognised follow-up
still shows the first picks in catalog order (existing behaviour, pinned by tests).

Fuzzy guard: a cart change guessed by the fuzzy (typo) stage now asks Yes/No first (`IVA_FUZZY_CART_CONFIRM`,
default on), reusing the LLM confirmation machinery; rules-stage adds are unchanged. Never correcting a
word INTO a number word is `IVA_FUZZY_NUMBER_GUARD`, now ON by default (changed 2026-10-07 with the owner's
approval; `config/settings/test.py` also sets it on). Reason: the fuzzy stage once corrected "weight" into the
number word "eight" and produced a quantity of 8. Exactly one existing test was changed with the owner's
approval: `test_the_old_quoted_reply_is_unchanged_without_the_llm_layer` (`test_engine_recommend_priority.py`)
used to pin the buggy corrected text ("recommend if lose"); it now asserts "weight" is kept and never becomes
a quantity. No other test was edited. The Yes/No question for fuzzy cart changes still applies either way.

Product-data dependency: quality is bounded by what the backend returns. The Fake Backend has
name, category, price, diet_type (veg/egg/non_veg), protein_g (null on 8 of 55), calories,
tags (light, high_protein), pack_size (null on 47), image, nutrition_verified (false for all)
and no descriptions. A real adapter that supplies descriptions, allergens or ingredients
would let more questions be answered, but that needs Backend Port fields first.
`manage.py eval_intent_llm --set recommend` (dev only, live Groq calls) scores
`apps/ai/golden/recommend_golden.json` (50 synthetic entries); non-English entries are
flagged `needs_native_review`, so Telugu/Hindi quality stays unverified until reviewed.

Stated constraints and prices (2026-10-07). Two rules, both in code. (1) **A number beside a price
word or a currency word is never a quantity**, on any path. `apps/ai/constraints.py` (data in
`apps/ai/constraint_cues.py`, non-English entries marked for native review) finds the number in
"under 300", "below Rs 15", "15 rupees", "₹300", "300 ke andar", "300 లోపు", "300 से कम" and the like,
and the rules layer keeps those tokens out of quantity extraction, order-number detection and the
search words (`rules._match`), the fuzzy stage leaves comparator/currency/diet words as typed, a
model-supplied quantity that is really a price in the message is dropped
(`constraints.drop_price_quantity`), and `parse_quantity` skips them. Before this fix "something high
protein under 300" became ADD_TO_CART with quantity 300 (the 300 was stored in the pending
"choose" state and read back as the quantity when a list row was tapped, failing only on the
Port's per-line cap of 20). A pending choice with no real quantity now follows the existing rule:
ask "how many?". A MIN comparator ("above", "at least") counts as a price only next to a currency
word, because "at least 3 packets" is a quantity; a number followed by a pack-size unit ("500 g") or
a unit word ("2 packets") is never a price. (2) **Constraints are never silently dropped by a name
search.** With `IVA_LLM_ENABLED` and `IVA_LLM_RECOMMEND_ENABLED` on (and the LLM allowed for the
message), a rules/fuzzy PRODUCT_SEARCH, an ADD_TO_CART with a search term, or a category listing that
carries a price limit, a diet word (veg / non-veg / egg) or a high-protein / low-calorie word takes the
recommendation flow instead (`IntentPipeline._constraint_result`): one extraction call, then the price,
diet and ordering read in code are applied over the model's answer (`constraints.overlay`; the
code-read value wins, a model sort is kept). If the extraction is unusable (down, skipped, low
confidence) the request is built from the code-read constraints alone, with a category taken from the
message when a category name's words are all present, so a "Which one?" list that could violate the
constraint is never shown. The exception: the search words resolve exactly to a real product name once
the comparator words are set aside ("protein smoothie under 300" still finds that product; the name
comes from the catalog hints the engine already gives the pipeline). An exact CATEGORY name does not
bypass the flow: a category plus a constraint is a filtered recommendation, never an unfiltered
browse. Medical phrases still win. The price filter is strict (`select_products`: nothing above
`max_price` ever appears; the limit is inclusive) and the intro states it
("Showing items up to ₹{max}.", `recommend.max_price`, en/te/hi, te/hi need native review); no
qualifying item gives the existing `recommend.none` reply. With the flags off nothing is routed (only
the quantity fix applies). Not handled: spelled-out numbers ("three hundred"), ranges other than
"between X and Y" (English only), decimals ("299.50": the tokenizer splits them). Logging: the
recommendation log line gains `cues=[...]` (constraint NAMES only: price, diet, protein_sort,
calorie_sort) and `stage=constraint`; no values or text. Eval: `manage.py eval_intent_llm --set
recommend_price [--dry-run]` scores `apps/ai/golden/recommend_price_golden.json` (22 synthetic entries, all
languages) through `IntentPipeline.constraint_answer` and fails any entry that shows an item above its limit.

Price ranges, spelled-out prices and the advice line (2026-10-07). Built on the constraint rules
above, same flags. (1) **Ranges**: `constraints.parse` reads min and max from "250-300", "250 to
300", "between 250 and 300", "Rs 250-300", "₹250 to ₹300", "250 se 300 [tak]", "250 nundi 300
varaku" (en/hinglish/tenglish/hi/te, number first or currency first, in either order: reversed
bounds are swapped, never dropped). A hyphen is normalized away by the tokenizer, so
`constraints.prepare_ranges` rewrites a hyphen between two numbers to the sentinel word `rngto`
and the pipeline keeps that tokenization only when a range was actually read; a bare "N to M",
"N se M" or hyphen pair is a range only when the message also holds a price or constraint cue
(currency word, comparator, diet / protein / calorie word), otherwise it is tokenized as before and
a hyphenated pair with no explicit add/update word is kept out of quantity extraction
(`rules.match(no_quantity=...)`: "snacks 15-20" no longer becomes an add; "add 2-3 quinoa" is
unchanged). `select_products` already filters both bounds strictly; the intro states them
(`recommend.price_range` "Showing items from ₹{min} to ₹{max}.", `recommend.min_price`,
`recommend.max_price`; te/hi need native review). (2) **Spelled-out prices**: English number words
(one..ninety, hundred, thousand, "two fifty", "three hundred and fifty", "a hundred") and Hindi /
Telugu words (Roman and native script, data in `apps/ai/constraint_cues.py`, all marked for native
review) are read ONLY beside a comparator, range word or currency ("under two hundred" = max 200); the
words are then price tokens, never a quantity, and "add two quinoa" is still a quantity. (3)
**Unreadable prices**: with the flags on, a currency word or an unambiguous comparator (under, below,
less than, within, up to, at most, cheaper than) with NO readable number anywhere in the message gets
`price.unreadable` ("I couldn't read the price you mentioned. Please write it like 'under 300'.") with
a Menu button, no model call, no picks and never the "couldn't find that" reply
(`IntentResult.request.kind == "price_unreadable"`). (4) **Advice line**: on the constraint route
(`IntentPipeline.constraint_answer`) `advice.disclaimer` needs the model's `asks_advice` AND a code-side cue
from `apps/ai/advice_cues.py` (`recommend.advice_cue_matches`; owner-editable, non-English entries
need native review; plain "suggest"/"recommend" are not cues). The cue and classifier routes still
trust `asks_advice` alone, because `test_engine_health.py::test_asks_advice_adds_the_disclaimer_before_the_picks`
(text "suggest zork florp", no advice cue) pins that behaviour; wiring the gate there is one line in
`_from_extraction` once that test is updated. Logging: the cue names are `price_range`, `price_min`
and (upper limit only) `price`, the name release 1 logged and existing tests pin; never values.
Eval: `manage.py eval_intent_llm --set recommend_range [--dry-run]` scores
`apps/ai/golden/recommend_range_golden.json` (25 entries, all languages) and fails any entry that shows an
item outside its bounds. Not handled: bare "250 to 300" / "250 se 300" with no cue, other-language
"between" wording, spelled-out decimals and "dhai sau"/"dedh sau"-style fractions, ranges without a
joiner ("250 300").

Advice questions, the advice-line gate and Hindi fractions (2026-10-07). With the two LLM flags on,
`recommend.routing_cue_matches` is the recommendation cue: a configured cue (`recommend_cues.py`), an
advice cue ("what food do I need", "what should I eat", "is it good for me"; `apps/ai/advice_cues.py`,
owner-editable), or a goal word from `GOAL_SORT` (gym, workout, muscle, protein ...) together with a
question word (`QUESTION_WORDS`: what, which, should, need, kya, emi, ela and equivalents). It has the
same priority as before over a weak rules/fuzzy PRODUCT_SEARCH guess and loses to a message that
names a real product or category ("what protein bowls do you have" still opens the Protein Bowls
category, "suggest ragi mix" still finds it); before this change "What food do I need to take if I go
to the gym regularly?" was a rules PRODUCT_SEARCH ("food take if gym regularly") with no cue, so the
engine ran a name search and replied `search.none_neutral`. Health words were already cues
(HEALTH_CUES) so "I'm having a little headache, what do I have for dinner?" is unchanged: safety.medical
+ safety.see_doctor + meal_time.unsupported + the category browse, no picks (pinned by a new test).
`advice.disclaimer` is now gated on EVERY route inside `IntentPipeline._from_extraction`: it needs the
model's `asks_advice` AND an advice cue in the message; the model's flag alone shows nothing.
Hindi fractional prices ("dhai sau" 250, "dedh sau" 150, "saade teen sau" 350, "saade paanch sau" 550,
"paune do sau" 175, Roman and Devanagari; `constraint_cues.SPELLED_FRACTIONS`, native review needed)
are read only beside a price word and never become a quantity ("dhai sau ke andar protein" is max 250
+ protein sort; before, "sau" alone read as 100). Flags off: nothing is routed. Eval:
`eval_intent_llm --set recommend_advice [--dry-run]` scores `recommend_advice_golden.json` (21 entries,
all languages: model flag, expected reply keys, expected routing).

Deterministic constraints (2026-10-07). The price amount, the comparator and the protein / calorie
sort are read in CODE (`constraints.parse`, NFKC + lower-case + whitespace-collapsed text) and never
depend on the model. Before: a model reply of `request_type=product_question` (or medical) with no
constraints was returned as-is by `constraint_answer` and became the generic `recommend.clarify`
reply; the model's own sort beat the code's; the bare word "protein" was not a sort cue; and in any
state where the LLM may not be called (a pending choice) the constraint route did not run at all, so
the message fell to a plain name search. Now, with the two flags on: (1) the constraint route runs in
every state, with no model call when none is allowed; (2) whatever request_type the model returned
(except an emergency), a code-read price, diet or sort turns the message into a recommendation
carrying the code-read values (`constraints.overlay`: code wins for max/min price, diet and sort; the
model may only ADD fields the code did not find, e.g. a category, which then narrows the picks
as usual); an unusable reply (invalid JSON, timeout, "other", low confidence) gives the same
code-only request; (3) zero picks is the existing `recommend.none` (never `recommend.clarify`);
(4) bare "protein" (`constraint_cues.BARE_PROTEIN`, native review needed) counts as the protein sort
only beside a price constraint. The recommend log line now ends `code=[...] model=[...]` (field
names: what code read vs what only the model supplied). A diet_plan reply keeps its planner hand-off
(with the code-read bounds applied). Eval: `eval_intent_llm --set recommend_determinism [--dry-run]`
(16 entries; also checks the shown items are sorted by protein).

### Code-only routes (2026-10-12)

Order of decisions for a free-text message (each stage ends the turn when it answers):

1. Emergency check in `ConversationEngine` (`safety_keywords.py`: emergency, injury and self-harm
   phrasings, plus a starter set per language). Self-harm wording uses the template `safety.self_harm`,
   which has the same text as `safety.emergency` until a qualified reviewer supplies wording. Contact text
   comes only from `IVA_EMERGENCY_CONTACT_TEXT`; the code never invents a number.
2. Plan hand-off message, then button / list replies.
3. `apps/ai/code_router.py` (data in `code_cues.py`), ahead of the rules and fuzzy stages:
   multi-day plan requests (the planner hand-off; a day count is never a quantity); allergen, ingredient
   and suitability wording (fixed replies, never yes or no, never inferred from names, tags, diet type or
   images; a named product's card may follow); product-fact questions (answered from the Backend Port
   record only, with the "nutrition not verified" note while the record says so); unsupported labels and
   popularity (disclosed, never applied silently).
4. Rules and fuzzy, with the constraint route (price, diet, sort, goal words, a category word) run in code
   whether or not a model exists; the fuzzy stage has a stoplist so common words are never corrected into
   product words. A recommendation cue with nothing to apply gets the category menu, never catalog-order picks.
5. The model, when available, may ADD fields; it cannot change or remove what the code read, and an
   "ambiguous" reply cannot override it. A message that still ends unanswered gets ONE health-only model
   check through the same guard, budget and kill switch.

Nothing the customer typed is repeated back: search and category replies are neutral, and phone numbers,
links, emails and handles are removed from search phrases and never reach a reply, a stored listing or a log.
Messages of one reply are sent in order: any text or buttons message that follows a carousel or image card
waits for that message's status (the "nutrition not verified" note used to overtake the carousel).
A Yes/No for a question that is no longer waiting gets one neutral reply and never calls the model.
`IVA_CODE_CONSTRAINTS_ENABLED` (default on) switches the code-only routes as a group.

## 8. Checkout and payment flow

```text
Cart → Checkout validation → PENDING order + PENDING payment → payment link sent
     → provider confirmation (backend verifies) → payment PAID → order CONFIRMED
```

- The backend owns every step and every status change.
- The WhatsApp side only sends the payment link and displays statuses the port returns.
- Each checkout attempt carries a stable `idempotency_key`, so a retried message cannot create two orders.

## 9. Data storage

| Data | Where | Owner |
|---|---|---|
| Conversation session (current state, selections) | Redis, with TTL | This repository |
| Conversation and message **metadata** (message ID for dedup, type, status, timestamps, text length, reply ID, reply template key). **No message text** | PostgreSQL (`Conversation`, `ConversationMessage`), deleted after `IVA_MESSAGE_RETENTION_DAYS` (default 90) | This repository |
| Message text in transit | Worker memory and the Celery broker until the task runs; never PostgreSQL or logs | This repository |
| Customers, catalog, cart, orders, payments | Backend | Organization |
| Cache | Redis | Shared |
| Celery broker | Redis | Shared |

## 10. Security

- Webhook: signature verification on the raw body, constant-time comparisons, verify-token check on subscription. An empty configured secret or token never matches.
- WhatsApp IDs are stored and used as keys only as an HMAC pseudonym keyed with the dedicated `IVA_PSEUDONYM_KEY` (`apps.iva_common.pseudonym`; never `SECRET_KEY`), plus a masked form. A plain hash of a phone number could be reversed by trying every number.
- Outbound HTTPS always verifies certificates and hostnames; there is no way to switch this off.
- **Redis connections (S5).** All three (`REDIS_URL` cache, `CONVERSATION_REDIS_URL` sessions/locks, `CELERY_BROKER_URL` broker) are built with `ssl_cert_reqs=ssl.CERT_REQUIRED`. Without this, `rediss://` connections silently accept any certificate (kombu's own documented default for an unconfigured secure broker); `apps/iva_common/tests/test_redis_tls.py` proves a verifying client refuses a self-signed certificate that an unverifying one would accept.
- **`manage.py check --deploy` (S5).** Clean against prod settings except one explained warning: `security.W021` (HSTS preload not enabled). `SECURE_HSTS_PRELOAD` stays `False` deliberately — preload submission is effectively permanent, applies to every subdomain, and should be a separate decision the organization makes once this domain's HTTPS has been stable in production, not a default this codebase turns on. See the comment in `config/settings/prod.py`.
- The Cloud API access token appears only in the Authorization header. It is never in errors, logs or `repr`. Celery task arguments are shown masked (`kwargsrepr`).
- Secrets only in environment variables; never logged or committed.
- Phone numbers and WhatsApp IDs masked in all logs, including tracebacks.
- Customers never see internal errors.
- No admin panel, admin route or admin UI.
- Production: HTTPS redirect (health paths exempt), HSTS, secure cookies, explicit hosts, no wildcards, fail-fast on missing configuration.

### Model-call threat model

What an attacker (a customer, or text a customer forwards) can try against the model layer, and the
mechanism that answers each. Mechanisms only; the operational values live in `.env.example`.

| Threat | Mechanism |
|---|---|
| Cost or quota exhaustion (flooding, one customer, many customers) | Per-customer, per-minute, daily and token budgets in Redis; a customer's share of the daily budget is capped; counters that cannot be read fail closed. A provider rate-limit response pauses all callers for the advised period. |
| Prompt injection and instructions inside a message | The message is sent as a JSON string and the system prompt states it is data. The model's reply is validated against a strict schema and allow-lists; unknown fields reject the whole reply; the model never writes customer-visible text. |
| Model text reaching a customer | Only allow-listed values and a sanitised product word (control characters, markup, links and personal-data-like tokens removed, length-capped) rendered inside a fixed template. |
| Personal data leaving the process | Only the normalised, redacted message plus the fixed prompt and allow-lists are sent: no phone, name, address, customer or order identifier, cart, or conversation history. A test captures the exact request bodies. Over-length, empty-after-redaction and non-text messages are not sent. |
| Unconfirmed cart changes | A model-derived cart change is only ever offered as a Yes/No question; nothing changes without Yes. |
| Provider outage or bad output | Never retried on the customer path; a shared circuit breaker; a rolling failure/rejection rate trips a runtime kill switch automatically. |
| Key leakage | The key is only in the request header; errors carry fixed categories; the object representation hides it; settings dumps mask it; a test checks exceptions, logs and tracebacks. |
| Safety and ordering rules | Emergency, injury and self-harm keyword replies; allergen, ingredient and suitability replies; product-fact, label, popularity and plan answers; price limits, diet words, sort and goal words; and cart guards are read and answered in code, independently of every `IVA_LLM_*` setting, the Groq key, the circuit breaker and the runtime kill switch (tests run each with the flags off, the model on, the kill switch on and the breaker open). The model may only ADD a field the code did not read. The keyword data and the model cannot catch every phrasing, and ALL safety wording is a default that must be reviewed (see `HANDOVER.md` section 14). |

Residual risks are listed in the operations runbook (`HANDOVER.md` §12) and the session notes.

## 11. Failure handling

| Failure | Behaviour |
|---|---|
| Backend unavailable | Friendly "please try again" message; session kept |
| LLM unavailable or invalid output | Rules-only understanding; clarifying question if unsure |
| WhatsApp API error | Retry with backoff on 429/5xx; log masked failure |
| Duplicate webhook | Acknowledged and ignored via message-ID constraint |
| Database or queue unavailable at the webhook | 503, so Meta retries later. A message recorded but not queued is removed first, so the retry is not taken for a duplicate |
| Worker receives a task twice | The message is claimed in the database before the engine runs; the second task does nothing (at most once) |
| Worker crashes mid-message | No reply is sent for that message. After 5 minutes the stalled-message check logs one masked warning; the message is not retried |
| Last inbound message older than 24 h | No reply is sent (WhatsApp customer-service window) |
| Invalid signature | 403, nothing processed |
| Expired session | Resume at main menu |
| Missing data (ETA, tracking) | Say it's unavailable; never guess |
| Redis (session store) down mid-conversation | Unreadable/unreachable session counts as "no session"; resumes at main menu, same as expired |
| Redis (cache/lock) down (S5) | Rate limit fails open (message allowed, uncounted); the per-customer lock proceeds unlocked (no ordering guarantee for that message, but it is still processed) rather than the message being dropped or the task crashing |

**Where each of the above is tested:** backend down / LLM down / clarify / expired session / session-store down — `apps/conversations/tests/test_engine_failures.py`. WhatsApp API retries/failures — `apps/whatsapp/tests/test_client.py`, `test_delivery.py`. Duplicate webhook / invalid signature / DB-unavailable-at-webhook — `apps/whatsapp/tests/test_views.py`, `test_database.py`. Worker crash / stalled message — `apps/whatsapp/tests/test_stalled.py`. Per-customer lock (serializing, TTL self-heal, Redis down) and rate limit (window, reset, Redis down) — `apps/whatsapp/tests/test_locks.py`, `test_rate_limit.py`.

## 12. Integration with the organization's backend

1. Backend team implements the Backend Port in their project.
2. They run our contract test suite against their adapter until it passes.
3. Copy `apps/whatsapp`, `apps/conversations`, `apps/ai`, `apps/iva_common` into their project.
4. Add `apps.conversations` and `apps.whatsapp` to `INSTALLED_APPS` and include `apps.whatsapp.urls` at `webhooks/whatsapp/`. Set `IVA_BACKEND_ADAPTER` to their adapter, and add the WhatsApp, Redis and LLM environment variables.
5. Copy `IVA_PSEUDONYM_KEY` over unchanged from the existing deployment (a new key resets conversation records and sessions).
6. Run `migrate`. Schedule `manage.py purge_conversation_messages` daily and `manage.py report_stalled_messages` every 5 minutes.
7. Run the full test suite in their project.

## 13. Out of scope (Phase 1)

Subscribe & Save, Build a Box, Corporate Orders, Wellness, Yoga, marketing broadcasts, proactive notifications, website, mobile app, admin or merchant interfaces.

**AI Planner (2026-09-28 scope update):** the planner page itself — its UI, and any
recipe/nutrition computation — is out of scope; it is built and hosted on the website.
What *is* in scope, and built, is the WhatsApp-side handoff only: linking out to the
planner (welcome message, main menu) and receiving/acting on its "IVA PLAN v1" reply
message. See §5's Contract decisions and §7's "AI Planner handoff (`PLAN_RECEIVED`)",
and `PLANNER_INTEGRATION.md` for the website team's contract.

### Future work

- Cart screen via WhatsApp Flows: deferred until the real backend exists. Needs a data endpoint, Meta endpoint encryption (likely the cryptography library), a flow_token bound to the customer, and manual Meta setup. The in-memory Fake Backend only works with Flows in inline mode.
