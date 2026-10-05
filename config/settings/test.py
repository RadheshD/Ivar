from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import DATABASES, IVA_PSEUDONYM_KEY, SECRET_KEY

DEBUG = False

if not SECRET_KEY:
    SECRET_KEY = "insecure-test-only-key-do-not-use-in-production"  # noqa: S105

# Tests run on real PostgreSQL only. Without DATABASE_URL the dummy backend stays in
# place, so database tests fail loudly instead of silently using another engine.
_engine = DATABASES["default"]["ENGINE"]
if _engine not in ("django.db.backends.postgresql", "django.db.backends.dummy"):
    raise ImproperlyConfigured("Tests require PostgreSQL (DATABASE_URL=postgres://...).")

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

IVA_BACKEND_ADAPTER = "apps.conversations.fake_backend.FakeBackend"

# Forced regardless of the developer's local .env (same reasoning as IVA_BACKEND_ADAPTER
# above): unset by default so onboarding lands straight on the main menu, like almost
# every existing test expects via the ``onboard()`` harness helper. Tests exercising the
# AI Planner welcome/menu link opt in explicitly with the ``settings`` fixture
# (``settings.IVA_PLANNER_URL = "https://..."``).
IVA_PLANNER_URL = ""

# No waiting for status callbacks in the suite; ordering tests opt in explicitly.
IVA_ORDER_WAIT_SECONDS = 0

# Forced off regardless of .env: the suite must never read or write the developer's
# saved Fake Backend state. Persistence tests opt in with a temporary path.
IVA_FAKE_BACKEND_STATE_FILE = ""
IVA_WEBSITE_SHOP_URL = ""

# Forced regardless of the developer's local .env (same reasoning as IVA_BACKEND_ADAPTER
# above): tests must be deterministic, not dependent on whichever mode a developer
# happens to have set locally. Tests that need inline mode opt in explicitly with
# @override_settings(IVA_INLINE_PROCESSING=True).
IVA_INLINE_PROCESSING = False

if not IVA_PSEUDONYM_KEY:
    IVA_PSEUDONYM_KEY = "insecure-test-only-pseudonym-key-do-not-use-in-production"

# Forced regardless of the developer's local .env (same reasoning as IVA_BACKEND_ADAPTER
# above): tests must not depend on whichever product-image host a developer happens to
# have configured locally. Tests that need images opt in explicitly via the Fake
# Backend's ``image_base_url`` constructor argument.
IVA_PRODUCT_IMAGE_BASE_URL = ""

# Real-Redis tests (session store, per-customer lock) use this prefix in place of the
# dev/prod "iva:" one, so a test run can never read, overwrite or leave behind dev keys
# (and ``reset_dev_data`` never sees test keys). Applied by the autouse fixture in the
# repo-root conftest.py.
IVA_TEST_REDIS_KEY_PREFIX = "iva-test:"
