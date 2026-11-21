# ai/admin/emails/funcs.py

from ai.models import UpdateLog
from ai.email import send_email_message_dl, get_email_of_user
import logging

"""
Функции связанные с работой с email
"""

logger = logging.getLogger(__name__)

TEST_EMAIL = "egorbespalovwork@gmail.com"

# Email Долинского
DOLINSKY_EMAIL = get_email_of_user("Михаил Долинский")

def send_update_log_email(log: UpdateLog) -> None:
  from ai.services.user_directory import display_name_for_user
  # Показываем только ключевые.
  if log.hidden:
    return

  author_name = display_name_for_user(log.author)
  author_email = log.author
  
  title = f"На dl.ai вышло обновление"
  text = f"""Обновление от {author_name} ({author_email}) от {format_date(log.commit.date)}:
    {log.description}
  """
  
  # В ПРОДУ ПОПРАВИТЬ
  send_email_message_dl(
    [
      TEST_EMAIL,
      # DOLINSKY_EMAIL,  
    ],
    title,
    text
  )
  

# TODO: В рефактор. Сделать format.py где будут лежать ВСЕ функции
#   отвечающие исключительно за работу с общими форматами
# Первые 16 знаков из даты из бд
# EX: "11-09-2026 19:02:15:1020240...."
#  -> "11-09-2026 19:02"
def format_date(raw_date: str) -> str:
  return raw_date[0:16]


def send_tasks_notifications() -> None:
  """
    Отправляет каждому разработчику и копию Долинскому сообщение.
    В сообщении содержится напоминание о заданиях взятых разработчиком

    Формат:
      Напоминание о заданиях

    Еженедельное напоминание о том, что у вас есть незакрытые задачи:
    1. Имя таска1 (год-месяц-день) 
      Описание таска
    
    2. Имя таска2 (год-месяц-день) 
      Описание таска2
            
  """
  from ai.models import AIDevTask
  from itertools import groupby
  from operator import attrgetter
  from django.db.models import Prefetch

  # Получаем таски которые сейчас открыты
  tasks_taken = (
    AIDevTask.objects
    .filter(closed_at__isnull=True, taken_by__isnull=False)
    .select_related("taken_by")
    .order_by("taken_by_id", "kind", "priority_no", "-created_at", "-id")
  )

  # Группируем по разработчику
  for _, developer_tasks in groupby(tasks_taken, key=attrgetter("taken_by_id")):
      try:
        tasks = list(developer_tasks)
        developer = tasks[0].taken_by
        
        email_text = f"Еженедельное напоминание о том, что у вас есть незакрытые задачи:"
        task_counter = 0
        for task in tasks:
          task_counter+=1
          email_text += f"\n{task_counter}. {task.title} ({format_date(task.taken_at)}. \n\t{task.description})"
      
        # developer_email = get_email_of_user(developer),

        

        send_email_message_dl(
          [
            TEST_EMAIL
            # developer_email,
            # DOLINSKY_EMAIL,
          ],
          "Напоминание о заданиях",
          email_text
        )
        
      except Exception as e:
        logger.exception(e)
      
def send_task_assign_notification(task, assigned_to, assigned_by) -> None:
  from ai.email import send_email_message_dl, get_email_of_user
  from ai.services.user_directory import display_name_for_user
  from ai.admin.tasks import _claimant_display
  
  email = get_email_of_user(assigned_to)
  
  email_subject = "Вам выдали задачу"
  email_text =f"""{_claimant_display(assigned_to)}, {display_name_for_user(assigned_by)} поручил вам задачу:
  \t{task.title} [{task.kind}]({task.estimate})
  {task.description}
  """

  send_email_message_dl(
    [
      TEST_EMAIL,
      # DOLINSKY_EMAIL,
      # email,
    ],
    email_subject,
    email_text
  )