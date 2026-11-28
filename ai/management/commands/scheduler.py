# ai/scheduler.py
import time
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from django.core.management.base import BaseCommand
from django.utils import timezone

"""
Запланированые и регулярные ивенты расставляются здесь.
"""

class Command(BaseCommand):
  help = "Запускает APScheduler в блокирующем режиме (один процесс на кластер)."

  def handle(self, *args, **options):
    scheduler = BlockingScheduler(timezone=timezone.get_current_timezone())
    scheduler.add_job(
      weekly,
      CronTrigger(day_of_week="mon", hour=12, minute=40),
      id="weekly",
      max_instances=1,
      coalesce=True,
      misfire_grace_time=3600,
    )
    self.stdout.write(self.style.SUCCESS("APScheduler started (weekly_event @ Mon 03:00)"))
    try:
      scheduler.start()
    except (KeyboardInterrupt, SystemExit):
      scheduler.shutdown()
            
def weekly() -> None:
  """
  Еженедельные ивенты располагаются здесь.
  """
  from ai.email import send_tasks_notifications
  send_tasks_notifications()
  