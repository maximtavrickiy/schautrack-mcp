# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2.2,<3", "httpx>=0.28"]
# ///
"""MCP-сервер для Schautrack: учёт питания, где считает код, а не модель.

Модель только разбирает фразу пользователя на продукты и граммы и вызывает
log_food. Сервер сам находит продукт в сохранённых продуктах Schautrack,
пересчитывает КБЖУ на указанный вес и создаёт записи через REST API.

Соглашение об именах сохранённых продуктов (суффикс в скобках задаёт базу):
  "Гречка (100г)"          — значения на 100 г, логируется в граммах
  "Молоко 2.5% (100мл)"    — значения на 100 мл, логируется в мл (поле grams)
  "Яйцо (1шт=55г)"         — значения на 1 шт, можно в штуках или граммах
  "Протеиновый батончик (1шт)" — значения на 1 шт, только в штуках
  "Шаурма"                 — без суффикса: значения на порцию, только в порциях

Авторизация:
  stdio — токен Schautrack из SCHAUTRACK_TOKEN.
  http  — каждый клиент передаёт свой токен Schautrack в заголовке
          `Authorization: Bearer stk_...`, поэтому один сервер обслуживает
          нескольких пользователей, и каждый видит только свои данные.
          Если задан MCP_AUTH_TOKEN, включается однопользовательский режим:
          клиент передаёт MCP_AUTH_TOKEN, а в Schautrack идёт SCHAUTRACK_TOKEN.
"""

from __future__ import annotations

import contextvars
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

SCHAUTRACK_URL = os.environ.get("SCHAUTRACK_URL", "http://localhost:8080").rstrip("/")
SCHAUTRACK_TOKEN = os.environ.get("SCHAUTRACK_TOKEN", "")
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")
HTTP_TIMEOUT = float(os.environ.get("SCHAUTRACK_TIMEOUT", "15"))
# Schautrack хранит КБЖУ целыми числами; точные значения с этикетки держим рядом.
PRECISE_FILE = os.environ.get(
    "SCHAUTRACK_MCP_DATA", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "precise_foods.json")
)

logging.getLogger("httpx").setLevel(logging.WARNING)

MACROS = ("protein_g", "fat_g", "carbs_g", "fiber_g", "sugar_g")
ENTRY_NAME_MAX_BYTES = 120
FOOD_NAME_MAX_BYTES = 80

# ---------------------------------------------------------------------------
# HTTP-клиент Schautrack
# ---------------------------------------------------------------------------


class SchautrackError(ToolError):
    pass


_client: httpx.AsyncClient | None = None
# Токен Schautrack текущего вызова инструмента (выставляется в use_auth).
_token: contextvars.ContextVar[str] = contextvars.ContextVar("schautrack_token", default="")


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=f"{SCHAUTRACK_URL}/api/v1",
            timeout=HTTP_TIMEOUT,
            transport=httpx.AsyncHTTPTransport(retries=2),
        )
    return _client


def bearer_from_headers(headers: Any) -> str:
    if not headers:
        return ""
    value = headers.get("authorization") or headers.get("Authorization") or ""
    scheme, _, token = value.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


# True, когда сервер запущен по HTTP и токен Schautrack приходит от клиента.
HTTP_PASSTHROUGH = False


def use_auth(ctx: Context) -> None:
    """Выбирает токен Schautrack для текущего вызова инструмента."""
    if HTTP_PASSTHROUGH:
        try:
            token = bearer_from_headers(ctx.headers)
        except ValueError:
            token = ""
    else:
        token = SCHAUTRACK_TOKEN
    if not token:
        raise SchautrackError(
            "Нет токена Schautrack: передай заголовок `Authorization: Bearer stk_...` "
            "(Settings → Account → API tokens) или задай SCHAUTRACK_TOKEN."
        )
    _token.set(token)


def _problem_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}: {resp.text[:300]}"
    parts = [body.get("title") or f"HTTP {resp.status_code}"]
    if body.get("detail"):
        parts.append(body["detail"])
    for p in body.get("invalid_params") or []:
        parts.append(f"{p.get('name')}: {p.get('reason')}")
    if body.get("required_scope"):
        parts.append(f"токену не хватает scope `{body['required_scope']}`")
    return f"Schautrack {resp.status_code}: " + "; ".join(parts)


async def api(method: str, path: str, *, json: Any = None, params: dict | None = None, idempotent_post: bool = False) -> Any:
    headers = {"Authorization": f"Bearer {_token.get()}"}
    if idempotent_post:
        # Один ключ на логическую операцию: повтор после таймаута не задвоит запись.
        headers["Idempotency-Key"] = str(uuid.uuid4())
    last_exc: Exception | None = None
    for _ in range(2 if (method != "POST" or idempotent_post) else 1):
        try:
            resp = await client().request(method, path, json=json, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            last_exc = exc
            continue
        except httpx.HTTPError as exc:
            raise SchautrackError(f"Не удалось связаться со Schautrack ({SCHAUTRACK_URL}): {exc}") from exc
        if resp.status_code >= 400:
            raise SchautrackError(_problem_text(resp))
        return resp.json() if resp.content else None
    raise SchautrackError(f"Schautrack не ответил за {HTTP_TIMEOUT} с: {last_exc}")


# ---------------------------------------------------------------------------
# Продукты
# ---------------------------------------------------------------------------

Basis = Literal["100g", "100ml", "piece", "portion"]

_BASIS_RE = re.compile(
    r"\s*\(\s*(?:100\s*(?P<unit>г|гр|g|мл|ml)|1\s*(?:шт|pc|pcs)\.?(?:\s*=\s*(?P<pg>\d+(?:[.,]\d+)?)\s*(?:г|гр|g))?)\s*\)\s*$",
    re.IGNORECASE,
)


@dataclass
class Food:
    id: int
    raw_name: str
    name: str
    basis: Basis
    piece_grams: float | None
    calories: float | None
    macros: dict[str, float | None]

    def describe(self) -> dict[str, Any]:
        per = {"100g": "100 г", "100ml": "100 мл", "piece": "1 шт", "portion": "1 порция"}[self.basis]
        d: dict[str, Any] = {"id": self.id, "name": self.name, "per": per, "calories": self.calories, **self.macros}
        d = {k: (round(v, 1) if isinstance(v, float) else v) for k, v in d.items()}
        if self.piece_grams:
            d["piece_grams"] = self.piece_grams
        return d


def parse_food(raw: dict[str, Any]) -> Food:
    raw_name = raw["name"]
    m = _BASIS_RE.search(raw_name)
    piece_grams = None
    if not m:
        basis: Basis = "portion"
        name = raw_name.strip()
    else:
        name = raw_name[: m.start()].strip()
        unit = (m.group("unit") or "").lower()
        if unit in ("мл", "ml"):
            basis = "100ml"
        elif unit:
            basis = "100g"
        else:
            basis = "piece"
            if m.group("pg"):
                piece_grams = float(m.group("pg").replace(",", "."))
    macros = raw.get("macros") or {}
    return Food(
        id=raw["id"],
        raw_name=raw_name,
        name=name,
        basis=basis,
        piece_grams=piece_grams,
        calories=raw.get("calories"),
        macros={k: macros.get(k) for k in MACROS},
    )


def _norm(s: str) -> str:
    s = s.lower().replace("ё", "е")
    s = re.sub(r"[^\w%.,]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _tokens(s: str) -> list[str]:
    return [t.strip(".,") for t in _norm(s).split() if t.strip(".,")]


_user_ids: dict[str, tuple[int, float]] = {}
USER_CACHE_TTL = 300.0


def _token_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def current_user_id() -> int:
    key = _token_key(_token.get())
    cached = _user_ids.get(key)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    me = await api("GET", "/me")
    _user_ids[key] = (me["user"]["id"], time.monotonic() + USER_CACHE_TTL)
    return me["user"]["id"]


def _read_precise_file() -> dict[str, Any] | None:
    """None — файл есть, но не читается: тогда его нельзя перезаписывать."""
    try:
        with open(PRECISE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {"users": {}}
    except (OSError, ValueError) as exc:
        logging.warning("Не удалось прочитать %s: %s", PRECISE_FILE, exc)
        return None
    if "users" not in data:
        # Старый формат без разделения по пользователям: id продуктов в Schautrack
        # глобально уникальны, поэтому записи можно оставить общими.
        data = {"users": {}, "legacy": data}
    return data


async def read_precise() -> dict[str, dict[str, float | None]]:
    uid = str(await current_user_id())
    data = _read_precise_file() or {"users": {}}
    return {**data.get("legacy", {}), **data["users"].get(uid, {})}


async def save_precise(food_id: int, values: dict[str, float | None]) -> None:
    uid = str(await current_user_id())
    data = _read_precise_file()
    if data is None:
        return
    data["users"].setdefault(uid, {})[str(food_id)] = values
    data.get("legacy", {}).pop(str(food_id), None)
    _write_precise(data)


async def drop_precise(food_id: int) -> None:
    uid = str(await current_user_id())
    data = _read_precise_file()
    if data is None:
        return
    removed = data["users"].get(uid, {}).pop(str(food_id), None)
    removed_legacy = data.get("legacy", {}).pop(str(food_id), None)
    if removed is not None or removed_legacy is not None:
        _write_precise(data)


def _write_precise(data: dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(PRECISE_FILE), exist_ok=True)
        tmp = PRECISE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, PRECISE_FILE)
    except OSError as exc:
        logging.warning("Не удалось сохранить точные значения в %s: %s", PRECISE_FILE, exc)


def _apply_precise(food: Food, precise: dict[str, dict[str, float | None]]) -> Food:
    """Берём точные значения, только если они совпадают с сохранёнными в Schautrack после округления.

    Если продукт отредактировали в приложении, значения разойдутся — тогда доверяем Schautrack.
    """
    p = precise.get(str(food.id))
    if not p:
        return food
    stored = {"calories": food.calories, **food.macros}
    for key, value in stored.items():
        pv = p.get(key)
        if (pv is None) != (value is None) or (pv is not None and _round(pv) != value):
            return food
    food.calories = p.get("calories")
    food.macros = {k: p.get(k) for k in MACROS}
    return food


async def load_foods() -> list[Food]:
    data = await api("GET", "/saved-foods")
    precise = await read_precise()
    return [_apply_precise(parse_food(f), precise) for f in data["data"]]


def match_food(foods: list[Food], query: str) -> list[Food]:
    """Точное совпадение → одно; иначе все продукты, где каждое слово запроса — начало слова в названии."""
    q = query.strip()
    if re.fullmatch(r"#?\d+", q):
        fid = int(q.lstrip("#"))
        return [f for f in foods if f.id == fid]
    qn = _norm(_BASIS_RE.sub("", q))
    exact = [f for f in foods if _norm(f.name) == qn]
    if exact:
        return exact
    qt = _tokens(qn)
    if not qt:
        return []
    out = []
    for f in foods:
        ft = _tokens(f.name)
        if all(any(w.startswith(t) for w in ft) for t in qt):
            out.append(f)
    return out


NOT_FOUND_HINT = (
    "Не спрашивай пользователя и не создавай продукт: найди КБЖУ в интернете (этикетка/сайт производителя, "
    "если назван бренд, иначе USDA/Скурихин) и запиши через log_custom_entry(per=\"100g\", grams=..., approximate=true). "
    "Остальные продукты из этого списка запиши через log_food без него."
)


def resolve_food(foods: list[Food], query: str, not_found_hint: str = "") -> Food:
    found = match_food(foods, query)
    if len(found) == 1:
        return found[0]
    if not found:
        raise SchautrackError(f"Продукт «{query}» не найден в сохранённых продуктах. {not_found_hint}".strip())
    names = ", ".join(f"«{f.name}» (id {f.id})" for f in found[:10])
    raise SchautrackError(f"«{query}» подходит под несколько продуктов: {names}. Уточни у пользователя или передай id.")


def _round(x: float) -> int:
    return int(x + 0.5) if x >= 0 else -int(-x + 0.5)


def _trim_bytes(s: str, limit: int) -> str:
    b = s.encode()
    if len(b) <= limit:
        return s
    return b[: limit - 3].decode(errors="ignore").rstrip() + "…"


def _fmt_num(x: float) -> str:
    return f"{x:g}"


# ---------------------------------------------------------------------------
# Даты
# ---------------------------------------------------------------------------

_RELATIVE = {"today": 0, "сегодня": 0, "yesterday": -1, "вчера": -1, "позавчера": -2}


async def server_today() -> dt.date:
    me = await api("GET", "/me")
    return dt.date.fromisoformat(me["server"]["today"])


async def resolve_date(value: str | None) -> str | None:
    """None → сегодня (решает сервер по часовому поясу аккаунта)."""
    if value is None or value.strip() == "":
        return None
    v = value.strip().lower()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        dt.date.fromisoformat(v)
        return v
    if v in _RELATIVE:
        offset = _RELATIVE[v]
    elif re.fullmatch(r"[+-]?\d+", v):
        offset = int(v)
    else:
        raise SchautrackError(f"Непонятная дата «{value}». Формат: YYYY-MM-DD, today/yesterday или смещение в днях (-1).")
    return (await server_today() + dt.timedelta(days=offset)).isoformat()


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------

mcp = MCPServer(
    name="schautrack",
    instructions=(
        "Учёт питания, КБЖУ и веса в Schautrack. Не пересчитывай КБЖУ на вес сам — это делает сервер. "
        "Источники значений по приоритету: цифры пользователя → сохранённый продукт (log_food) → интернет. "
        "Цифры пользователя и найденные в интернете записывай через log_custom_entry (значения на 100 г + граммы). "
        "Продукты в базу (add_food) добавляй только по явной просьбе пользователя. "
        "Итоги за день бери из day_summary, а не складывай сам."
    ),
)


class MealItem(BaseModel):
    food: str = Field(description="Название сохранённого продукта (как сказал пользователь) или его id, например 'гречка' или '#12'.")
    grams: float | None = Field(default=None, gt=0, description="Вес в граммах (для жидкостей — в мл). Указывай ровно то, что сказал пользователь.")
    pieces: float | None = Field(default=None, gt=0, description="Количество штук/порций (можно дробное, 0.5). Используй, если пользователь считал в штуках.")


def _scale(food: Food, item: MealItem) -> tuple[float, str]:
    """Возвращает (множитель к значениям продукта, подпись количества)."""
    if (item.grams is None) == (item.pieces is None):
        raise SchautrackError(f"Для «{item.food}» укажи ровно одно из grams или pieces.")
    unit = "мл" if food.basis == "100ml" else "г"
    if food.basis in ("100g", "100ml"):
        if item.pieces is not None:
            raise SchautrackError(
                f"«{food.name}» хранится на 100 {unit} — передай вес в grams (сколько весит порция, спроси у пользователя)."
            )
        return item.grams / 100.0, f"{_fmt_num(item.grams)} {unit}"
    if food.basis == "piece":
        if item.pieces is not None:
            label = f"{_fmt_num(item.pieces)} шт"
            if food.piece_grams:
                label += f" (~{_fmt_num(round(item.pieces * food.piece_grams))} г)"
            return item.pieces, label
        if not food.piece_grams:
            raise SchautrackError(
                f"«{food.name}» хранится на 1 шт без веса штуки — передай pieces, либо добавь вес в название: '{food.name} (1шт=55г)'."
            )
        return item.grams / food.piece_grams, f"{_fmt_num(item.grams)} г"
    # portion
    if item.pieces is None:
        raise SchautrackError(f"«{food.name}» хранится как порция без веса — передай pieces (количество порций).")
    return item.pieces, f"{_fmt_num(item.pieces)} порц."


def _values(food: Food, k: float) -> dict[str, int | None]:
    out: dict[str, int | None] = {"calories": None if food.calories is None else _round(food.calories * k)}
    for m in MACROS:
        v = food.macros[m]
        out[m] = None if v is None else _round(v * k)
    return out


def _totals(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {key: sum((r.get(key) or 0) for r in rows) for key in ("calories", *MACROS)}


def _table(rows: list[dict[str, Any]], totals: dict[str, int], first_col: str = "Продукт") -> str:
    head = f"| {first_col} | Ккал | Б | Ж | У |\n|---|---:|---:|---:|---:|\n"
    # Незаполненное значение показываем прочерком, а не нулём, чтобы пропуск был виден.
    cell = lambda v: "—" if v is None else v  # noqa: E731
    body = "".join(
        f"| {r['label']} | {cell(r.get('calories'))} | {cell(r.get('protein_g'))} | {cell(r.get('fat_g'))} | {cell(r.get('carbs_g'))} |\n"
        for r in rows
    )
    foot = f"| **Итого** | **{totals['calories']}** | **{totals['protein_g']}** | **{totals['fat_g']}** | **{totals['carbs_g']}** |"
    return head + body + foot


def _entry_row(e: dict[str, Any]) -> dict[str, Any]:
    m = e.get("macros") or {}
    return {
        "id": e["id"],
        "date": e["date"],
        "time": e.get("local_time"),
        "label": e.get("name") or "—",
        "calories": e.get("calories"),
        **{k: m.get(k) for k in MACROS},
    }


def _stored_mismatch(sent: dict[str, Any], row: dict[str, Any]) -> list[str]:
    """Сверка отправленного с тем, что сервер реально сохранил (ккал не сверяем: auto_calc_calories может их пересчитать)."""
    out = []
    for k in MACROS:
        if k in sent and row.get(k) != sent[k]:
            out.append(f"{k}: отправлено {sent[k]}, сохранено {row.get(k)}")
    return out


KCAL_TOLERANCE = 0.15
KCAL_TOLERANCE_ABS = 15


def _kcal_from_macros(protein: float, fat: float, carbs: float) -> float:
    return 4 * protein + 9 * fat + 4 * carbs


def _kcal_warning(calories: float, protein: float, fat: float, carbs: float) -> str | None:
    expected = _kcal_from_macros(protein, fat, carbs)
    diff = abs(calories - expected)
    if diff > KCAL_TOLERANCE_ABS and diff > KCAL_TOLERANCE * max(calories, expected):
        return (
            f"Калории ({_fmt_num(round(calories, 1))}) не сходятся с БЖУ (4·Б+9·Ж+4·У = {_fmt_num(round(expected, 1))}). "
            "Проверь значения; если ошибка — delete_entry и запиши заново."
        )
    return None


@mcp.tool()
async def log_food(
    ctx: Context,
    items: list[MealItem],
    date: str | None = None,
    meal: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Записать съеденное: сервер сам находит продукты и пересчитывает КБЖУ на указанный вес.

    Для продуктов из базы. Если пользователь сам назвал КБЖУ — используй log_custom_entry.
    Передай список продуктов с граммами или штуками — ничего не считай сам. Если хоть один
    продукт не найден или неоднозначен, ничего не записывается и возвращается ошибка с подсказкой.

    Args:
        items: продукты и количество, например [{"food": "гречка", "grams": 150}, {"food": "яйцо", "pieces": 2}]
        date: день: YYYY-MM-DD, "today"/"yesterday" или смещение "-1". По умолчанию сегодня.
        meal: необязательная метка приёма пищи ("Завтрак", "Обед"), попадёт в название записи.
        dry_run: true — только посчитать и показать, ничего не записывать.
    """
    use_auth(ctx)
    if not items:
        raise SchautrackError("Список items пуст.")
    foods = await load_foods()
    planned = []
    for item in items:
        food = resolve_food(foods, item.food, NOT_FOUND_HINT)
        k, qty = _scale(food, item)
        vals = _values(food, k)
        if all(v is None for v in vals.values()):
            raise SchautrackError(f"У продукта «{food.name}» не заполнены ни калории, ни БЖУ — обнови его через update_food.")
        label = f"{food.name} {qty}"
        name = _trim_bytes(f"{meal}: {label}" if meal else label, ENTRY_NAME_MAX_BYTES)
        planned.append({"food": food, "label": label, "name": name, "vals": vals})

    day = await resolve_date(date)

    if dry_run:
        rows = [{"label": p["label"], **p["vals"]} for p in planned]
        totals = _totals(rows)
        return {"dry_run": True, "date": day or "today", "items": rows, "totals": totals, "table": _table(rows, totals)}

    created, rows, mismatches = [], [], []
    for p in planned:
        body: dict[str, Any] = {"name": p["name"], **{k: v for k, v in p["vals"].items() if v is not None}}
        if day:
            body["date"] = day
        try:
            entry = await api("POST", "/entries", json=body, idempotent_post=True)
        except SchautrackError as exc:
            done = ", ".join(f"«{r['label']}» (id {r['id']})" for r in rows) or "ничего"
            raise SchautrackError(f"{exc}. Уже записано до ошибки: {done}. Остальное НЕ записано.") from exc
        created.append(entry)
        # В таблицу — то, что реально сохранил сервер (при auto_calc_calories он пересчитывает ккал из БЖУ).
        row = {**_entry_row(entry), "label": p["label"]}
        rows.append(row)
        mismatches += [f"«{p['label']}» {m}" for m in _stored_mismatch(body, row)]
    totals = _totals(rows)
    out = {
        "logged": True,
        "date": created[0]["date"],
        "entries": rows,
        "totals": totals,
        "table": _table(rows, totals),
    }
    if mismatches:
        out["stored_mismatch"] = mismatches
    return out


@mcp.tool()
async def log_custom_entry(
    ctx: Context,
    name: str,
    protein_g: float = Field(ge=0),
    fat_g: float = Field(ge=0),
    carbs_g: float = Field(ge=0),
    per: Literal["100g", "piece", "portion"] = "100g",
    grams: float | None = Field(default=None, gt=0),
    pieces: float | None = Field(default=None, gt=0),
    piece_grams: float | None = Field(default=None, gt=0),
    calories: float | None = Field(default=None, ge=0),
    approximate: bool = False,
    meal: str | None = None,
    date: str | None = None,
) -> dict[str, Any]:
    """Записать съеденное по известным КБЖУ, без сохранения продукта в базу. Сервер сам пересчитает на вес.

    Для цифр, которые назвал пользователь, и для значений, найденных в интернете или по штрихкоду.
    Б, Ж и У обязательны: если в продукте чего-то нет — передай 0 явно.

    Args:
        name: что съедено, например "Йогурт" или "Белок яйца С0". Без веса — его добавит сервер.
        protein_g, fat_g, carbs_g: значения на `per`, как в источнике (дроби можно: 0.1).
        per: на что указаны значения: "100g" (по умолчанию; для жидкостей — 100 мл), "piece" (1 шт), "portion" (вся порция).
        grams: сколько съедено, г/мл — для per="100g".
        pieces: сколько штук — для per="piece", либо для per="100g" вместе с piece_grams.
        piece_grams: вес одной штуки в граммах (для per="100g" + pieces).
        calories: ккал на `per`. Не указано — сервер посчитает как 4·Б + 9·Ж + 4·У.
        approximate: true — значения из интернета (не от пользователя и не с этикетки); к названию добавится «~».
        meal: необязательная метка приёма пищи ("Завтрак", "Обед").
        date: YYYY-MM-DD, "today"/"yesterday" или "-1". По умолчанию сегодня.
    """
    use_auth(ctx)
    if per == "100g":
        if grams is not None and pieces is None:
            k, qty = grams / 100.0, f"{_fmt_num(grams)} г"
        elif grams is None and pieces is not None and piece_grams:
            k = pieces * piece_grams / 100.0
            qty = f"{_fmt_num(pieces)} шт (~{_fmt_num(round(pieces * piece_grams))} г)"
        else:
            raise SchautrackError('Для per="100g" передай grams либо pieces вместе с piece_grams.')
    elif per == "piece":
        if pieces is None or grams is not None:
            raise SchautrackError('Для per="piece" передай pieces (без grams).')
        k, qty = pieces, f"{_fmt_num(pieces)} шт"
    else:
        if grams is not None or pieces is not None:
            raise SchautrackError('per="portion" — значения уже на всю порцию, не передавай grams/pieces.')
        k, qty = 1.0, ""

    warning = _kcal_warning(calories, protein_g, fat_g, carbs_g) if calories is not None else None
    kcal = calories if calories is not None else _kcal_from_macros(protein_g, fat_g, carbs_g)
    body: dict[str, Any] = {
        "calories": _round(kcal * k),
        "protein_g": _round(protein_g * k),
        "fat_g": _round(fat_g * k),
        "carbs_g": _round(carbs_g * k),
    }
    label = f"{name.strip()} {qty}".strip() + (" ~" if approximate else "")
    body["name"] = _trim_bytes(f"{meal}: {label}" if meal else label, ENTRY_NAME_MAX_BYTES)
    day = await resolve_date(date)
    if day:
        body["date"] = day
    entry = await api("POST", "/entries", json=body, idempotent_post=True)
    row = {**_entry_row(entry), "label": label}
    out: dict[str, Any] = {"logged": True, "entry": row, "table": _table([row], _totals([row]))}
    if warning:
        out["warning"] = warning
    if mismatch := _stored_mismatch(body, row):
        out["stored_mismatch"] = mismatch
    return out


@mcp.tool()
async def day_summary(ctx: Context, date: str | None = None) -> dict[str, Any]:
    """Все записи о еде за день и итоги КБЖУ (посчитаны сервером), плюс дневная цель по калориям.

    Используй для отчётов и заметок в Obsidian вместо собственного сложения.

    Args:
        date: YYYY-MM-DD, "today"/"yesterday" или "-1". По умолчанию сегодня.
    """
    use_auth(ctx)
    me = await api("GET", "/me")
    day = await resolve_date(date) or me["server"]["today"]
    entries: list[dict[str, Any]] = []
    cursor = None
    while True:
        params = {"date": day, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        page = await api("GET", "/entries", params=params)
        entries.extend(page["data"])
        cursor = page.get("next_cursor")
        if not page.get("has_more") or not cursor:
            break
    entries.sort(key=lambda e: e.get("created_at") or "")
    rows = [_entry_row(e) for e in entries]
    totals = _totals(rows)
    goal = me["user"].get("daily_goal")
    table_rows = [{**r, "label": f"{r['time'] or ''} {r['label']}".strip()} for r in rows]
    return {
        "date": day,
        "entries": rows,
        "totals": totals,
        "calorie_goal": goal,
        "calories_left": None if goal is None else goal - totals["calories"],
        "table": _table(table_rows, totals, first_col="Запись") if rows else "Записей нет.",
    }


@mcp.tool()
async def period_summary(ctx: Context, date_from: str, date_to: str) -> dict[str, Any]:
    """Итоги КБЖУ по дням за период и средние значения (для недельных/месячных отчётов).

    Args:
        date_from: начало периода (YYYY-MM-DD или смещение, например "-6").
        date_to: конец периода включительно (YYYY-MM-DD, "today").
    """
    use_auth(ctx)
    start = await resolve_date(date_from) or (await server_today()).isoformat()
    end = await resolve_date(date_to) or (await server_today()).isoformat()
    if start > end:
        start, end = end, start
    by_day: dict[str, list[dict[str, Any]]] = {}
    cursor = None
    while True:
        params = {"from": start, "to": end, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        page = await api("GET", "/entries", params=params)
        for e in page["data"]:
            by_day.setdefault(e["date"], []).append(_entry_row(e))
        cursor = page.get("next_cursor")
        if not page.get("has_more") or not cursor:
            break
    days = [{"label": d, "date": d, **_totals(by_day[d])} for d in sorted(by_day)]
    n = len(days)
    avg = {k: _round(sum(d[k] for d in days) / n) for k in ("calories", *MACROS)} if n else None
    table = _table(days, _totals(days), first_col="День") if days else "Записей нет."
    if avg:
        table += f"\n| *Среднее за {n} дн.* | {avg['calories']} | {avg['protein_g']} | {avg['fat_g']} | {avg['carbs_g']} |"
    return {"from": start, "to": end, "days_with_entries": n, "days": days, "average_per_logged_day": avg, "table": table}


@mcp.tool()
async def delete_entry(ctx: Context, entry_id: int) -> dict[str, Any]:
    """Удалить запись о еде по id (id берутся из log_food или day_summary). Для исправления: удалить и записать заново."""
    use_auth(ctx)
    entry = await api("GET", f"/entries/{entry_id}")
    await api("DELETE", f"/entries/{entry_id}")
    return {"deleted": True, "entry": _entry_row(entry)}


@mcp.tool()
async def find_food(ctx: Context, query: str = "") -> dict[str, Any]:
    """Найти сохранённые продукты и их КБЖУ (на 100 г / 100 мл / 1 шт / порцию). Пустой query — весь список.

    Args:
        query: часть названия, например "творог".
    """
    use_auth(ctx)
    foods = await load_foods()
    found = match_food(foods, query) if query.strip() else foods
    return {"count": len(found), "foods": [f.describe() for f in found]}


def _food_vals(
    calories: float | None,
    protein_g: float | None,
    fat_g: float | None,
    carbs_g: float | None,
    fiber_g: float | None,
    sugar_g: float | None,
) -> dict[str, float]:
    vals = {"calories": calories, "protein_g": protein_g, "fat_g": fat_g, "carbs_g": carbs_g, "fiber_g": fiber_g, "sugar_g": sugar_g}
    for k, v in vals.items():
        if v is not None and v < 0:
            raise SchautrackError(f"{k} не может быть отрицательным.")
    return {k: v for k, v in vals.items() if v is not None}


def _food_raw_name(name: str, per: str, piece_grams: float | None) -> str:
    name = _BASIS_RE.sub("", name).strip()
    if not name:
        raise SchautrackError("Пустое название продукта.")
    if per == "100g":
        suffix = "(100г)"
    elif per == "100ml":
        suffix = "(100мл)"
    elif per == "piece":
        suffix = f"(1шт={_fmt_num(piece_grams)}г)" if piece_grams else "(1шт)"
    else:
        suffix = ""
    raw = f"{name} {suffix}".strip()
    if len(raw.encode()) > FOOD_NAME_MAX_BYTES:
        raise SchautrackError(f"Название слишком длинное (лимит Schautrack {FOOD_NAME_MAX_BYTES} байт вместе с суффиксом).")
    return raw


@mcp.tool()
async def add_food(
    ctx: Context,
    name: str,
    per: Literal["100g", "100ml", "piece", "portion"],
    calories: float,
    protein_g: float,
    fat_g: float,
    carbs_g: float,
    fiber_g: float | None = None,
    sugar_g: float | None = None,
    piece_grams: float | None = None,
    emoji: str | None = None,
) -> dict[str, Any]:
    """Сохранить продукт в базу — ТОЛЬКО по явной просьбе пользователя («добавь в базу», «сохрани продукт»).

    Для записи съеденного продукт не нужен: используй log_custom_entry. Значения — от пользователя,
    с этикетки или из lookup_barcode.

    Args:
        name: название без единиц, например "Творог 5% Простоквашино".
        per: на что указаны значения: "100g" (как на этикетке), "100ml", "piece" (1 шт), "portion" (порция).
        calories, protein_g, fat_g, carbs_g: значения на `per`.
        fiber_g, sugar_g: необязательно.
        piece_grams: вес одной штуки в граммах (для per="piece", позволяет логировать и в граммах).
        emoji: необязательный эмодзи для приложения.
    """
    use_auth(ctx)
    vals = _food_vals(calories, protein_g, fat_g, carbs_g, fiber_g, sugar_g)
    body: dict[str, Any] = {k: _round(v) for k, v in vals.items()}
    body["name"] = _food_raw_name(name, per, piece_grams)
    if emoji:
        body["emoji"] = emoji
    created = await api("POST", "/saved-foods", json=body, idempotent_post=True)
    await save_precise(created["id"], {k: vals.get(k) for k in ("calories", *MACROS)})
    return {"added": True, "food": _apply_precise(parse_food(created), await read_precise()).describe()}


@mcp.tool()
async def update_food(
    ctx: Context,
    food: str,
    calories: float | None = None,
    protein_g: float | None = None,
    fat_g: float | None = None,
    carbs_g: float | None = None,
    fiber_g: float | None = None,
    sugar_g: float | None = None,
    new_name: str | None = None,
    per: Literal["100g", "100ml", "piece", "portion"] | None = None,
    piece_grams: float | None = None,
) -> dict[str, Any]:
    """Исправить продукт в базе (значения с этикетки, название, базу или вес штуки). Меняются только переданные поля.

    Args:
        food: название или id продукта.
        new_name, per, piece_grams: изменить название / единицу / вес штуки.
        остальные: новые значения на единицу продукта.
    """
    use_auth(ctx)
    foods = await load_foods()
    f = resolve_food(foods, food)
    vals = _food_vals(calories, protein_g, fat_g, carbs_g, fiber_g, sugar_g)
    body: dict[str, Any] = {k: _round(v) for k, v in vals.items()}
    if new_name is not None or per is not None or piece_grams is not None:
        body["name"] = _food_raw_name(
            new_name or f.name,
            per or f.basis,
            piece_grams if piece_grams is not None else f.piece_grams,
        )
    if not body:
        raise SchautrackError("Нечего менять.")
    updated = await api("PATCH", f"/saved-foods/{f.id}", json=body)
    # f уже с точными значениями (если они были актуальны) — накладываем новые поверх.
    await save_precise(f.id, {**{"calories": f.calories, **f.macros}, **vals})
    return {"updated": True, "food": _apply_precise(parse_food(updated), await read_precise()).describe()}


@mcp.tool()
async def delete_food(ctx: Context, food: str) -> dict[str, Any]:
    """Удалить сохранённый продукт из базы — ТОЛЬКО по явной просьбе пользователя, после того как показал ему продукт.

    Записи о съеденном не затрагиваются: они хранят свои КБЖУ отдельно.

    Args:
        food: название или id продукта ("#12").
    """
    use_auth(ctx)
    f = resolve_food(await load_foods(), food)
    await api("DELETE", f"/saved-foods/{f.id}")
    await drop_precise(f.id)
    return {"deleted": True, "food": f.describe()}


@mcp.tool()
async def lookup_barcode(ctx: Context, code: str) -> dict[str, Any]:
    """Найти продукт по штрихкоду (EAN-8/13, UPC) в Open Food Facts. Значения на 100 г.

    Чтобы записать съеденное — передай per_100g в log_custom_entry(per="100g", grams=...).
    В базу (add_food) сохраняй только по явной просьбе пользователя.
    """
    use_auth(ctx)
    code = re.sub(r"\D", "", code)
    try:
        data = await api("GET", f"/barcode/{code}")
    except SchautrackError as exc:
        raise SchautrackError(
            f"Штрихкод {code} не найден в Open Food Facts или база недоступна ({exc}). "
            "Найди КБЖУ продукта в интернете и запиши через log_custom_entry(approximate=true)."
        ) from exc
    m = data.get("macrosPer100g") or {}
    per100 = {
        "calories": data.get("caloriesPer100g"),
        "protein_g": m.get("protein"),
        "fat_g": m.get("fat"),
        "carbs_g": m.get("carbs"),
        "fiber_g": m.get("fiber"),
        "sugar_g": m.get("sugar"),
    }
    return {
        "name": data.get("name"),
        "per_100g": per100,
        "serving_size": data.get("servingSize"),
        "serving_grams": data.get("servingQuantity"),
        "note": data.get("note")
        or "Данные Open Food Facts заполняют пользователи — сверь с этикеткой перед сохранением.",
    }


@mcp.tool()
async def log_weight(ctx: Context, weight: float, date: str | None = None, body_fat_percent: float | None = None) -> dict[str, Any]:
    """Записать вес (в единицах аккаунта, обычно кг) и, по желанию, % жира. Повторная запись за день заменяет прежнюю.

    Args:
        weight: вес, например 82.4.
        date: YYYY-MM-DD, "today"/"yesterday" или "-1". По умолчанию сегодня.
        body_fat_percent: процент жира, если пользователь его назвал.
    """
    use_auth(ctx)
    day = await resolve_date(date) or (await server_today()).isoformat()
    body: dict[str, Any] = {"weight": weight}
    if body_fat_percent is not None:
        body["body_fat"] = round(body_fat_percent, 1)
    saved = await api("PUT", f"/weight/{day}", json=body)
    return {"saved": True, "weight": saved}


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------


_token_checks: dict[str, tuple[bool, float]] = {}


async def check_schautrack_token(token: str) -> bool | None:
    """True/False — токен принят/отклонён Schautrack; None — Schautrack недоступен."""
    key = _token_key(token)
    if len(_token_checks) > 1000:
        _token_checks.clear()
    cached = _token_checks.get(key)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    try:
        resp = await client().get("/me", headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError:
        return None
    if resp.status_code >= 500:
        return None
    ok = resp.status_code == 200
    if ok:
        _user_ids[key] = (resp.json()["user"]["id"], time.monotonic() + USER_CACHE_TTL)
    # Отказ кэшируем ненадолго, чтобы перебор токенов не долбил Schautrack.
    _token_checks[key] = (ok, time.monotonic() + (USER_CACHE_TTL if ok else 30.0))
    return ok


def main() -> None:
    global HTTP_PASSTHROUGH
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    if transport == "stdio":
        if not SCHAUTRACK_TOKEN:
            sys.exit("Для stdio задайте SCHAUTRACK_TOKEN.")
        mcp.run()
        return
    if transport not in ("http", "streamable-http"):
        sys.exit(f"Неизвестный MCP_TRANSPORT={transport!r} (stdio | http)")

    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.responses import JSONResponse

    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8000"))
    if MCP_AUTH_TOKEN:
        if not SCHAUTRACK_TOKEN:
            sys.exit("MCP_AUTH_TOKEN задан (однопользовательский режим), но нет SCHAUTRACK_TOKEN.")
        logging.warning("Однопользовательский режим: все клиенты пишут в аккаунт SCHAUTRACK_TOKEN.")
    else:
        HTTP_PASSTHROUGH = True
        if SCHAUTRACK_TOKEN:
            logging.warning("SCHAUTRACK_TOKEN игнорируется: в HTTP-режиме токен передаёт каждый клиент.")

    app = mcp.streamable_http_app(
        host=host,
        # Проверку Host отключаем: в docker-сети имя хоста произвольное, доступ защищает токен.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    async def guarded(scope, receive, send):
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers") or []}
        given = bearer_from_headers(headers)
        if HTTP_PASSTHROUGH:
            ok = await check_schautrack_token(given) if given.startswith("stk_") else False
            if ok is None:
                await JSONResponse({"error": "schautrack unavailable"}, status_code=503)(scope, receive, send)
                return
        else:
            ok = hmac.compare_digest(given.encode(), MCP_AUTH_TOKEN.encode())
        if not ok:
            await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
            return
        await app(scope, receive, send)

    uvicorn.run(guarded, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
