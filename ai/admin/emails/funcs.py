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
  # Показываем только ключевые.
  if log.hidden:
    return
  
  title = f"На dl.ai вышло обновление!"
  text = f"""Обновление разработчика {log.author} от {log.commit.date}
    {log.description}
  """
  
  # В ПРОДУ ПОПРАВИТЬ И 
  send_email_message_dl(
    [
      TEST_EMAIL,
      # DOLINSKY_EMAIL,  
    ],
    title,
    text
  )
  
  
  

def send_tasks_notifications() -> None:
  """
    Отправляет каждому разработчику и копию Долинскому сообщение.
    В сообщении содержится напоминание о заданиях взятых разработчиком
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
  import os
  os.system("clear")
  print("Отправляю сообщение")
  for _, developer_tasks in groupby(tasks_taken, key=attrgetter("taken_by_id")):
      tasks = list(developer_tasks)
      developer = tasks[0].taken_by
      try:
        print("Отправляю сообщение в цикле")
        print(developer)
        print(tasks)
        send_email_message_dl(
          [
            TEST_EMAIL,
            # developer_email,
            # DOLINSKY_EMAIL,
          ],
          "Напоминалка",
          "Feoiujriogh"
        )
      except Exception as e:
        logger.exception(e)
        
      continue
      
      email_text = f"Еженедельное напоминание о том, что у вас есть незакрытые задачи:"
      task_counter = 0
      for task in tasks:
        task_counter+=1
        email_text += f"\n{task_counter}. {task.title} от {task.taken_at}. \n\n{task.description}"
      
      # developer_email = get_email_of_user(developer),
      
      send_email_message_dl(
        [
          "egorbespalovwork@gmail.com"
          # developer_email,
          # DOLINSKY_EMAIL,
        ],
        "Напоминание о заданиях",
        email_text
      )
          