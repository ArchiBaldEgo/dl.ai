from django.db.models.fields import TextField
from django.http import HttpRequest, HttpResponseForbidden
from django.template.response import TemplateResponse
from django.shortcuts import render, redirect
from django.contrib import messages
from django.db import IntegrityError
from django.db.models import Q
# from django.forms import forms, ModelForm 
from django.forms import Form, ModelForm, EmailField, TextInput, CharField, Textarea, EmailInput
from ai.email import send_email_message_dl
from ai.models import UserToEmailDictionary

from .reg import can_access

from ai.admin.site import ai_admin_site

def view(request: HttpRequest) -> TemplateResponse:
    if not can_access(request):
        return HttpResponseForbidden("Access denied")

    # Две независимые формы на странице → два экземпляра форм в контексте
    form = UserToEmailForm()
    email_form = TestEmailForm()

    if request.method == "POST":
        action = request.POST.get("action")

        # ---- удаление записи ----
        if action == "delete":
            UserToEmailDictionary.objects.filter(pk=request.POST.get("pk")).delete()
            messages.success(request, "Запись удалена.")
            return redirect(request.path)

        # ---- отправка тестового письма ----
        if action == "send_test_email":
            email_form = TestEmailForm(request.POST)
            if email_form.is_valid():
                try:
                    send_test_email(**email_form.cleaned_data)
                except Exception as exc:  # smtplib.SMTPException, OSError, ...
                    messages.error(request, f"Не удалось отправить письмо: {exc}")
                else:
                    messages.success(
                        request,
                        f"Письмо отправлено на {email_form.cleaned_data['to']}",
                    )
                    return redirect(request.path)  # PRG
            # Невалидная форма — падаем в общий рендер ниже,
            # чтобы показать ошибки прямо на странице.

        # ---- добавление записи (существующая логика) ----
        else:
            form = UserToEmailForm(request.POST)
            if form.is_valid():
                try:
                    form.save()
                    messages.success(request, "Запись добавлена.")
                except IntegrityError:
                    form.add_error("user", "Такой пользователь уже есть в справочнике.")
                else:
                    return redirect(request.path)

    # --- GET: список + поиск (без изменений) ---
    q = (request.GET.get("q") or "").strip()
    qs = UserToEmailDictionary.objects.all()
    if q:
        qs = qs.filter(Q(user__icontains=q) | Q(email__icontains=q))

    context = {
        **ai_admin_site.each_context(request),
        "title": "Emails",
        "form": form,
        "email_form": email_form,   # <-- новый
        "rows": qs,
        "filters": {"q": q},
    }
    return TemplateResponse(request, "admin/ai/emails.html", context)

# --- Functions ---

def send_test_email(*, to: str, subject: str, body: str) -> None:
  # recipients = [i.strip() for i in to.split(",")]
  # send_email_message_dl(recipients, subject, body)
  from ai.admin.emails.funcs import send_tasks_notifications 
  send_tasks_notifications()
  
  return

# --- Forms ---
class UserToEmailForm(ModelForm):
  class Meta:
    model = UserToEmailDictionary
    fields = ("user", "email")
    widgets = {
        "user": TextInput(attrs={"placeholder": "Иванов Иван"}),
        "email": EmailInput(attrs={"placeholder": "ivanov@example.com"}),
    }
    
class TestEmailForm(Form):
  to = CharField(
      label="Кому",
      widget=TextInput(
        attrs={
          "placeholder": "user1@ex.com, user2@ex.com",
          "style": "width: 70%;"
        }
      ),
  )
  subject = CharField(
      label="Тема",
      max_length=255,
      widget=TextInput(attrs={"placeholder": "Тема"}),
  )
  body = CharField(
      label="Сообщение",
      widget=Textarea(
        attrs={
          "placeholder": "Текст сообщения",
          "rows": 8,
          "style": "width: 100%; resize: none;"
        }
      ),
  )