from .base import *  # noqa: F403
from .base import IVA_PSEUDONYM_KEY, SECRET_KEY

DEBUG = True

if not SECRET_KEY:
    SECRET_KEY = "insecure-dev-only-key-do-not-use-in-production"  # noqa: S105

if not IVA_PSEUDONYM_KEY:
    IVA_PSEUDONYM_KEY = "insecure-dev-only-pseudonym-key-do-not-use-in-production"

# Allows `manage.py reset_dev_data` to run at all (apps/whatsapp/dev_reset.py). See
# config/settings/base.py; every other settings module leaves this False.
IVA_DEV_RESET_ALLOWED = True
