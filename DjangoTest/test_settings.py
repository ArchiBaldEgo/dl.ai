"""Settings for running tests locally without a Docker database."""
from DjangoTest.settings import *  # noqa: F401,F403

SECRET_KEY = "test-secret-key-not-for-production-use-only"

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'test_db.sqlite3',
    }
}

# Disable background schedulers during tests
START_MODEL_HEALTH_SCHEDULER = False
AI_DISABLE_HEALTH_SCHEDULER = "1"

# Тесты с обычными settings читают локальный .env через load_dotenv
# (DjangoTest/settings.py) — dev-bypass аутентификации должен быть выключен,
# иначе без-DLSID тесты middleware провижинят fake-юзера вместо 302.
# Присваиваем напрямую в os.environ (middleware читает os.getenv per-request).
import os
os.environ["AI_DEV_AUTH_BYPASS"] = "0"

# Ensure channels layer is in-memory for tests
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels.layers.InMemoryChannelLayer',
    }
}

# Use LocMem cache for tests so Redis is not required.
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
    }
}
