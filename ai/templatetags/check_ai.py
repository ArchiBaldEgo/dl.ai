from django import template
from django.template.defaultfilters import floatformat

from ..check_ai_results import SUSPICIOUS_AI_PERCENT, ai_involvement_percent


register = template.Library()


@register.filter
def check_ai_score_class(human_percent):
    """Выбирает цвет по доле текста, созданного с участием ИИ."""
    score = ai_involvement_percent(human_percent)
    if score is None:
        return ""
    if score >= SUSPICIOUS_AI_PERCENT:
        return "check-ai-score--high"
    if score >= 40:
        return "check-ai-score--medium"
    return "check-ai-score--low"


@register.filter
def check_ai_duration(seconds):
    if seconds is None or seconds == "":
        return "нет замера"
    return f"{floatformat(seconds, 1)} с"
