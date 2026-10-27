# HANDOVER.md — IVA WhatsApp Channel: Real Backend Integration

For the backend engineer wiring the organization's real backend (catalog, customers,
cart, orders, payments, delivery) in place of the Fake Backend. Read this alongside
`CLAUDE.md` and `ARCHITECTURE.md`, which remain the source of truth; this document
summarizes and points at exact locations, it does not replace them.

## 1. Purpose

This repository is the WhatsApp channel only: the webhook, the conversation engine and
the intent layer. It owns four packages — `apps/whatsapp`, `apps/conversations`,
`apps/ai`, `apps/iva_common` — and nothing else. The organization's backend team owns
catalog, customers, cart, orders, payments, delivery and infrastructure (PostgreSQL,
Redis, hosting). The two sides talk **only** through the Backend Port defined in
`apps/conversations/backend_port.py`. The channel never touches backend models or
tables directly.

For development and tests this repo ships `FakeBackend`
(`apps/conversations/fake_backend.py`), an in-memory adapter loaded from
`fixtures/ivar_catalog.json` (55 products, prices and nutrition explicitly unverified).
**Fake Backend must never run in production.** This is a hard rule (`CLAUDE.md` rule 5:
"Fake Backend is dev/test only. Production settings must refuse it.") and it is
mechanically enforced: `config/settings/prod.py`'s `validate_production()` raises
`ImproperlyConfigured("The Fake Backend must not be used in production.")` if
`IVA_BACKEND_ADAPTER` contains `apps.conversations.fake_backend` or `FakeBackend`
(see `FAKE_BACKEND_PATHS` in that file).

Other hard rules that bound what a real backend integration may and may not do (full
list in `CLAUDE.md`): the channel never fabricates price, stock, payment, order or
delivery status — anything missing must be reported to the customer as unavailable, not
guessed; the LLM never writes data or decides price/stock/payment/order status, it only
proposes an intent; money is always `Decimal` plus a currency code, never float.

## 2. The Backend Port contract

Defined in `apps/conversations/backend_port.py` as `typing.Protocol` interfaces with
frozen dataclasses. Money is `Decimal` plus an ISO currency code. Datetimes are
timezone-aware. All IDs are strings.

### CustomerPort

```python
def get_or_create_customer(self, whatsapp_id: str) -> CustomerRef: ...


def get_customer(self, customer_id: str) -> CustomerRef | None: ...


def update_profile(
    self,
    customer_id: str,
    *,
    name: str | None = None,
    city: str | None = None,
    language: str | None = None,
    diet: str | None = None,
    onboarding_complete: bool | None = None,
) -> CustomerRef: ...
```
- `get_or_create_customer`: resolves (or creates) a customer from a WhatsApp ID. Called on every turn.
- `get_customer`: looks a customer up by backend ID; `None` if unknown; never creates one.
- `update_profile`: partial update; `None` for any field means "unchanged" (this means a diet, once set, cannot be cleared this way — see Contract decisions, Open request #1).

### CatalogPort

```python
def list_categories(self, parent_id: str | None = None) -> list[Category]: ...


def list_products(
    self,
    *,
    category_id: str | None = None,
    search: str | None = None,
    diet: str | None = None,
    tag: str | None = None,
    limit: int = 10,
    offset: int = 0,
) -> tuple[list[ProductSummary], int]: ...


def get_product(self, product_id: str) -> ProductDetail: ...


def resolve_product_names(self, text: str, limit: int = 5) -> list[ProductSummary]: ...
```
- `list_categories(None)` returns top-level groups; with `parent_id` returns that group's children. Unknown `parent_id` → `NotFound`.
- `list_products(diet=...)` filters by dietary preference (see Contract decisions). `category_id` may name a group or a single category. Out-of-stock products are still listed, with `in_stock=False`. `limit` is 1–50 (`MAX_PAGE_SIZE`). Returns `(products, total_count)`.
- `get_product`: full detail including variants; raises `NotFound` if the product does not exist.
- `resolve_product_names`: fuzzy name resolution over free text, no `diet` parameter (see Contract decisions).

### CartPort

```python
def get_cart(self, customer_id: str) -> CartView: ...
def add_item(self, customer_id: str, variant_id: str, quantity: int) -> CartView: ...
def update_quantity(self, customer_id: str, line_id: str, quantity: int) -> CartView: ...
def remove_item(self, customer_id: str, line_id: str) -> CartView: ...
```
- `add_item` for a variant already in the cart increases that line's quantity rather than creating a duplicate line.
- Quantities are integers 1..`MAX_LINE_QUANTITY` (Fake Backend value: 20; real limit is up to the backend — see Open request #4). Quantity 0 is not a delete; `remove_item` is the delete path. Non-integer or out-of-range quantity raises `InvalidQuantity`.
- A cart line owned by another customer → `NotAuthorized`. A missing line/variant → `NotFound`.

### AddressPort

```python
def list_addresses(self, customer_id: str) -> list[Address]: ...
def add_address(self, customer_id: str, text: str, label: str | None = None) -> Address: ...
```

### CheckoutPort

```python
def checkout(self, customer_id: str, address_id: str, idempotency_key: str) -> CheckoutResult: ...
```
- Validates the cart, creates an order with `order_status=PENDING` and `payment_status=PENDING`, empties the cart, returns a payment URL.
- The same `(customer_id, idempotency_key)` returns the same result without creating a second order (idempotent).
- Only a verified payment-provider confirmation may move payment to `PAID` and then the order to `CONFIRMED` — never the channel.
- An empty cart at checkout → `CartEmpty`. A rejected checkout → `CheckoutRejected(reason=...)`.

### OrderPort

```python
def get_latest_order(self, customer_id: str) -> OrderStatusView | None: ...
def get_order(self, customer_id: str, order_number: str) -> OrderStatusView | None: ...
def list_recent_orders(self, customer_id: str, limit: int = 5) -> list[OrderStatusView]: ...
def get_order_detail(self, customer_id: str, order_number: str) -> OrderDetail | None: ...
```
- `get_order` returns `None` for an order number that does not exist — it never raises `NotFound` for orders (see Contract decisions). An order owned by another customer raises `NotAuthorized`.
- `get_order_detail` returns an `OrderDetail` (order number, `placed_at`, total, currency, statuses and `lines`, a tuple of `OrderLine` with names and prices as at the time of the order; no address). Same `None` / `NotAuthorized` rules as `get_order`.
- `OrderStatusView.events` are oldest first; order-status events use `OrderStatus` values, payment events use `PAYMENT_<PaymentStatus>` (built by `payment_event()`, e.g. `PAYMENT_PAID`).

### Errors (`BackendError` and subclasses, all in `backend_port.py`)

| Error | Raised when |
|---|---|
| `BackendUnavailable` | Timeout, connection failure or 5xx from the backend |
| `NotFound` | Product, variant, category, customer, address or cart line does not exist (never for orders) |
| `ProductUnavailable` | Product is out of stock or inactive |
| `InvalidQuantity` | Quantity is not an integer in 1..`MAX_LINE_QUANTITY` |
| `CartEmpty` | Checkout attempted with an empty cart |
| `CheckoutRejected` | Backend refused checkout; carries a `reason` code (see below) |
| `NotAuthorized` | The customer does not own the resource |

Invalid arguments that are programming errors (unknown diet/language code, `limit`/
`offset` out of range, empty `whatsapp_id`, address text, or `idempotency_key`) raise
plain `ValueError`, not a `BackendError` subclass.

`port_operations()` in `backend_port.py` lists every method name an adapter must
implement, in declaration order — useful for a completeness check.

## 3. Contract decisions

Recorded in `ARCHITECTURE.md` §"Contract decisions" (source of truth; overrides the
Reference PDF on conflict — `CLAUDE.md` hard rule 13). All items below are **DECIDED**
unless marked **OPEN**.

- **DECIDED — Diet filter.** `list_products(diet=...)`: `veg` → veg only; `egg` → veg + egg; `non_veg` → everything.
- **DECIDED — Quantity limit.** Integer 1–20 (`MAX_LINE_QUANTITY = 20`) is the Fake Backend's default only; the real limit is set by the backend. Non-integers (`bool`, `float`, `str`, `None`) and out-of-range values raise `InvalidQuantity`. Quantity 0 is not a delete.
- **DECIDED — Page limit.** `list_products(limit=...)` is 1–50 (`MAX_PAGE_SIZE = 50`); `offset` ≥ 0.
- **DECIDED — Payment event names.** `OrderStatusView.events` oldest first; order-status events use `OrderStatus` values, payment events use `PAYMENT_<PaymentStatus>` via `payment_event()`.
- **DECIDED — `get_order` semantics.** Returns `None` for a non-existent order number; never raises `NotFound` for orders. An order owned by another customer raises `NotAuthorized`.
- **DECIDED — `NotFound` scope.** Applies to products, variants, categories, customers, addresses, cart lines. Never used for orders. This overrides Reference PDF §5.3.
- **DECIDED — `CheckoutRejected.reason` codes.** `product_unavailable`, `price_changed`, `address_not_serviceable`, `below_minimum_order`, `other` (default). Codes are safe and customer-agnostic.
- **DECIDED — `ValueError` for invalid arguments.** Unknown diet/language code, out-of-range `limit`/`offset`, empty `whatsapp_id`/address text/`idempotency_key` — all `ValueError`, not `BackendError`.
- **DECIDED — Quantity limit ownership.** The conversation layer never hardcodes or imports the quantity limit; it passes the customer's quantity to the port and relies on `InvalidQuantity`.
- **DECIDED — `CLARIFY` result.** Added to the 15 intents in Reference PDF §8. Produced only by the intent pipeline for low confidence; never accepted from the LLM or a reply ID. Overrides the PDF's intent list.
- **DECIDED — Order numbers in the intent layer.** Extracted only for `ORDER_STATUS`, `PAYMENT_STATUS`, `TRACKING` (or `CLARIFY` suggesting one of these). Current pattern is generic (letters + digits, e.g. `IVA1002`) pending the real format — see Open request #3.
- **DECIDED — Search with a diet.** `resolve_product_names` has no `diet` parameter; the engine uses `list_products(search=…, diet=…)` for customers with a saved diet, and `resolve_product_names` only for customers without one.
- **DECIDED — Clearing a diet.** In `update_profile`, `None` means "unchanged", so a diet cannot be cleared once set; a customer can switch to `non_veg` instead. See Open request #1.
- **DECIDED — Payment link after checkout.** Only `CheckoutResult` carries `payment_url`; `OrderStatusView` does not. The engine keeps the link from `checkout` in session state. See Open request #2.
- **DECIDED — Checkout retries.** A `CheckoutRejected` checkout is assumed to create no order — the engine uses a new idempotency key next attempt. After `BackendUnavailable` (e.g. a lost response) it retries with the *same* key.
- **DECIDED — Pseudonym key.** See section 6 below.
- **DECIDED — Production WhatsApp configuration.** Production requires `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_API_VERSION`. No default API version — the team must set the version it has tested.
- **DECIDED — Production session store.** Production requires `CONVERSATION_REDIS_URL` (the in-memory session store is not shared across worker processes). Adds to the four required values in Reference PDF §4.3.
- **DECIDED — "menu" global intent.** The PDF lists "menu" as a global intent but defines no MENU intent; "menu" maps to `CATEGORY_SELECT` with `category_query = null` (show top-level groups).
- **DECIDED — AI Planner picks have no variant-choice step (2026-09-28).** `resolve_product_names(name, limit=1)` is called once per pick; the first in-stock variant is used automatically — "Add all to cart" never asks the customer to pick a variant. Displayed name/price always come from the resolved port data, never the plan message's own text.
- **DECIDED — Contact profile name is now read (2026-09-28, reverses a prior decision).** `apps/whatsapp/parser.py` previously deliberately did not read `contacts[].profile.name`. It now does, sanitizes it, and saves it via `update_profile(name=...)` only when the customer has no saved name yet — never logged, never overwrites an existing name.
- **DECIDED — Product image URL (2026-09-29).** `ProductSummary`/`ProductDetail` gained `image_url: str | None` — an `https://` URL or `None`, never guessed by the channel. The Fake Backend builds it from `IVA_PRODUCT_IMAGE_BASE_URL` + the fixture's relative `image_path`; a real adapter should populate `image_url` directly. All 55 fixture products have an image path as of 2026-10-01 (Open request #6 closed).
- **DECIDED — Cards vs. list for product browsing (2026-09-29).** Opening a category, or a plain product search/browse, now sends one `OutProductCard` per product (image if available, name + price, "Add to cart"/"Order now", 4 per page) instead of a compact list. The one exception, kept unchanged: when a quantity is already known or being collected (e.g. typed "add 2 oats" resolving to several matches), the customer still picks from the old compact `OutList`, because a card's quick actions always add quantity 1 and would silently drop the requested quantity otherwise.
- **DECIDED — Category pages are a media carousel (2026-10-01, option B approved).** Up to 10 products per page as one interactive media carousel (image, name + price from the port, "Add to cart"/"Order now"), followed by a "Show more"/"Menu" reply-buttons message. Falls back to the one-card-per-product shape (4 per page) when the page has under 2 products or any product lacks an image, and to the same cards if WhatsApp rejects the carousel -- no product is ever dropped. Search, the "add N of X" list and the diet filter are unchanged. Needs a real-Meta check (README checklist): the carousel JSON follows Meta's docs but has not been sent to a real number. A real adapter must supply `image_url` for every product to get carousels. te/hi `carousel.body` needs native review.
- **DECIDED — Greetings/commands at the address step (2026-10-01).** A message that is exactly a greeting or a global command (cart, order status, help, language, menu, cancel) is handled as that instead of as a delivery address; greetings get a localized "Welcome back ... middle of checkout" with Continue checkout / Menu. Any other text, including real addresses containing words like "Cart" or "Menu", is still an address. te/hi wording and word lists need native review. See `ARCHITECTURE.md`.
- **DECIDED — Dev-only Fake Backend persistence (2026-10-01).** `IVA_FAKE_BACKEND_STATE_FILE` keeps the Fake Backend's customer state (not the catalog) in a JSON file across restarts. Dev only: production refuses to start with it set, and a real adapter ignores it. `reset_dev_data --confirm` empties the file. Not a Backend Port change.
- **DECIDED — Follow-up ordering after a carousel/image card (2026-10-01).** A buttons message that follows a carousel or an image card waits for that message's WhatsApp `sent` status callback (max `IVA_ORDER_WAIT_SECONDS`, default 5, then sent anyway) so it cannot appear before it; fixes the live bug where "Show more / Menu" arrived before the carousel. Needs the Meta webhook to deliver message *status* callbacks (subscribe the `messages` field -- the same one that already carries inbound messages). Without callbacks every large message costs the full timeout. Re-check on a real device; switch `IVA_ORDER_WAIT_STATUS` to `delivered` if `sent` is not enough. See `ARCHITECTURE.md`.
- **DECIDED — Typed search results are a list (2026-09-30, supersedes the plain-search half of the cards entry above).** A typed search with 2+ matches sends one compact list (name + price from the port, up to 10 rows, or 9 + "Show more"); tapping a row re-checks the product with `get_product` and sends its card; out-of-stock/missing products get a message instead of a card. Category browsing keeps cards. No Backend Port change. See `ARCHITECTURE.md`.
- **DECIDED — Native catalog orders (2026-10-06).** WhatsApp `order` messages are handled with no Backend Port change: `product_retailer_id` must equal the port product ID (the Meta catalog uses the product slug), prices/stock always come from the port, and the IVA cart is replaced with the validated items. See `ARCHITECTURE.md` (Native WhatsApp catalog orders). Dev tool: `manage.py replay_order`. Live catalog linking is blocked on the catalog-enabled test number; only simulated payloads are tested.
- **DECIDED — LLM fallback (2026-10-06).** Optional Groq classifier behind an `IntentClassifier` interface, used only when rules cannot understand free text. No Backend Port change (it only reads the catalog list). Cart-changing LLM requests need a Yes/No confirmation. The LLM never sees phone numbers, names, addresses or orders. Telugu/Hindi/Hinglish/Tenglish quality is unverified until native-speaker review; run `manage.py eval_intent_llm` (dev only). See `ARCHITECTURE.md` (LLM fallback).
- **DECIDED — Grounded recommendations (2026-10-06).** No Backend Port change. The LLM only extracts constraints; products, numbers and wording come from the port and templates. Answers are limited to fields the port returns (name, category, price, diet type, protein, calories, tags, pack size, image, nutrition_verified). Allergen, ingredient and suitability facts are not in the port, so those questions get "please check with the Ivar team". A backend that adds such fields (or descriptions) lets this flow answer more. See `ARCHITECTURE.md`.
- **DECIDED — Health-aware recommendations and emergency check (2026-10-07).** No Backend Port change. Health wording only adds fixed safety sentences; it never selects or sorts products. Emergency keywords get only a fixed reply, in code, before any model call. **The owner must have the safety wording ("I can't give medical or dietary advice.", "If you feel unwell, please see a doctor.", the allergen and emergency sentences) reviewed by a qualified person before launch.** Data the Ivar team must provide: `apps/ai/meal_time_map.py` category lists (empty today), `IVA_EMERGENCY_CONTACT_TEXT`, native-speaker review of every non-English cue, keyword and template (emergency keyword coverage is incomplete), and backend fields for allergens, ingredients and sales/popularity if those questions should ever be answered. Limits: single turn only (no history is sent to the model). See `ARCHITECTURE.md`.
- **DECIDED — Price numbers are never quantities; stated constraints are never dropped by a name search (2026-10-07).** No Backend Port change. A number beside a price word or currency ("under 300", "below Rs 15", "15 rupees", "₹300", "300 ke andar", "300 లోపు") is never read or stored as a quantity on any path (rules, fuzzy, model reply, pending choice, taps); a choice with no real quantity asks "how many?". With `IVA_LLM_ENABLED` + `IVA_LLM_RECOMMEND_ENABLED` on, a name search that carries a price limit, diet word or high-protein/low-calorie word goes to the recommendation flow, where the price is applied strictly in code and stated ("Showing items up to ₹300."), unless the words name one real product. Data the Ivar team must provide: native-speaker review of `apps/ai/constraint_cues.py` and the te/hi `recommend.max_price` text. Known gaps: spelled-out numbers, non-English ranges, decimals. See `ARCHITECTURE.md`.
- **DECIDED — Price ranges, spelled-out prices, unreadable prices, advice-line gate (2026-10-07).** No Backend Port change. Ranges ("250-300", "250 to 300", "between 250 and 300", "Rs 250-300", "250 se 300 tak", "250 nundi 300 varaku") set min and max strictly in code and are stated ("Showing items from ₹250 to ₹300."); a hyphenated number pair with no price cue is left alone and never becomes a quantity. Spelled-out prices ("under two hundred", Hindi/Telugu words) are read only next to a price word. A price word with no readable number gets "I couldn't read the price you mentioned..." instead of a failed search. On the constraint route the advice line needs a code-side cue as well as the model's flag. Data the Ivar team must provide: native-speaker review of `apps/ai/constraint_cues.py` (number words, range words), `apps/ai/advice_cues.py` and the te/hi `recommend.price_range`, `recommend.min_price`, `price.unreadable` text. **Open:** the advice gate is not yet applied on the cue/classifier routes (one existing test pins the old behaviour). See `ARCHITECTURE.md`.
- **DECIDED — Advice questions route to the recommendation flow; the advice line needs a real cue on every route; Hindi fractional prices (2026-10-07).** No Backend Port change. With the LLM flags on, an advice cue, or a goal word plus a question word, is a recommendation cue (named products and categories still win). `advice.disclaimer` shows only with the model's `asks_advice` AND an advice cue in the message (supersedes the 'Open' note in the entry above). Data the Ivar team must provide: native-speaker review of `apps/ai/advice_cues.py` (advice cues, question words) and of the Hindi fraction words in `apps/ai/constraint_cues.py`. See `ARCHITECTURE.md`.
- **DECIDED — Price, comparator and protein sort are read in code (2026-10-07).** No Backend Port change. The same message now gives the same constraints whatever the model returns or the conversation state: the model can add a category but never remove or change a code-read price, diet or sort; with a price cue the generic "Could you tell me a bit more" reply is never shown (no picks is the no-match reply). Data to review: `BARE_PROTEIN` in `apps/ai/constraint_cues.py`. See `ARCHITECTURE.md`.
- **DECIDED — Website menu link (2026-09-30).** `IVA_WEBSITE_SHOP_URL` (https only) adds a "See full menu on website" main-menu row that sends a `cta_url` message. No Backend Port change.
- **DECIDED — A plan message mid free-text step is a known limitation (2026-09-28).** If a customer pastes an "IVA PLAN v1" message while changing their diet preference or typing a delivery address, the engine's existing free-text interception for those two states takes it as that answer, not as a plan (same pre-existing behavior as any other free text in those states).

### Open contract requests — needs backend team input

From `ARCHITECTURE.md` §"Open contract requests for the backend team". Each has a
current workaround in place; none block integration but all should be confirmed or
resolved with the backend team.

| # | Request | Why | Current workaround |
|---|---|---|---|
| 1 | **OPEN** — a way to clear the diet preference in `update_profile` (e.g. an explicit "none" value) | `None` means "unchanged", so a preference cannot be removed once set | Customer can switch to `non_veg`, which shows everything |
| 2 | **OPEN** — `payment_url` on `OrderStatusView` while payment is `PENDING` or `FAILED` | Only `checkout` returns the link; it is lost once the session expires | Engine keeps the link from `checkout` in the session |
| 3 | **OPEN** — the real order-number format | Channel recognizes typed order numbers with a generic "letters + digits" pattern | Pattern accepts e.g. `IVA1002`; will be tightened once known |
| 4 | **OPEN** — the real quantity limit per cart line | Fake Backend uses 1–20 | Channel never hardcodes a limit, relies on `InvalidQuantity` |
| 5 | **OPEN** — an optional `diet` filter on `resolve_product_names` | Fuzzy name matching cannot respect the saved diet today | Used only for customers without a diet; others use `list_products(search=…, diet=…)` |
| 6 | **CLOSED 2026-10-01** — image file names for the 10 products that had 404'd under `eat/<product-id>.jpg` were supplied and stored (HEAD-verified): `ivar-oats`, `ivar-active-nutrition-drinks`, `millet-seed-crackers`, `ivar-muesli-bowl`, `fruits-nuts-seeds-bowl`, `morning-boost-shot`, `night-reset-shot`, `immunity-shield-shot`, `digest-ease-shot`, `breath-fresh-shot` | Every other product's image matches `eat/<its-own-id>.jpg` exactly; these 10 don't | `image_path` is `null` for these; they render as text-only cards, never hidden |
### Future work

- Cart screen via WhatsApp Flows: deferred until the real backend exists. Needs a data endpoint, Meta endpoint encryption (likely the cryptography library), a flow_token bound to the customer, and manual Meta setup. The in-memory Fake Backend only works with Flows in inline mode.


## 4. Running the contract test suite against a real adapter

Location: `apps/conversations/tests/contract/` (`harness.py`, `conftest.py`,
`test_contract.py`). It runs the same tests against any adapter that satisfies
`ContractHarness`.

From `apps/conversations/tests/contract/harness.py` and `README.md`, verbatim:

```powershell
$env:IVA_CONTRACT_HARNESS = "your_package.tests.iva_harness.RealHarness"
pytest apps/conversations/tests/contract -v
```

To implement a `RealHarness`, write a class with the same members as
`ContractHarness` (`apps/conversations/tests/contract/harness.py`):

```python
class ContractHarness(Protocol):
    backend: BackendPort

    def new_whatsapp_id(self) -> str:
        """A WhatsApp ID that has never been used with this backend."""

    def in_stock_variant(self) -> str:
        """ID of a variant that can be added to a cart and checked out."""

    def other_in_stock_variant(self) -> str:
        """A second, different in-stock variant."""

    def out_of_stock_variant(self) -> str:
        """ID of a variant whose product is out of stock."""

    def close(self) -> None:
        """Release resources after a test."""
```

Requirements stated in the harness module docstring: the harness must give each test an
isolated backend state (fresh customers are enough) and provide one in-stock and one
out-of-stock variant. `conftest.py` reads `IVA_CONTRACT_HARNESS` (in addition to the
built-in `"fake"` harness) and parametrizes every contract test to run against it via
`import_string`.

## 5. Environment variables

Names and formats exactly as in `.env.example` (no real values reproduced here — see
that file for placeholders, and never commit a filled `.env`). "Stage" is the stage
each was introduced per `CLAUDE.md`/`ARCHITECTURE.md`.

| Variable | Purpose | Format / example | Stage |
|---|---|---|---|
| `DJANGO_SETTINGS_MODULE` | Which settings module to load | `config.settings.dev` | S1 |
| `DJANGO_SECRET_KEY` | Django's own secret key | generated string; not yet defined further in `.env.example` | S1 |
| `IVA_PSEUDONYM_KEY` | Keys the HMAC pseudonyms of WhatsApp IDs; separate from `DJANGO_SECRET_KEY`; required in production, ≥32 chars | opaque string | S1 (see section 6) |
| `DJANGO_DEBUG` | Django debug flag | `false` | S1 |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated allowed hosts | `localhost,127.0.0.1` | S1 |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Comma-separated trusted origins for CSRF | not yet defined in `.env.example` (empty default) | S1 |
| `DATABASE_URL` | Org PostgreSQL connection string | `postgres://USER:PASSWORD@HOST:5432/DBNAME?sslmode=require` (per README) | S1 |
| `DB_CONN_MAX_AGE` | DB connection max age (seconds) | `60` | S1 |
| `DB_CONNECT_TIMEOUT` | DB connect timeout (seconds) | `5` | S1 |
| `REDIS_URL` | Cache Redis URL | org-provided; empty falls back to local memory cache | S1/S5 (TLS hardening) |
| `CELERY_BROKER_URL` | Celery broker Redis URL | org-provided | S2 |
| `CONVERSATION_REDIS_URL` | Session-store Redis URL | org-provided; required in production | S3 |
| `CONVERSATION_SESSION_TTL_SECONDS` | Session TTL | default `3600` if empty | S3 |
| `IVA_BACKEND_ADAPTER` | Dotted path to the Backend Port adapter class | dev/test: `apps.conversations.fake_backend.FakeBackend`; prod: the org's adapter (fake path rejected) | S1 |
| `IVA_PLANNER_URL` | The AI Planner page (built on the website, not this repo) | a real `https://` URL, or empty; unset/non-`https` skips the welcome CTA and menu link (not required in production) | AI Planner handoff (2026-09-28) |
| `IVA_WEBSITE_SHOP_URL` | The website's full-menu page | a real `https://` URL, or empty; unset/non-`https` hides the "See full menu on website" main-menu option (not required in production) | Website menu link (2026-09-30) |
| `IVA_CATALOG_ID` | The WhatsApp catalog's ID | empty = accept an order from any catalog; if set, orders from another catalog are ignored | Native catalog orders (2026-10-06) |
| `IVA_CATALOG_ORDER_MAX_QTY` | Per-product quantity cap for one catalog order (clamped, customer told) | integer, default `10`, effectively limited to the port's max line quantity | Native catalog orders (2026-10-06) |
| `IVA_LLM_ENABLED` | Turns the Groq LLM fallback on | `false` (default) or `true`; needs `GROQ_API_KEY` | LLM fallback (2026-10-06) |
| `GROQ_API_KEY` | Groq API key | secret, environment only; never logged or committed | LLM fallback (2026-10-06) |
| `IVA_LLM_MODEL` | Groq model | default `openai/gpt-oss-20b` | LLM fallback (2026-10-06) |
| `IVA_LLM_TIMEOUT_S`, `IVA_LLM_MIN_CONFIDENCE` | Call timeout; below this confidence the bot asks instead of acting | `4`, `0.7` | LLM fallback (2026-10-06) |
| `IVA_LLM_DAILY_CAP`, `IVA_LLM_CUSTOMER_PER_MINUTE` | Global daily call cap (Redis) and per-customer per-minute cap | `500`, `3` | LLM fallback (2026-10-06) |
| `IVA_LLM_BREAKER_FAILURES`, `IVA_LLM_BREAKER_COOLDOWN_S` | Circuit breaker: consecutive failures to open, cool-off seconds | `5`, `120` | LLM fallback (2026-10-06) |
| `IVA_LLM_MAX_INPUT_CHARS` | Longest message sent to the model (longer = skipped, never truncated) | integer, default in `.env.example` | LLM guards |
| `IVA_LLM_DAILY_TOKEN_CAP`, `IVA_LLM_MINUTE_TOKEN_CAP` | Daily and per-minute token budgets for model calls | integer; empty/0 = built-in default (never unlimited); set the daily one explicitly in production | LLM guards |
| `IVA_LLM_MINUTE_CAP`, `IVA_LLM_CUSTOMER_DAILY_CAP`, `IVA_LLM_CUSTOMER_BUDGET_SHARE_PCT` | Global per-minute request cap; per-customer daily cap; one customer's share of the daily token budget | defaults in `.env.example` | LLM guards |
| `IVA_LLM_TRIP_*` | Auto-trip of the kill switch: window, minimum calls, failure and rejection rates, cool-off | defaults in `.env.example` | LLM guards |
| `IVA_LLM_RECOMMEND_ENABLED` | Grounded product recommendations (needs `IVA_LLM_ENABLED`) | `false` (default) or `true` | Recommendations (2026-10-06) |
| `IVA_LLM_RECOMMEND_MAX_PICKS` | Products shown per recommendation | integer, default `4` | Recommendations (2026-10-06) |
| `IVA_CODE_CONSTRAINTS_ENABLED` | Code-only routes (price, diet, sort, goal words, product facts, labels, plan requests) | `true` (default); independent of every `IVA_LLM_*` setting, the Groq key and the kill switch | Code-only fixes (2026-10-12) |
| `IVA_SAFETY_KEYWORDS_ENABLED` | Emergency keyword check on free text (independent of the LLM flags) | `true` (default); only turn off to test | Health-aware recommendations (2026-10-07) |
| `IVA_EMERGENCY_CONTACT_TEXT` | Text shown after the fixed emergency sentence (for example the local emergency number) | owner-supplied wording, or empty; the channel never invents numbers | Health-aware recommendations (2026-10-07) |
| `IVA_FUZZY_CART_CONFIRM` | A cart change guessed by the typo-correction stage asks Yes/No first | `true` (default) | Fuzzy guard (2026-10-07) |
| `IVA_FUZZY_NUMBER_GUARD` | Never correct a word into a number word ("weight" -> "eight") | `true` (default, since 2026-10-07, owner-approved): the fuzzy stage once turned "weight" into "eight" and a quantity of 8. One existing test (`test_the_old_quoted_reply_is_unchanged_without_the_llm_layer`) was changed, with approval, to assert "weight" is never corrected into a quantity | Fuzzy guard (2026-10-07) |
| `IVA_PRODUCT_IMAGE_BASE_URL` | Base URL the Fake Backend joins with a product's relative image path to build `image_url` | a real `https://` URL, or empty; unset/non-`https` means no product images (text-only cards) | Product cards (2026-09-29) |
| `LOG_LEVEL` | Logging level | `INFO` | S1 |
| `LLM_PROVIDER` | LLM provider name | empty = no LLM fallback (rules/fuzzy only); no provider chosen yet | S4 |
| `LLM_API_KEY` | LLM provider API key | not yet defined in this repo (no provider chosen) | S4 |
| `LLM_MODEL` | LLM model name | not yet defined in this repo (no provider chosen) | S4 |
| `LLM_TIMEOUT_SECONDS` | LLM call timeout | default `5` if empty | S4 |
| `WHATSAPP_ACCESS_TOKEN` | Meta Cloud API access token | required in production | S2 |
| `WHATSAPP_APP_SECRET` | Verifies `X-Hub-Signature-256` on inbound webhooks | required in production | S2 |
| `WHATSAPP_VERIFY_TOKEN` | Webhook GET subscription verify token | required in production | S2 |
| `WHATSAPP_PHONE_NUMBER_ID` | Meta phone number ID | required in production | S2 |
| `WHATSAPP_API_VERSION` | Graph API version | no default; team sets the tested version (tests use placeholder `v21.0`) | S2 |
| `WHATSAPP_GRAPH_BASE_URL` | Graph API base URL | defaults to `https://graph.facebook.com` if empty | S2 |
| `IVA_MESSAGE_RETENTION_DAYS` | Days to retain conversation message metadata | default `90` if empty | S2/S5 |
| `IVA_RATE_LIMIT_PER_MINUTE` | Per-customer per-minute message cap | default `20` if empty | S5 |
| `IVA_FAKE_BACKEND_STATE_FILE` | Dev-only JSON file where the Fake Backend keeps customer state across restarts | a path (relative to the project root) or empty = in memory only; production refuses it | Dev persistence (2026-10-01) |
| `IVA_ORDER_WAIT_SECONDS` / `IVA_ORDER_WAIT_STATUS` | How long a buttons message waits for the preceding carousel/image card's status callback, and which status ends the wait | seconds (default `5`, `0` = off) / `sent` (default) or `delivered` | Message ordering (2026-10-01) |
| `IVA_CUSTOMER_LOCK_WAIT_SECONDS` | Max wait for the per-customer Redis lock | default `30` if empty | S5 |
| `IVA_CELERY_POLLING_INTERVAL_SECONDS` | Broker BRPOP timeout (idle command volume vs. Upstash free tier) | default `30` if empty | S5 |
| `IVA_CELERY_HEALTH_CHECK_INTERVAL_SECONDS` | Broker connection keepalive PING interval | default `60` if empty | S5 |
| `IVA_INLINE_PROCESSING` | Development-only synchronous processing (no worker/broker) | `true`/`false`; production refuses `true` | S5 |

Production additionally refuses wildcard `DJANGO_ALLOWED_HOSTS`/`DJANGO_CSRF_TRUSTED_ORIGINS`
and requires `IVA_PSEUDONYM_KEY` to differ from `DJANGO_SECRET_KEY` — see
`config/settings/prod.py::validate_production()`.

Not found in this repo: `IVA_CONTRACT_HARNESS` is not listed in `.env.example` — it is
set ad hoc as a shell environment variable only when running the contract suite against
a real adapter (see section 4), not part of the app's normal `.env`.

## 6. IVA_PSEUDONYM_KEY carry-over rule

Exact wording found in the repo:

`apps/iva_common/pseudonym.py` (module docstring):
> "A plain SHA-256 of a phone number can be reversed by trying every number, so the
> pseudonym is an HMAC keyed with a dedicated secret, `settings.IVA_PSEUDONYM_KEY`
> (never `SECRET_KEY`, so rotating Django's key does not touch conversation records).
> The key must be carried over unchanged when the apps move to another project;"

`config/settings/base.py` (comment above the setting):
> "Keys the HMAC pseudonyms of WhatsApp IDs (sessions, conversation records). Separate from
> SECRET_KEY; must be carried over unchanged into the organization's project."

`ARCHITECTURE.md` §"Contract decisions" (Pseudonym key):
> "Production requires it: at least 32 characters, and different from `DJANGO_SECRET_KEY`.
> Dev and test fall back to a clearly labelled insecure key; the dev `.env` has a generated one.
> **When integrating into the organization's project, carry this key over unchanged.**
> **Rotating it resets all conversation records and sessions:** existing rows and Redis
> sessions can no longer be matched to customers, and every customer starts at the main
> menu (onboarding data lives in the backend and is kept)."

`CLAUDE.md` §Environment:
> "Never rotate it casually: a new key resets all conversation records and sessions.
> Carry it over unchanged when integrating."

Practical consequence: when copying the apps into the organization's project, use the
exact same `IVA_PSEUDONYM_KEY` value already in use in this deployment. Enforced
mechanically: `config/settings/prod.py` requires it be ≥32 characters and different
from `DJANGO_SECRET_KEY`, or the process refuses to start (`ImproperlyConfigured`).

## 7. Scheduled jobs

Two management commands exist. **Confirmed: this repo does not configure Celery beat.**
`config/celery.py` only creates the `Celery("iva")` app, calls
`config_from_object("django.conf:settings", namespace="CELERY")` and
`autodiscover_tasks()` — no `CELERY_BEAT_SCHEDULE` (or any beat/crontab configuration)
appears anywhere in `config/`. As of the 2026-09-25 status note in `CLAUDE.md`, both
commands have only been run manually (`manage.py report_stalled_messages`, `manage.py
purge_conversation_messages`) as part of the hardening verification checklist. The
backend engineer must set up the actual recurring schedule (Celery beat, cron, or
another scheduler) in the target deployment — it does not exist in this repo today.

**`report_stalled_messages`** (`apps/whatsapp/management/commands/report_stalled_messages.py`,
wrapping `apps.whatsapp.services.report_stalled_messages`)
- What: logs one masked warning for each inbound message claimed (`processing_started_at`
  set) but not completed (`processing_completed_at` still empty) after 5 minutes —
  crash visibility for a worker that died mid-message. Marks each as reported. Never
  retries the message (keeps at-most-once processing).
- Intended schedule: every 5 minutes (`ARCHITECTURE.md` §4, §12 step 6).
- Current invocation: manual management command only; also exposed as the
  `whatsapp.report_stalled` Celery task per `ARCHITECTURE.md`, but nothing in
  `config/` schedules it periodically.

**`purge_conversation_messages`** (`apps/conversations/management/commands/purge_conversation_messages.py`,
wrapping `apps.conversations.retention.purge_old_messages` / `retention_days`)
- What: deletes `ConversationMessage`/`Conversation` metadata rows older than
  `IVA_MESSAGE_RETENTION_DAYS` (default 90). Message text is never stored in the first
  place, so this purges metadata only (message ID, type, status, timestamps, text
  length, reply ID, template key).
- Accepts an optional `--days` override (must be ≥1).
- Intended schedule: daily (`ARCHITECTURE.md` §12 step 6).
- Current invocation: manual management command only; no Celery beat entry exists.

## 8. Step-by-step integration instructions

1. Implement a real Backend Port adapter class in the organization's project,
   satisfying `apps.conversations.backend_port.BackendPort` (all six ports: Customer,
   Catalog, Cart, Address, Checkout, Order) exactly as specified in section 2 above,
   including every error type and the decisions in section 3.
2. Set `IVA_BACKEND_ADAPTER` (env var) to the dotted path of that class.
3. Implement a `RealHarness` (section 4) and run the contract suite against it until
   it passes:
   ```powershell
   $env:IVA_CONTRACT_HARNESS = "your_package.tests.iva_harness.RealHarness"
   pytest apps/conversations/tests/contract -v
   ```
4. Copy `apps/whatsapp`, `apps/conversations`, `apps/ai`, `apps/iva_common` into the
   organization's project (`ARCHITECTURE.md` §12). Add `apps.conversations` and
   `apps.whatsapp` to `INSTALLED_APPS`; include `apps.whatsapp.urls` at
   `webhooks/whatsapp/`.
5. Set every real environment variable per section 5 above: WhatsApp
   (`WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`,
   `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_API_VERSION`, `WHATSAPP_GRAPH_BASE_URL`),
   `DATABASE_URL`, `REDIS_URL`, `CELERY_BROKER_URL`, `CONVERSATION_REDIS_URL`,
   `DJANGO_SECRET_KEY`, and LLM variables if/when a provider is chosen (none is chosen
   in this repo yet — `CLAUDE.md` §Approved dependencies).
6. Copy `IVA_PSEUDONYM_KEY` over **unchanged** from the existing deployment (section 6)
   — do not generate a new one.
7. Confirm production settings refuse the Fake Backend: `config/settings/prod.py`'s
   `validate_production()` raises `ImproperlyConfigured("The Fake Backend must not be
   used in production.")` whenever `IVA_BACKEND_ADAPTER` contains
   `apps.conversations.fake_backend` or `FakeBackend` (`FAKE_BACKEND_PATHS`). The same
   function also refuses `IVA_INLINE_PROCESSING=true` (development-only, §9/§11) and
   enforces every other required production value (secret key, database, Redis URLs,
   allowed hosts, WhatsApp credentials, pseudonym key length/difference from secret
   key), and rejects wildcard hosts/origins.
8. Set up the actual recurring schedule for `manage.py purge_conversation_messages`
   (daily) and `manage.py report_stalled_messages` (every 5 minutes) — Celery beat, cron
   or equivalent. Confirmed: neither is scheduled anywhere in this repo today (section 7).
9. Run `manage.py migrate`.
10. Run the full test suite in the new project (`pytest -v`), plus
    `manage.py check --deploy` against production settings — expect it clean except the
    explained `security.W021` HSTS-preload warning (`config/settings/prod.py` comment).
11. Before going live, verify: `GET /health/ready/` returns 200 with `database` and
    `redis` both `ok`; `manage.py migrate --check` shows no pending migrations; a real
    Celery worker (Windows: `--pool=solo`) connects to the real broker and processes a
    real `whatsapp.process_inbound` task end to end; `report_stalled_messages` and
    `purge_conversation_messages` both run cleanly against the real database; then work
    through the manual Meta test checklist in `README.md` (§"Manual test checklist
    (Meta's test number)") with a real WhatsApp client and Meta's test number — this is
    explicitly not automated and must be done by a human. Also complete §11's
    pre-handover real-worker check (also human-only) and confirm `IVA_INLINE_PROCESSING`
    is unset or `false` — production refuses to start otherwise.

## 9. Deployment notes

- **Idle broker usage.** ~74 Redis commands per 5 minutes with one worker (~21,300/day),
  measured against the real Upstash broker after the 2026-09-25 free-tier tuning
  (`CLAUDE.md` has the full per-source breakdown and daily estimate). The worker already
  runs with `--without-gossip --without-mingle --without-heartbeat` by default (this is
  a single-worker deployment) and `IVA_CELERY_POLLING_INTERVAL_SECONDS` /
  `IVA_CELERY_HEALTH_CHECK_INTERVAL_SECONDS` default to the lowest-usage values actually
  measured (30 / 60). A paid or less command-constrained tier can lower those two env
  vars, or add a second worker (bringing gossip/mingle/heartbeat back), with no code
  change. The dominant remaining source (~88%) is Kombu's own unacked-message restore
  scan. It is **not** gated by `CELERY_TASK_ACKS_LATE` — confirmed by measuring with
  `acks_late=False` against the real broker (same ~20-24s cadence, same command volume,
  no change) before concluding this, per `CLAUDE.md`'s 2026-09-25 restore-scan
  investigation note. It is registered unconditionally by Kombu's redis transport for
  any channel using the async event-loop consumer at all, not exposed via any
  Celery/Kombu setting, so it was left alone rather than monkeypatching a third-party
  library's transport internals — see `CLAUDE.md` for the full trade-off.
- **Two processing modes, one shared code path.** `apps/whatsapp/tasks.py`'s
  `_process_once` is the single implementation of claim/lock/rate-limit/at-most-once
  processing. `process_inbound` (the Celery task) and `process_inbound_inline`
  (`IVA_INLINE_PROCESSING=true`, called directly from the webhook view, no worker or
  broker) both call it; only the lock-retry mechanism differs (Celery's `self.retry()`
  vs. a blocking sleep with the same attempt budget). **Inline mode is development
  only** — `config/settings/prod.py` refuses to start with it on. Do not carry it into
  the organization's project; production always uses the worker path. See §11 below
  before going live.

## 10. Production Redis sizing

Measured against the real Upstash broker during the 2026-09-25 free-tier tuning and
inline-processing work (full detail and per-source breakdown in `CLAUDE.md`).

- **Idle worker usage.** ~74 Redis commands per 5 minutes with one worker (~21,300/day)
  with the low-usage defaults (`IVA_CELERY_POLLING_INTERVAL_SECONDS=30`,
  `IVA_CELERY_HEALTH_CHECK_INTERVAL_SECONDS=60`, worker run with `--without-gossip
  --without-mingle --without-heartbeat`). Breakdown: Kombu's own unacked-message restore
  scan ≈ 88% of idle volume, `BRPOP` polling ≈ 9%, the rest negligible. The restore scan
  is registered unconditionally by Kombu's Redis transport for any channel using the
  async event-loop consumer — it is not exposed via any Celery/Kombu setting and is not
  gated by `CELERY_TASK_ACKS_LATE` (confirmed by measuring with `acks_late=False`
  against the real broker: same cadence, same volume). **It cannot be disabled safely.
  Never patch Kombu internals to suppress it.**
- **Per-message usage.** ~6 Redis commands for a simple message (per-customer lock
  acquire/release, rate-limit counter, conversation session save), roughly 6–20
  depending on the action.
- **Free tier.** Development only, with `IVA_INLINE_PROCESSING=true` (no worker or
  broker at all — idle usage ~0, since inline mode bypasses Celery/Kombu entirely).
- **Production.** The idle restore-scan volume above makes a hard-capped free/low-tier
  Redis plan unsuitable for a production worker. Options: Redis without tight
  per-command limits (self-hosted, or a fixed-price plan), a paid pay-as-you-go plan
  that absorbs the idle volume, or RabbitMQ as the Celery broker instead of Redis
  (Redis is still required separately for sessions, the per-customer lock, cache and
  rate limiting — see section 5's `REDIS_URL`/`CONVERSATION_REDIS_URL`).

## 11. Pre-handover real-worker check

Inline mode (§9 above) is what day-to-day development actually runs on, so the
lock-retry path's real-worker behavior needs a separate, explicit check before
handover — it is only automatically tested under Celery's eager mode
(`apps/whatsapp/tests/test_lock_retry.py`). **This needs a real phone and has not been
run as part of this work:**

1. Set `IVA_INLINE_PROCESSING=false` in `.env`, start `python manage.py runserver` and
   the Celery worker with the documented low-usage flags (README.md's Run section:
   `--pool=solo --without-gossip --without-mingle --without-heartbeat`).
2. Send three messages rapidly, back to back, from one real phone. All three should
   get replies, in order.
3. Run one full journey: onboarding → add to cart → checkout → order status.
4. Stop the worker afterwards and set `IVA_INLINE_PROCESSING` back to `true`.

## 12. Operations runbook: the model (Groq) layer

The model is optional. Price limits, diet words, sorting, goal words, emergency, self-harm, allergen,
ingredient and medical replies, product-fact, label and plan answers, and cart guards are code and keep
working when it is off (tests cover the flags off, the kill switch on and the breaker open). These commands need `REDIS_URL` set so the flag is shared by every
process (without it the flag is per-process and the command says so).

**Switch the model off (and on) without a restart**

```powershell
python manage.py llm_killswitch --on       # no model call is made from the next message
python manage.py llm_killswitch --status
python manage.py llm_killswitch --off
```

The system can also switch itself off for a cool-off period when the failure or validation-
rejection rate gets too high; `--status` shows `auto-tripped`. A manual switch is never overridden
by an automatic one. If the shared store cannot be read, the model stays off.

**Check usage and budget**

```powershell
python manage.py llm_stats                 # today's calls, tokens, budget used, skips by reason
```

Output is counts only (no customer text, identifiers or secrets). Skips by reason tell you what is
limiting calls (budget, per-customer limit, provider pause, kill switch, input limit).

**Suspected key leak**

1. Switch the model off: `llm_killswitch --on`.
2. Revoke the key in the provider console and create a new one.
3. Put the new key only in the environment (`GROQ_API_KEY`); restart web and worker.
4. Check provider usage for calls you did not make; check application logs and the repository
   history for the old value (rotate anyway; do not rewrite shared history).
5. Switch back on: `llm_killswitch --off`.

**Provider limits.** A 429 pauses model calls for the period the provider advises; customers get
the normal code-only reply meanwhile. Repeated failures open the circuit breaker the same way.

## 13. Test isolation and the cost of the model guard

**Redis in tests.** Tests that touch the real, shared Redis server use only keys under the test
root (`IVA_TEST_REDIS_KEY_PREFIX`, "iva-test:") and a private Celery queue name; the cache used by
the application code under test is in-memory. For `reset_dev_data`, an autouse fixture in
`apps/whatsapp/tests/test_reset_dev_data.py` repoints the command's key prefixes, queue and purge
at test values for EVERY test in that file and asserts loudly that none of them equals or sits under
the dev root, the real dev prefixes or the shared queue. Celery's `control.purge()` empties every
queue, so the suite never calls it (a test-queue-only purge stands in; a mocked test pins that the
production function still uses the supported API). Those tests use long socket timeouts and a
bounded retry, because the remote server's first TLS handshake is sometimes slow. The session
fixture in the root `conftest.py` sweeps only the test root.

**Guard cost.** A guarded model call used to cost about thirty sequential Redis commands (kill
switch, pause, breaker, request counters, token reservation, settlement, metrics, rolling window):
seconds of added latency at a typical round-trip time. On Redis the guard now does its work in two
server-side scripts, one before and one after the call (`apps/ai/llm_scripts.py`): two commands and
two round trips per call, still atomic. The in-memory cache (tests, runs without `REDIS_URL`) keeps
the plain per-command code; `apps/ai/tests/test_llm_guard_scripts.py` runs the same scenarios on both
and checks the command count on Redis.

**Per-minute window.** `IVA_LLM_CUSTOMER_PER_MINUTE` is counted per customer in FIXED wall-clock minutes (`int(timestamp // 60)`), not a sliding 60 seconds: at most the limit inside each minute, so up to twice the limit across a minute boundary, and refused attempts still increment the per-customer counter (only the global counters skip refused calls); tests that assert a call count therefore pin the clock inside one minute with `LLMGuard(now=...)`, and a separate test pins the boundary behaviour.

## 14. Safety wording and data that MUST be reviewed before real customers use it

All safety wording (emergency, self-harm, allergen, ingredient, medical and suitability) is a DEFAULT and
UNREVIEWED. It MUST be reviewed by a qualified person before real customers use it, and all non-English
text by native speakers. Nothing here has been reviewed.

- Reply templates: `safety.emergency`, `safety.self_harm` (same text as emergency until a reviewer supplies
  wording), `safety.allergy`, `safety.ingredient_info`, `safety.medical`, `safety.see_doctor`,
  `advice.disclaimer`, `recommend.medical`, `recommend.unsupported`, `popularity.unsupported`,
  `recommend.browse`, `stale.expired`, `numbers.ignored` (Telugu and Hindi included).
- Keyword and cue data: `safety_keywords.py` (emergency, injury, self-harm), `code_cues.py` (allergen,
  ingredient, suitability, facts, labels, plans), `recommend_cues.py`. Keywords and the model cannot catch
  every phrasing; coverage is incomplete in every language, and the non-English lists are starter sets.
- `IVA_EMERGENCY_CONTACT_TEXT` is the owner's own wording; leave it empty if unsure (default empty).
- Golden data `apps/ai/golden/recommend_fixes_golden.json` (non-English entries marked
  `needs_native_review`): `python manage.py eval_intent_llm --set recommend_fixes --dry-run` (no model).

**Hindi and Telugu text added or changed in the address, opening and closing fixes (needs native-speaker review).** New in both `messages/hi.py` and `messages/te.py`: `checkout.address_unclear` (asks again for a delivery address and shows an example), `greeting_noname` and `welcome_back_noname` (greetings without a name), `checkout.thanks` (thank-you line after an order is placed) and `fallback.hint` (one-line hint after "I did not understand"). Spelling only, no other change: Hindi "मेनू" became "मेन्यू" (the form used most) in `diet.ask`, `welcome.browse_prompt`, `btn.browse_menu`, `menu.button`, `help`, `btn.menu`, `clarify.CATEGORY_SELECT`, `search.none`, `ask.product`, `cards.footer` and `order.excluded_unknown`, and "मेन्यू" was added to the Hindi words that leave the address step; Telugu "చెక్‌అవుట్" and "చెకౌట్" became "చెక్అవుట్" in `checkout.welcome_back` and `btn.continue_checkout`.
