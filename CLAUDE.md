# CLAUDE.md — IVA WhatsApp Channel

Standing instructions for any AI coding agent working in this repository. Read this file and `ARCHITECTURE.md` at the start of every session. The full specification is in `Reference/IVA_WhatsApp_FreshStart_Prompt.pdf`.

## What this project is

IVA is a WhatsApp ordering assistant for Ivar (healthy foods). **This repository builds only the WhatsApp channel:** the webhook, the conversation engine and the intent layer. The organization's backend team builds the catalog, customers, cart, orders, payments, delivery and infrastructure. We talk to their backend only through the **Backend Port** (`apps/conversations/backend_port.py`).

## Current status

Update this table at the end of every session.

| Stage | Content | Status |
|---|---|---|
| S1 | Host project, Backend Port, Fake Backend, catalog fixture | Built and verified — DB tests pass on the org's PostgreSQL (Neon) |
| S2 | WhatsApp webhook + Cloud API client | Built and verified — all 13 database tests pass |
| S3 | Conversation engine | Built and verified — 2 real-Redis session-store tests pass (`CONVERSATION_REDIS_URL` set) |
| S4 | Intent layer | Built — LLM provider still not chosen (FakeLLMClient only) |
| S5 | Hardening + end-to-end tests | Done (2026-09-25) — per-customer ordering, per-customer rate limit, `check --deploy`, e2e and failure-test coverage all built and passing against real Neon/Upstash infra; review fixes applied and approved 2026-09-25 (lock TTL derived from the task time limit, lock-timeout retry-then-report instead of dropping, Upstash idle-polling tuning). Two manual, human-only items remain outside automated scope: native-speaker review of Telugu/Hindi text, and the manual Meta test checklist in README.md (needs a real WhatsApp client and Meta test number) |

**2026-09-24 pre-S5 hardening:** all three Redis connections (cache, conversation
sessions, Celery broker) now explicitly require certificate verification
(`ssl_cert_reqs=ssl.CERT_REQUIRED`); the `rediss` "no ssl options" warning is gone.
`DATABASE_URL`/`REDIS_URL`/`CONVERSATION_REDIS_URL`/`CELERY_BROKER_URL` are all live
(org-provided Neon Postgres + Upstash Redis). `pip check`, `ruff check`, `ruff format
--check` and the full `pytest -v` (787 tests) are clean. `showmigrations` confirms no
pending migrations; the running dev server's `/health/ready/` returns 200 with database
and redis both `ok`; a real `--pool=solo` Celery worker connected to the real broker and
executed a real queued `whatsapp.process_inbound` task; `report_stalled_messages` and
`purge_conversation_messages` both ran cleanly against the real database. **Known infra
issue (resolved 2026-09-25):** `DATABASE_URL` used Neon's pooled (PgBouncer) endpoint,
which intermittently left one idle backend connection open on `test_neondb` after a
test run, causing the next `pytest` run's database creation to fail with "database is
being accessed by other users" until Neon recycled it (unpredictable, sometimes
minutes). Fixed by pointing `DATABASE_URL` at Neon's direct/unpooled endpoint (same
host without `-pooler`) for local dev and test use — Neon's own documented fix for this
exact Django-test-runner conflict. See the 2026-09-25 note below for confirmation.

**2026-09-24 S5 hardening:** `apps/whatsapp/locks.py` (per-customer Redis lock,
`CustomerLockTimeout` reported like a crash via `report_stalled_messages`, fails open
if Redis is unreachable) and `apps/whatsapp/rate_limit.py` (per-minute cap via
`settings.CACHES`, one localized "please slow down" reply then silence, fails open if
Redis is unreachable) are wired into `tasks.process_inbound` ahead of the engine call.
New settings `IVA_RATE_LIMIT_PER_MINUTE` (default 20) and
`IVA_CUSTOMER_LOCK_WAIT_SECONDS` (default 30). `manage.py check --deploy` against prod
settings is clean except one deliberately-explained warning (`security.W021`, HSTS
preload — see `config/settings/prod.py`); a regression test pins this
(`test_deploy_check_has_no_unexplained_warnings`). New end-to-end tests
(`apps/whatsapp/tests/test_e2e.py`) drive the real webhook through a full customer
journey (onboarding, discovery, cart, checkout, payment pending → paid, order status,
then a returning-user message) with signed payloads, the real database, real Redis and
an eager Celery task, in English, Telugu, Hindi, and three romanized/code-mixed
variants — all pass. Failure-mode tests (backend/LLM/WhatsApp-API down, duplicate
webhook, invalid signature, expired session, session-Redis down, worker
crash/stalled) were already covered pre-S5; new lock/rate-limit-specific failure tests
added in `test_locks.py`/`test_rate_limit.py`. The full suite was 805 tests, all
passing, at that point — see the 2026-09-25 note below for the current total (809).
**Scope decision:** the per-customer-lock ordering test ("two near-simultaneous
messages") uses real threads at the unit level (`test_locks.py`), not two concurrent
webhook requests — `CELERY_TASK_ALWAYS_EAGER` (test settings) makes webhook-level calls
execute synchronously one after another, so they can never actually race; the unit test
is the only way to exercise genuine concurrency here. README.md has a manual test
checklist for a live check against Meta's test number (not automated; nothing in the
test suite calls the real Meta API).

**2026-09-25 S5 hardening confirmation:** `DATABASE_URL` switched from Neon's pooled
(PgBouncer) endpoint to Neon's direct/unpooled endpoint, resolving the pooled-connection
issue noted above. Full confirmation checklist re-run against real infra, all passing:
`pytest -v` — 809 passed, 0 skipped, 0 warnings, 234.78s (4 more than the previous 805,
from broker TLS verification tests added for the pre-S5 hardening work); `pip check` —
"No broken requirements found"; `manage.py migrate --check` — exit 0, no pending
migrations; the running dev server's `/health/ready/` returns 200 with body
`{"status": "ok", "checks": {"database": "ok", "redis": "ok"}}`; a real `--pool=solo`
Celery worker connected to the real Upstash broker (`rediss://`) and executed two real
queued `whatsapp.process_inbound` tasks successfully end to end (one unrelated stray
leftover message, `iva_verification_probe`, an unregistered task from earlier ad hoc
debugging, was received and discarded by Celery with a harmless KeyError/traceback —
queue debris, not a code defect); `manage.py report_stalled_messages` — "Reported 0
stalled message(s)"; `manage.py purge_conversation_messages` — "Deleted 0 message rows
older than 90 days." The dev server and Celery worker were both stopped afterward; no
stray python processes remained.

**2026-09-25 S5 review fixes (approved):** three fixes from review. (1) The
per-customer lock's TTL (`apps/whatsapp/locks.py`) now defaults to
`CELERY_TASK_TIME_LIMIT` (60s) plus a 10s safety margin, derived at call time, instead
of a fixed 30s that could be outlived by a slow message before its lock was — a slow
task's hard-kill skips the lock's own cleanup, so the TTL must always be the longer of
the two. (2) A `CustomerLockTimeout` no longer silently drops the message:
`apps/whatsapp/tasks.py` releases the claim and retries via Celery (`self.retry()`, up
to 3 attempts, 2s countdown) since the engine hasn't run yet at that point; only once
retries are exhausted is it reported stalled immediately (`services.report_lock_exhausted`)
rather than waiting for the 5-minute sweep. At-most-once still holds throughout. (3) An
initial Upstash idle-command reduction: `CELERY_BROKER_TRANSPORT_OPTIONS =
{"polling_interval": 5}` (from Kombu's 1s default), verified via the installed Kombu
source that this maps straight onto `brpop_timeout`, a blocking read that returns
immediately on message arrival — raising it cannot add reply latency. Measured against
the real Upstash broker, idle, 300s each: before 270 commands, after 214 commands (a
smaller drop than a naive 5x BRPOP estimate, because most of the remainder turned out to
be Celery's own Gossip component republishing worker heartbeats every 5s regardless of
this setting — see the free-tier tuning note below, where that gets removed too). Full
suite re-verified at 816/816 passing.

**2026-09-25 Upstash free-tier tuning:** the user is on Upstash's free tier and asked
for idle command volume to come down as far as possible without adding reply latency or
weakening correctness. Changes, all confirmed against the real broker: (1) the worker
now always runs with `--without-gossip --without-mingle --without-heartbeat` (README.md,
this file's Common commands, and the manual-test-checklist command all updated) — this
is a single-worker deployment, so multi-worker coordination and `celery inspect`
visibility cost nothing to give up; add the flags back if a second worker ever joins.
(2) `polling_interval` raised from 5 to 30 via a new env var,
`IVA_CELERY_POLLING_INTERVAL_SECONDS` (default 30) — a real worker still picked up a
freshly-enqueued task in ~1-2s either way (measured directly, and confirmed invariant
whether `polling_interval` was 1 or 30, so the delay is network/Celery overhead, not the
setting). (3) `health_check_interval` raised from Kombu's 25s default to 60s via a new
env var, `IVA_CELERY_HEALTH_CHECK_INTERVAL_SECONDS` (default 60) — empirically, a
connection with no traffic and no health check at all survived 130s of true silence
against this Upstash instance (Upstash documents no idle-disconnect threshold), so 60s
leaves a wide margin; for this app's workload (no fanout/pub-sub queues) the async
transport's health-check timer only touches an unused pub/sub subclient anyway, so this
setting mainly matters as documented headroom, not as a large direct command saving
here. (4) Investigated the periodic unacked-message restore scan
(`kombu.transport.redis.MultiChannelPoller.maybe_restore_messages`): real behaviour
(measured with `MONITOR`: fires roughly every 20-24s) does not match what a plain
reading of its source suggests (a 10s schedule gated by a 10-call skip counter, which
implies ~100s) — the exact reason for the faster real cadence was not pinned down. This
was investigated further in a later session (see the 2026-09-25 restore-scan
investigation note below): it turned out not to be gated by `CELERY_TASK_ACKS_LATE` at
all. It is now the dominant idle-command source (see numbers below). (5) Confirmed no
other component
in this codebase polls Redis while idle: the cache, session store, per-customer lock and
rate limiter are all request/task-driven only (grepped for background threads/timers and
`AppConfig.ready()` hooks; none exist), and worker/task events were already off and stay
off. (6) Every tuned value is now an env var defaulting to the measured low-usage value,
so a paid tier can raise them again with no code change:
`IVA_CELERY_POLLING_INTERVAL_SECONDS` (30), `IVA_CELERY_HEALTH_CHECK_INTERVAL_SECONDS`
(60) — both in `.env.example`. (7) Final measurement, idle, 300s, real broker, pytest
and runserver both stopped throughout: **74 commands total** (down from 270 before any
of this session's tuning, down from 214 after just the first `polling_interval: 5`
step). Per-source breakdown via a 130s real `MONITOR` capture (Upstash's own `INFO
commandstats` came back empty despite a real total, so this was the only way to get
per-command names): restore-scan family (`SET`+`ZREVRANGEBYSCORE`+`EVALSHA`+`GET`+`DEL`
per cycle) ≈ 65 commands/5min (~88%, dominant); `BRPOP` ≈ 7 commands/5min (~9%,
matches the 30s `polling_interval`); health-check `PING` ≈ 0 observed (see point 4
above); other ≈ 0. Estimated daily total at this idle rate: 74 x 288 ≈ **21,300
commands/day** (down from an original unoptimized baseline of ~78,000/day at the old
1s-polling/25s-health-check/gossip-on defaults). Full suite re-verified passing after
every change (final run: 818/818).

**2026-09-25 restore-scan investigation (no code change adopted):** the restore scan is
~88% of idle command volume, so the user asked whether `CELERY_TASK_ACKS_LATE=False`
would stop it, given the claim guard in `apps.whatsapp.services` already makes a
redelivered-but-claimed message a no-op (`acks_late` would then only protect the
narrow window between dequeue and claim, not the whole task). Measured directly against
the real broker, no code adopted without measuring first: with `CELERY_TASK_ACKS_LATE`
temporarily set to `False`, a 130s real `MONITOR` capture showed the *same* restore-scan
pattern at the *same* ~20-24s cadence — 28 commands/130s vs. 31/130s with
`acks_late=True`, i.e. no measurable change (a matching 300s checkpointed
`total_commands_processed` diff read 0 both before and during, which turned out to be
an artifact of that counter — it doesn't update reliably over short windows on this
Upstash instance; `MONITOR`, a direct observation of real commands as they happen, is
the trustworthy source here, not that counter). **Conclusion: reverted immediately,
`acks_late` stays `True`.** What actually drives the scan: Kombu's
`Transport.register_with_event_loop()` (`kombu/transport/redis.py`) registers the
`maybe_restore_messages` timer *unconditionally* for any channel run through the async
event-loop consumer at all — it is core to how Kombu implements reliable delivery over
Redis, wired in at the transport level, not gated by Celery's `task_acks_late` setting.
There is no supported way to disable or slow it (only its batch size,
`unacked_restore_limit`, is exposed) short of monkeypatching Kombu's transport
internals, which was not done. Before/after idle-command daily estimate: unchanged —
still ~21,300 commands/day (see the free-tier tuning note above); this investigation
did not reduce it further. Full suite re-verified passing (818/818) after reverting.

**2026-09-25 inline-processing mode (development only):** to eliminate Upstash usage
entirely for day-to-day development (no worker running at all, not just a tuned-down
one), the webhook can now process a message synchronously instead of enqueuing it.
`apps/whatsapp/tasks.py` was refactored so both paths share one implementation:
`_process_once` holds all claim/lock/rate-limit/at-most-once logic; `process_inbound`
(the Celery task) and the new `process_inbound_inline` (called directly from
`apps/whatsapp/views.py` when `IVA_INLINE_PROCESSING=true`) both call it, differing
only in how a `CustomerLockTimeout` retries — Celery's `self.retry()` for the task,
a blocking `time.sleep()` with the same `MAX_LOCK_RETRIES`/countdown for inline, since
there is no broker to re-enqueue onto. The webhook always returns 200 once a message
is recorded: `apps/whatsapp/views.py::_process_inline` catches any exception from
inline processing (masked-logged, never propagated into the existing 503 path), since
the message is already claimed and Meta must not redeliver something we already have
— it surfaces via `report_stalled_messages` exactly as a worker crash would.
`config/settings/prod.py::validate_production()` now also refuses
`IVA_INLINE_PROCESSING=true`. New tests: inline lock-retry succeeds after the lock
frees, inline lock-retry exhausted reports stalled once
(`apps/whatsapp/tests/test_inline_processing.py`), the webhook returning 200 despite
an inline-processing crash, and the production refusal
(`host/tests/test_prod_settings.py::test_inline_processing_is_refused`). Set
`true` in the dev `.env`; `.env.example` keeps the safe default (`false`), documented
there. `config/settings/test.py` forces `IVA_INLINE_PROCESSING = False` regardless of
the developer's own `.env` (same reasoning as its existing `IVA_BACKEND_ADAPTER`
override), since `read_env()` would otherwise leak the dev `.env`'s `true` into the
whole test process; `host/tests/test_prod_settings.py`'s `VALID_ENV` was given the same
explicit-override treatment for its own subprocess tests. Measured against the real
broker with `IVA_INLINE_PROCESSING=true`, `runserver` running, no worker at all: idle
Redis usage was low enough that a separate `MONITOR` connection's own socket read timed
out after 300+ seconds of total silence — i.e. genuinely ~0, not just low. One real
message (a fresh customer's first "hi", via `django.test.Client` with the outbound
WhatsApp client faked so nothing ever reached Meta, instrumented at
`redis.Redis.execute_command` since a separate `MONITOR` connection did not reliably
observe this short-lived script's own connections against Upstash) cost **6 Redis
commands** (`SET`×3, `EXISTS`×1, `GET`×1, `EVAL`×1 — the per-customer lock
acquire/release, the rate-limit counter, and the conversation session save). Full
suite re-verified passing (823/823) before the live measurements (no code changed
after that run). README.md and HANDOVER.md both now describe the two modes
consistently (inline = development only; worker = production and the pre-handover
real-worker check) and carry the same "Pre-handover real-worker check" steps — not run
as part of this work, needs a real phone.

**2026-09-28 shorter onboarding + AI Planner handoff (scope update, approved):** two
changes, both requested and approved together. (1) Onboarding is now language-only:
name and city are no longer asked. The customer's WhatsApp contact profile name
(`contacts[].profile.name` in the webhook, previously deliberately not read — see the
2026-09-28 note in `apps/whatsapp/parser.py`) is read, sanitized (trim, collapse
whitespace, strip control/format characters, cap at 60 chars) and saved via
`update_profile(name=...)` only when the customer has no saved name yet; it is never
logged. City is no longer collected at all (the port still has the field; nothing in
the channel sets it now). Diet stays exactly as before, reachable only via "Change
preference" in the menu. `State.ONBOARD_NAME`/`ONBOARD_CITY` were removed;
`State.ONBOARD_DIET` is unchanged (still reused for the later change-preference flow).
(2) The AI Planner handoff: a new `IVA_PLANNER_URL` setting (unset/non-https is treated
as "not configured" and the link is simply skipped, straight to the main menu). When
configured, onboarding ends with a new `OutCTA` message (a real WhatsApp `cta_url`
interactive button, added to `apps/conversations/channel.py` and
`apps/conversations/renderers.py` — this channel previously only had reply
buttons/lists) plus a "Browse menu" follow-up, and a "Plan my meals" row is added to the
main menu (`MAX_MENU_GROUPS` reduced 5 -> 4 to keep the list within WhatsApp's 10-row
cap). The planner sends the customer back via `wa.me` with an `IVA PLAN v1` message;
`apps/ai/plan_message.py` parses it deterministically (own length cap, checked before
`apps.ai.pipeline`'s normal `MAX_TEXT_LENGTH` truncation) into a new, LLM-unreachable
`Intent.PLAN_RECEIVED` (excluded from `CLASSIFIABLE_INTENTS` on purpose — this must only
ever come from the header parser). Unknown keys/values and health data we do not accept
(BMI, BMR, TDEE, weight range, nutrition score) are dropped silently, never logged, never
stored. Picks are resolved one at a time through
`resolve_product_names`/`get_product` (first in-stock variant used automatically — the
customer is never asked to pick a variant here); only the port's name/price are ever
shown. "Add all to cart" adds each resolved pick via `add_item`, skipping (and
mentioning) anything that has gone out of stock since. See `PLANNER_INTEGRATION.md` for
the website team's contract and `ARCHITECTURE.md`'s Contract decisions for the design
record. Tests: 860 passed (up from 823), full suite, real Neon/Upstash infra, `ruff
check`/`ruff format --check` clean on every file touched this session. A repo-wide run
of both also surfaced pre-existing findings in files this session never touched
(`apps/whatsapp/services.py`, `apps/whatsapp/tasks.py`, `apps/whatsapp/tests/
test_inline_processing.py`, `apps/whatsapp/tests/test_lock_retry.py`) — left alone,
out of scope for this change; worth a follow-up cleanup pass. Telugu/Hindi text for the
new messages follows the existing best-effort convention and falls under the same
standing native-speaker-review item below.

**2026-09-28 `reset_dev_data` management command (approved):** a dev-only, reusable way
to wipe accumulated test data, always dry-run unless `--confirm` is passed. Lives under
`apps/whatsapp/` (not `apps/conversations/`), since it needs to touch both apps' data
and the declared import direction is `whatsapp → conversations → ai → iva_common`.
Deletes exactly three things: (1) Postgres — `Conversation`/`ConversationMessage` via
the ORM's own `.all().delete()`, never raw SQL or migrations, confirmed the only two
IVA-owned tables in this database (`django_migrations`/`django_content_type` are
Django/`contenttypes`-internal and untouched); (2) Redis — only IVA's three known key
prefixes (`apps.conversations.state`'s session prefix, `apps.whatsapp.locks`'s
per-customer-lock prefix, `apps.whatsapp.rate_limit`'s counter prefix, the last matched
against Django's actual wrapped cache key format, read from `settings.CACHES`, not a
bare prefix guess), found via `scan_iter` and deleted in chunks — never `FLUSHDB`/
`FLUSHALL`, never an unscoped `SCAN MATCH "*"` (this matters: `REDIS_URL`/
`CONVERSATION_REDIS_URL`/`CELERY_BROKER_URL` can share one physical Redis instance,
which also holds Celery's own bookkeeping keys); (3) Celery — a read-only `LLEN` on the
default queue for the dry-run count, `celery.current_app.control.purge()` (the
supported API, not a raw key delete) for `--confirm`. Refuses to run (raises
`CommandError`, nothing touched) unless **both** independent guards pass:
`settings.IVA_DEV_RESET_ALLOWED` is `True` — a plain, non-env setting
(`config/settings/base.py`, `False`; only `config/settings/dev.py` sets it `True`, so no
stray `.env` value can ever flip it on) — and `settings.IVA_BACKEND_ADAPTER` is the Fake
Backend. (An earlier draft compared `settings.SETTINGS_MODULE` against the literal
string `"config.settings.prod"`; replaced because `apps/iva_common/tests/
test_portability.py` treats a dotted `config.`/`host.` string the same as a real import
and rightly flagged it — apps/ must never reference the host's module layout at all, not
even as a string. `IVA_DEV_RESET_ALLOWED` avoids that while being an even more direct,
explicit switch, per request.) Output is counts only — no key, pseudonym, message text
or row field is ever printed. New tests (`apps/whatsapp/tests/test_reset_dev_data.py`) run against the
real dev Postgres/Redis but scope every Redis-touching assertion to per-test, throwaway
prefixes/queue names (monkeypatched onto `apps.whatsapp.dev_reset`) so running the test
suite itself never wipes real session/lock/rate-limit state; the one unavoidable
exception is `control.purge()` in the `--confirm` test, which purges every queue Celery
knows about with no supported way to scope it to one queue (expected to be a no-op in
practice: no beat schedule exists, and runserver/worker are stopped before tests).
Fixed two small pre-existing findings while getting `ruff check .`/`ruff format
--check .` fully clean repo-wide as requested (an `except`-without-`from` on a Celery
retry in `apps/whatsapp/tasks.py`, an over-long log line in `apps/whatsapp/services.py`,
plus import-sort/formatting drift in two test files and one doc). Documented in
README.md and here. Tests: 867 passed (up from 860), full suite, real Neon/Upstash
infra. Ran a dry run against the real dev database/Redis, reported the counts, and
waited for explicit approval before any deletion, per instructions.

**Follow-up (2026-09-28, same day, approved): ran `--confirm` for real** against the dev
database/Redis — 6 `Conversation` rows and 162 `ConversationMessage` rows deleted
(Redis/Celery were already empty both times). Replaced the `SECURE_SSL_REDIRECT` guard
with the explicit `IVA_DEV_RESET_ALLOWED` setting described above (both the setting and
the test rework were requested after the initial build). Confirmed no `runserver`,
`ngrok` or Celery worker processes were left running at the end of the session — see the
new hard rule below, added at the same time.

**2026-09-29 product cards (approved, option A built; option B awaiting approval):**
requested feature: product cards with images when a category is opened or a search
returns products. Research first, per instructions: Meta's Cloud API does have a
non-template, non-catalog **Interactive Media Carousel** (confirmed via two independent
doc fetches — 2–10 cards, image/video headers only, card body ≤160 chars, main body
≤1024, button labels ≤20 chars); this is option B and was **not built**, since building
it required a separate explicit approval that has not yet been given — see
`ARCHITECTURE.md`'s Contract decisions for the full findings, reported to the user for
that decision. Option A (build now, no approval gate) was built: `OutProductCard`
(`apps/conversations/channel.py`/`renderers.py`) — an interactive button message with an
optional image header — sent one per product, 4 per page (`CARD_PAGE_SIZE`), for
category browsing and plain product search/browse (the quantity-carrying "add N of X"
disambiguation list is unchanged — see Contract decisions for why). New Backend Port
field `ProductSummary.image_url` (`str | None`, https or `None`); the Fake Backend
builds it from a new `IVA_PRODUCT_IMAGE_BASE_URL` setting plus each product's fixture
`image_path`. Card quick actions ("Add to cart", "Order now") always re-check the
product through the port on tap and never trust anything but the ID in the button; a
stale/removed/out-of-stock tap says so and re-offers the same page, never crashes, even
after the session has expired. An image that fails to send is retried once without the
header rather than dropping the product (`apps/whatsapp/delivery.py`). No extra Redis
calls: a page of 4 cards is still exactly one session load and one save, like any other
turn. Verified all 55 candidate image paths with one HEAD request each (no retries): 45
work; the other 10 were fixed on 2026-10-01 (see the carousel note below), so all 55 now have a path. Tests added/rewritten:
renderer (`OutProductCard`), Fake Backend image wiring, a new `test_engine_cards.py`
(add/order-now/stale/continue-shopping/session-expiry/image wiring), delivery
image-retry, and the pre-existing category/search browse tests updated for the new
card shape (they asserted `OutList` rows, which category/search browsing no longer
produces). Full suite: 887 passed (up from 867), real Neon/Upstash infra,
`ruff check .`/`ruff format --check .` clean.
`config/settings/test.py` now also forces `IVA_PRODUCT_IMAGE_BASE_URL = ""` regardless
of the developer's `.env` (same reasoning as its other forced settings), so tests stay
deterministic; dedicated tests opt into images via `FakeBackend(image_base_url=...)`.
CLAUDE.md, ARCHITECTURE.md, HANDOVER.md and README.md all updated. No `runserver` or
worker was started this session.

**2026-09-29 two live-testing fixes (approved):**

**Fix 1 — "Continue shopping" no longer re-sends product cards.** Tapping "Continue
shopping" after adding a card's product to the cart used to re-render the very same
page of cards (`_show_current_listing`), duplicating what was already visible in the
chat. It now goes up one level instead: `ACT:continue` calls a new
`_continue_shopping`, which resolves the group behind the category the customer was
browsing (via `Session.listing["category_id"]` and `_all_categories()`) and shows that
group's category list (`_show_group`) — no product cards. With no group to resolve
(session expired, or a listing with no category context), it falls back to the main
menu. "View cart" and the stale-card recovery path (`_card_stale`, which still
re-shows the same page — that one is expected to repeat cards, since it's recovering
from a failed tap, not following a successful add) are unchanged.

**Fix 2 — first message greets by WhatsApp profile name.** A brand-new customer's very
first reply (the language prompt) now opens with "Hi {name}! 👋 Welcome to Ivar!" when
a profile name was captured on that message, or a name-free "Welcome to Ivar!"
otherwise — before language is even chosen, so the line is deliberately
language-neutral apart from the name (new keys `language.prompt_greeting_named`/
`_unnamed`). It greets only on the actual first prompt in a session (tracked by
`Session.state` not yet being `LANGUAGE`), not on a re-prompt after unclear text, and
not on the separate "change language" flow later. The planner message shown right
after language selection no longer repeats the greeting (`welcome.new_named`/
`welcome.new_unnamed` removed, replaced by a single un-named `welcome.planner_intro`)
— avoids the duplicate "Hi {name}!" the old flow produced. Name-saving rules
unchanged: only when unset, never overwritten, never logged. See `ARCHITECTURE.md`'s
§6 for both, in full.

Tests: `test_engine_cards.py` gained three "Continue shopping" tests (both groups,
after session expiry, asserting no cards are re-sent); `test_engine_onboarding.py`
gained/renamed tests for the first-message greeting (with and without a profile name)
and confirmed the planner message no longer contains the name; a handful of
pre-existing tests that asserted the old `language.prompt`/`welcome.new_*` keys for
the very first message were updated to the new keys (`test_engine_onboarding.py`,
`apps/whatsapp/tests/test_database.py`, `test_delivery.py`) — the "change language"
and "unclear text re-prompt" cases correctly kept the plain `language.prompt` key
unchanged. Full suite: 891 passed (up from 887), real Neon/Upstash infra, `ruff
check .`/`ruff format --check .` clean. `python manage.py reset_dev_data` (dry run
then `--confirm`) run afterward at the user's request to reset for manual retesting —
see the counts in the session report. `CLAUDE.md`, `ARCHITECTURE.md` and `README.md`
(manual checklist) updated; `HANDOVER.md` not touched (no Backend Port contract
change). No `runserver` or worker started this session.

**2026-09-30 search results list, website menu link, Flows deferral:** (1) A typed
search with 2+ matches now sends one compact list (mode `"results"`): name + price from
the port, 10 rows if they fit, otherwise 9 + "Show more"; tapping a row
(`ACT:showcard:<id>`) re-checks the product via `get_product` and sends its existing
card (out-of-stock / missing get a message instead). A single match now sends its card (see the later note).
Category browsing keeps cards; the "add N of X" list and the diet
filter/notice are unchanged. (2) New setting `IVA_WEBSITE_SHOP_URL` (https only; dev
`.env` set, `.env.example` empty): a "See full menu on website" main-menu row
(`ACT:website`) sends a `cta_url` message; hidden if unset/invalid. Main-menu category
groups now shrink to fit WhatsApp's 10-row limit (max 3 with both links). (3) WhatsApp
Flows cart screen recorded as deferred future work in `ARCHITECTURE.md` and
`HANDOVER.md`. New tests: `test_engine_search_list.py` (14), `test_engine_website_link.py`
(16); four `test_engine_browse.py` tests updated from cards to list rows. Full suite:
921 passed (up from 891), `ruff check .` and `ruff format --check .` clean, no
`runserver`/worker started. No new dependencies; no Backend Port change.

**2026-09-30 single-match search → card:** a typed search (no quantity) with exactly one
match now sends that product's card via `_show_card` (port re-check; out of stock /
missing messages; diet notice and "Show anyway" kept) instead of the detail + quantity
prompt. Requests with a quantity ("add 2 quinoa") keep the detail path. Single-match
recovery never re-runs the search (`relist=False`), avoiding a stale-loop. Tests updated
in `test_engine_browse/cart/failures`, new ones in `test_engine_search_list.py`.

**2026-09-30 test isolation (tests only, no behaviour change):** a leftover real-Redis
session key from an interrupted run made a webhook end-to-end test fail on the next run.
Rules now enforced by the repo-root `conftest.py`:
- **Test Redis prefix.** Under pytest, session, customer-lock and rate-limit keys use
  `IVA_TEST_REDIS_KEY_PREFIX` (`iva-test:`, set in `config/settings/test.py`) instead of
  the dev/prod `iva:` prefixes, so tests never touch dev keys and `reset_dev_data` never
  sees test keys. Never point a test at a dev prefix.
- **Cleanup.** An autouse fixture deletes every real Redis key a test touched in a
  `finally` (also on failure), and a session-scoped fixture sweeps `iva-test:*` at the
  start and end of the run (covers an interrupted run). Tests that write Redis keys need
  no cleanup code of their own.
- **Unique sender per run.** `apps/whatsapp/tests/fakes.py` builds `WA_ID` (and the e2e
  sender IDs) with a random per-run token, so leftover state can never match.
- `test_redis_test_isolation.py` proves test keys never start with a dev prefix.
- Leftover dev keys are cleared with `python manage.py reset_dev_data --confirm`.

**2026-10-06 native catalog orders (built against simulated payloads):** inbound `order` messages are parsed (`parser.py`), validated through the Backend Port (`apps/conversations/catalog_order.py`) and replace the IVA cart, then continue into the normal cart/checkout flow (`engine._catalog_order`). New settings `IVA_CATALOG_ID` (empty = no check) and `IVA_CATALOG_ORDER_MAX_QTY` (10). Dev-only `manage.py replay_order slug:qty ... --to <wa_id>` posts a signed simulated order to the real webhook. No Backend Port change. Live catalog linking is blocked: no catalog-enabled test number exists yet. See `ARCHITECTURE.md`.

**2026-10-06 LLM fallback (Groq), off by default:** `apps/ai/classifier.py` (interface, redaction, prompt, strict validation), `apps/ai/groq.py`, `apps/ai/llm_fallback.py`, `apps/ai/llm_guard.py`; engine integration in `ConversationEngine._llm_context` / `_llm_cart_confirmation` (cart changes need Yes/No). Settings `IVA_LLM_*` and `GROQ_API_KEY` (env only). Dev-only `manage.py eval_intent_llm` scores the synthetic golden set. Telugu/Hindi quality unverified. See `ARCHITECTURE.md`.

**2026-10-06 grounded recommendations (off by default):** `apps/ai/recommend.py` + `recommend_cues.py` (extraction schema, cues, validation), `apps/conversations/recommend_select.py` / `recommend_flow.py` (selection in code, template replies). The LLM never writes customer text, prices, nutrition or cart changes. Settings `IVA_LLM_RECOMMEND_ENABLED`, `IVA_LLM_RECOMMEND_MAX_PICKS`. See `ARCHITECTURE.md`.

**2026-10-07 health-aware recommendations, emergency check, fuzzy guard:** extraction gained `health_context`/`asks_advice`/`meal_time`; composition in `recommend_flow._reco_compose`; code-only emergency keyword check (`apps/ai/safety_keywords.py`, `IVA_SAFETY_KEYWORDS_ENABLED`, default on); empty `apps/ai/meal_time_map.py`; fuzzy cart changes ask Yes/No (`IVA_FUZZY_CART_CONFIRM`); `IVA_FUZZY_NUMBER_GUARD` now defaults to ON (owner-approved, 2026-10-07): the fuzzy stage once corrected "weight" into the number word "eight" and produced a quantity of 8. Exactly one existing test was changed with the owner's approval, `test_the_old_quoted_reply_is_unchanged_without_the_llm_layer` (it pinned the buggy text; it now asserts "weight" is never corrected into a quantity); `config/settings/test.py` sets the guard on too. Golden set `recommend_health` (`eval_intent_llm --set recommend_health`). Safety wording needs qualified human review before launch. See `ARCHITECTURE.md`/`HANDOVER.md`.

**Work on one stage per session. Stop after reporting. Never start the next stage without explicit approval.**

**2026-10-01 image fixes + category carousel:** (1) The 10 fixture products with no image now have `image_path` values (`ivar-oats` -> `eat/oats.jpg`, ... `breath-fresh-shot` -> `eat/breath-fresh.jpg`; full list in the fixture); each was checked once with a HEAD request against `IVA_PRODUCT_IMAGE_BASE_URL`: all `200`, `image/jpeg`, 24-331 KB (WhatsApp limit 5 MB). All 55 products now have an image. (2) Category pages go out as ONE interactive media carousel (up to 10 cards: image, name + price from the port, "Add to cart"/"Order now") followed by a "Show more"/"Menu" message; falls back to the 4-per-page cards if any product lacks an image, the page has under 2 products, or WhatsApp rejects the carousel (`OutCarousel.fallback`, `delivery.send_replies`) -- never dropping a product. Search, "add N of X" and the diet filter are unchanged. Details: `ARCHITECTURE.md` Contract decisions. te/hi `carousel.body` needs native review. The carousel payload follows Meta's documentation but has not been sent to a real number: first item in the README manual checklist. Verification: `ruff check` and `ruff format --check` clean; full `pytest -v` 963 passed, no runserver/worker running.

**2026-10-01 follow-up ordering fix:** live test showed the "Show more / Menu" message arriving before the carousel. Sends were already strictly sequential (next send only after the previous call returned), so the cause is Meta preparing media messages asynchronously after acknowledging the send. `delivery.send_replies` now holds a buttons message that follows a carousel or image card until that message's `sent` status callback arrived (`apps/whatsapp/ordering.py`; the webhook records callbacks in the cache; worker polls; timeout `IVA_ORDER_WAIT_SECONDS`=5 then sends anyway; `IVA_ORDER_WAIT_STATUS` switches to `delivered`). Fails open. Details in `ARCHITECTURE.md`. Not yet verified on a real device. Verification: ruff clean, full `pytest -v` passed (count in the final report of that session), nothing left running.

**2026-10-01 dev-only Fake Backend persistence:** new setting `IVA_FAKE_BACKEND_STATE_FILE` (dev `.env`: `.local-data/fake_backend.json`, relative to the project root; empty in `.env.example` and forced empty in test settings). When set, the Fake Backend saves customer state (profiles, carts, addresses, orders, idempotent checkouts, id counters) after every change, via a temp file plus `os.replace` so a crash cannot corrupt it, and loads it on startup, so state survives restarts and autoreloads. Only customer state is stored; the catalog always comes from the fixture (a saved cart line for a product no longer in the catalog is dropped on load). Each operation first checks the file's mtime/size and reloads if another process (a worker, or `reset_dev_data`) changed it, so a running server does not resurrect cleared state. A missing file starts empty silently; a corrupt one starts empty with a warning (never its contents) and is kept as `<file>.corrupt`. A failed save is logged and never crashes a request. Unset (tests, production) = in-memory only, unchanged. Production settings refuse to start with it set (`validate_production`). `reset_dev_data` reports the file's counts in its dry run and, only with `--confirm`, empties it (the file is kept, as an empty state). `.local-data/` is git-ignored. **Hard rule 16** added: never run `reset_dev_data` or delete any data unless the user explicitly asks in that session. Tests: `test_fake_backend_persistence.py`, `test_reset_state_file.py` (the command's other effects are stubbed there), `test_prod_settings.py`.

**2026-10-01 address-step greetings fix:** typing "Hi" at the delivery-address prompt was saved as an address. At that step an EXACT whole-message greeting or global command (any language, romanized included) is now handled as such: greetings reply "Welcome back, <name>! You were in the middle of checkout." with Continue checkout (`ACT:resume`) / Menu, keeping cart and state; real addresses containing command words still work. Word lists live in `messages/{en,te,hi}.py` (te/hi need native review); logic is `engine._checkout_command`. Tests: `test_engine_checkout_commands.py`. Details in `ARCHITECTURE.md`.

## Hard rules

1. **No admin panel of any kind.** No `django.contrib.admin`, no `/admin/` route, no `admin.py`, no dashboard or admin UI.
2. **Never fabricate** prices, stock, payment status, order status, delivery status, ETA or tracking. All of it comes from the Backend Port. If it's missing, tell the customer it's unavailable.
3. **The LLM never writes data** and never decides price, stock, payment, order status or availability. It only proposes a structured intent that is schema-validated and executed through the Backend Port.
4. **Portability.** Delivered code lives only in `apps/whatsapp`, `apps/conversations`, `apps/ai`, `apps/iva_common`. These must not import from `config/` or `host/`. Allowed direction: `whatsapp → conversations → ai → iva_common`.
5. **Fake Backend is dev/test only.** Production settings must refuse it.
6. **Secrets only in `.env`** (git-ignored). Never print, log or commit tokens, keys or passwords. `.env.example` holds names only.
7. **Mask phone numbers and WhatsApp IDs** in all logs.
8. Customers never see tracebacks, exception text or internal details.
9. **Tests at every stage.** Never proceed with failing tests. Never claim a test passed without running it; show real output.
10. **Phase 1 scope only.** No Subscribe & Save, meal plans (Subscribe & Save meal-plan subscriptions), Build a Box, Corporate Orders, Wellness, Yoga, broadcasts or proactive notifications. **AI Planner scope update (2026-09-28):** the planner *page* itself is out of scope — it is built and hosted on the website, not this repo. What *is* in scope is the WhatsApp-side handoff only: (a) a link out to the planner from the welcome message and main menu, and (b) recognising and acting on the "IVA PLAN v1" message the planner sends back via `wa.me`. No recipe/nutrition computation, meal-plan storage or planner UI belongs here. See `PLANNER_INTEGRATION.md` for the wire contract and `ARCHITECTURE.md`'s Contract decisions for the design.
11. **No new dependencies** beyond the approved list without asking first and explaining why.
12. Money is `Decimal` with a currency code, never float.
13. Where ARCHITECTURE.md's Contract decisions section and the Reference PDF disagree, ARCHITECTURE.md wins. Record any new conflict there and tell me.
14. **Never leave `runserver` or a worker running at the end of a session.** Stop only processes you started; ask before stopping any other process.
15. **Never commit or push without explicit approval from the user.**
16. **Never run `reset_dev_data` or delete any data (database rows, Redis keys, files) unless the user explicitly asks for it in that session.**

## Infrastructure policy

- **PostgreSQL is provided by the organization. Never install it.** A team member fills `DATABASE_URL` in `.env`. It must be a development database. If it's missing or unreachable, report the error type (never credentials), finish non-database work and wait.
- **Redis:** use organization-provided URLs if present. Otherwise a user-level install in `.local-services/` bound to `127.0.0.1` is allowed.
- **Ask first** before anything needing admin rights, UAC, reboot, WSL, Docker, Windows features, system services, PATH/registry/firewall changes, unofficial builds, or touching existing software or ports. Give options, what each changes, and a recommendation.
- Never stop or modify remote/shared services. Simulate failures in tests.

## Approved dependencies

- **base:** Django 5.2 LTS, psycopg 3 (binary), django-environ, redis, celery
- **dev:** pytest, pytest-django, pytest-cov, ruff
- **prod:** gunicorn

Run `pip check` after any install.

**Unverified pairing:** `redis==8.1.0` + `celery==5.6.3` pass `pip check`, but the combination is unverified until a Celery worker runs against a real broker.

**LLM:** no provider chosen yet; only `FakeLLMClient` exists and no SDK or HTTP library may be added without approval. When a real provider is added, the HTTP client's own connect/read timeouts are the primary guard; the timeout thread pool must be small and bounded.

## Environment

- **`IVA_PSEUDONYM_KEY`** keys the pseudonyms of WhatsApp IDs (separate from `DJANGO_SECRET_KEY`; required in production). Never rotate it casually: a new key resets all conversation records and sessions. Carry it over unchanged when integrating.
- **`IVA_INLINE_PROCESSING`** (default `false`): development only. `true` processes the
  webhook synchronously — no Celery worker or broker needed — through the same
  claim/lock/rate-limit/at-most-once code the worker uses
  (`apps.whatsapp.tasks._process_once`). Production refuses to start with it on. See
  the 2026-09-25 inline-processing-mode note below.

- Development machine is **Windows**.
- Celery worker on Windows needs `--pool=solo`. This is a single-worker deployment, so
  the worker also runs with `--without-gossip --without-mingle --without-heartbeat` to
  cut idle Redis command volume (Upstash free tier); add them back if a second worker
  ever joins. See the 2026-09-25 Upstash free-tier tuning note below for the measured
  numbers and `IVA_CELERY_POLLING_INTERVAL_SECONDS`/`IVA_CELERY_HEALTH_CHECK_INTERVAL_SECONDS`.
  Day-to-day development instead runs with `IVA_INLINE_PROCESSING=true` and skips the
  worker entirely (see above) — the worker is still what production uses, and what the
  pre-handover real-worker check (README.md) verifies before handover.

## Common commands (Windows)

```text
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements\dev.txt
pip check
python manage.py migrate
python manage.py runserver
# IVA_INLINE_PROCESSING=true (dev default, see .env): the line below is not needed for
# day-to-day development. Only run it when IVA_INLINE_PROCESSING=false, e.g. for the
# pre-handover real-worker check (README.md).
celery -A config worker -l info --pool=solo --without-gossip --without-mingle --without-heartbeat
pytest -v
pytest -v -m "not django_db"        # when PostgreSQL is not available yet
pytest -v -m redis                  # real-Redis session tests; skipped (pending) without CONVERSATION_REDIS_URL
ruff check .
ruff format --check .
python manage.py reset_dev_data              # dry run: dev-only Postgres/Redis/Celery reset, counts only
python manage.py reset_dev_data --confirm    # actually deletes; refuses outside dev + Fake Backend
```

## End-of-session report (always)

1. Files added or changed.
2. Anything installed (name, version, source, location).
3. Actual output of `pip check`, `pytest -v` (list skipped/deselected tests and why), `ruff check .`, `ruff format --check .`.
4. Deviations from the specification with reasons, or "No deviations."
5. Updated status table above, and any `ARCHITECTURE.md` changes.
6. Stop.

## Things that need human review before launch

- Telugu and Hindi reply text (native-speaker review).
- Catalog data in the Fake Backend fixture is unverified and never used in production.
- Real prices, pack sizes, stock, allergens and bowl nutrition come from the backend team.
