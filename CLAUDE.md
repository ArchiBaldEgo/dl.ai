# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Django + Channels (Daphne) web app for AI-assisted programming tasks. The app is mounted under `/ai/` on the same domain as the main DL site (`dl.gsu.by`). Authentication is delegated to the main site via `DLSID` session cookie and the external auth API (`EXTERNAL_AUTH_API_URL`). There is also a `bot/` Node.js service that wraps `chat.deepseek.com` through Puppeteer for free-tier DeepSeek access.

## Common commands

### Local development (without Docker)

Requires Python 3.11+ and PostgreSQL 14+.

```bash
cp .env.example .env
# edit .env: set DB_HOST=127.0.0.1 and create DB/user from README.md
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
# source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver 0.0.0.0:8000
```

The app is reachable at `http://127.0.0.1:8000/ai/...`. Static files are served through the `/ai/assets/` endpoint in development as well as production.

### Docker

```bash
cp .env.example .env
# dev (nginx on 8080):
docker compose --env-file .env up -d --build
docker compose --env-file .env exec -T web python manage.py migrate
docker compose --env-file .env exec -T web python manage.py collectstatic --noinput

# prod (nginx on 127.0.0.1:8081):
AI_NGINX_BIND=127.0.0.1 AI_NGINX_PORT=8081 docker compose --env-file .env up -d --build
```

Update from git:

```bash
git pull
docker compose --env-file .env up -d --build
docker compose --env-file .env exec -T web python manage.py migrate
docker compose --env-file .env exec -T web python manage.py collectstatic --noinput
```

### Tests

There is no dedicated test runner configured beyond Django. Run tests with:

```bash
python manage.py test ai
```

Run a single test class:

```bash
python manage.py test ai.tests.ProblemDataApiUiLanguageTests
```

Run a single test method:

```bash
python manage.py test ai.tests.ProblemDataApiUiLanguageTests.test_problem_data_localizes_topic_and_prompt_names
```

There is no linting configuration (ruff/flake8/eslint) checked in.

### Static files

```bash
python manage.py collectstatic --noinput
```

Source static files live in `static/`, collected output is `staticfiles/`. The web container serves them through nginx (`/ai/static/` maps to `/app/staticfiles/`).

### Management commands

```bash
python manage.py check_models_health
python manage.py check_models_health --force

# Auto-translate empty *_en / *_fr fields for Topic, SharedPrompt, Prompt
python manage.py auto_translate                  # translate all empty fields
python manage.py auto_translate --model Topic     # only one model
python manage.py auto_translate --overwrite       # overwrite existing translations
python manage.py auto_translate --lang fr          # only French
python manage.py auto_translate --dry-run          # show what would be translated
```

```bash
# Sync UpdateLog table from git history (powers /ai/admin/updates/)
python manage.py sync_update_log            # import new commits since last sync
python manage.py sync_update_log --rebuild  # rebuild the table from scratch

# Prompt regression testing from CLI (CI / scheduled checks after a prompt edit)
python manage.py run_prompt_tests --model <key> [--prompt <id>] [--cases 1,2,3]

# LLM-based translation of Prompt/SharedPrompt *_en/*_fr fields via a registered
# model handler (default DeepSeek_V3_1 via SambaNova/SC_TOKEN) — fills only empty
# fields unless --overwrite. Complements auto_translate (Google/deep-translator).
python manage.py translate_prompts [--overwrite]
```

## High-level architecture

### Django app structure

- `DjangoTest/` — project settings, root URLconf, ASGI/WSGI entrypoints.
- `ai/` — the only Django app.
  - `ai/views.py` — page views, API endpoints (`/ai/api/problem-data/`, `/ai/api/prompts/`, `/ai/api/languages/`, `/ai/api/topics/`, `/ai/api/shared-prompts/`, `/ai/api/task-info/`, `/ai/api/task-solution/`, `/ai/api/groq-limits/` — surfaces Groq rate-limit headers), `health`, password setup, test-panel login, audio transcription. API URLconf is split: page/asset routes in `ai/urls.py`, API/admin routes wired in `DjangoTest/urls.py` via `ai/admin/urls.py`.
  - `ai/consumers.py` — Channels WebSocket consumer (`/ai/chat/ws/<client_id>`, see `ai/routing.py`). Thin orchestrator: delegates auth, prompt resolution, message composition, model invocation, and logging to the `ai/services/` layer. Legacy model aliases are resolved in `ModelCaller`.
  - `ai/models.py` — `ProgrammingLanguage`, `Topic`, `Task` (DL task reference for batch-solve ARM), `Prompt`, `SharedPrompt`, `AIRequestLog`, `ExternalDLAccount`, `AIModelAvailability`, `AIModelHealthRun`, `AIModelTestRun` (`run_type` single/batch), `AIModelTestResult`, `PromptTestCase`, `PromptTestRun` (prompt regression testing), `UpdateLog` (commit history rows synced from git for the «Обновления» page), etc. Model capability metadata (Text/Vision/Reasoning) lives on the registry entries in `ai/model_clients/registry.py`, not in the DB.
  - `ai/querysets.py` — `prompt_queryset_for_user`, the single shared ACL helper for prompt visibility (superusers/staff see all; prompt developers see owned + editor prompts).
  - `ai/serializers.py` / `ai/i18n.py` — lightweight serializers and UI-language localization (`name_ru`, `name_en`, `name_fr`).
  - `ai/middleware.py` — `ExternalAuthMiddleware` validates the `DLSID` cookie and auto-provisions local users; `CsrfSessionFallbackMiddleware` migrates old cookie-based CSRF tokens into the session. External-auth logic lives in `ai/external_auth.py` (error classes `ExternalAuthMisconfigured` / `ExternalAuthUnavailable` / `ExternalAuthUnauthorized`, `fetch_external_user_info`, cookie/url helpers) and is reused by both the middleware and the WebSocket auth service.
  - `ai/dl_api_client.py` — thin client for the external `dl.gsu.by` REST API (task info, sample solutions, user names by ID); reuses the same SSL/proxy settings as external auth. Backs `/ai/api/task-info/` and `/ai/api/task-solution/`. `fetch_user_names(userId)` calls `GET /restapi/get-id-user-info` to enrich `ExternalDLAccount` with `firstName`/`lastName`.
  - `ai/external_account.py` — creates/updates Django users and `ExternalDLAccount` from external API payload; ensures all users are added to the `prompt_developer` group. When `get-user-info` doesn't return `firstName`/`lastName`, enriches from `fetch_user_names` (`GET /restapi/get-id-user-info?userId=N`).
  - `ai/auth_backends.py` — external admin auth backend and helper functions for prompt-developer group management.
  - `ai/http_utils.py` — `safe_relative_url` for safe redirect targets.
  - `ai/throttling.py` — per-user rate limiting (`rate_limiter`) for HTTP views and WebSocket messages, backed by the Django cache. Defaults: 120 WS / 200 HTTP messages per 60s, configurable via `AI_WS_RATE_LIMIT` / `AI_HTTP_RATE_LIMIT` / `AI_RATE_LIMIT_WINDOW` / `AI_RATE_LIMIT_ENABLED`. `RateLimitMiddleware` excludes `/ai/admin/`, `/ai/assets/`, `/ai/static/` from counting — admin page loads fetch 10-20 CSS/JS/image assets each, and counting them would exhaust the per-user budget in 3-4 page navigations.
  - `ai/constants.py` — `MOSCOW_TZ`, `PROMPT_DEVELOPER_GROUP`, `ADMIN_LOGOUT_COOKIE_NAME`, `AI_CACHE_KEY_PREFIX`.
  - `ai/signals.py` — `post_save` / `post_delete` signal on `UpdateLog` that invalidates the `_get_last_update_date` cache so the «last update» date on the index page refreshes instantly (wired in `AiConfig.ready()`). The `ensure_default_groups` `post_migrate` signal (creates the `prompt_developer` group) also lives in `ai/apps.py::AiConfig.ready()`.
  - `ai/grading.py` — shared solution/answer grading helpers (`normalize_solution`, `difflib`-based `ratio` comparator, `SOLVE_RATIO_THRESHOLD`). Used by both batch-solve ARM (`ai/arm_runner.py`) and prompt regression (`ai/prompt_test_runner.py`) so the comparison logic lives in one place (DRY). All comparators are deterministic — no LLM-as-judge.
  - `ai/services/` — high-level services consumed by `consumers.py` (and admin code), re-exported from `ai/services/__init__.py`. This is the KISS/SOLID extraction called out below; consumers orchestrate, services execute.
    - `auth.py` — `WebSocketAuthService` (DLSID auth for the WS scope), `get_user_identity_for_log`, `resolve_external_account`.
    - `prompt_resolver.py` — `PromptResolver` (resolves effective prompt text + names, parses `shared_<pk>` ids), `get_default_shared_prompt`.
    - `message_composer.py` — `MessageComposer` + per-mode builders (`ChatModeBuilder`, `SolveModeBuilder`, …) for chat / solve / find-error message composition. Language instruction (`get_language_instruction`) is appended for every non-Russian message (not only on language change). The «Препромпт» label is localized to «Preprompt» for non-Russian UI languages.
    - `model_caller.py` — `ModelCaller` + `ModelCallResult`; resolves legacy model aliases and invokes the registry, surfacing `humanize_model_error`.
    - `log_writer.py` — `LogWriter` creates/updates `AIRequestLog` records.
    - `conversation_history.py` — compatibility re-export of the shared history store (see `ai/model_clients/history.py`).
    - `auto_translate.py` — automatic translation of localized fields (`*_en`, `*_fr`) using `deep-translator` (Google Translate). Handles placeholder protection (`{language}`, `{язык}`, `{topic}`), chunking for texts >2000 chars, and proxy bypass for direct Google API access. Used by the `auto_translate` management command and the admin «Автоперевод» action.
    - `task_registry.py` — `ensure_task(node_id, ...)` auto-registers a DL task into the local `Task` table when a user solves it on `/ai/solve-problem/` (called by the WebSocket consumer), so solved tasks become available for batch-solve ARM. `apply_dl_task_info(task, data)` maps the DL `get-task-info` response to `Task` fields and is shared with `TaskAdmin.refresh_from_dl` (DRY).
  - `ai/admin/` — custom admin site (`ai_admin_site`) mounted under `/ai/admin/`. URL wiring in `urls.py`.
    - `site.py` — `AIAdminSite`; core permission logic lives here. `app_index()` redirects `/ai/admin/ai/` → `/ai/admin/` (per-app index not used).
    - `models.py` — `PromptAdmin`, `SharedPromptAdmin`, `TopicAdmin`, `ProgrammingLanguageAdmin`, `PromptTestCaseAdmin`, `PromptTestRunAdmin`, `ExternalDLAccountAdmin`, `RestrictedUserAdmin`. Admin action «Автоперевод → EN / FR» on Topic/Prompt/SharedPrompt. `RestrictedUserAdmin` displays DL ID (strips `user_` prefix), reordered columns (last_name, first_name, DL ID, email, role badge).
    - `forms.py` — admin ModelForms (e.g. `PromptForm`) with localized/wide text widgets.
    - `arm.py` — ARM (multi-model check) views: `/ai/admin/arm/find-error/`.
    - `my_prompt.py` — `/ai/admin/prompts/my/` filtered to the current user's prompts.
    - `logs.py` — custom `/ai/admin/ai/airequestlog/` list/detail views.
    - `model_status.py` — `/ai/admin/arm/models/` model availability dashboard.
    - `updates.py` — `/ai/admin/updates/` «Обновления» page: commit history rendered from the `UpdateLog` table with search (author/description) and date-range filtering. Superusers only.
    - `auth.py` / `permissions.py` — admin login form and permission helpers.
  - `ai/model_clients/` — model clients. `registry.py` maps model ids → handler + title + capabilities (used by the consumer and health checker); `config.py` centralizes API tokens/ids/proxy from `.env`; `history.py` provides `ConversationHistory`, a Redis/Django-cache-backed shared conversation history (replaces the legacy in-memory `hist` dict); `exceptions.py` (`humanize_model_error`, `safe_parse_response`); concrete clients `groq.py`, `openrouter.py`, `web_deepseek.py` (free `bot/` pool). Provider wiring: the registry currently only imports `groq`, `openrouter`, and `web_deepseek` — the active catalog is **Groq** (`Groq_*` Llama/Qwen/GPT-OSS models via `GROQ_TOKEN`, `groq.py`) and **OpenRouter** (`OR_*` free models via `OPENROUTER_API_KEY`, `openrouter.py`); `Web_DeepSeek` / `Web_DeepSeek_Thinking` are served by the free `bot/` pool (`web_deepseek.py` → `BOT_POOL_URL`). The SambaNova entries (`DeepSeek_*`, `Llama_*`, `Meta_*`, `MiniMax_*`, `Gemma_*`, `Gpt_*` served via `SC_TOKEN`) are **commented out** in `registry.py` and currently inactive; `sambanova.py` still ships the handlers for when SambaNova is re-enabled. `gigachat.py` / `huggingface.py` are present but not wired into the registry. `DEEPSEEK_API_TOKEN` / `MIST_TOKEN` are currently **not** read by any active handler. `OR_Nemotron_Nano_12B_VL` is the first registered model with `vision: true`.
  - `ai/model_health.py` — daily 04:00 MSK model availability scheduler and checks. The chat page (`ai/views.py::_render_ai_page`) self-heals: when no model is available for the current window it kicks a non-blocking forced sweep via `trigger_model_health_refresh_async()`, so fixing an expired key/balance recovers without waiting for 04:00 MSK or a manual `check_models_health --force`.
  - `ai/arm_runner.py` — asynchronous ARM sequential runner.
  - `ai/utils.py` — helper that calls the bot-pool API.
  - `ai/templates/ai/` — user-facing chat/task pages; `ai/templates/admin/ai/` — custom admin templates.
- `static/admin/js/` — page-specific JS. `chat_template.js` is for the chat page only; `decide_task.js` / `find_error.js` are self-contained for their respective pages (do not load `chat_template.js` on them).
- `bot/` — Node.js/Puppeteer service exposing an OpenAI-compatible API wrapper around `chat.deepseek.com` (`bot/api/` HTTP server + `bot/worker/` browser workers). Runs inside the same container as Django. See `bot/README.md` for details.
  - `bot/worker/bot.js` — Puppeteer launch options include `protocolTimeout: 120000` (2 min) to prevent `Input.insertText` / `dispatchKeyEvent` timeouts on slow pages.
  - `bot/worker/modules/promtps.js` — `sendMessage` sends only the user's message text (not JSON conversation history). `page.goto` is called only for the first message in a conversation (DeepSeek keeps server-side context). Answer stability check uses `stableTicks=4, pollMs=1500` (6 seconds of stable HTML required before reading) to avoid truncated responses. DeepThink toggle logic: `thinking=true` → click `thinkingButtonDisabled` (turn on), `thinking=false` → click `thinkingButtonEnabled` (turn off).
- `nginx/` — internal nginx config. The external reverse-proxy snippet for `dl.gsu.by` lives at `nginx/external-dl.gsu.by.example.nginx-snippet`.
- `doc/` — Russian user/admin/superuser/tester/sysadmin documentation (`.docx`) and `Документация для разработчика.md`. `DOCX.md` and `README.md`/`DEPLOY.md` are the canonical developer/deploy references.
- `mail_bridge.py` — standalone script (not part of the Django app) that polls an IMAP inbox for emails from allowed senders (`MAIL_BRIDGE_ALLOWED_SENDERS`, e.g. `dolinsky@gsu.by`), parses the subject as a command, executes it, replies to the sender over SMTP, and mirrors the result to Telegram. Commands: `restart`, `backup`, `health`, `status`, `make` (forwards email body to Telegram as a task/хотелка), or any other text (forwards to Telegram). Configured via `MAIL_BRIDGE_IMAP_*` / `MAIL_BRIDGE_SMTP_*` / Telegram env vars; intended to run via `nohup` on the prod host. Documented in `README.md`/`DEPLOY.md`.
- `scripts/` — operational shell scripts: `setup_hooks.sh` (installs the git `pre-push` hook from `scripts/hooks/`), `backup_runner.sh` (weekly DB backup), `health_check.sh`, `restart_docker.sh`. `backups/` is git-ignored.
- `static/admin/js/` — page-specific JS. `chat_template.js` is for the chat page only; `decide_task.js` / `find_error.js` are self-contained for their respective pages (do not load `chat_template.js` on them).
- `bot/` — Node.js/Puppeteer service exposing an OpenAI-compatible API wrapper around `chat.deepseek.com`. Runs inside the same container as Django. See `bot/README.md` for details.
- `nginx/` — internal nginx config. The external reverse-proxy snippet for `dl.gsu.by` lives at `nginx/external-dl.gsu.by.example.nginx-snippet`.

### Authentication and permissions

- External auth: `ExternalAuthMiddleware` reads `DLSID` cookie, calls `EXTERNAL_AUTH_API_URL`, and either redirects unauthenticated users to the main site or provisions a local Django user.
- Admin access: only `staff`/`superuser` users have access to the full Django admin (`/ai/admin/`). All normal users are added to the `prompt_developer` group on creation and can access ARM, "My prompt", and "All prompts" inside the custom admin area.
- Test-panel login (`/ai/test-panel/login/`) is a separate password-based entry for prompt developers.

### Prompt model

- `Prompt` — topic-bound prompt with localized `prompt_name_*` / `prompt_text_*` fields. It can reference a `SharedPrompt` and/or override its text via `prompt_text_override`.
- `SharedPrompt` — topic-independent prompt, optionally restricted to specific programming languages through `programming_languages`.
- `Prompt.get_effective_text(ui_language, programming_language_name)` resolves the final text, replacing `{language}` / `{язык}` placeholders.
- Prompt ACL: prompt developers see all prompts but can only edit prompts they own or are listed in `editors`. Admins can change `owner` and `editors`.

### UI language

Supported UI languages are Russian (`Русский`), English (`English`), and French (`Français`). API endpoints accept `ui_language`; serializers return localized `name` fields (`topic_name_*`, `prompt_name_*`). The front-end pages store the selected language in `localStorage` under `ai_interface_language`.

### Cache, Redis, and rate limiting

- `CACHES` (configured in `DjangoTest/settings.py`) uses Redis when `REDIS_URL` is set, otherwise Django `LocMemCache` for local dev. The cache backs both the rate limiter (`ai/throttling.py`) and the shared conversation history (`ai/model_clients/history.py`, `ConversationHistory`) — so history survives process restarts and is shared across Daphne workers in production.
- Per-user rate limiting is on by default (`AI_RATE_LIMIT_ENABLED`); see defaults above. `RateLimitMiddleware` already enforces the HTTP limit on every `/ai/` path, so do NOT also wrap such views in `@rate_limited` — it double-counts the same per-user counter. The 429 response is JSON for any AJAX/fetch caller (`Accept: application/json`, `X-Requested-With`, `Sec-Fetch-Mode: cors`, or `/ai/api/` paths) and plain text only for browser navigations — frontend fetch sites must guard `response.ok` before `response.json()`.

### Model capabilities

- Model capability annotations (Text/Vision/Reasoning) are declared per registry entry in `ai/model_clients/registry.py` (`capabilities(key)`) and surfaced on the «Состояние моделей» page and in the chat model selector (reasoning models get a «думающая» marker). Add new capabilities there, not in the DB.

### Prompt regression testing

Prompt regression tests (`PromptTestCase`, `PromptTestRun`) allow running a set of test cases against AI models to verify prompt quality. Test cases define input text, expected output, comparator, and match threshold. Runs are executed asynchronously via `ai/prompt_test_runner.py` (CLI: `python manage.py run_prompt_tests`), and share the normalization/comparison logic with batch-solve ARM through `ai/grading.py`. The admin pages `/ai/admin/prompt-regression/` (start/status) and the ModelAdmins for `PromptTestCase` / `PromptTestRun` are available to staff/superusers and prompt developers (view-only for developers). Migration `0023_prompt_tests` creates the DB tables.

### Model availability

The health scheduler runs once per day for the 04:00 MSK window. It starts automatically inside Daphne/Gunicorn/Uvicorn/Django runserver unless `AI_DISABLE_HEALTH_SCHEDULER=1` is set. It queries the handlers registered in `ai/model_clients/registry.py`.

When Web DeepSeek (`Web_DeepSeek` / `Web_DeepSeek_Thinking`) is found down, the health check auto-restarts the `bot/` pool (автоподъём) and re-checks once, gated by `AI_WEB_DEEPSEEK_AUTORECOVERY` (default on). The bot pool exposes `POST /api/restart` (`bot/api/server.js` → `botManager.restartAll()`); Django calls it via `ai/model_clients/web_deepseek.py::restart_bot_pool`. The recovery outcome is annotated in `AIModelAvailability.last_message`.

### ARM persistence and reporting

ARM runs are persisted: `AIModelTestRun` (one per run, `run_type` single/batch) + `AIModelTestResult` (one per model for single runs, one per (model, task) for batch runs). `ai/arm_runner.py` keeps an in-memory job for live progress but the DB is the source of truth for completed/evicted runs (`get_arm_run_snapshot` falls back to it). The find-error report (`_build_report` / `_build_summary`) includes a `summary` table per model — % solved (desc), average response time (asc), tokens — rendered on `/ai/admin/arm/find-error/`.

Batch-solve ARM (`run_type="batch"`, `/ai/admin/arm/solve/`) sends each available model the statement of every active `Task`, grades the model's solution against the DL sample solution (`fetch_task_solution`), and records `verdict` (solved/failed/skipped) + `duration_seconds` per (task, model) in `AIModelTestResult`. Grading is approximate (`normalize_solution` + `difflib` ratio ≥ `SOLVE_RATIO_THRESHOLD`, not a real test run); the normalization/comparison logic lives in `ai/grading.py` and is shared with prompt regression. The report (`_build_batch_report` via `_per_bucket`) renders per-model and per-topic tables (% solved, avg time) plus an overall model table sorted by % solved desc / avg time asc, and surfaces the DL test result/error and the code that was sent to DL for each (task, model). The operator sources tasks from DL by `node_id` (`TaskAdmin.refresh_from_dl` → `fetch_task_info` fills name/statement/task_id; the shared mapping is `apply_dl_task_info` in `ai/services/task_registry.py`) and assigns topic / programming language / `file_extension` locally. A per-task prompt can be selected (endpoint `/ai/admin/arm/solve/prompts/`), dynamically filtered by the task's topic and programming language; tasks can be added mid-run via `/ai/admin/arm/solve/add-task/`. Requires the admin's DLSID at run start (DL sample fetches use the captured session id).

### Update log

`UpdateLog` (migrations `0025_add_update_log` / `0026_populate_update_log`) stores the project's commit history in the DB so the superuser-only `/ai/admin/updates/` page can render it with search and date filtering without hitting git at request time. The table is kept in sync by the `sync_update_log` management command (also exposed as an admin action), which reads `git log` from the current branch and maps English commit messages to Russian descriptions. The «last update» date shown on the admin index is cached under `ai_last_update_date` and invalidated instantly by the `post_save`/`post_delete` signal in `ai/signals.py`. The git `pre-push` hook (installed via `scripts/setup_hooks.sh` from `scripts/hooks/`) can trigger a `sync_update_log` run on push.

### Important files to read when working on...

- Auth flow: `ai/middleware.py`, `ai/external_auth.py`, `ai/external_account.py`, `ai/auth_backends.py`, `ai/services/auth.py`, `ai/admin/auth.py`, `ai/admin/permissions.py`.
- Admin access control: `ai/admin/site.py` (especially `has_permission` and `each_context`), `ai/admin/urls.py`.
- Prompts / UI language / ACL: `ai/models.py`, `ai/querysets.py`, `ai/serializers.py`, `ai/i18n.py`, `ai/services/prompt_resolver.py`, `ai/views.py` (`get_problem_data`), `static/admin/js/decide_task.js`, `static/admin/js/find_error.js`.
- Auto-translation: `ai/services/auto_translate.py`, `ai/management/commands/auto_translate.py`.
- Chat / WebSocket: `ai/consumers.py`, `ai/routing.py`, `ai/services/` (auth / prompt_resolver / message_composer / model_caller / log_writer), `ai/model_clients/` (`registry.py`, `config.py`, `history.py`), `ai/throttling.py`, `ai/templates/ai/base_chat.html`, `static/admin/js/chat_template.js`.
- DL REST API integration: `ai/dl_api_client.py` (task info, solutions, user names), `ai/views.py` (`get_task_info_view`, `get_task_solution_view`).
- ARM: `ai/admin/arm.py`, `ai/arm_runner.py`, `ai/grading.py`, `ai/services/task_registry.py` (`apply_dl_task_info`), `ai/templates/admin/ai/arm_find_error.html`, `ai/templates/admin/ai/arm_solve.html`.
- Prompt regression: `ai/prompt_test_runner.py`, `ai/admin/prompt_regression.py`, `ai/management/commands/run_prompt_tests.py`, `ai/grading.py`, `ai/models.py` (`PromptTestCase`, `PromptTestRun`).
- Update log: `ai/models.py` (`UpdateLog`), `ai/admin/updates.py`, `ai/management/commands/sync_update_log.py`, `ai/signals.py`, `scripts/setup_hooks.sh`, `scripts/hooks/`.
- Bot pool: `bot/README.md`, `bot/api/server.js`, `bot/api/botManager.js`, `bot/worker/bot.js`, `bot/worker/modules/promtps.js`, `bot/worker/data.json`.
- Email→ops bridge: `mail_bridge.py` (IMAP/SMTP/Telegram, restart/backup/health/status/make commands), `scripts/backup_runner.sh`, `scripts/health_check.sh`, `scripts/restart_docker.sh`.

## Coding standards and architecture principles

When modifying code in this repository, follow SOLID, DRY, and KISS. The audit found several violations; new code must not reintroduce them.

### DRY (Don't Repeat Yourself)

- Do not copy-paste large blocks of JavaScript between `static/admin/js/chat_template.js`, `decide_task.js`, and `find_error.js`. Shared behavior (voice controls, accordion rendering, markdown conversion, WebSocket helpers, localization) must live in `static/admin/js/ai-common.js` and be imported or reused by page-specific scripts.
- Do not duplicate placeholder substitution logic between `Prompt.get_effective_text()` and `SharedPrompt.get_effective_text()` in `ai/models.py`. Use a shared helper such as `replace_placeholders(base, language, topic, message, code)`.
- Do not duplicate message-building logic for chat / solve / find-error modes across `ai/consumers.py` and `ai/admin/arm.py`. Centralize prompt/message composition in a dedicated service module.
- Do not duplicate model-client wrappers in `ai/model_clients/sambanova.py`. Prefer a factory or generic caller that receives the model name and parameters.
- Avoid re-implementing error detection in multiple places; reuse `humanize_model_error` and `safe_parse_response` from `ai/model_clients/exceptions.py`.

### KISS (Keep It Simple, Stupid)

- Keep WebSocket consumer logic focused. Prompt resolution, message building, logging, and model invocation already live in dedicated services under `ai/services/` (`WebSocketAuthService`, `PromptResolver`, `MessageComposer` + mode builders, `ModelCaller`, `LogWriter`, `ConversationHistory`) — `ai/consumers.py` should stay a thin orchestrator and not grow new business logic. New chat modes belong as a `ModeMessageBuilder` subclass, not `if type == …` branches in the consumer.
- Avoid module-level side effects such as `django.setup()` in consumers or `load_dotenv()` in middleware `__init__`. Environment loading is handled in `DjangoTest/settings.py`.
- Do not hardcode timezone offsets (e.g. `+ timedelta(hours=3)`). Use `MOSCOW_TZ` from `ai/constants.py` and `timezone.localtime()`.
- Prefer standard Django / Channels patterns over custom reinvention.
- Remove unused globals and aliases (e.g. `current_tokens`, `_safe_relative_url`) when refactoring.
- Move large inline scripts from templates (e.g. `ai/templates/admin/ai/arm_find_error.html`) into `static/admin/js/` files.

### SOLID

- **Single Responsibility:** each module, class, and function should do one thing. Consumers orchestrate; services execute; external API clients only talk to APIs.
- **Open/Closed:** new chat modes and AI models should be added by registering a handler or entry in a registry, not by editing `if type == "1" / "2" / "3"` blocks in `ai/consumers.py`.
- **Liskov Substitution:** custom auth backends must honor the Django base interface and must not silently bypass required DLSID validation.
- **Interface Segregation:** avoid "god modules" such as `ai/utils.py` that re-export everything. Import from the actual module that owns the code.
- **Dependency Inversion:** high-level code (`consumers`, `views`) should depend on abstractions (`registry`, service classes), not concrete model-client implementations.

### Security baseline

- Never use `verify=False` for HTTPS requests in production. `SKIP_SSL_VERIFICATION` is only for local development and must be clearly documented.
- Never mark endpoints `@csrf_exempt` without strong authentication. `ai/views.transcribe_audio` must require authentication.
- Never log full external API responses, session tokens, or `user_info` at INFO level.
- Do not expose the bot pool (port 3000) to public networks; keep it on `127.0.0.1` or an internal Docker network.
- Static files in production must be served by nginx, not by Django's `static()` helper.
- Escape any user/model-generated HTML before inserting it into the DOM. Think-block content in particular must not be assigned to `innerHTML` unescaped.
- Admin set-password flow must accept `external_user_id` only when it has been validated by `ExternalAuthMiddleware` and matches the provisioned Django user (`_session_matches_external_id`).

## Notes from existing docs

- Branch workflow for students: each student works in a separate branch named after their surname and opens a PR to `main`. (from `README.md`)
- The repository does not implement its own login; in production it runs behind the existing `dl.gsu.by` reverse proxy. (from `DEPLOY.md`)
- For production, set `DEBUG=0`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `USE_X_FORWARDED_PROTO=1`, and consider `CSRF_COOKIE_DOMAIN`/`SESSION_COOKIE_DOMAIN=.gsu.by` for cross-subdomain sessions. (from `DEPLOY.md`)
- The active model catalog is **Groq** (`Groq_*`, `GROQ_TOKEN`) and **OpenRouter** (`OR_*` free models, `OPENROUTER_API_KEY`), wired in `ai/model_clients/registry.py`; `Web_DeepSeek` / `Web_DeepSeek_Thinking` are served by the free `bot/` pool. The SambaNova entries (`DeepSeek_*` etc. via `SC_TOKEN`) are currently commented out in the registry — re-enable them in `registry.py` to serve the SambaNova catalog again. `DEEPSEEK_API_TOKEN` / `MIST_TOKEN` are not read by any active handler. Rotate `GROQ_TOKEN` / `OPENROUTER_API_KEY` to fix the catalog models; restart the `bot/` pool to fix Web DeepSeek.
- Auto-translation of localized fields has two paths: `auto_translate` (Google via `deep-translator`, `requirements.txt: deep-translator==1.11.4`, no API key) and `translate_prompts` (LLM-based, via a registered model handler — default `DeepSeek_V3_1` over SambaNova/`SC_TOKEN`, swaps `{language}`/`{язык}`/`{topic}`/`{тема}`/`{message}`/`{code}` placeholders for sentinel tokens so the model cannot reword them). Run `python manage.py auto_translate` / `translate_prompts` to fill empty `*_en` / `*_fr` fields, or use the admin «Автоперевод» action on Topic/Prompt/SharedPrompt.
- Language instruction: `get_language_instruction(ui_language)` in `ai/i18n.py` appends a language constraint to the AI message for non-Russian UI languages, ensuring the model replies in the selected language.
