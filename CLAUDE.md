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
```

## High-level architecture

### Django app structure

- `DjangoTest/` — project settings, root URLconf, ASGI/WSGI entrypoints.
- `ai/` — the only Django app.
  - `ai/views.py` — page views, API endpoints (`/ai/api/problem-data/`, `/ai/api/prompts/`, `/ai/api/languages/`, `/ai/api/topics/`, `/ai/api/shared-prompts/`, `/ai/api/task-info/`, `/ai/api/task-solution/`), `health`, password setup, test-panel login, audio transcription. API URLconf is split: page/asset routes in `ai/urls.py`, API/admin routes wired in `DjangoTest/urls.py` via `ai/admin/urls.py`.
  - `ai/consumers.py` — Channels WebSocket consumer (`/ai/chat/ws/<client_id>`, see `ai/routing.py`). Thin orchestrator: delegates auth, prompt resolution, message composition, model invocation, and logging to the `ai/services/` layer. Legacy model aliases are resolved in `ModelCaller`.
  - `ai/models.py` — `ProgrammingLanguage`, `Topic`, `Prompt`, `SharedPrompt`, `AIRequestLog`, `ExternalDLAccount`, `AIModelAvailability`, `AIModelHealthRun`, `AIModelTokenBudget` (admin-maintained token allowance; spent aggregated from `AIRequestLog.tokens` via `ai/token_budget.py`), etc. Model capability metadata (Text/Vision/Reasoning) lives on the registry entries in `ai/model_clients/registry.py`, not in the DB.
  - `ai/querysets.py` — `prompt_queryset_for_user`, the single shared ACL helper for prompt visibility (superusers/staff see all; prompt developers see owned + editor prompts).
  - `ai/serializers.py` / `ai/i18n.py` — lightweight serializers and UI-language localization (`name_ru`, `name_en`, `name_fr`).
  - `ai/middleware.py` — `ExternalAuthMiddleware` validates the `DLSID` cookie and auto-provisions local users; `CsrfSessionFallbackMiddleware` migrates old cookie-based CSRF tokens into the session. External-auth logic lives in `ai/external_auth.py` (error classes `ExternalAuthMisconfigured` / `ExternalAuthUnavailable` / `ExternalAuthUnauthorized`, `fetch_external_user_info`, cookie/url helpers) and is reused by both the middleware and the WebSocket auth service.
  - `ai/dl_api_client.py` — thin client for the external `dl.gsu.by` REST API (task info, sample solutions); reuses the same SSL/proxy settings as external auth. Backs `/ai/api/task-info/` and `/ai/api/task-solution/`.
  - `ai/external_account.py` — creates/updates Django users and `ExternalDLAccount` from external API payload; ensures all users are added to the `prompt_developer` group.
  - `ai/auth_backends.py` — external admin auth backend and helper functions for prompt-developer group management.
  - `ai/http_utils.py` — `safe_relative_url` for safe redirect targets.
  - `ai/throttling.py` — per-user rate limiting (`rate_limiter`) for HTTP views and WebSocket messages, backed by the Django cache. Defaults: 120 WS / 60 HTTP messages per 60s, configurable via `AI_WS_RATE_LIMIT` / `AI_HTTP_RATE_LIMIT` / `AI_RATE_LIMIT_WINDOW` / `AI_RATE_LIMIT_ENABLED`.
  - `ai/constants.py` — `MOSCOW_TZ`, `PROMPT_DEVELOPER_GROUP`, `ADMIN_LOGOUT_COOKIE_NAME`, `AI_CACHE_KEY_PREFIX`.
  - `ai/services/` — high-level services consumed by `consumers.py` (and admin code), re-exported from `ai/services/__init__.py`. This is the KISS/SOLID extraction called out below; consumers orchestrate, services execute.
    - `auth.py` — `WebSocketAuthService` (DLSID auth for the WS scope), `get_user_identity_for_log`, `resolve_external_account`.
    - `prompt_resolver.py` — `PromptResolver` (resolves effective prompt text + names, parses `shared_<pk>` ids), `get_default_shared_prompt`.
    - `message_composer.py` — `MessageComposer` + per-mode builders (`ChatModeBuilder`, `SolveModeBuilder`, …) for chat / solve / find-error message composition.
    - `model_caller.py` — `ModelCaller` + `ModelCallResult`; resolves legacy model aliases and invokes the registry, surfacing `humanize_model_error`.
    - `log_writer.py` — `LogWriter` creates/updates `AIRequestLog` records.
    - `conversation_history.py` — compatibility re-export of the shared history store (see `ai/model_clients/history.py`).
  - `ai/admin/` — custom admin site (`ai_admin_site`) mounted under `/ai/admin/`. URL wiring in `urls.py`.
    - `site.py` — `AIAdminSite`; core permission logic lives here.
    - `models.py` — `PromptAdmin`, `SharedPromptAdmin`, `TopicAdmin`, `ProgrammingLanguageAdmin`.
    - `forms.py` — admin ModelForms (e.g. `PromptForm`) with localized/wide text widgets.
    - `arm.py` — ARM (multi-model check) views: `/ai/admin/arm/find-error/`.
    - `my_prompt.py` — `/ai/admin/prompts/my/` filtered to the current user's prompts.
    - `logs.py` — custom `/ai/admin/ai/airequestlog/` list/detail views.
    - `model_status.py` — `/ai/admin/arm/models/` model availability dashboard.
    - `auth.py` / `permissions.py` — admin login form and permission helpers.
  - `ai/model_clients/` — model clients. `registry.py` maps model ids → handler + title (used by the consumer and health checker); `config.py` centralizes API tokens/ids/proxy from `.env`; `history.py` provides `ConversationHistory`, a Redis/Django-cache-backed shared conversation history (replaces the legacy in-memory `hist` dict); `exceptions.py` (`humanize_model_error`, `safe_parse_response`); concrete clients `gigachat.py`, `sambanova.py`, `huggingface.py`, `web_deepseek.py` (free `bot/` pool).
  - `ai/model_health.py` — daily 04:00 MSK model availability scheduler and checks.
  - `ai/arm_runner.py` — asynchronous ARM sequential runner.
  - `ai/utils.py` — helper that calls the bot-pool API.
  - `ai/templates/ai/` — user-facing chat/task pages; `ai/templates/admin/ai/` — custom admin templates.
- `static/admin/js/` — page-specific JS. `chat_template.js` is for the chat page only; `decide_task.js` / `find_error.js` are self-contained for their respective pages (do not load `chat_template.js` on them).
- `bot/` — Node.js/Puppeteer service exposing an OpenAI-compatible API wrapper around `chat.deepseek.com` (`bot/api/` HTTP server + `bot/worker/` browser workers). Runs inside the same container as Django. See `bot/README.md` for details.
- `nginx/` — internal nginx config. The external reverse-proxy snippet for `dl.gsu.by` lives at `nginx/external-dl.gsu.by.example.nginx-snippet`.
- `doc/` — Russian user/admin/superuser/tester/sysadmin documentation (`.docx`) and `Документация для разработчика.md`. `DOCX.md` and `README.md`/`DEPLOY.md` are the canonical developer/deploy references.
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

### Token budget and model capabilities

- Model capability annotations (Text/Vision/Reasoning) are declared per registry entry in `ai/model_clients/registry.py` (`capabilities(key)`) and surfaced on the «Состояние моделей» page and in the chat model selector (reasoning models get a «думающая» marker). Add new capabilities there, not in the DB.
- Token budget (`AIModelTokenBudget`, `ai/token_budget.py`) is admin-maintained: an admin sets the total limit + issue date; spent tokens are aggregated from `AIRequestLog.tokens` since `issued_at`. Attribution per provider is approximate until a per-model/per-provider token field exists — `ai/token_budget.py` is the single place to make it exact.

### Model availability

The health scheduler runs once per day for the 04:00 MSK window. It starts automatically inside Daphne/Gunicorn/Uvicorn/Django runserver unless `AI_DISABLE_HEALTH_SCHEDULER=1` is set. It queries the handlers registered in `ai/model_clients/registry.py`.

When Web DeepSeek (`Web_DeepSeek` / `Web_DeepSeek_Thinking`) is found down, the health check auto-restarts the `bot/` pool (автоподъём) and re-checks once, gated by `AI_WEB_DEEPSEEK_AUTORECOVERY` (default on). The bot pool exposes `POST /api/restart` (`bot/api/server.js` → `botManager.restartAll()`); Django calls it via `ai/model_clients/web_deepseek.py::restart_bot_pool`. The recovery outcome is annotated in `AIModelAvailability.last_message`.

### ARM persistence and reporting

ARM runs are persisted: `AIModelTestRun` (one per run) + `AIModelTestResult` (one per model). `ai/arm_runner.py` keeps an in-memory job for live progress but the DB is the source of truth for completed/evicted runs (`get_arm_run_snapshot` falls back to it). The report (`_build_report`) includes a `summary` table per model — % solved (desc), average response time (asc), tokens — rendered on `/ai/admin/arm/find-error/`. The batch-over-all-tasks and «pull erroneous solutions from DL» parts of the ARM spec are not implemented (they depend on the unbuilt DL-API); the DB/report schema is ready for them.

### Important files to read when working on...

- Auth flow: `ai/middleware.py`, `ai/external_auth.py`, `ai/external_account.py`, `ai/auth_backends.py`, `ai/services/auth.py`, `ai/admin/auth.py`, `ai/admin/permissions.py`.
- Admin access control: `ai/admin/site.py` (especially `has_permission` and `each_context`), `ai/admin/urls.py`.
- Prompts / UI language / ACL: `ai/models.py`, `ai/querysets.py`, `ai/serializers.py`, `ai/i18n.py`, `ai/services/prompt_resolver.py`, `ai/views.py` (`get_problem_data`), `static/admin/js/decide_task.js`, `static/admin/js/find_error.js`.
- Chat / WebSocket: `ai/consumers.py`, `ai/routing.py`, `ai/services/` (auth / prompt_resolver / message_composer / model_caller / log_writer), `ai/model_clients/` (`registry.py`, `config.py`, `history.py`), `ai/throttling.py`, `ai/templates/ai/base_chat.html`, `static/admin/js/chat_template.js`.
- DL REST API integration: `ai/dl_api_client.py`, `ai/views.py` (`get_task_info_view`, `get_task_solution_view`).
- ARM: `ai/admin/arm.py`, `ai/arm_runner.py`, `ai/templates/admin/ai/arm_find_error.html`.
- Bot pool: `bot/README.md`, `bot/api/server.js`, `bot/api/botManager.js`, `bot/worker/bot.js`.

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
- If `DEEPSEEK_API_TOKEN` is set, Django uses the official DeepSeek API and the bot pool is bypassed. Remove the token to force requests through the free `bot/` pool. (from `bot/README.md`)
