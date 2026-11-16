"""Единственный источник правила «есть прокси в окружении → прод, нет → локально».

Один комплект Docker (docker-compose.yml + Dockerfile) обслуживает и локальный
запуск, и production: маркером служит НАЛИЧИЕ ПРОКСИ в окружении (HTTP_PROXY /
HTTPS_PROXY / RUNTIME_HTTP_PROXY — из .env). На прод-машине прокси прописан —
и там всё серьёзно (DEBUG=0, куки домена, health-проверки пулов); на локальной
машине прокси пуст/отсутствует — запускаются дев-удобства автоматически.

Явное значение в окружении всегда сильнее деривации: setdefault ниже не умеет
переопределять уже заданные переменные, так что любой флаг из .env сохраняет
приоритет.

Оболочечные слои, где Python недоступен (CMD в Dockerfile и healthcheck в
docker-compose.yml), дублируют то же правило инлайновым `[ -z "$HTTP_PROXY" ]`
— при правке правила согласуйте их с этим модулем.
"""

import os

_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "RUNTIME_HTTP_PROXY")


def is_local_mode() -> bool:
    """True — ни один прокси задан не в одном из _PROXY_VARS → локальный запуск."""
    return not any(os.getenv(name, "").strip() for name in _PROXY_VARS)


def apply_local_mode_defaults() -> None:
    """В локальном режиме включает дев-удобства по умолчанию через setdefault.

    Явное значение в окружении (в т.ч. непустое из .env) всегда побеждает —
    деривация применяется только там, где флаг вовсе не задан.
    """
    if not is_local_mode():
        return
    os.environ.setdefault("AI_DEV_AUTH_BYPASS", "1")
    os.environ.setdefault("AI_DISABLE_HEALTH_SCHEDULER", "1")
    os.environ.setdefault("AI_WEB_DEEPSEEK_AUTORECOVERY", "0")
    os.environ.setdefault("AI_WEB_KIMI_AUTORECOVERY", "0")