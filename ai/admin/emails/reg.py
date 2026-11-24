from django.http import HttpRequest
from ai.admin.permissions import is_superuser_user, user_has_key

# Новый формат .py описания странички, содержит функции для регистрации
# оставить в таком же виде, в разработке рефакторинг и примерно такое же представление
# будет в будущем: страница разделена на регистрационную инфу и views

SIDEBAR_NAME = "Почта"
DESCRIPTION = "Emails: создание и редактирование таблицы email для разработчиков"
PAGE = "page.emails"
TITLE = "Почта: Справочник и тестирование"
 
# Отдаёт путь до странички
def full_url() -> str:
  return "/ai/admin/emails/"

def url() -> str:
  return "emails/"

def can_access(request: HttpRequest) -> bool:
    if not request.user.is_authenticated:
        return False

    return is_superuser_user(request.user) or user_has_key(
        request.user, PAGE
    )