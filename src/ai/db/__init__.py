# ai/db/__init__.py
"""Модели данных AI-приложения.

Содержит модели для:
- Промптов (SharedPrompt, Prompt) с поддержкой мультиязычности и плейсхолдеров.
- Тем и языков программирования (Topic, ProgrammingLanguage).
- Задач dl.gsu.by (Task) для batch-solve ARM.
- Журнала запросов к AI-моделям (AIRequestLog).
- Проверки доступности моделей (AIModelHealthRun, AIModelAvailability).
- Результатов тестирования моделей (AIModelTestRun, AIModelTestResult).
- Глобальных настроек приложения (AIAppSettings).
- Связи пользователей с внешними аккаунтами dl.gsu.by (ExternalDLAccount).
"""

from ai.db.AIAdminGroupPermission import AIAdminGroupPermission
from ai.db.AIAccessGroup import AIAccessGroup
from ai.db.AIAppSettings import AIAppSettings
from ai.db.AICourseTreeCache import AICourseTreeCache
from ai.db.AIDevTask import AIDevTask
from ai.db.AIDevTaskDeveloperGrant import AIDevTaskDeveloperGrant
from ai.db.AIDLApiRequest import AIDLApiRequest
from ai.db.AIModelAvailability import AIModelAvailability
from ai.db.AIModelHealthRun import AIModelHealthRun
from ai.db.AIModelStats import AIModelStats
from ai.db.AIModelTestResult import AIModelTestResult
from ai.db.AIModelTestRun import AIModelTestRun
from ai.db.AIPinnedBatchRun import AIPinnedBatchRun
from ai.db.AIRequestLog import AIRequestLog
from ai.db.AIUserModel import AIUserModel
from ai.db.AIWizardSeen import AIWizardSeen
from ai.db.ArmPromptBinding import ArmPromptBinding
from ai.db.AuthorAlias import AuthorAlias
from ai.db.DLEducationName import DLEducationName
from ai.db.ExternalDLAccount import ExternalDLAccount
from ai.db.FindErrorCaseGroup import FindErrorCaseGroup
from ai.db.FindErrorCaseLabel import FindErrorCaseLabel
from ai.db.FindErrorCase import FindErrorCase
from ai.db.ProgrammingLanguage import ProgrammingLanguage
from ai.db.Prompt import Prompt
from ai.db.PromptLabResult import PromptLabResult
from ai.db.PromptLabRun import PromptLabRun
from ai.db.SharedPrompt import SharedPrompt
from ai.db.Task import Task
from ai.db.TaskLanguageStatus import TaskLanguageStatus
from ai.db.TaskSolution import TaskSolution
from ai.db.Topic import Topic
from ai.db.UpdateLog import UpdateLog

from ai.db.utils import replace_placeholders

__all__ = [
  "UpdateLog",
  "AIAdminGroupPermission",
  "AIAccessGroup",
  "AIAppSettings",
  "AICourseTreeCache",
  "AIDevTask",
  "AIDevTaskDeveloperGrant",
  "AIDLApiRequest",
  "AIModelAvailability",
  "AIModelHealthRun",
  "AIModelStats",
  "AIModelTestResult",
  "AIModelTestRun",
  "AIPinnedBatchRun",
  "AIRequestLog",
  "AIUserModel",
  "AIWizardSeen",
  "ArmPromptBinding",
  "AuthorAlias",
  "DLEducationName",
  "ExternalDLAccount",
  "FindErrorCaseGroup",
  "FindErrorCaseLabel",
  "FindErrorCase",
  "ProgrammingLanguage",
  "Prompt",
  "PromptLabResult",
  "PromptLabRun",
  "SharedPrompt",
  "Task",
  "TaskLanguageStatus",
  "TaskSolution",
  "Topic",
  "UpdateLog",
  "replace_placeholders"
]
