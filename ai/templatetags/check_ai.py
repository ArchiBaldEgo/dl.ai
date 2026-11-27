from django import template


register = template.Library()


@register.filter
def check_ai_score_class(human_percent):
    """Выбирает цвет по доле текста, созданного с участием ИИ."""
    if type(human_percent) not in (float, int) or not 0 <= human_percent <= 100:
        return ""
    ai_involvement_percent = 100 - human_percent
    if ai_involvement_percent >= 60:
        return "check-ai-score--high"
    if ai_involvement_percent >= 40:
        return "check-ai-score--medium"
    return "check-ai-score--low"
