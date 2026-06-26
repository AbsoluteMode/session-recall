# session-recall

Локальный агентный semantic-recall по истории сессий **Claude Code**. Даёт агенту
четыре инструмента (через MCP):

- `recall_search(query)` — найти прошлое обсуждение **по смыслу** (не по подстроке).
- `expand_around(session_id, uuid)` — «курсор» в детали сырого турна (tool-вызовы, выводы, thinking).
- `step(session_id, uuid, direction)` — соседний турн (дешёвый шаг курсора).
- `grep(pattern)` — подстрочный скан по сырым транскриптам on-demand.

On-demand (без проактивного авто-инжекта в v1). Локально, open source.

**Статус:** v1 собран и проверён на реальной истории. Обоснования ключевых
решений — см. [docs/decisions/](docs/decisions/).

## Как работает (кратко)

Индексируется только «поверхность» разговора — промпты пользователя и текстовые
ответы ассистента; `tool_result` / `thinking` / служебка не эмбеддятся, но
доступны через `expand_around` / `grep`. Эмбеддинги Voyage `voyage-4-large`
(dim 1024) → SQLite (`sqlite-vec` KNN + FTS5) → Voyage `rerank-2.5` → top-k.
Индексация инкрементальная (по mtime+size). Сабагентские сайдчейны
(`<session>/subagents/`) намеренно пропускаются — это «под капотом», не разговор.

## Установка / запуск

```bash
python -m venv .venv && .venv/bin/pip install -e .
export VOYAGE_API_KEY=...                      # ключ Voyage

.venv/bin/session-recall index                 # проиндексировать ~/.claude/projects
.venv/bin/session-recall search "запрос"        # семантический поиск из CLI
```

### Подключить к Claude Code (MCP)

```bash
claude mcp add session-recall --scope user -- \
  /абсолютный/путь/.venv/bin/python -m session_recall.server
```

Сервер читает `VOYAGE_API_KEY` из env; тулы (`recall_search` и др.) станут
доступны агенту в новых сессиях. Проверка: `claude mcp list` → `✔ Connected`.

## Свежесть индекса

Индексация инкрементальная (пропускает уже проиндексированное по сигнатуре файла),
поэтому держать индекс свежим дёшево. Самый прямой путь — хук Claude Code на
`SessionStart`, который запускает `session-recall index` в фоне при старте каждой
сессии. В `~/.claude/settings.json`:

```json
"hooks": {
  "SessionStart": [
    { "hooks": [ {
      "type": "command",
      "async": true,
      "command": "pgrep -f 'session-recall index' >/dev/null 2>&1 || (VOYAGE_API_KEY=... /абс/путь/.venv/bin/session-recall index >/tmp/sr-index.log 2>&1 &)"
    } ] }
  ]
}
```

`pgrep`-guard не даёт запускам наслаиваться; `( … & )` детачит, чтобы старт сессии не
ждал. Альтернатива — `launchd`/cron-таймер. (Локально на одной машине этого достаточно;
серверный индекс осмыслен только для нескольких машин — ценой приватности и сети.)

## ⚠️ Приватность — жёсткий инвариант

Это (будущий) публичный репозиторий. В него попадает **только код**.

- Данные, индексы, сырые транскрипты, эмбеддинги → `~/.local/share/session-recall/`,
  **вне дерева репозитория**. Их физически нельзя закоммитить.
- API-ключи → только через env (`VOYAGE_API_KEY`); `.gitignore` блокирует `.env`.
- Тесты → только синтетические фикстуры, ни одного реального куска сессий.
