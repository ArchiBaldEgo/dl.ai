# ai/debug.py
"""
Всё что связано с локальным запуском, тестированием:
Проверка на debug
"""
import os
from functools import wraps

DEBUG = os.getenv("DEBUG")

def is_debug() -> bool:
    return DEBUG

# local only.
# Запрещает исполнение в не debug среде.
def debug_only(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not is_debug():
            return
        return func(*args, **kwargs)
    return wrapper

# production only.
# Разрешает исполнение только в проде
def not_debug_only(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if is_debug():
            return
        return func(*args, **kwargs)
    return wrapper