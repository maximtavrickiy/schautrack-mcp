# schautrack-mcp

MCP-сервер для [Schautrack](https://github.com/schaurian/schautrack): агент называет продукт и граммы, а КБЖУ считает сервер.

## Инструменты

| Инструмент | Что делает |
|---|---|
| `log_food` | Записать съеденное: `[{food, grams \| pieces}]`. Находит продукт, пересчитывает на вес и создаёт записи. Если хотя бы один продукт не найден, не записывает ничего. Есть `dry_run`. |
| `log_custom_entry` | Запись с готовыми итоговыми КБЖУ (блюдо из меню) |
| `day_summary` | Записи за день, итоги, цель и остаток калорий, markdown-таблица |
| `period_summary` | Итоги по дням и средние за период |
| `delete_entry` | Удалить запись |
| `find_food` / `add_food` / `update_food` | База продуктов |
| `lookup_barcode` | Поиск по штрихкоду в Open Food Facts (через Schautrack) |
| `log_weight` | Вес и % жира |

Даты: `YYYY-MM-DD`, `today`/`yesterday`/`сегодня`/`вчера` или смещение `-1`. Какой сейчас день, определяется по часовому поясу аккаунта Schautrack.

## Как хранятся продукты

Единица измерения задаётся суффиксом в названии сохранённого продукта. `add_food` ставит его сам, а при ручном заведении продукта в приложении суффикс нужно дописать вручную:

| Название | Значения указаны на | Как записывать |
|---|---|---|
| `Гречка (100г)` | 100 г | `grams` |
| `Молоко 2.5% (100мл)` | 100 мл | `grams` (это мл) |
| `Яйцо (1шт=55г)` | 1 шт | `pieces` или `grams` |
| `Батончик (1шт)` | 1 шт | `pieces` |
| `Шаурма` (без суффикса) | порцию | `pieces` (порции) |

**Точность.** Schautrack хранит КБЖУ продукта целыми числами: 12.6 г превращается в 13, и на 150 г набегает ошибка около 1 г. Поэтому `add_food` и `update_food` дополнительно сохраняют точные значения в `precise_foods.json`, и расчёт идёт по ним. Если продукт отредактировать в самом приложении, точные значения перестанут совпадать с сохранёнными в Schautrack. Тогда сервер переключится на значения из Schautrack, пока продукт снова не обновят через `update_food`.

## Запуск

### 1. Токен Schautrack
Schautrack → Settings → Account → API tokens. Нужны scopes `entries:write`, `foods:write`, `weight:write` (права на запись включают чтение).

### 2а. Docker (рекомендуется, HTTP)

Образ собирается GitHub Actions при каждом пуше в `main` (тег `latest`) и при git-тегах `vX.Y.Z` (теги `X.Y.Z` и `X.Y`) для `linux/amd64` и `linux/arm64`:
`ghcr.io/maximtavrickiy/schautrack-mcp`.

```bash
cp .env.example .env   # заполнить SCHAUTRACK_URL, SCHAUTRACK_TOKEN, MCP_AUTH_TOKEN
docker compose up -d
```

В свой compose-файл достаточно добавить сервис с `image: ghcr.io/maximtavrickiy/schautrack-mcp:latest` и теми же `env_file`, `ports` и `volumes`, что в `compose.yml`. Для локальной сборки раскомментируй `build: .`.

Если Schautrack запущен через свой `compose.yml` (проект `schautrack`), раскомментируй в `compose.yml` блоки `networks` и укажи `SCHAUTRACK_URL=http://web:3000`. Иначе укажи адрес, по которому Schautrack доступен из контейнера.

Подключение в picoclaw:

```bash
picoclaw mcp add -t http -H "Authorization: Bearer <MCP_AUTH_TOKEN>" schautrack http://127.0.0.1:8765/mcp
picoclaw mcp test schautrack
```

Если picoclaw тоже в Docker, подключи оба контейнера к одной сети и используй адрес `http://schautrack-mcp:8000/mcp`.

### 2б. stdio (без Docker, нужен Python 3.11+ и uv на хосте picoclaw)

```bash
picoclaw mcp add schautrack \
  -e SCHAUTRACK_URL=http://127.0.0.1:8080 \
  -e SCHAUTRACK_TOKEN=stk_... \
  -- uv run /opt/schautrack-mcp/schautrack_mcp.py
```

Зависимости указаны в заголовке скрипта, `uv` поставит их сам. Точные значения продуктов лягут в `data/` рядом со скриптом (путь меняется переменной `SCHAUTRACK_MCP_DATA`).

### 3. Скилл
Скопируй `skill/nutrition/` в `<workspace picoclaw>/skills/nutrition/`. Скилл запрещает модели считать КБЖУ самой.

## Переменные окружения

| Переменная | По умолчанию | |
|---|---|---|
| `SCHAUTRACK_URL` | `http://localhost:8080` | адрес Schautrack |
| `SCHAUTRACK_TOKEN` | — | API-токен |
| `MCP_TRANSPORT` | `stdio` (`http` в Docker) | `stdio` или `http` |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `8000` | для HTTP |
| `MCP_AUTH_TOKEN` | — | Bearer-токен для HTTP; обязателен, если сервер слушает не только localhost |
| `SCHAUTRACK_MCP_DATA` | `./data/precise_foods.json` (`/data/...` в Docker) | файл точных значений |
| `SCHAUTRACK_TIMEOUT` | `15` | таймаут запросов, секунды |
