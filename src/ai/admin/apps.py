"""Backward-compatible re-export of AIAppSettingsAdmin.

The canonical definition now lives in ai.admin.models to keep all
ModelAdmins in one place and avoid duplicate admin registrations.
"""
from ai.db import AIAppSettingsAdmin

__all__ = ["AIAppSettingsAdmin"]
