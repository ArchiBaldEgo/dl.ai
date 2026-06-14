""""My prompts" helper view for prompt developers."""

from django.contrib import admin
from django.http import HttpResponse, HttpResponseForbidden

from ..models import Prompt
from .permissions import can_access_prompt_admin
from .models import PromptAdmin


def is_mine_only_request(request):
    if getattr(request, "_mine_only", False):
        return True
    value = (request.GET.get("mine") or "").strip().lower()
    return value in {"1", "true", "yes"}


def get_my_prompt_admin_url(request):
    return "/ai/admin/prompts/my/"


def admin_my_prompt_view(request):
    if not can_access_prompt_admin(request):
        return HttpResponseForbidden("Access denied")
    request._mine_only = True
    prompt_admin = admin.site._registry.get(Prompt)
    if prompt_admin is None:
        return HttpResponse("Prompt admin is not registered", status=404)
    return prompt_admin.changelist_view(request, extra_context={"mine_only": True})
