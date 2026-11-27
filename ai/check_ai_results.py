SUSPICIOUS_AI_PERCENT = 60
BATCH_CHECK_TIMEOUT_SECONDS = 120


def ai_involvement_percent(human_percent):
    if type(human_percent) in (float, int) and 0 <= human_percent <= 100:
        return 100 - human_percent
    return None


def is_suspicious_result(result):
    score = ai_involvement_percent(result.get("human_percent"))
    return not result.get("short_input") and score is not None and score >= SUSPICIOUS_AI_PERCENT
