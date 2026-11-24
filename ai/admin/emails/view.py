from django.http import HttpRequest, HttpResponseForbidden
from django.template.response import TemplateResponse
from django.shortcuts import render, redirect
from django.contrib import messages
from django.db import IntegrityError
from django.db.models import Q
# from django.forms import forms, ModelForm 
from django.forms import ModelForm, EmailField, TextInput, EmailInput
from ai.models import UserToEmailDictionary

from .reg import can_access

from ai.admin.site import ai_admin_site

def view(request: HttpRequest) -> TemplateResponse:
    if not can_access(request):
        return HttpResponseForbidden("Access denied")

    # --- POST: добавить запись ---
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "delete":
            try:
                pk = int(request.POST.get("pk") or "")
            except (TypeError, ValueError):
                messages.error(request, "Некорректная запись.")
                return redirect(request.path)
            deleted, _ = UserToEmailDictionary.objects.filter(pk=pk).delete()
            if deleted:
                messages.success(request, "Запись удалена.")
            else:
                messages.error(request, "Запись не найдена.")
            return redirect(request.path)

        form = UserToEmailForm(request.POST)
        if form.is_valid():
            try:
                form.save()
                messages.success(request, "Запись добавлена.")
            except IntegrityError:
                form.add_error("user", "Такой пользователь уже есть в справочнике.")
            else:
                return redirect(request.path)   # PRG: F5 не отправит форму повторно
    else:
        form = UserToEmailForm()

    # --- GET: список + поиск ---
    q = (request.GET.get("q") or "").strip()
    qs = UserToEmailDictionary.objects.all()
    if q:
        qs = qs.filter(Q(user__icontains=q) | Q(email__icontains=q))

    context = {
        **ai_admin_site.each_context(request),
        "title": "DL.AI: Справочник ФИО → Email",
        "form": form,
        "rows": qs,
        "filters": {"q": q},
    }
    return TemplateResponse(
        request, "admin/ai/emails.html", context
    )

class UserToEmailForm(ModelForm):
  class Meta:
    model = UserToEmailDictionary
    fields = ("user", "email")
    widgets = {
        "user": TextInput(attrs={"placeholder": "Иванов Иван"}),
        "email": EmailInput(attrs={"placeholder": "ivanov@example.com"}),
    }