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

# Ensure channels layer is in-memory for tests
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels.layers.InMemoryChannelLayer',
    }
}
