# pip install "aiogram>=3.7,<4" aiohttp aiosqlite
"""Telegram-бот «Хранилище».

Один процесс: long-polling aiogram + HTTP-сервер Mini App.
Файлы сохраняются в репозиторий GitHub (папка GITHUB_FOLDER)
и/или в Telegram — в зависимости от конфигурации.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import time
import urllib.parse
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, BinaryIO

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiohttp import web

# --------------------------------------------------------------------------- #
# Конфигурация
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent
INDEX_PATH = BASE_DIR / "index.html"

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").strip()
BOT_API_URL = (
    os.environ.get("BOT_API_URL", "https://api.telegram.org").strip().rstrip("/")
    or "https://api.telegram.org"
)
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "storage.db"))

# GitHub — необязательный слой хранения.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.environ.get("GITHUB_REPO", "").strip()          # "user/repo"
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main").strip() or "main"
GITHUB_FOLDER = os.environ.get("GITHUB_FOLDER", "files").strip().strip("/") or "files"
GITHUB_API = os.environ.get("GITHUB_API", "https://api.github.com").strip().rstrip("/")
GITHUB_ENABLED = bool(GITHUB_TOKEN and GITHUB_REPO)

# Необязательный список разрешённых origin'ов через запятую.
_extra_origins_raw = os.environ.get("WEBAPP_ORIGINS", "").strip()
EXTRA_ORIGINS = {o.strip().rstrip("/") for o in _extra_origins_raw.split(",") if o.strip()}

INIT_DATA_TTL = 24 * 60 * 60
RATE_LIMIT = 240
RATE_WINDOW = 60.0
MAX_IDS_PER_REQUEST = 500
MAX_UPLOAD_BYTES = 2 * 1024 ** 3
MAX_GITHUB_BYTES = 45 * 1024 ** 2
ZIP_MEMORY_LIMIT = 100 * 1024 ** 2
ZIP_MAX_TOTAL = 512 * 1024 ** 3
TICKET_TTL = 15 * 60
CHUNK_SIZE = 256 * 1024

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("storage")

if GITHUB_ENABLED:
    log.info("GitHub storage включён: %s (folder=%s, branch=%s)",
             GITHUB_REPO, GITHUB_FOLDER, GITHUB_BRANCH)
else:
    log.info("GitHub storage отключён, файлы сохраняются только в Telegram")

# --------------------------------------------------------------------------- #
# Утилиты
# --------------------------------------------------------------------------- #


def human_size(num: int | None) -> str:
    value = float(num or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            if unit == "Б":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ТБ"


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def placeholders(count: int) -> str:
    return ",".join("?" * max(count, 1))


def sanitize_filename(name: str) -> str:
    cleaned = "".join(ch for ch in name if ch not in "\x00\r\n").strip()
    cleaned = cleaned.replace("/", "_").replace("\\", "_")
    if not cleaned:
        cleaned = "file"
    return cleaned[:180]


def unique_name(name: str, used: set[str]) -> str:
    candidate = name or "file"
    if candidate not in used:
        used.add(candidate)
        return candidate
    stem, dot, ext = candidate.rpartition(".")
    base = stem if dot else candidate
    suffix = f".{ext}" if dot else ""
    index = 1
    while True:
        candidate = f"{base} ({index}){suffix}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        index += 1


def build_file_url(file_path: str) -> str:
    path = file_path if file_path.startswith("/") else "/" + file_path
    return f"{BOT_API_URL}/file/bot{BOT_TOKEN}{path}"


def content_disposition(filename: str) -> str:
    quoted = urllib.parse.quote(filename)
    return f"attachment; filename=\"{quoted}\"; filename*=UTF-8''{quoted}"


def absolute_url(request: web.Request, path: str) -> str:
    return f"{request.url.origin()}{path}"


def public_base() -> str:
    parsed = urllib.parse.urlparse(WEBAPP_URL)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return WEBAPP_URL.rstrip("/")


def parse_ids(raw: Any, limit: int = MAX_IDS_PER_REQUEST) -> list[int]:
    if not isinstance(raw, list):
        return []
    result: list[int] = []
    for item in raw[:limit]:
        if isinstance(item, int) and item > 0:
            result.append(item)
    return result


def normalize_origin(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return value.rstrip("/")


ALLOWED_ORIGINS: set[str] = set()
if WEBAPP_URL:
    ALLOWED_ORIGINS.add(normalize_origin(WEBAPP_URL))
ALLOWED_ORIGINS.update(EXTRA_ORIGINS)
# Локальный запуск фронта для отладки.
ALLOWED_ORIGINS.update({"http://localhost:8080", "http://127.0.0.1:8080"})


# --------------------------------------------------------------------------- #
# initData
# --------------------------------------------------------------------------- #


class AuthError(Exception):
    pass


def parse_init_data(init_data: str, max_age: int = INIT_DATA_TTL) -> dict[str, Any]:
    if not init_data:
        raise AuthError("initData отсутствует")
    pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
    data = dict(pairs)
    received_hash = data.pop("hash", "")
    if not received_hash:
        raise AuthError("hash отсутствует")

    check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise AuthError("подпись не совпадает")

    try:
        auth_date = int(data.get("auth_date", "0"))
    except ValueError as exc:
        raise AuthError("auth_date повреждён") from exc
    if auth_date <= 0 or time.time() - auth_date > max_age:
        raise AuthError("initData устарел")

    user_raw = data.get("user")
    if not user_raw:
        raise AuthError("user отсутствует")
    try:
        user = json.loads(user_raw)
        user_id = int(user["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise AuthError("user повреждён") from exc

    return {"user_id": user_id, "user": user, "auth_date": auth_date}


class RateLimiter:
    def __init__(self, limit: int, window: float) -> None:
        self._limit = limit
        self._window = window
        self._hits: dict[int, list[float]] = {}

    def allow(self, user_id: int) -> bool:
        now = time.monotonic()
        hits = self._hits.setdefault(user_id, [])
        cutoff = now - self._window
        while hits and hits[0] < cutoff:
            hits.pop(0)
        if len(hits) >= self._limit:
            return False
        hits.append(now)
        if len(self._hits) > 10_000:
            for key in [k for k, v in self._hits.items() if not v or v[-1] < cutoff]:
                self._hits.pop(key, None)
        return True


rate_limiter = RateLimiter(RATE_LIMIT, RATE_WINDOW)

# --------------------------------------------------------------------------- #
# SQLite
# --------------------------------------------------------------------------- #

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS folders (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL,
        name       TEXT    NOT NULL,
        created_at INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_folders_user ON folders(user_id)",
    """
    CREATE TABLE IF NOT EXISTS files (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id        INTEGER NOT NULL,
        file_id        TEXT    NOT NULL DEFAULT '',
        file_unique_id TEXT    NOT NULL,
        name           TEXT    NOT NULL,
        size           INTEGER NOT NULL DEFAULT 0,
        mime           TEXT    NOT NULL DEFAULT '',
        folder_id      INTEGER,
        uploaded_at    INTEGER NOT NULL,
        github_path    TEXT,
        FOREIGN KEY (folder_id) REFERENCES folders(id) ON DELETE SET NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_files_user ON files(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder_id)",
    "CREATE INDEX IF NOT EXISTS idx_files_user_folder ON files(user_id, folder_id)",
)


class Database:
    def __init__(self, path: str, pool_size: int = 5) -> None:
        self._path = path
        self._pool_size = pool_size
        self._pool: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue()
        self._connections: list[aiosqlite.Connection] = []

    async def start(self) -> None:
        for _ in range(self._pool_size):
            conn = await aiosqlite.connect(self._path)
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.commit()
            self._connections.append(conn)
            self._pool.put_nowait(conn)
        await self._migrate()
        log.info("SQLite готова: %s (соединений: %d)", self._path, self._pool_size)

    async def _migrate(self) -> None:
        async with self.connection() as conn:
            for statement in SCHEMA:
                await conn.execute(statement)
            async with conn.execute("PRAGMA table_info(files)") as cursor:
                columns = {row[1] for row in await cursor.fetchall()}
            if "github_path" not in columns:
                await conn.execute("ALTER TABLE files ADD COLUMN github_path TEXT")
                log.info("Миграция: добавлена колонка github_path")
            await conn.commit()

    async def close(self) -> None:
        for conn in self._connections:
            with contextlib.suppress(Exception):
                await conn.close()
        self._connections.clear()

    @contextlib.asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        conn = await self._pool.get()
        try:
            yield conn
        finally:
            self._pool.put_nowait(conn)

    async def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        async with self.connection() as conn:
            async with conn.execute(sql, params) as cursor:
                return list(await cursor.fetchall())

    async def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        async with self.connection() as conn:
            async with conn.execute(sql, params) as cursor:
                return await cursor.fetchone()

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        async with self.connection() as conn:
            cursor = await conn.execute(sql, params)
            await conn.commit()
            lastrowid = cursor.lastrowid or 0
            await cursor.close()
            return int(lastrowid)


SORT_COLUMNS = {
    "name": "fi.name COLLATE NOCASE",
    "size": "fi.size",
    "date": "fi.uploaded_at",
}


# --------------------------------------------------------------------------- #
# GitHub
# --------------------------------------------------------------------------- #


class GitHubError(Exception):
    pass


class GitHubStorage:
    """Тонкая обёртка над GitHub Contents API.

    Использует личный access token. Для приватных репозиториев достаточно
    scope `repo`, для публичных — `public_repo`.
    """

    def __init__(
        self,
        http: aiohttp.ClientSession,
        token: str,
        repo: str,
        branch: str,
        folder: str,
        api_base: str,
    ) -> None:
        self._http = http
        self._token = token
        self._repo = repo
        self._branch = branch
        self._folder = folder
        self._api = api_base

    @property
    def repo(self) -> str:
        return self._repo

    @property
    def folder(self) -> str:
        return self._folder

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "storage-bot/1.0",
        }

    def _contents_url(self, path: str) -> str:
        encoded = urllib.parse.quote(path, safe="/")
        return f"{self._api}/repos/{self._repo}/contents/{encoded}"

    def build_path(self, user_id: int, unique_id: str, name: str) -> str:
        safe = sanitize_filename(name)
        if len(safe) > 150:
            stem, dot, ext = safe.rpartition(".")
            if dot:
                keep = 150 - len(ext) - 1
                safe = (stem[:keep] if keep > 0 else stem[:140]) + "." + ext
            else:
                safe = safe[:150]
        return f"{self._folder}/{user_id}/{unique_id}_{safe}"

    async def get_sha(self, path: str) -> str | None:
        url = self._contents_url(path)
        params = {"ref": self._branch}
        async with self._http.get(url, headers=self._headers(), params=params) as resp:
            if resp.status == 404:
                return None
            if resp.status != 200:
                text = await resp.text()
                raise GitHubError(f"get_sha {resp.status}: {text[:180]}")
            data = await resp.json()
            return data.get("sha")

    async def upload(self, path: str, content: bytes) -> str:
        url = self._contents_url(path)
        sha = await self.get_sha(path)
        payload: dict[str, Any] = {
            "message": f"storage: add {path}",
            "content": base64.b64encode(content).decode("ascii"),
            "branch": self._branch,
        }
        if sha:
            payload["sha"] = sha
        async with self._http.put(url, headers=self._headers(), json=payload) as resp:
            if resp.status not in (200, 201):
                text = await resp.text()
                raise GitHubError(f"upload {resp.status}: {text[:180]}")
            data = await resp.json()
            return data.get("content", {}).get("path", path)

    async def delete(self, path: str) -> bool:
        sha = await self.get_sha(path)
        if not sha:
            return False
        url = self._contents_url(path)
        payload = {
            "message": f"storage: delete {path}",
            "sha": sha,
            "branch": self._branch,
        }
        async with self._http.request(
            "DELETE", url, headers=self._headers(), json=payload
        ) as resp:
            if resp.status not in (200, 201):
                text = await resp.text()
                log.warning("github delete %s -> %s %s", path, resp.status, text[:120])
                return False
            return True

    def download(self, path: str) -> Any:
        """Возвращает async context manager с сырым stream файла."""
        url = self._contents_url(path)
        params = {"ref": self._branch}
        return self._http.get(
            url, headers=self._headers(accept="application/vnd.github.raw"), params=params
        )


# --------------------------------------------------------------------------- #
# Доменный слой
# --------------------------------------------------------------------------- #


class Storage:
    def __init__(self, db: Database, github: GitHubStorage | None = None) -> None:
        self._db = db
        self._github = github

    # --- папки ----------------------------------------------------------- #

    async def list_folders(self, user_id: int) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            "SELECT fo.id, fo.name, "
            "       COUNT(fi.id) AS files, COALESCE(SUM(fi.size), 0) AS size "
            "FROM folders fo "
            "LEFT JOIN files fi ON fi.folder_id = fo.id AND fi.user_id = fo.user_id "
            "WHERE fo.user_id = ? "
            "GROUP BY fo.id, fo.name "
            "ORDER BY fo.name COLLATE NOCASE",
            (user_id,),
        )
        return [dict(row) for row in rows]

    async def create_folder(self, user_id: int, name: str) -> int:
        return await self._db.execute(
            "INSERT INTO folders (user_id, name, created_at) VALUES (?, ?, ?)",
            (user_id, name, int(time.time())),
        )

    async def folder_owned(self, user_id: int, folder_id: int) -> bool:
        row = await self._db.fetch_one(
            "SELECT 1 FROM folders WHERE id = ? AND user_id = ?",
            (folder_id, user_id),
        )
        return row is not None

    async def delete_folder(self, user_id: int, folder_id: int) -> bool:
        if not await self.folder_owned(user_id, folder_id):
            return False
        await self._db.execute(
            "UPDATE files SET folder_id = NULL WHERE user_id = ? AND folder_id = ?",
            (user_id, folder_id),
        )
        await self._db.execute(
            "DELETE FROM folders WHERE id = ? AND user_id = ?",
            (folder_id, user_id),
        )
        return True

    # --- файлы ----------------------------------------------------------- #

    async def list_files(
        self,
        user_id: int,
        folder_id: int | None,
        query: str = "",
        sort: str = "date",
        order: str = "desc",
    ) -> list[dict[str, Any]]:
        column = SORT_COLUMNS.get(sort, SORT_COLUMNS["date"])
        direction = "ASC" if order == "asc" else "DESC"

        sql = (
            "SELECT fi.id, fi.name, fi.size, fi.mime, fi.folder_id, fi.uploaded_at, "
            "       fi.github_path, fo.name AS folder_name "
            "FROM files fi "
            "LEFT JOIN folders fo ON fo.id = fi.folder_id AND fo.user_id = fi.user_id "
            "WHERE fi.user_id = ?"
        )
        params: list[Any] = [user_id]

        if query:
            sql += " AND fi.name LIKE ? ESCAPE '\\'"
            params.append(f"%{escape_like(query)}%")
        elif folder_id is None:
            sql += " AND fi.folder_id IS NULL"
        else:
            sql += " AND fi.folder_id = ?"
            params.append(folder_id)

        sql += f" ORDER BY {column} {direction}, fi.id DESC LIMIT 1000"
        rows = await self._db.fetch_all(sql, tuple(params))
        return [dict(row) for row in rows]

    async def get_file(self, user_id: int, file_row_id: int) -> dict[str, Any] | None:
        row = await self._db.fetch_one(
            "SELECT id, file_id, name, size, mime, folder_id, uploaded_at, github_path "
            "FROM files WHERE id = ? AND user_id = ?",
            (file_row_id, user_id),
        )
        return dict(row) if row else None

    async def get_github_paths(self, user_id: int, ids: list[int]) -> list[str]:
        if not ids:
            return []
        sql = (
            "SELECT github_path FROM files "
            f"WHERE user_id = ? AND github_path IS NOT NULL AND id IN ({placeholders(len(ids))})"
        )
        rows = await self._db.fetch_all(sql, (user_id, *ids))
        return [row["github_path"] for row in rows if row["github_path"]]

    async def add_file(
        self,
        *,
        user_id: int,
        file_id: str,
        file_unique_id: str,
        name: str,
        size: int,
        mime: str,
        folder_id: int | None,
        uploaded_at: int,
        github_path: str | None = None,
    ) -> int:
        return await self._db.execute(
            "INSERT INTO files "
            "(user_id, file_id, file_unique_id, name, size, mime, folder_id, uploaded_at, github_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, file_id, file_unique_id, name, size, mime,
             folder_id, uploaded_at, github_path),
        )

    async def delete_files(self, user_id: int, ids: list[int]) -> int:
        if not ids:
            return 0
        sql = f"DELETE FROM files WHERE user_id = ? AND id IN ({placeholders(len(ids))})"
        async with self._db.connection() as conn:
            cursor = await conn.execute(sql, (user_id, *ids))
            await conn.commit()
            count = cursor.rowcount or 0
            await cursor.close()
        return int(count)

    async def move_files(self, user_id: int, ids: list[int], folder_id: int | None) -> int:
        if not ids:
            return 0
        sql = (
            "UPDATE files SET folder_id = ? "
            f"WHERE user_id = ? AND id IN ({placeholders(len(ids))})"
        )
        async with self._db.connection() as conn:
            cursor = await conn.execute(sql, (folder_id, user_id, *ids))
            await conn.commit()
            count = cursor.rowcount or 0
            await cursor.close()
        return int(count)

    async def clear_files(self, user_id: int) -> tuple[int, list[str]]:
        paths = await self.get_github_paths(
            user_id,
            [row["id"] for row in await self._db.fetch_all(
                "SELECT id FROM files WHERE user_id = ?", (user_id,)
            )],
        )
        async with self._db.connection() as conn:
            cursor = await conn.execute("DELETE FROM files WHERE user_id = ?", (user_id,))
            await conn.commit()
            count = cursor.rowcount or 0
            await cursor.close()
        return int(count), paths

    async def stats(self, user_id: int) -> dict[str, int]:
        files_row = await self._db.fetch_one(
            "SELECT COUNT(*) AS files, COALESCE(SUM(size), 0) AS size "
            "FROM files WHERE user_id = ?",
            (user_id,),
        )
        folders_row = await self._db.fetch_one(
            "SELECT COUNT(*) AS folders FROM folders WHERE user_id = ?",
            (user_id,),
        )
        return {
            "files": int(files_row["files"]) if files_row else 0,
            "size": int(files_row["size"]) if files_row else 0,
            "folders": int(folders_row["folders"]) if folders_row else 0,
        }


async def stats_text(storage: Storage, user_id: int) -> str:
    data = await storage.stats(user_id)
    return (
        f"Файлов: {data['files']}\n"
        f"Объём: {human_size(data['size'])}\n"
        f"Папок: {data['folders']}"
    )


# --------------------------------------------------------------------------- #
# Временные ссылки
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Ticket:
    name: str
    mime: str
    expires_at: float
    size: int = 0
    file_path: str | None = None
    github_path: str | None = None
    stream: BinaryIO | None = None


TICKETS: dict[str, Ticket] = {}


def issue_ticket(ticket: Ticket) -> str:
    token = secrets.token_urlsafe(24)
    TICKETS[token] = ticket
    return token


async def tickets_cleanup_loop() -> None:
    while True:
        await asyncio.sleep(60)
        now = time.time()
        expired = [token for token, ticket in TICKETS.items() if ticket.expires_at <= now]
        for token in expired:
            ticket = TICKETS.pop(token, None)
            if ticket and ticket.stream is not None:
                with contextlib.suppress(Exception):
                    ticket.stream.close()


# --------------------------------------------------------------------------- #
# HTTP middleware
# --------------------------------------------------------------------------- #


@web.middleware
async def cors_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    origin = request.headers.get("Origin", "")
    is_allowed = bool(origin) and origin in ALLOWED_ORIGINS

    if request.method == "OPTIONS":
        response: web.StreamResponse = web.Response(status=204)
    else:
        response = await handler(request)

    if is_allowed:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Headers"] = "X-Init-Data, Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        response.headers["Access-Control-Max-Age"] = "86400"
    return response


@web.middleware
async def auth_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    if not request.path.startswith("/api/"):
        return await handler(request)

    try:
        payload = parse_init_data(request.headers.get("X-Init-Data", ""))
    except AuthError as exc:
        return web.json_response(
            {"error": "unauthorized", "message": str(exc)}, status=401
        )

    user_id = int(payload["user_id"])
    if not rate_limiter.allow(user_id):
        return web.json_response(
            {"error": "rate_limited", "message": "Слишком много запросов"}, status=429
        )

    request["user_id"] = user_id
    return await handler(request)


async def read_json(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def bad_request(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": "bad_request", "message": message}, status=status)


# --------------------------------------------------------------------------- #
# HTTP handlers
# --------------------------------------------------------------------------- #


async def handle_health(request: web.Request) -> web.StreamResponse:
    return web.json_response({
        "ok": True,
        "github_enabled": GITHUB_ENABLED,
        "github_repo": GITHUB_REPO if GITHUB_ENABLED else None,
    })


async def handle_index(request: web.Request) -> web.StreamResponse:
    """Локальная отдача HTML — удобно, если фронт хочется держать на том же хосте."""
    try:
        body = INDEX_PATH.read_bytes()
    except FileNotFoundError:
        raise web.HTTPNotFound(text="index.html не найден рядом с bot.py")
    return web.Response(
        body=body,
        content_type="text/html",
        charset="utf-8",
        headers={"Cache-Control": "no-store"},
    )


async def handle_folders_list(request: web.Request) -> web.StreamResponse:
    storage: Storage = request.app["storage"]
    folders = await storage.list_folders(request["user_id"])
    return web.json_response({"folders": folders})


async def handle_folders_create(request: web.Request) -> web.StreamResponse:
    storage: Storage = request.app["storage"]
    body = await read_json(request)
    name = str(body.get("name", "")).strip()
    if not name:
        return bad_request("Название папки не может быть пустым")
    if len(name) > 64:
        return bad_request("Название длиннее 64 символов")
    folder_id = await storage.create_folder(request["user_id"], name)
    return web.json_response({"id": folder_id, "name": name}, status=201)


async def handle_folder_delete(request: web.Request) -> web.StreamResponse:
    storage: Storage = request.app["storage"]
    folder_id = int(request.match_info["folder_id"])
    deleted = await storage.delete_folder(request["user_id"], folder_id)
    if not deleted:
        return bad_request("Папка не найдена", status=404)
    return web.json_response({"ok": True})


async def handle_files_list(request: web.Request) -> web.StreamResponse:
    storage: Storage = request.app["storage"]
    raw_folder = request.query.get("folder_id", "")
    folder_id = int(raw_folder) if raw_folder.isdigit() else None
    query = (request.query.get("q") or "").strip()[:128]
    sort = request.query.get("sort", "date")
    order = request.query.get("order", "desc")

    files = await storage.list_files(request["user_id"], folder_id, query, sort, order)
    return web.json_response({"files": files})


async def handle_files_delete(request: web.Request) -> web.StreamResponse:
    storage: Storage = request.app["storage"]
    github: GitHubStorage | None = request.app.get("github")
    body = await read_json(request)
    ids = parse_ids(body.get("ids"))
    if not ids:
        return bad_request("Не переданы идентификаторы файлов")

    paths = await storage.get_github_paths(request["user_id"], ids)
    deleted = await storage.delete_files(request["user_id"], ids)

    if github and paths:
        for path in paths:
            asyncio.create_task(_github_delete_bg(github, path))

    return web.json_response({"deleted": deleted})


async def _github_delete_bg(github: GitHubStorage, path: str) -> None:
    try:
        await github.delete(path)
    except Exception:
        log.exception("github delete failed: %s", path)


async def handle_files_move(request: web.Request) -> web.StreamResponse:
    storage: Storage = request.app["storage"]
    body = await read_json(request)
    ids = parse_ids(body.get("ids"))
    if not ids:
        return bad_request("Не переданы идентификаторы файлов")

    raw_folder = body.get("folder_id")
    folder_id = raw_folder if isinstance(raw_folder, int) and raw_folder > 0 else None
    if folder_id is not None and not await storage.folder_owned(request["user_id"], folder_id):
        return bad_request("Папка не найдена", status=404)

    moved = await storage.move_files(request["user_id"], ids, folder_id)
    return web.json_response({"moved": moved})


async def handle_file_link(request: web.Request) -> web.StreamResponse:
    storage: Storage = request.app["storage"]
    bot: Bot = request.app["bot"]
    file_row_id = int(request.match_info["file_id"])

    row = await storage.get_file(request["user_id"], file_row_id)
    if row is None:
        return bad_request("Файл не найден", status=404)

    github_path = row.get("github_path")
    file_id = row.get("file_id") or ""

    if github_path:
        token = issue_ticket(
            Ticket(
                name=row["name"],
                mime=row["mime"] or "application/octet-stream",
                expires_at=time.time() + TICKET_TTL,
                size=int(row["size"] or 0),
                github_path=github_path,
            )
        )
        return web.json_response({"url": absolute_url(request, f"/dl/{token}")})

    if not file_id:
        return bad_request("У файла нет источника", status=500)

    try:
        tg_file = await bot.get_file(file_id)
    except Exception:
        log.exception("getFile failed for row=%s", file_row_id)
        return bad_request("Telegram не отдал файл, попробуйте позже", status=502)

    if not tg_file.file_path:
        return bad_request("У файла нет пути на сервере Telegram", status=502)

    token = issue_ticket(
        Ticket(
            name=row["name"],
            mime=row["mime"] or "application/octet-stream",
            expires_at=time.time() + TICKET_TTL,
            size=int(row["size"] or 0),
            file_path=tg_file.file_path,
        )
    )
    return web.json_response({"url": absolute_url(request, f"/dl/{token}")})


async def _download_from_telegram(bot: Bot, http: aiohttp.ClientSession, file_id: str) -> bytes:
    tg_file = await bot.get_file(file_id)
    if not tg_file.file_path:
        raise RuntimeError("Файл недоступен в Telegram")
    url = build_file_url(tg_file.file_path)
    async with http.get(url) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Telegram вернул {resp.status}")
        return await resp.read()


async def build_zip(
    bot: Bot,
    http: aiohttp.ClientSession,
    github: GitHubStorage | None,
    files: list[dict[str, Any]],
) -> BinaryIO:
    stream = tempfile.SpooledTemporaryFile(max_size=ZIP_MEMORY_LIMIT, mode="w+b", suffix=".zip")
    used: set[str] = set()

    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for item in files:
            entry_name = unique_name(sanitize_filename(item["name"]), used)
            try:
                if item.get("github_path") and github:
                    async with github.download(item["github_path"]) as resp:
                        if resp.status != 200:
                            log.warning("zip: github %s -> %s", item["github_path"], resp.status)
                            continue
                        with archive.open(entry_name, "w") as entry:
                            async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                                entry.write(chunk)
                    continue

                file_id = item.get("file_id") or ""
                if not file_id:
                    continue

                tg_file = await bot.get_file(file_id)
                if not tg_file.file_path:
                    continue
                async with http.get(build_file_url(tg_file.file_path)) as resp:
                    if resp.status != 200:
                        continue
                    with archive.open(entry_name, "w") as entry:
                        async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                            entry.write(chunk)
            except Exception:
                log.exception("zip: skip %s", item.get("name"))

    stream.seek(0)
    return stream


async def handle_zip(request: web.Request) -> web.StreamResponse:
    storage: Storage = request.app["storage"]
    bot: Bot = request.app["bot"]
    http: aiohttp.ClientSession = request.app["http"]
    github: GitHubStorage | None = request.app.get("github")

    body = await read_json(request)
    raw_folder = body.get("folder_id")
    folder_id = raw_folder if isinstance(raw_folder, int) and raw_folder > 0 else None

    files = await storage.list_files(request["user_id"], folder_id, "", "name", "asc")
    if not files:
        return bad_request("Нет файлов для архивации")

    total = sum(int(item["size"] or 0) for item in files)
    if total > ZIP_MAX_TOTAL:
        return bad_request("Слишком большой объём для одного архива", status=413)

    try:
        stream = await build_zip(bot, http, github, files)
    except Exception:
        log.exception("zip build failed")
        return bad_request("Не удалось собрать архив", status=500)

    folder_label = "storage"
    if folder_id is not None:
        for folder in await storage.list_folders(request["user_id"]):
            if folder["id"] == folder_id:
                folder_label = folder["name"]
                break
    archive_name = f"{sanitize_filename(folder_label)}.zip"

    token = issue_ticket(
        Ticket(
            name=archive_name,
            mime="application/zip",
            expires_at=time.time() + TICKET_TTL,
            size=total,
            stream=stream,
        )
    )
    return web.json_response({"url": absolute_url(request, f"/dl/{token}")})


async def handle_download(request: web.Request) -> web.StreamResponse:
    token = request.match_info["token"]
    ticket = TICKETS.pop(token, None)
    if ticket is None or ticket.expires_at < time.time():
        raise web.HTTPNotFound(text="Ссылка устарела")

    disposition = content_disposition(ticket.name)

    # 1. GitHub
    if ticket.github_path:
        github: GitHubStorage | None = request.app.get("github")
        if github is None:
            raise web.HTTPBadGateway(text="GitHub отключён")
        async with github.download(ticket.github_path) as upstream:
            if upstream.status != 200:
                log.warning("github download %s -> %s", ticket.github_path, upstream.status)
                raise web.HTTPBadGateway(text="GitHub недоступен")
            response = web.StreamResponse(
                status=200,
                headers={
                    "Content-Type": upstream.headers.get("Content-Type", ticket.mime),
                    "Content-Disposition": disposition,
                    "Cache-Control": "no-store",
                },
            )
            await response.prepare(request)
            async for chunk in upstream.content.iter_chunked(CHUNK_SIZE):
                await response.write(chunk)
            await response.write_eof()
            return response

    # 2. Telegram
    if ticket.file_path is not None:
        http: aiohttp.ClientSession = request.app["http"]
        async with http.get(build_file_url(ticket.file_path)) as upstream:
            if upstream.status != 200:
                log.warning("download upstream status=%s", upstream.status)
                raise web.HTTPBadGateway(text="Telegram недоступен")
            response = web.StreamResponse(
                status=200,
                headers={
                    "Content-Type": upstream.headers.get("Content-Type", ticket.mime),
                    "Content-Disposition": disposition,
                    "Cache-Control": "no-store",
                },
            )
            await response.prepare(request)
            async for chunk in upstream.content.iter_chunked(CHUNK_SIZE):
                await response.write(chunk)
            await response.write_eof()
            return response

    # 3. In-memory (ZIP)
    stream = ticket.stream
    if stream is None:
        raise web.HTTPNotFound(text="Ссылка устарела")

    size = int(ticket.size or 0)
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": ticket.mime,
            "Content-Disposition": disposition,
            "Content-Length": str(size),
            "Cache-Control": "no-store",
        },
    )
    await response.prepare(request)
    loop = asyncio.get_running_loop()
    try:
        while True:
            chunk = await loop.run_in_executor(None, stream.read, CHUNK_SIZE)
            if not chunk:
                break
            await response.write(chunk)
        await response.write_eof()
    finally:
        with contextlib.suppress(Exception):
            stream.close()
    return response


async def handle_upload(request: web.Request) -> web.StreamResponse:
    user_id: int = request["user_id"]
    bot: Bot = request.app["bot"]
    storage: Storage = request.app["storage"]
    github: GitHubStorage | None = request.app.get("github")

    if not request.content_type.startswith("multipart/"):
        return bad_request("Ожидается multipart/form-data")

    reader = await request.multipart()
    folder_id: int | None = None
    tmp_path: str | None = None
    original_name = "file"
    mime = "application/octet-stream"
    size = 0

    try:
        while True:
            part = await reader.next()
            if part is None:
                break

            if part.name == "folder_id":
                raw = (await part.text()).strip()
                folder_id = int(raw) if raw.isdigit() else None
                continue

            if part.name != "file":
                await part.release()
                continue

            original_name = sanitize_filename(part.filename or "file")
            mime = part.headers.get("Content-Type") or "application/octet-stream"
            fd, tmp_path = tempfile.mkstemp(prefix="storage-upload-")
            os.close(fd)

            with open(tmp_path, "wb") as target:
                while True:
                    chunk = await part.read_chunk(CHUNK_SIZE)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        return bad_request("Файл слишком большой", status=413)
                    target.write(chunk)

        if tmp_path is None:
            return bad_request("Поле file не найдено")

        if folder_id is not None and not await storage.folder_owned(user_id, folder_id):
            folder_id = None

        file_unique_id = secrets.token_hex(8)
        github_path: str | None = None
        file_id = ""

        # Пытаемся сохранить в GitHub
        if github and size <= MAX_GITHUB_BYTES:
            try:
                with open(tmp_path, "rb") as fh:
                    content = fh.read()
                github_path = github.build_path(user_id, file_unique_id, original_name)
                await github.upload(github_path, content)
                log.info("uploaded to github: %s (%d bytes)", github_path, size)
            except Exception:
                log.exception("github upload failed, fallback to telegram")
                github_path = None

        # Fallback или основной путь — Telegram
        if github_path is None:
            try:
                message = await bot.send_document(
                    chat_id=user_id,
                    document=FSInputFile(tmp_path, filename=original_name),
                )
            except Exception:
                log.exception("send_document failed for user=%s", user_id)
                return bad_request(
                    "Telegram не принял файл. Проверьте размер и повторите попытку",
                    status=502,
                )

            document = message.document
            if document is None:
                return bad_request("Telegram вернул сообщение без документа", status=502)

            file_id = document.file_id
            file_unique_id = document.file_unique_id
            size = int(document.file_size or size)
            mime = document.mime_type or mime

        row_id = await storage.add_file(
            user_id=user_id,
            file_id=file_id,
            file_unique_id=file_unique_id,
            name=original_name,
            size=size,
            mime=mime,
            folder_id=folder_id,
            uploaded_at=int(time.time()),
            github_path=github_path,
        )

        return web.json_response(
            {
                "id": row_id,
                "name": original_name,
                "size": size,
                "mime": mime,
                "folder_id": folder_id,
                "uploaded_at": int(time.time()),
                "storage": "github" if github_path else "telegram",
            },
            status=201,
        )
    finally:
        if tmp_path:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)


def build_web_app(
    bot: Bot,
    storage: Storage,
    http: aiohttp.ClientSession,
    github: GitHubStorage | None,
) -> web.Application:
    app = web.Application(
        middlewares=[cors_middleware, auth_middleware],
        client_max_size=MAX_UPLOAD_BYTES,
    )
    app["bot"] = bot
    app["storage"] = storage
    app["http"] = http
    if github is not None:
        app["github"] = github

    app.router.add_get("/health", handle_health)
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/folders", handle_folders_list)
    app.router.add_post("/api/folders", handle_folders_create)
    app.router.add_delete("/api/folders/{folder_id:\\d+}", handle_folder_delete)
    app.router.add_get("/api/files", handle_files_list)
    app.router.add_post("/api/files/delete", handle_files_delete)
    app.router.add_post("/api/files/move", handle_files_move)
    app.router.add_get("/api/files/{file_id:\\d+}/link", handle_file_link)
    app.router.add_post("/api/zip", handle_zip)
    app.router.add_post("/api/upload", handle_upload)
    app.router.add_get("/dl/{token}", handle_download)
    return app


# --------------------------------------------------------------------------- #
# Бот
# --------------------------------------------------------------------------- #

router = Router()

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Статистика")]],
    resize_keyboard=True,
)


@dataclass(slots=True)
class FileMeta:
    file_id: str
    file_unique_id: str
    name: str
    size: int
    mime: str


def extract_file_meta(message: Message) -> FileMeta:
    if message.document:
        doc = message.document
        return FileMeta(doc.file_id, doc.file_unique_id,
                        doc.file_name or f"document_{doc.file_unique_id}",
                        int(doc.file_size or 0),
                        doc.mime_type or "application/octet-stream")
    if message.photo:
        photo = message.photo[-1]
        return FileMeta(photo.file_id, photo.file_unique_id,
                        f"photo_{photo.file_unique_id}.jpg",
                        int(photo.file_size or 0), "image/jpeg")
    if message.video:
        video = message.video
        return FileMeta(video.file_id, video.file_unique_id,
                        video.file_name or f"video_{video.file_unique_id}.mp4",
                        int(video.file_size or 0),
                        video.mime_type or "video/mp4")
    if message.audio:
        audio = message.audio
        if audio.file_name:
            name = audio.file_name
        elif audio.performer and audio.title:
            name = f"{audio.performer} - {audio.title}"
        else:
            name = f"audio_{audio.file_unique_id}.mp3"
        return FileMeta(audio.file_id, audio.file_unique_id, name,
                        int(audio.file_size or 0),
                        audio.mime_type or "audio/mpeg")
    if message.voice:
        voice = message.voice
        return FileMeta(voice.file_id, voice.file_unique_id,
                        f"voice_{voice.file_unique_id}.ogg",
                        int(voice.file_size or 0),
                        voice.mime_type or "audio/ogg")
    if message.video_note:
        note = message.video_note
        return FileMeta(note.file_id, note.file_unique_id,
                        f"round_{note.file_unique_id}.mp4",
                        int(note.file_size or 0), "video/mp4")
    if message.animation:
        anim = message.animation
        return FileMeta(anim.file_id, anim.file_unique_id,
                        anim.file_name or f"animation_{anim.file_unique_id}.mp4",
                        int(anim.file_size or 0),
                        anim.mime_type or "video/mp4")
    if message.sticker:
        st = message.sticker
        if st.is_video:
            ext, mime = "webm", "video/webm"
        elif st.is_animated:
            ext, mime = "tgs", "application/x-tgsticker"
        else:
            ext, mime = "webp", "image/webp"
        return FileMeta(st.file_id, st.file_unique_id,
                        f"sticker_{st.file_unique_id}.{ext}",
                        int(st.file_size or 0), mime)
    raise ValueError("Этот тип сообщения не поддерживается")


def file_keyboard(row_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="В папку", callback_data=f"mv:{row_id}"),
                InlineKeyboardButton(text="Ссылка", callback_data=f"ln:{row_id}"),
                InlineKeyboardButton(text="Удалить", callback_data=f"rm:{row_id}"),
            ]
        ]
    )


def folder_keyboard(row_id: int, folders: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=folder["name"][:48], callback_data=f"mvf:{row_id}:{folder['id']}")]
        for folder in folders[:40]
    ]
    rows.append([InlineKeyboardButton(text="Без папки", callback_data=f"mvf:{row_id}:0")])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="noop")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    try:
        storage_note = (
            f"\n\nФайлы сохраняются в GitHub: {GITHUB_REPO} → {GITHUB_FOLDER}/"
            if GITHUB_ENABLED else ""
        )
        await message.answer(
            "Хранилище — личное облако внутри Telegram.\n\n"
            "Пришлите боту любой файл, и он сохранится. "
            "В Mini App есть папки, поиск, сортировка и массовые операции."
            + storage_note,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(
                    text="Открыть хранилище", web_app=WebAppInfo(url=WEBAPP_URL)
                )]]
            ),
        )
        await message.answer("Быстрые действия:", reply_markup=MAIN_KEYBOARD)
    except Exception:
        log.exception("cmd_start failed")
        await message.answer("Не удалось показать меню, попробуйте позже.")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "/start — открыть хранилище\n"
        "/stats — статистика\n"
        "/clear — удалить все файлы\n"
        "/help — эта справка\n\n"
        "Отправьте файл, чтобы сохранить его."
    )


@router.message(Command("stats"))
@router.message(F.text == "Статистика")
async def cmd_stats(message: Message, storage: Storage) -> None:
    try:
        text = await stats_text(storage, message.from_user.id)
        await message.answer(text)
    except Exception:
        log.exception("cmd_stats failed")
        await message.answer("Не удалось получить статистику.")


@router.message(Command("clear"))
async def cmd_clear(message: Message) -> None:
    await message.answer(
        "Удалить все файлы из хранилища? Действие необратимо.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text="Удалить всё", callback_data="clear:confirm"),
                InlineKeyboardButton(text="Отмена", callback_data="clear:cancel"),
            ]]
        ),
    )


@router.callback_query(F.data == "clear:confirm")
async def cb_clear_confirm(callback: CallbackQuery, storage: Storage) -> None:
    github: GitHubStorage | None = None
    # достаём github из app через bot
    bot: Bot = callback.bot
    try:
        deleted, paths = await storage.clear_files(callback.from_user.id)
    except Exception:
        log.exception("clear failed")
        await callback.answer("Не удалось очистить хранилище", show_alert=True)
        return

    if GITHUB_ENABLED and paths:
        # ленивая ссылка на GitHubStorage через session — создаём на лету
        for path in paths:
            asyncio.create_task(_clear_github_bg(path))

    await callback.answer()
    if callback.message:
        await callback.message.edit_text(f"Удалено файлов: {deleted}")


async def _clear_github_bg(path: str) -> None:
    try:
        async with aiohttp.ClientSession() as http:
            gh = GitHubStorage(http, GITHUB_TOKEN, GITHUB_REPO,
                               GITHUB_BRANCH, GITHUB_FOLDER, GITHUB_API)
            await gh.delete(path)
    except Exception:
        log.exception("github cleanup failed for %s", path)


@router.callback_query(F.data == "clear:cancel")
async def cb_clear_cancel(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.edit_text("Отменено.")


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith("rm:"))
async def cb_remove(callback: CallbackQuery, storage: Storage) -> None:
    try:
        row_id = int(callback.data.split(":", 1)[1])
        paths = await storage.get_github_paths(callback.from_user.id, [row_id])
        deleted = await storage.delete_files(callback.from_user.id, [row_id])
    except Exception:
        log.exception("cb_remove failed")
        await callback.answer("Не удалось удалить", show_alert=True)
        return

    if GITHUB_ENABLED and paths:
        for path in paths:
            asyncio.create_task(_clear_github_bg(path))

    await callback.answer("Удалено" if deleted else "Файл не найден")
    if callback.message and deleted:
        await callback.message.edit_text("Файл удалён из хранилища.")
        await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("ln:"))
async def cb_link(callback: CallbackQuery, storage: Storage, bot: Bot) -> None:
    try:
        row_id = int(callback.data.split(":", 1)[1])
        row = await storage.get_file(callback.from_user.id, row_id)
        if row is None:
            await callback.answer("Файл не найден", show_alert=True)
            return

        if row.get("github_path"):
            token = issue_ticket(
                Ticket(
                    name=row["name"],
                    mime=row["mime"] or "application/octet-stream",
                    expires_at=time.time() + TICKET_TTL,
                    size=int(row["size"] or 0),
                    github_path=row["github_path"],
                )
            )
        else:
            tg_file = await bot.get_file(row["file_id"])
            if not tg_file.file_path:
                await callback.answer("Telegram не отдал файл", show_alert=True)
                return
            token = issue_ticket(
                Ticket(
                    name=row["name"],
                    mime=row["mime"] or "application/octet-stream",
                    expires_at=time.time() + TICKET_TTL,
                    size=int(row["size"] or 0),
                    file_path=tg_file.file_path,
                )
            )

        await callback.message.answer(f"{public_base()}/dl/{token}")
        await callback.answer("Ссылка отправлена")
    except Exception:
        log.exception("cb_link failed")
        await callback.answer("Не удалось получить ссылку", show_alert=True)


@router.callback_query(F.data.startswith("mv:"))
async def cb_move_menu(callback: CallbackQuery, storage: Storage) -> None:
    try:
        row_id = int(callback.data.split(":", 1)[1])
        folders = await storage.list_folders(callback.from_user.id)
    except Exception:
        log.exception("cb_move_menu failed")
        await callback.answer("Не удалось открыть список папок", show_alert=True)
        return

    await callback.answer()
    if callback.message:
        if folders:
            await callback.message.edit_text(
                "Куда переместить файл?", reply_markup=folder_keyboard(row_id, folders)
            )
        else:
            await callback.message.edit_text(
                "Папок пока нет. Создайте их в Mini App.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="noop")]]
                ),
            )


@router.callback_query(F.data.startswith("mvf:"))
async def cb_move_apply(callback: CallbackQuery, storage: Storage) -> None:
    try:
        _, raw_id, raw_folder = callback.data.split(":")
        row_id = int(raw_id)
        folder_id = int(raw_folder) or None
        moved = await storage.move_files(callback.from_user.id, [row_id], folder_id)
    except Exception:
        log.exception("cb_move_apply failed")
        await callback.answer("Не удалось переместить", show_alert=True)
        return

    await callback.answer("Перемещено" if moved else "Не удалось")
    if callback.message:
        if moved:
            await callback.message.edit_text(
                "Файл в корне." if folder_id is None else "Файл перемещён в папку."
            )
        else:
            await callback.message.edit_text("Файл не найден.")


@router.message(
    F.document | F.photo | F.video | F.audio | F.voice
    | F.video_note | F.sticker | F.animation
)
async def handle_incoming_file(
    message: Message,
    storage: Storage,
    bot: Bot,
    http: aiohttp.ClientSession,
) -> None:
    try:
        meta = extract_file_meta(message)
    except ValueError as exc:
        await message.answer(str(exc))
        return

    github_path: str | None = None
    file_id = meta.file_id
    file_unique_id = meta.file_unique_id
    size = meta.size

    # Если GitHub включён и файл влезает — тянем из Telegram и коммитим.
    if GITHUB_ENABLED and 0 < size <= MAX_GITHUB_BYTES:
        try:
            content = await _download_from_telegram(bot, http, meta.file_id)
            gh_path = f"{GITHUB_FOLDER}/{message.from_user.id}/{meta.file_unique_id}_{sanitize_filename(meta.name)}"
            gh = GitHubStorage(http, GITHUB_TOKEN, GITHUB_REPO,
                               GITHUB_BRANCH, GITHUB_FOLDER, GITHUB_API)
            github_path = await gh.upload(gh_path, content)
            size = len(content)
            log.info("bot file -> github: %s (%d bytes)", github_path, size)
        except Exception:
            log.exception("github upload failed for bot file, keeping telegram only")
            github_path = None

    try:
        row_id = await storage.add_file(
            user_id=message.from_user.id,
            file_id=file_id,
            file_unique_id=file_unique_id,
            name=meta.name,
            size=size,
            mime=meta.mime,
            folder_id=None,
            uploaded_at=int(time.time()),
            github_path=github_path,
        )
    except Exception:
        log.exception("file save failed")
        await message.answer("Не удалось сохранить файл, попробуйте ещё раз.")
        return

    where = " · GitHub" if github_path else ""
    await message.answer(
        f"Сохранено: {meta.name}\nРазмер: {human_size(size)}{where}",
        reply_markup=file_keyboard(row_id),
    )


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Не задана переменная окружения BOT_TOKEN")
    if not WEBAPP_URL:
        raise SystemExit("Не задана переменная окружения WEBAPP_URL")

    db = Database(DB_PATH)
    await db.start()

    http_session = aiohttp.ClientSession()
    github: GitHubStorage | None = None
    if GITHUB_ENABLED:
        github = GitHubStorage(
            http_session, GITHUB_TOKEN, GITHUB_REPO,
            GITHUB_BRANCH, GITHUB_FOLDER, GITHUB_API,
        )

    storage = Storage(db, github)

    session = AiohttpSession(api=TelegramAPIServer.from_base(BOT_API_URL))
    bot = Bot(
        token=BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    app = build_web_app(bot, storage, http_session, github)
    # пробрасываем storage и http в контекст polling
    dispatcher.workflow_data.update(
        storage=storage, bot=bot, http=http_session
    )

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_HOST, WEB_PORT)
    await site.start()
    log.info("HTTP API доступен на %s:%s", WEB_HOST, WEB_PORT)
    log.info("Разрешённые origin'ы: %s", ", ".join(sorted(ALLOWED_ORIGINS)) or "—")

    cleanup_task = asyncio.create_task(tickets_cleanup_loop())

    try:
        with contextlib.suppress(Exception):
            await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(bot)
    finally:
        cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task
        await runner.cleanup()
        await http_session.close()
        await session.close()
        await db.close()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())