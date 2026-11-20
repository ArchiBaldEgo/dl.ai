# ai/email.py
"""
Layer взаимодействия с почтой
"""
import os
import smtplib
from email.mime.text import MIMEText

import logging

from ai.models import UserToEmailDictionary

logger = logging.getLogger(__name__)

DL_EMAIL = os.getenv("DL_EMAIL_BOT_USERNAME")
DL_EMAIL_PASSWORD = os.getenv("DL_EMAIL_BOT_PASSWORD")

#  EMAIL

def send_email_message_dl(to_emails: list[str], msg_subject: str, msg_body: str):
    send_email_message(DL_EMAIL, DL_EMAIL_PASSWORD, to_emails, msg_subject, msg_body)

def send_email_message(auth_email: str, auth_email_password: str, to_emails: list[str], msg_subject: str, msg_body: str):
    msg = MIMEText(msg_body, 'plain', 'utf-8')
    msg['Subject'] = msg_subject
    msg['From'] = auth_email
    msg['To'] = ", ".join(to_emails)
    try:
      with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(auth_email, auth_email_password)
        server.sendmail(auth_email, to_emails, msg.as_string())
    except Exception as e:
      logger.exception(f"Сбой при отправке почты: {e}")
      
#  

def get_email_of_user(user) -> str:
  """
  Словарь: user -> email: str
  """
  from ai.services.user_directory import display_name_for_user
  name = display_name_for_user(user)
  return (
    UserToEmailDictionary.objects
    .filter(user=name)
    .values_list("email", flat=True)
    .first()
    or ""
  )