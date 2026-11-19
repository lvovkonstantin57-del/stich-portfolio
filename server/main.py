"""Backend сайта-портфолио Stich: контент, вход в редактор, загрузка картинок, форма сообщения.

Запуск для разработки:  .venv/bin/uvicorn server.main:app --reload --port 8000
"""
import hashlib
import hmac
import http.client
import io
import json
import os
import secrets
import shutil
import socket
import ssl
import threading
import time
import urllib.parse
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR") or ROOT / "data")
CONTENT_FILE = DATA_DIR / "content.json"
BACKUP_FILE = DATA_DIR / "content.backup.json"
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "").strip()
# Без SECRET_KEY сессии живут до перезапуска сервера
SECRET_KEY = (os.getenv("SECRET_KEY") or secrets.token_hex(32)).encode()
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() in ("1", "true", "yes")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
# С некоторых хостингов адрес api.telegram.org из DNS недоступен. Здесь можно перечислить
# через запятую IP серверов Telegram, которые пробуются первыми (сертификат всё равно проверяется).
TELEGRAM_API_IPS = [ip.strip() for ip in os.getenv("TELEGRAM_API_IPS", "").split(",") if ip.strip()]
TELEGRAM_HOST = "api.telegram.org"
TELEGRAM_FALLBACK_IP = "149.154.167.220"

COOKIE_NAME = "stich_session"
SESSION_TTL = 60 * 60 * 24 * 14  # 14 дней
MAX_UPLOAD = 12 * 1024 * 1024
MAX_CONTENT = 2 * 1024 * 1024
MAX_IMAGE_SIDE = 2000

app = FastAPI(title="Stich portfolio API", docs_url=None, redoc_url=None, openapi_url=None)
# В проде /uploads отдаёт nginx; это — для локальной разработки
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")

_write_lock = threading.Lock()


# ───── Пароль и сессия ─────

def hash_password(password: str, iterations: int = 600_000) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iterations).hex()
    return f"pbkdf2:{iterations}:{salt}:{digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, digest = stored.split(":")
        if scheme != "pbkdf2":
            return False
        check = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations)).hex()
        return hmac.compare_digest(check, digest)
    except (ValueError, TypeError):
        return False


def _sign(payload: str) -> str:
    return hmac.new(SECRET_KEY, payload.encode(), hashlib.sha256).hexdigest()


def make_token() -> str:
    exp = str(int(time.time()) + SESSION_TTL)
    return f"{exp}.{_sign(exp)}"


def token_ok(token: str) -> bool:
    try:
        exp, sig = token.split(".")
        return hmac.compare_digest(sig, _sign(exp)) and int(exp) > time.time()
    except (ValueError, AttributeError):
        return False


def is_authed(request: Request) -> bool:
    return token_ok(request.cookies.get(COOKIE_NAME, ""))


def require_auth(request: Request) -> None:
    if not is_authed(request):
        raise HTTPException(401, "Нужно войти в режим редактора")


# ───── Ограничение частоты запросов (в памяти процесса) ─────

_hits: Dict[str, List[float]] = defaultdict(list)


def client_ip(request: Request) -> str:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "?")


def rate_limit(request: Request, bucket: str, limit: int, window: int) -> None:
    key = f"{bucket}:{client_ip(request)}"
    now = time.time()
    recent = [t for t in _hits[key] if now - t < window]
    if len(recent) >= limit:
        _hits[key] = recent
        raise HTTPException(429, "Слишком много попыток, попробуй позже")
    recent.append(now)
    _hits[key] = recent


# ───── Контент ─────

@app.get("/api/content")
def get_content(response: Response):
    response.headers["Cache-Control"] = "no-cache"
    return json.loads(CONTENT_FILE.read_text(encoding="utf-8"))


@app.put("/api/content", dependencies=[Depends(require_auth)])
async def put_content(request: Request):
    raw = await request.body()
    if len(raw) > MAX_CONTENT:
        raise HTTPException(413, "Слишком большой контент")
    try:
        data = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Это не JSON")
    if not isinstance(data, dict) or not isinstance(data.get("sections"), list):
        raise HTTPException(400, "В контенте нет списка sections")
    for key in ("meta", "start", "select", "ui"):
        if not isinstance(data.get(key), dict):
            raise HTTPException(400, f"В контенте нет блока {key}")
    if not isinstance(data.get("contacts"), list):
        raise HTTPException(400, "В контенте нет списка contacts")
    slugs = [s.get("slug") for s in data["sections"] if isinstance(s, dict)]
    if len(slugs) != len(data["sections"]) or len(set(slugs)) != len(slugs) or not all(slugs):
        raise HTTPException(400, "У разделов должны быть разные непустые адреса (slug)")

    with _write_lock:
        if CONTENT_FILE.exists():
            shutil.copyfile(CONTENT_FILE, BACKUP_FILE)
        tmp = CONTENT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, CONTENT_FILE)
    return {"ok": True}


# ───── Вход ─────

class LoginIn(BaseModel):
    password: str = Field(max_length=200)


@app.get("/api/me")
def me(request: Request):
    return {"authed": is_authed(request)}


@app.post("/api/login")
def login(body: LoginIn, request: Request, response: Response):
    if not ADMIN_PASSWORD_HASH:
        raise HTTPException(503, "Пароль не настроен: задай ADMIN_PASSWORD_HASH в .env")
    rate_limit(request, "login", limit=5, window=300)
    if not verify_password(body.password, ADMIN_PASSWORD_HASH):
        raise HTTPException(401, "Неверный пароль")
    response.set_cookie(
        COOKIE_NAME, make_token(), max_age=SESSION_TTL,
        httponly=True, samesite="strict", secure=COOKIE_SECURE, path="/",
    )
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


# ───── Загрузка картинок ─────

@app.post("/api/upload", dependencies=[Depends(require_auth)])
def upload(file: UploadFile = File(...)):
    raw = file.file.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(413, "Файл больше 12 МБ")
    try:
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img)
        img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA" if "transparency" in img.info else "RGB")
        name = f"{uuid.uuid4().hex[:16]}.webp"
        img.save(UPLOAD_DIR / name, "WEBP", quality=82, method=5)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Не получилось прочитать картинку (нужен JPG, PNG, WebP или GIF)")
    return {"url": f"/uploads/{name}", "width": img.width, "height": img.height}


# ───── Сообщение из формы → Telegram ─────

class _PinnedHTTPS(http.client.HTTPSConnection):
    """HTTPS к api.telegram.org по заданному IP; имя хоста в сертификате проверяется как обычно."""

    def __init__(self, ip, timeout):
        super().__init__(TELEGRAM_HOST, timeout=timeout)
        self._ip = ip
        self._ctx = ssl.create_default_context()

    def connect(self):
        sock = socket.create_connection((self._ip or self.host, self.port), self.timeout)
        self.sock = self._ctx.wrap_socket(sock, server_hostname=self.host)


def telegram_send(params: Dict[str, str]) -> bool:
    body = urllib.parse.urlencode(params)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    # None — адрес из DNS; последним пробуем известный запасной IP
    for ip in TELEGRAM_API_IPS + [None, TELEGRAM_FALLBACK_IP]:
        conn = _PinnedHTTPS(ip, timeout=6)
        try:
            conn.request("POST", f"/bot{TELEGRAM_BOT_TOKEN}/sendMessage", body=body, headers=headers)
            answer = json.loads(conn.getresponse().read())
            if answer.get("ok"):
                return True
            return False  # Telegram ответил отказом (неверный токен/chat_id) — другой IP не поможет
        except (OSError, ValueError, http.client.HTTPException):
            continue
        finally:
            conn.close()
    return False


class MessageIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    contact: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=4000)
    website: str = Field(default="", max_length=200)  # ловушка для ботов


@app.post("/api/message")
def message(body: MessageIn, request: Request):
    if body.website:
        return {"ok": True}  # бот заполнил скрытое поле — молча игнорируем
    rate_limit(request, "message", limit=5, window=600)
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        raise HTTPException(503, "Telegram не настроен: задай TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env")
    text = (
        "Новое сообщение с сайта\n\n"
        f"Имя: {body.name.strip()}\n"
        f"Контакт: {body.contact.strip()}\n\n"
        f"{body.message.strip()}"
    )
    params = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": "true"}
    if not telegram_send(params):
        raise HTTPException(502, "Не получилось переслать сообщение в Telegram")
    return {"ok": True}

# --- Stich AI: мини-чат + админка «Нейросеть» ---
try:
    from .ai_module import router as ai_router
except ImportError:
    from ai_module import router as ai_router
app.include_router(ai_router)
