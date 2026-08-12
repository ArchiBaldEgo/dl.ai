# CLAUDE.md

Django + Channels (Daphne) app mounted under `/ai/` on the same domain as `dl.gsu.by`.
DLSID cookie authentication is delegated to dl.gsu.by via `EXTERNAL_AUTH_API_URL`
(local users auto-provisioned). The model catalog is **Web DeepSeek** (`Web_DeepSeek[_Thinking]`
served by the `WebDeepseek/` Node/Puppeteer service, OpenAI-compatible `BOT_POOL_URL` API on
loopback `:3000`) + **Web Kimi** (`Web_Kimi` served by the `WebKimi/` Node/Puppeteer service,
`KIMI_POOL_URL` on loopback `:3001`; Kimi login is NOT automated — the Chrome profile is seeded
manually once via `WebKimi/seed.js`) + **OpenRouter** (`OR_*` free models via `OPENROUTER_API_KEY`)
+ **Ollama** (`Ollama_*` cloud models `<name>:cloud` via `OLLAMA_API_KEY` against
`https://api.ollama.com`, plus optional local Ollama on `OLLAMA_HOST`). All four run in the same
container (Dockerfile `CMD` starts both bot pools + Daphne). Groq and SambaNova providers are
gated behind `AI_ENABLE_GROQ` / `AI_ENABLE_SAMBANOVA` (default off) — flip in `.env` to activate.

## ai/ module map

- `views.py` — page views + API endpoints (`/ai/api/problem-data|prompts|languages|topics|shared-prompts|task-info|task-solution|groq-limits/`), `health`, password setup, audio transcription; `_render_ai_page` builds the model selector and self-heals availability via `trigger_model_health_refresh_async()`; `asset_view` serves `/ai/assets/` with HTTP revalidation (304, no `?v=` bump needed). Gotcha: page/asset routes in `ai/urls.py`, API/admin routes wired via `ai/admin/urls.py` into `DjangoTest/urls.py`.
- `consumers.py` — Channels WS consumer (`/ai/chat/ws/<client_id>`, see `routing.py`); thin orchestrator delegating to `services/`. Gotcha: add chat modes as a `ModeMessageBuilder` subclass, never `if type==…` here; legacy aliases resolved in `ModelCaller`.
- `models.py` — `ProgrammingLanguage`, `Topic`, `Task`, `Prompt`, `SharedPrompt`, `AIRequestLog`, `ExternalDLAccount`, `AIModelAvailability`, `AIModelHealthRun`, `AIModelTestRun`/`AIModelTestResult`, `PromptTestCase`/`PromptTestRun`, `UpdateLog`, `AIAppSettings` (singleton `get_solo()`, holds `favorites_epoch`). Gotcha: capability metadata (Text/Vision/Reasoning) lives on registry entries, not here.
- `querysets.py` — `prompt_queryset_for_user`, the single prompt-visibility ACL helper (superusers/staff all; prompt developers owned + editor). Reuse it; do not hand-roll.
- `serializers.py` / `i18n.py` — lightweight serializers + UI-language localization (`name_ru/en/fr`); `get_language_instruction(ui_language)` appends a non-Russian reply constraint for every non-Russian message.
- `middleware.py` — `ExternalAuthMiddleware` (validates `DLSID`, caches `user_info` in session for `AI_AUTH_CACHE_TTL` s; falls back to last cache when dl.gsu.by is down, `503` only when no cache and path not optional) and `CsrfSessionFallbackMiddleware`. Gotcha: real external-auth logic lives in `ai/external_auth.py` (`fetch_external_user_info`, error classes), reused by WS auth service.
- `dl_api_client.py` — thin client for dl.gsu.by REST API (task info, sample solutions, user names via `GET /restapi/get-id-user-info`); backs `/ai/api/task-info/` and `/ai/api/task-solution/`; reuses external-auth SSL/proxy settings.
- `external_account.py` — creates/updates Django users + `ExternalDLAccount` from external payload; enrolls everyone into `prompt_developer` group; enriches names via `fetch_user_names` when `get-user-info` omits them.
- `auth_backends.py` — external admin auth backend + `prompt_developer` group management helpers.
- `http_utils.py` — `safe_relative_url` for safe redirect targets.
- `throttling.py` — per-user rate limiting (Django cache-backed; 120 WS / 200 HTTP per 60s via `AI_WS_RATE_LIMIT`/`AI_HTTP_RATE_LIMIT`/`AI_RATE_LIMIT_WINDOW`/`AI_RATE_LIMIT_ENABLED`). Gotcha: `RateLimitMiddleware` already counts every `/ai/` HTTP path (excludes `/ai/admin|assets|static/`), so do NOT also `@rate_limited` such views — it double-counts the same counter.
- `constants.py` — `MOSCOW_TZ`, `PROMPT_DEVELOPER_GROUP`, `ADMIN_LOGOUT_COOKIE_NAME`, `AI_CACHE_KEY_PREFIX`. Use `MOSCOW_TZ`, never hardcoded offsets.
- `signals.py` — `post_save`/`post_delete` on `UpdateLog` invalidates the `ai_last_update_date` cache; `ensure_default_groups` `post_migrate` creates `prompt_developer`. Wired in `AiConfig.ready()`.
- `grading.py` — shared solution/answer comparator (`normalize_solution`, difflib `ratio`, `SOLVE_RATIO_THRESHOLD`); consumed by `arm_runner.py` (batch-solve) and `prompt_test_runner.py`. Gotcha: deterministic only — no LLM-as-judge; keep it DRY.
- `services/` — `WebSocketAuthService` (auth.py), `PromptResolver` + `get_default_shared_prompt` (prompt_resolver.py), `MessageComposer` + `ChatModeBuilder`/`SolveModeBuilder`/… (message_composer.py), `ModelCaller` + `ModelCallResult` (model_caller.py), `LogWriter` (log_writer.py), `ConversationHistory` re-export (conversation_history.py), `auto_translate` helpers (auto_translate.py), `ensure_task`/`apply_dl_task_info` (task_registry.py). Consumers orchestrate; services execute.
- `admin/` — custom `ai_admin_site` at `/ai/admin/`: `site.py` (`AIAdminSite`, `/ai/admin/ai/`→`/ai/admin/` redirect), `models.py` (ModelAdmins + «Автоперевод» action), `forms.py`, `arm.py` (ARM views), `my_prompt.py`, `logs.py`, `model_status.py`, `updates.py` (commit-history page), `prompt_regression.py`, `test_console.py` (тестовая консоль — запуск `ai/tests.py` в изолированном subprocess), `auth.py`/`permissions.py` (`can_access_*` helpers, incl. `can_access_test_console`). Core permission logic in `site.py`.
- `test_console_runner.py` — runner for the admin test console: runs `ai/tests.py` as `manage.py test ai --settings=DjangoTest.test_settings --verbosity=2` in a subprocess (isolated sqlite/locmem test-settings — `setup_test_environment` patches globals, unsafe in a live Daphne thread). In-memory single-flight job (no DB model), mirrors `arm_runner.py`/`prompt_test_runner.py` shape.
- `model_clients/` — `registry.py` (model id → handler + title + capabilities; active: `web_deepseek`, `web_kimi`, `openrouter`, `ollama`; `groq` + `sambanova` gated via `AI_ENABLE_GROQ`/`AI_ENABLE_SAMBANOVA`), `web_deepseek.py` (free DeepSeek pool client + `restart_bot_pool`), `web_kimi.py` (Kimi pool client + `restart_kimi_bot_pool`), `ollama.py` (cloud `<name>:cloud` models via `OLLAMA_API_KEY` + local; declarative `OLLAMA_MODELS` table), `_base.py` (DRY helpers: `make_table_handlers`, `bearer_headers`, `proxy_bypass_session`), `config.py` (centralized .env tokens/proxy), `exceptions.py` (`humanize_model_error`, `safe_parse_response`), `history.py` (`ConversationHistory`, Redis/Django-cache shared history), plus provider modules `openrouter.py`/`groq.py`/`sambanova.py`/`gigachat.py`/`huggingface.py`. Add new models as declarative-table rows + a generated handler (via `_base.make_table_handlers`), not hand-rolled `ask_*` copies. Gotcha: `OR_Nemotron_Nano_12B_VL` is the first `vision:true` entry; add capabilities in `registry.py`, not the DB.
- `model_health.py` — daily 04:00 MSK availability scheduler (auto-starts under Daphne/runserver unless `AI_DISABLE_HEALTH_SCHEDULER=1`); queries registry handlers. When Web DeepSeek / Web Kimi is down it auto-restarts the corresponding `Web*/` pool and re-checks once, gated by `AI_WEB_DEEPSEEK_AUTORECOVERY` / `AI_WEB_KIMI_AUTORECOVERY` (default on).
- `arm_runner.py` — async ARM runner; in-memory live job, but DB (`AIModelTestRun`/`AIModelTestResult`, `run_type` single/batch) is the source of truth (`get_arm_run_snapshot` falls back to it).
- `utils.py` — helper that calls the bot-pool API; do not grow this into a god-module, import from the owning module instead.
- Root-level non-`ai/` pieces: `WebDeepseek/` (DeepSeek bot pool, loopback `:3000`), `WebKimi/` (Kimi bot pool, loopback `:3001` — separate `KIMI_*` env namespace to avoid collisions with DeepSeek; login is manual via `seed.js`), `bot/` (LEGACY predecessor of `WebDeepseek/` — only stale `.env` + auth-debug screenshots, NOT referenced by code; safe to ignore, candidate for cleanup), `REST API.md` (docs for the external DL REST API at dl.gsu.by, not the `/ai/` app), `test_arm_solve.py` (standalone ARM-solve smoke script), `mail_bridge.py`/`backup_runner.sh`/`health_check.sh` (email→ops bridge).
- `templates/` — `ai/templates/ai/` user-facing chat/task pages, `ai/templates/admin/ai/` custom admin templates. Static JS in `static/admin/js/`: `chat_template.js` (chat only), `decide_task.js`/`find_error.js` self-contained (do NOT load `chat_template.js` there).

## Gotchas invisible from code

- `RateLimitMiddleware` already enforces the HTTP per-user limit on every `/ai/` path — do NOT also `@rate_limited` those views (double-counts).
- `_render_ai_page` self-heals model availability: when no model is available it kicks a non-blocking forced sweep via `trigger_model_health_refresh_async()`, so an expired key/balance recovers without waiting for 04:00 MSK or `check_models_health --force`.
- `sync_update_log` auto-runs in the `Dockerfile` `CMD` (`python manage.py sync_update_log || true; exec node WebDeepseek/api/index.js & exec node WebKimi/api/index.js & exec daphne … & wait -n`) and via the `post-merge`/`pre-push` git hooks (`scripts/setup_hooks.sh` from `scripts/hooks/`) on `git pull`/`git push` — the `/ai/admin/updates/` table refreshes without a manual run.
- Web DeepSeek down → the health check auto-restarts the `WebDeepseek/` pool (`POST /api/restart` → `botManager.restartAll()`, called via `ai/model_clients/web_deepseek.py::restart_bot_pool`) and re-checks once; gated by `AI_WEB_DEEPSEEK_AUTORECOVERY` (default on). Outcome annotated in `AIModelAvailability.last_message`.
- Web Kimi down → mirror autorecovery: `_maybe_autorecover_kimi` restarts the `WebKimi/` pool via `ai/model_clients/web_kimi.py::restart_kimi_bot_pool` and re-checks once; gated by `AI_WEB_KIMI_AUTORECOVERY` (default on). Kimi login is never automated (phone/WeChat/email) — the Chrome profile is seeded manually once via `WebKimi/seed.js` (visible Chromium, login once → cookie persists in the profile).
- Docker `web` healthcheck polls `:8000/health` AND both bot pools (`:3000/health`, `:3001/health` with `ready_count > 0`) — the container is "healthy" only when Daphne + both pools answer.
- `AI_HTTP_POLL_RATE_LIMIT` is a separate, higher limit for read-only background polling endpoints (`/ai/admin/arm/models/state/`, `/ai/admin/arm/find-error/status/`) so watching an ARM run / the model-status page does not eat the action budget. The default `AI_HTTP_RATE_LIMIT` still covers every other `/ai/` path.
- Model selector orders the user's top-2 (`_get_user_top_model_keys`, gated by `AIAppSettings.favorites_epoch`) first, then the rest strictly alphabetical by title — no `Web_DeepSeek` priority.
- Capability metadata (Text/Vision/Reasoning) lives in `ai/model_clients/registry.py` (`capabilities(key)`), not in the DB.
- `ai/grading.py` is the shared comparator (DRY) — deterministic difflib ratio, no LLM-as-judge.
- Use `MOSCOW_TZ` from `ai/constants.py` + `timezone.localtime()`; never hardcode `+ timedelta(hours=3)`.
- Escape any user/model-generated HTML before `innerHTML` — think-block content in particular must not be assigned unescaped.

## Canonical docs

- `README.md` — local/Docker run, prompt ACL, update log, daily checks.
- `DEPLOY.md` — prod `.env`, Redis, reverse proxy, git hooks/workflow.
- `docs/web_deepseek_bot.md` — Web DeepSeek pool internals (formerly the `WebDeepseek/README.md` content).
- `WebKimi/` — Kimi bot pool (mirror of `WebDeepseek/`): `api/server.js`/`botManager.js`/`openaiFormat.js`, `worker/bot.js` + `modules/auth.js`/`promtps.js`, `seed.js` (manual one-time login to seed the Chrome profile). No standalone md doc — read the source.
- `REST API.md` — external DL REST API at dl.gsu.by (consumed by `ai/dl_api_client.py`); NOT the `/ai/` app.
- `DOCX.md` — Russian user/admin/superuser/tester/sysadmin docs.

## Tests

```
python manage.py test ai
```

Non-obvious management commands (see `--help` for flags):

- `auto_translate` — Google/deep-translator fill of empty `*_en`/`*_fr` for Topic/Prompt/SharedPrompt (`--model`, `--overwrite`, `--lang fr`, `--dry-run`).
- `translate_prompts` — LLM-based translation of Prompt/SharedPrompt fields via a registered model handler (fills empty unless `--overwrite`).
- `reset_favorites_epoch` — sets `AIAppSettings.favorites_epoch = now` for all users; `_get_user_top_model_keys` then counts only logs newer than the epoch, so every user's list goes alphabetical until new successful requests rebuild top-2 (logs not deleted).

## Security baseline

- No `@csrf_exempt` without strong authentication (e.g. `transcribe_audio` must require auth).
- Escape think-block HTML before `innerHTML` (models emit raw `<think>` blocks).
- No `verify=False` for HTTPS in production — `SKIP_SSL_VERIFICATION` is local-dev only.
- Admin set-password flow must accept `external_user_id` only after `ExternalAuthMiddleware` validation and `_session_matches_external_id` match.
- Do not expose the `WebDeepseek/` pool (port 3000) or the `WebKimi/` pool (port 3001) to public networks — keep them on `127.0.0.1`/internal Docker network.

## Files to read when working on…

- Auth flow: `ai/middleware.py`, `ai/external_auth.py`, `ai/external_account.py`, `ai/auth_backends.py`, `ai/services/auth.py`, `ai/admin/auth.py`, `ai/admin/permissions.py`.
- Admin access control: `ai/admin/site.py`, `ai/admin/urls.py`.
- Prompts / UI language / ACL: `ai/models.py`, `ai/querysets.py`, `ai/serializers.py`, `ai/i18n.py`, `ai/services/prompt_resolver.py`, `ai/views.py` (`get_problem_data`).
- Auto-translation: `ai/services/auto_translate.py`, `ai/management/commands/auto_translate.py`.
- Chat / WebSocket: `ai/consumers.py`, `ai/routing.py`, `ai/services/` (auth/prompt_resolver/message_composer/model_caller/log_writer), `ai/model_clients/` (registry/config/history), `ai/throttling.py`.
- DL REST API: `ai/dl_api_client.py`, `ai/views.py` (`get_task_info_view`, `get_task_solution_view`).
- ARM: `ai/admin/arm.py`, `ai/arm_runner.py`, `ai/grading.py`, `ai/services/task_registry.py` (`apply_dl_task_info`).
- Prompt regression: `ai/prompt_test_runner.py`, `ai/admin/prompt_regression.py`, `ai/management/commands/run_prompt_tests.py`, `ai/grading.py`.
- Test console: `ai/admin/test_console.py`, `ai/test_console_runner.py`, `DjangoTest/test_settings.py`, `ai/tests.py`.
- Update log: `ai/models.py` (`UpdateLog`), `ai/admin/updates.py`, `ai/management/commands/sync_update_log.py`, `ai/signals.py`, `scripts/hooks/`.
- Web DeepSeek pool: `docs/web_deepseek_bot.md`, `WebDeepseek/api/server.js`, `WebDeepseek/api/botManager.js`, `WebDeepseek/worker/bot.js`, `WebDeepseek/worker/modules/promtps.js`, `WebDeepseek/worker/data.json`.
- Web Kimi pool: `ai/model_clients/web_kimi.py`, `WebKimi/api/server.js`, `WebKimi/api/botManager.js`, `WebKimi/worker/bot.js`, `WebKimi/worker/modules/auth.js`/`promtps.js`, `WebKimi/seed.js`.
- Ollama provider: `ai/model_clients/ollama.py`, `ai/model_clients/_base.py` (`make_table_handlers`), `ai/model_clients/config.py` (`OLLAMA_HOST`/`OLLAMA_API_KEY`).
- Email→ops bridge: `mail_bridge.py`, `backup_runner.sh`, `health_check.sh`.