# ai/email.py
"""
Layer взаимодействия 
"""
import os
import smtplib
from email.mime.text import MIMEText
# from dotenv import load_dotenv

DL_EMAIL = os.getenv("BOT_USERNAME")
DL_EMAIL_PASSWORD = os.getenv("BOT_PASSWORD")


def send_email_message_dl(to_email: str, msg_subject: str, msg_body: str):
    send_email_message(DL_EMAIL, DL_EMAIL_PASSWORD, to_email, msg_subject, msg_body)

def send_email_message(auth_email: str, auth_email_password: str, to_email: str, msg_subject: str, msg_body: str):
    msg = MIMEText(msg_body, 'plain', 'utf-8')
    msg['Subject'] = msg_subject
    msg['From'] = auth_email
    msg['To'] = to_email
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(auth_email, auth_email_password)
        server.sendmail(auth_email, [to_email], msg.as_string())