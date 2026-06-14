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
  - `ai/views.py` — page views, API endpoints (`/ai/api/problem-data/`, `/ai/api/prompts/`, etc.), password setup, test-panel login.
  - `ai/consumers.py` — Channels WebSocket consumer (`/ai/chat/ws/<client_id>`). Resolves legacy model aliases, loads prompt text, talks to model clients, writes `AIRequestLog`.
  - `ai/models.py` — `ProgrammingLanguage`, `Topic`, `Prompt`, `SharedPrompt`, `AIRequestLog`, `ExternalDLAccount`, etc.
  - `ai/serializers.py` / `ai/i18n.py` — lightweight serializers and UI-language localization (`name_ru`, `name_en`, `name_fr`).
  - `ai/middleware.py` — `ExternalAuthMiddleware` validates the `DLSID` cookie against the external DL API and auto-provisions local users; `CsrfSessionFallbackMiddleware` migrates old cookie-based CSRF tokens into the session.
  - `ai/external_account.py` — creates/updates Django users and `ExternalDLAccount` from external API payload; ensures all users are added to the `prompt_developer` group.
  - `ai/auth_backends.py` — external admin auth backend and helper functions for prompt-developer group management.
  - `ai/admin/` — custom admin site (`ai_admin_site`) mounted under `/ai/admin/`.
    - `site.py` — `AIAdminSite`; core permission logic lives here.
    - `models.py` — `PromptAdmin`, `SharedPromptAdmin`, `TopicAdmin`, `ProgrammingLanguageAdmin`.
    - `arm.py` — ARM (multi-model check) views: `/ai/admin/arm/find-error/`.
    - `my_prompt.py` — `/ai/admin/prompts/my/` filtered to the current user's prompts.
    - `logs.py` — custom `/ai/admin/ai/airequestlog/` list/detail views.
    - `model_status.py` — `/ai/admin/arm/models/` model availability dashboard.
    - `auth.py` / `permissions.py` — admin login form and permission helpers.
  - `ai/model_clients/` — model client registry and implementations (GigaChat, SambaNova, HuggingFace, web DeepSeek via `bot/` pool, etc.).
  - `ai/model_health.py` — daily 04:00 MSK model availability scheduler and checks.
  - `ai/arm_runner.py` — asynchronous ARM sequential runner.
  - `ai/utils.py` — helper that calls the bot-pool API.
  - `ai/templates/ai/` — user-facing chat/task pages; `ai/templates/admin/ai/` — custom admin templates.
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

### Model availability

The health scheduler runs once per day for the 04:00 MSK window. It starts automatically inside Daphne/Gunicorn/Uvicorn/Django runserver unless `AI_DISABLE_HEALTH_SCHEDULER=1` is set.

### Important files to read when working on...

- Auth flow: `ai/middleware.py`, `ai/external_account.py`, `ai/auth_backends.py`, `ai/admin/auth.py`, `ai/admin/permissions.py`.
- Admin access control: `ai/admin/site.py` (especially `has_permission` and `each_context`).
- Prompts / UI language: `ai/models.py`, `ai/serializers.py`, `ai/i18n.py`, `ai/views.py` (`get_problem_data`), `static/admin/js/decide_task.js`, `static/admin/js/find_error.js`.
- Chat / WebSocket: `ai/consumers.py`, `ai/model_clients/`, `ai/templates/ai/base_chat.html`, `static/admin/js/chat_template.js`.
- ARM: `ai/admin/arm.py`, `ai/arm_runner.py`, `ai/templates/admin/ai/arm_find_error.html`.
- Bot pool: `bot/README.md`, `bot/api/server.js`, `bot/api/botManager.js`, `bot/worker/bot.js`.

## Notes from existing docs

- Branch workflow for students: each student works in a separate branch named after their surname and opens a PR to `main`. (from `README.md`)
- The repository does not implement its own login; in production it runs behind the existing `dl.gsu.by` reverse proxy. (from `DEPLOY.md`)
- For production, set `DEBUG=0`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `USE_X_FORWARDED_PROTO=1`, and consider `CSRF_COOKIE_DOMAIN`/`SESSION_COOKIE_DOMAIN=.gsu.by` for cross-subdomain sessions. (from `DEPLOY.md`)
- If `DEEPSEEK_API_TOKEN` is set, Django uses the official DeepSeek API and the bot pool is bypassed. Remove the token to force requests through the free `bot/` pool. (from `bot/README.md`)
