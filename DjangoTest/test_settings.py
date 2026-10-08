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

# Автовосстановление пулов в сьюте должно быть детерминированно ВКЛЮЧЕНО
# (дефолт прежних settings): AutorecoveryTests проверяют и путь «вкл», и
# конкретные off-кейсы через @override_settings. Без пина деривация локального
# режима (нет прокси → setdefault "0", ai/env_mode.py) ломало бы путь «вкл».
AI_WEB_DEEPSEEK_AUTORECOVERY = True
AI_WEB_KIMI_AUTORECOVERY = True

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
