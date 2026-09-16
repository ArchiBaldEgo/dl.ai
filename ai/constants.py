"""Константы, используемые throughout AI-приложение.

Определяет имя группы RBAC для разработчиков промптов, имя cookie для logout
из админки, часовую зону Москвы (для планировщика) и префикс ключей кэша.
"""

from zoneinfo import ZoneInfo

PROMPT_DEVELOPER_GROUP = "prompt_developer"
ADMIN_LOGOUT_COOKIE_NAME = "ai_admin_logged_out"
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
AI_CACHE_KEY_PREFIX = "ai"
# Единственное дерево задач (курс) в dl.gsu.by: cid для пользовательских
# ссылок на задачи, когда конкретный курс записи неизвестен (см. dl_task_url
# и fallback в admin/arm.py). Если в DL появятся другие курсы — сюда их не
# складывать: константа ровно про «дефолтное дерево», а не список курсов.
DL_DEFAULT_COURSE_ID = 1450
