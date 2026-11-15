import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

APP_NAME = "Selam"
BASE = Path(__file__).resolve().parent

# ---------- .env okuma (birkaç yere bakar, hangisini okuduğunu yazar) ----------
ENV_LOADED = []
ENV_WARNINGS = []


def _read_text_any(p: Path) -> str:
    raw = p.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def load_env_file():
    seen = set()
    for d in (BASE, Path.cwd(), BASE.parent):
        p = d / ".env"
        try:
            rp = p.resolve()
        except Exception:
            continue
        if rp in seen:
            continue
        seen.add(rp)
        if not p.is_file():
            if (d / ".env.txt").is_file():
                ENV_WARNINGS.append(f"'{d / '.env.txt'}' bulundu ama adı '.env' olmalı (sonunda .txt olmamalı).")
            continue
        try:
            text = _read_text_any(p)
        except Exception as e:
            ENV_WARNINGS.append(f"'{p}' okunamadı: {e}")
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v:
                os.environ.setdefault(k, v)
        ENV_LOADED.append(str(p))


load_env_file()

PORT = int(os.environ.get("PORT", 3000))
HOST = os.environ.get("HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
TRUST_PROXY = os.environ.get("TRUST_PROXY", "1" if os.environ.get("PORT") else "0") == "1"
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE))
DATA_FILE = Path(os.environ.get("DATA_FILE", DATA_DIR / "data.json"))
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", DATA_DIR / "uploads"))
_m = re.search(r"postgres(?:ql)?://[^\s'\"]+", os.environ.get("DATABASE_URL", ""))
DATABASE_URL = _m.group(0) if _m else ""  # "psql '...'" diye yapıştırılsa bile adresi ayıklar
CODE_TTL = 10 * 60
MAX_ATTEMPTS = 5
SESSION_TTL = 365 * 24 * 3600 * 1000  # ms, her kullanımda yenilenir
MAX_UPLOAD = 4 * 1024 * 1024
IMG_RE = re.compile(r"^/uploads/[0-9a-f]{32}\.(jpg|png|gif|webp)$")


def now_ms() -> int:
    return int(time.time() * 1000)


# ---------- Depolama ----------
db = {"users": [], "sessions": {}, "messages": []}


# Postgres (DATABASE_URL varsa): barındırma servislerinde disk silindiği için veriler orada durur.
# Her işlemde kısa süreli bağlantı açılır; Neon'un uykuya geçmesi sorun olmaz.
def _pg():
    import psycopg  # yalnızca DATABASE_URL varsa gerekir

    return psycopg.connect(DATABASE_URL, connect_timeout=20)


def pg_init():
    with _pg() as c:
        c.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS files (name TEXT PRIMARY KEY, data BYTEA NOT NULL)")


def pg_load():
    with _pg() as c:
        row = c.execute("SELECT v FROM kv WHERE k = 'db'").fetchone()
    return json.loads(row[0]) if row else None


def pg_put_file(name: str, data: bytes):
    with _pg() as c:
        c.execute("INSERT INTO files (name, data) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING", (name, data))


def pg_get_file(name: str):
    with _pg() as c:
        row = c.execute("SELECT data FROM files WHERE name = %s", (name,)).fetchone()
    return bytes(row[0]) if row else None


def pg_has_file(name: str) -> bool:
    with _pg() as c:
        return c.execute("SELECT 1 FROM files WHERE name = %s", (name,)).fetchone() is not None


def pg_del_file(name: str):
    with _pg() as c:
        c.execute("DELETE FROM files WHERE name = %s", (name,))


def load_db():
    if DATABASE_URL:
        loaded = None
        for i in range(5):
            try:
                pg_init()
                loaded = pg_load()
                break
            except Exception as e:
                print(f"Veritabanına bağlanılamadı ({i + 1}/5): {e}", flush=True)
                time.sleep(3)
        else:
            # Boş başlayıp mevcut verilerin üstüne yazmamak için durdur
            raise SystemExit("Veritabanına bağlanılamadı. DATABASE_URL doğru mu? Sunucu durduruldu.")
        if loaded:
            db.update(loaded)
        return
    try:
        db.update(json.loads(DATA_FILE.read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    except Exception as e:
        bad = DATA_FILE.with_name(DATA_FILE.name + f".bozuk-{int(time.time())}")
        try:
            DATA_FILE.replace(bad)
        except OSError:
            pass
        print(f"UYARI: {DATA_FILE} okunamadı ({e}). Yedeği {bad.name} olarak ayrıldı, boş veritabanıyla başlanıyor.", flush=True)


load_db()
users_by_id = {u["id"]: u for u in db["users"]}
dirty = False


def mark():
    global dirty
    dirty = True


def _persist(data: str):
    if DATABASE_URL:
        with _pg() as c:
            c.execute("INSERT INTO kv (k, v) VALUES ('db', %s) ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v", (data,))
        return
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, DATA_FILE)


async def flush():
    global dirty
    if not dirty:
        return
    dirty = False
    data = json.dumps(db, ensure_ascii=False)
    try:
        await asyncio.to_thread(_persist, data)
    except Exception as e:
        dirty = True
        print("Kaydedilemedi:", e, flush=True)


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def new_id() -> str:
    return secrets.token_hex(8)


def norm_email(e) -> str:
    return str(e or "").strip().lower()


def valid_email(e: str) -> bool:
    return bool(re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", e)) and len(e) <= 254


online = {}  # user_id -> set[WebSocket]


def pub(u):
    return {"id": u["id"], "username": u["username"], "online": bool(online.get(u["id"])), "last": u.get("last")}


def me(u):
    return {"id": u["id"], "username": u["username"], "email": u["email"]}


# ---------- E-posta ----------
def mail_mode():
    if all(os.environ.get(k) for k in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")):
        return "gmail"
    if os.environ.get("BREVO_API_KEY"):
        return "brevo"
    if os.environ.get("SMTP_HOST"):
        return "smtp"
    return None


def _mail_text(code: str) -> str:
    return f"Giriş kodun: {code}\nBu kod 10 dakika geçerli. Bunu sen istemediysen görmezden gel."


def _send_smtp(email: str, code: str):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", 587))
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS", "")
    if "gmail" in host or "google" in host:
        pw = pw.replace(" ", "")  # Google şifreyi boşluklu gösterir
    msg = EmailMessage()
    msg["From"] = os.environ.get("MAIL_FROM") or user
    msg["To"] = email
    msg["Subject"] = f"{APP_NAME} giriş kodun: {code}"
    msg.set_content(_mail_text(code))
    if port == 465 or os.environ.get("SMTP_SECURE") == "true":
        s = smtplib.SMTP_SSL(host, port, timeout=20)
    else:
        s = smtplib.SMTP(host, port, timeout=20)
        if os.environ.get("SMTP_STARTTLS", "true") != "false":
            s.starttls()
    with s:
        if user:
            s.login(user, pw)
        s.send_message(msg)


def _send_brevo(email: str, code: str):
    name, addr = parseaddr(os.environ.get("MAIL_FROM", ""))
    if not addr:
        raise RuntimeError("MAIL_FROM ayarlı değil (Brevo'da doğruladığın gönderen adresi olmalı)")
    payload = {
        "sender": {"name": name or APP_NAME, "email": addr},
        "to": [{"email": email}],
        "subject": f"{APP_NAME} giriş kodun: {code}",
        "textContent": _mail_text(code),
    }
    req = urllib.request.Request(
        os.environ.get("BREVO_API_URL", "https://api.brevo.com/v3/smtp/email"),
        data=json.dumps(payload).encode(),
        headers={"api-key": os.environ["BREVO_API_KEY"], "content-type": "application/json", "accept": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Brevo {e.code}: {e.read().decode(errors='replace')[:300]}")


_gmail_tok = {"t": None, "exp": 0.0}


def _gmail_token() -> str:
    if _gmail_tok["t"] and time.time() < _gmail_tok["exp"] - 60:
        return _gmail_tok["t"]
    body = urllib.parse.urlencode({
        "client_id": os.environ["GMAIL_CLIENT_ID"],
        "client_secret": os.environ["GMAIL_CLIENT_SECRET"],
        "refresh_token": os.environ["GMAIL_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(os.environ.get("GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token"), data=body, method="POST")
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=20).read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Google {e.code}: {e.read().decode(errors='replace')[:300]}")
    _gmail_tok["t"], _gmail_tok["exp"] = r["access_token"], time.time() + int(r.get("expires_in", 3600))
    return _gmail_tok["t"]


def _send_gmail(email: str, code: str):
    """Gmail API (HTTPS, 443): SMTP portlarını engelleyen barındırma servislerinde de çalışır."""
    msg = EmailMessage()
    if os.environ.get("MAIL_FROM"):
        msg["From"] = os.environ["MAIL_FROM"]
    msg["To"] = email
    msg["Subject"] = f"{APP_NAME} giriş kodun: {code}"
    msg.set_content(_mail_text(code))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    req = urllib.request.Request(
        os.environ.get("GMAIL_SEND_URL", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"),
        data=json.dumps({"raw": raw}).encode(),
        headers={"Authorization": "Bearer " + _gmail_token(), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Gmail API {e.code}: {e.read().decode(errors='replace')[:300]}")


def explain_mail_error(e: Exception) -> str:
    if "invalid_grant" in str(e):
        return ("Google yenileme anahtarı geçersiz ya da süresi dolmuş. OAuth uygulamasını 'Production' (Yayında) durumuna "
                "almadıysan anahtar 7 günde ölür; yenisini al ve GMAIL_REFRESH_TOKEN'ı güncelle.")
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return ("Giriş reddedildi. Gmail kullanıyorsan SMTP_PASS normal şifren değil, 16 haneli UYGULAMA ŞİFRESİ olmalı "
                "(https://myaccount.google.com/apppasswords) ve SMTP_USER tam e-posta adresin olmalı.")
    if isinstance(e, (TimeoutError, ConnectionError, OSError)):
        return "Mail sunucusuna bağlanılamadı (internet, güvenlik duvarı ya da barındırma servisi SMTP'yi engelliyor olabilir)."
    return str(e)


MAILERS = {"gmail": _send_gmail, "brevo": _send_brevo, "smtp": _send_smtp}


async def send_code(email: str, code: str):
    mode = mail_mode()
    if mode is None:
        print(f"\n[GELİŞTİRME] {email} için giriş kodu: {code}\n", flush=True)
        return
    await asyncio.to_thread(MAILERS[mode], email, code)


# ---------- Kod ve oturum ----------
codes = {}  # email -> dict(hash, exp, attempts, last)
ip_hits = {}  # ip -> [timestamps]
upload_hits = {}  # user_id -> [timestamps]


def client_ip(request: Request) -> str:
    if TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


def ip_limited(ip: str) -> bool:
    now = time.time()
    arr = [t for t in ip_hits.get(ip, []) if now - t < 15 * 60]
    arr.append(now)
    ip_hits[ip] = arr
    if len(ip_hits) > 5000:
        for k in [k for k, v in ip_hits.items() if not v or now - v[-1] > 900]:
            ip_hits.pop(k, None)
    return len(arr) > 20


def auth_user(token):
    if not token:
        return None
    h = sha(token)
    s = db["sessions"].get(h)
    if not s:
        return None
    if isinstance(s, str):  # eski biçim
        s = {"u": s, "t": now_ms()}
        db["sessions"][h] = s
        mark()
    if now_ms() - s["t"] > SESSION_TTL:
        db["sessions"].pop(h, None)
        mark()
        return None
    if now_ms() - s["t"] > 24 * 3600 * 1000:
        s["t"] = now_ms()
        mark()
    return users_by_id.get(s["u"])


@asynccontextmanager
async def lifespan(app):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    async def loop():
        while True:
            await asyncio.sleep(1)
            await flush()

    task = asyncio.create_task(loop())
    yield
    task.cancel()
    await flush()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@app.middleware("http")
async def headers_mw(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    if request.url.path in ("/", "/index.html", "/sw.js", "/manifest.webmanifest"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.exception_handler(HTTPException)
async def http_exc(request, exc):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def val_exc(request, exc):
    return JSONResponse({"error": "Geçersiz istek"}, status_code=400)


def bearer(request: Request) -> str:
    h = request.headers.get("authorization", "")
    return h[7:] if h.startswith("Bearer ") else ""


def require_auth(request: Request):
    user = auth_user(bearer(request))
    if not user:
        raise HTTPException(401, "Giriş gerekli")
    return user


async def body_of(request: Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@app.post("/api/auth/request")
async def auth_request(request: Request):
    data = await body_of(request)
    email = norm_email(data.get("email"))
    if not valid_email(email):
        raise HTTPException(400, "Geçerli bir e-posta gir")
    if ip_limited(client_ip(request)):
        raise HTTPException(429, "Çok fazla deneme, biraz bekle")
    prev = codes.get(email)
    if prev and time.time() - prev["last"] < 30:
        raise HTTPException(429, "Yeni kod için 30 saniye bekle")
    code = str(secrets.randbelow(1_000_000)).zfill(6)
    codes[email] = {"hash": sha(email + code), "exp": time.time() + CODE_TTL, "attempts": 0, "last": time.time()}
    try:
        await send_code(email, code)
    except Exception as e:
        print(f"\n[MAIL HATASI] {email} adresine kod gönderilemedi.\n  Neden: {explain_mail_error(e)}\n  Ayrıntı: {e}\n", flush=True)
        codes.pop(email, None)
        raise HTTPException(500, "E-posta gönderilemedi. Sunucu ayarlarını kontrol et.")
    return {"ok": True, "dev": mail_mode() is None}


@app.post("/api/auth/verify")
async def auth_verify(request: Request):
    data = await body_of(request)
    email = norm_email(data.get("email"))
    code = str(data.get("code") or "").strip()
    entry = codes.get(email)
    if not entry or time.time() > entry["exp"]:
        raise HTTPException(400, "Kodun süresi doldu, yenisini iste")
    if entry["attempts"] >= MAX_ATTEMPTS:
        codes.pop(email, None)
        raise HTTPException(429, "Çok fazla yanlış deneme, yeni kod iste")
    entry["attempts"] += 1
    if not hmac.compare_digest(entry["hash"], sha(email + code)):
        raise HTTPException(400, "Kod yanlış")
    codes.pop(email, None)
    user = next((u for u in db["users"] if u["email"] == email), None)
    if not user:
        user = {"id": new_id(), "email": email, "username": None, "created": now_ms()}
        db["users"].append(user)
        users_by_id[user["id"]] = user
    token = secrets.token_hex(32)
    db["sessions"][sha(token)] = {"u": user["id"], "t": now_ms()}
    mark()
    await flush()
    return {"token": token, "user": me(user)}


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    require_auth(request)
    db["sessions"].pop(sha(bearer(request)), None)
    mark()
    return {"ok": True}


@app.get("/api/me")
async def api_me(request: Request):
    return me(require_auth(request))


@app.post("/api/me/username")
async def set_username(request: Request):
    user = require_auth(request)
    data = await body_of(request)
    name = str(data.get("username") or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{3,20}", name):
        raise HTTPException(400, "3-20 karakter: harf, rakam, alt çizgi")
    if any(u["username"] == name and u["id"] != user["id"] for u in db["users"]):
        raise HTTPException(409, "Bu kullanıcı adı alınmış")
    user["username"] = name
    mark()
    await flush()
    return me(user)


_TR = str.maketrans({"ı": "i", "İ": "i", "I": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
                     "ü": "u", "Ü": "u", "ö": "o", "Ö": "o", "ç": "c", "Ç": "c"})


def fold(s: str) -> str:
    """Büyük/küçük harf ve Türkçe karakter farkını yok sayar (ş=s, ı=i ...)."""
    return s.translate(_TR).casefold()


@app.get("/api/healthz")
async def healthz():
    return {"ok": True}


@app.get("/healthz")
async def healthz2():
    return {"ok": True}


@app.get("/api/users/search")
async def search_users(request: Request, q: str = ""):
    user = require_auth(request)
    q = q.strip().lstrip("@")
    if len(q) < 2:
        return []
    fq = fold(q)
    scored = []
    for u in db["users"]:
        if not u["username"] or u["id"] == user["id"]:
            continue
        name = fold(u["username"])
        if u["email"] == q.lower():
            score = 0
        elif name == fq:
            score = 1
        elif name.startswith(fq):
            score = 2
        elif fq in name:
            score = 3
        else:
            continue
        scored.append((score, name, u))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [pub(u) for _, _, u in scored[:20]]


@app.get("/api/users/by-username/{name}")
async def user_by_name(name: str, request: Request):
    require_auth(request)
    name = name.strip().lower().lstrip("@")
    u = next((x for x in db["users"] if x["username"] == name), None)
    if not u:
        raise HTTPException(404, "Kullanıcı bulunamadı")
    return pub(u)


@app.get("/api/search/messages")
async def search_messages(request: Request, q: str = ""):
    user = require_auth(request)
    q = q.strip()
    if len(q) < 2:
        return []
    fq, uid, out = fold(q), user["id"], []
    for m in reversed(db["messages"]):
        if m.get("del") or not m.get("text"):
            continue
        if m["from"] != uid and m["to"] != uid:
            continue
        if fq in fold(m["text"]):
            peer = users_by_id.get(m["to"] if m["from"] == uid else m["from"])
            if peer:
                out.append({"peer": pub(peer), "msg": m})
                if len(out) >= 30:
                    break
    return out


@app.get("/api/chats")
async def api_chats(request: Request):
    user = require_auth(request)
    uid = user["id"]
    last, unread = {}, {}
    for m in db["messages"]:
        if m["from"] != uid and m["to"] != uid:
            continue
        peer = m["to"] if m["from"] == uid else m["from"]
        last[peer] = m
        if m["to"] == uid and not m.get("rd") and not m.get("del"):
            unread[peer] = unread.get(peer, 0) + 1
    chats = []
    for pid, m in last.items():
        u = users_by_id.get(pid)
        chats.append({"user": pub(u) if u else {"id": pid, "username": "?", "online": False, "last": None},
                      "last": m, "unread": unread.get(pid, 0)})
    chats.sort(key=lambda c: c["last"]["ts"], reverse=True)
    return chats


@app.get("/api/messages/{peer}")
async def api_messages(peer: str, request: Request, limit: int = 300):
    user = require_auth(request)
    mid = user["id"]
    msgs = [m for m in db["messages"]
            if (m["from"] == mid and m["to"] == peer) or (m["from"] == peer and m["to"] == mid)]
    p = users_by_id.get(peer)
    return {"peer": pub(p) if p else None, "messages": msgs[-max(1, min(limit, 2000)):]}


def sniff_image(b: bytes):
    if b[:3] == b"\xff\xd8\xff":
        return "jpg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "webp"
    return None


@app.post("/api/upload")
async def upload(request: Request):
    user = require_auth(request)
    now = time.time()
    arr = [t for t in upload_hits.get(user["id"], []) if now - t < 600]
    if len(arr) >= 30:
        raise HTTPException(429, "Çok fazla fotoğraf, biraz bekle")
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_UPLOAD:
        raise HTTPException(413, "Fotoğraf çok büyük (en fazla 4 MB)")
    body = await request.body()
    if len(body) > MAX_UPLOAD:
        raise HTTPException(413, "Fotoğraf çok büyük (en fazla 4 MB)")
    ext = sniff_image(body)
    if not ext:
        raise HTTPException(400, "Sadece JPG, PNG, GIF ve WEBP gönderilebilir")
    arr.append(now)
    upload_hits[user["id"]] = arr
    name = f"{secrets.token_hex(16)}.{ext}"
    await asyncio.to_thread((UPLOAD_DIR / name).write_bytes, body)
    if DATABASE_URL:  # disk silinebilir, asıl kopya veritabanında durur
        try:
            await asyncio.to_thread(pg_put_file, name, body)
        except Exception as e:
            print("Fotoğraf veritabanına yazılamadı:", e, flush=True)
            (UPLOAD_DIR / name).unlink(missing_ok=True)
            raise HTTPException(500, "Fotoğraf kaydedilemedi, tekrar dene")
    return {"url": f"/uploads/{name}"}


IMG_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(jpg|png|gif|webp)$")
IMG_MIME = {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}
IMG_CACHE = {"Cache-Control": "public, max-age=31536000, immutable"}


async def image_exists(name: str) -> bool:
    if (UPLOAD_DIR / name).is_file():
        return True
    if DATABASE_URL:
        try:
            return await asyncio.to_thread(pg_has_file, name)
        except Exception:
            return False
    return False


async def remove_image(name: str):
    (UPLOAD_DIR / name).unlink(missing_ok=True)
    if DATABASE_URL:
        try:
            await asyncio.to_thread(pg_del_file, name)
        except Exception as e:
            print("Fotoğraf silinemedi:", e, flush=True)


@app.get("/uploads/{name}")
async def get_upload(name: str):
    if not IMG_NAME_RE.match(name):
        raise HTTPException(404, "Bulunamadı")
    mime = IMG_MIME[name.rsplit(".", 1)[1]]
    p = UPLOAD_DIR / name
    if p.is_file():
        return FileResponse(p, media_type=mime, headers=IMG_CACHE)
    if DATABASE_URL:
        try:
            data = await asyncio.to_thread(pg_get_file, name)
        except Exception:
            data = None
        if data:
            try:
                p.write_bytes(data)  # yerel önbellek
            except OSError:
                pass
            return Response(data, media_type=mime, headers=IMG_CACHE)
    raise HTTPException(404, "Bulunamadı")


# ---------- WebSocket ----------
async def send_to(uid, payload: dict, exclude=None):
    s = json.dumps(payload, ensure_ascii=False)
    for c in list(online.get(uid, ())):
        if c is exclude:
            continue
        try:
            await c.send_text(s)
        except Exception:
            online[uid].discard(c)


def contacts_of(uid):
    out = set()
    for m in db["messages"]:
        if m["from"] == uid:
            out.add(m["to"])
        elif m["to"] == uid:
            out.add(m["from"])
    return out


async def broadcast_presence(user):
    payload = {"type": "presence", "id": user["id"], "online": bool(online.get(user["id"])), "last": user.get("last")}
    for pid in contacts_of(user["id"]):
        await send_to(pid, payload)


# ---------- Sesli arama (WebRTC sinyalleşmesi; ses doğrudan kullanıcılar arasında akar) ----------
calls = {}  # callId -> {from, to, ts, answered}
bg_tasks = set()
CALL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,40}$")


def has_history(a, b) -> bool:
    return any((m["from"] == a and m["to"] == b) or (m["from"] == b and m["to"] == a) for m in db["messages"])


async def finish_call(cid, reason, by=None):
    """Aramayı bitirir ve sohbete bir arama kaydı (cevapsız / reddedildi / süre) ekler."""
    c = calls.pop(cid, None)
    if not c:
        return
    if c["answered"]:
        status, dur = "ended", max(0, int((now_ms() - c["answered"]) / 1000))
    elif reason == "reject" or (reason == "end" and by == c["to"]):
        status, dur = "declined", 0
    else:
        status, dur = "missed", 0
    msg = {"id": new_id(), "from": c["from"], "to": c["to"], "text": "", "ts": now_ms(),
           "dl": bool(online.get(c["to"])), "rd": status != "missed", "del": False,
           "call": {"status": status, "dur": dur}}
    db["messages"].append(msg)
    mark()
    payload = {"type": "message", "msg": msg, "fromUser": pub(users_by_id[c["from"]])}
    await send_to(c["from"], payload)
    await send_to(c["to"], payload)


async def ring_timeout(cid):
    await asyncio.sleep(60)
    c = calls.get(cid)
    if c and not c["answered"]:
        await finish_call(cid, "timeout")
        for u in (c["from"], c["to"]):
            await send_to(u, {"type": "call", "kind": "end", "callId": cid})


async def handle_call(user, ws, data):
    uid, ck, cid = user["id"], data.get("kind"), str(data.get("callId") or "")
    if ck not in ("offer", "answer", "ice", "end", "reject", "busy") or not CALL_ID_RE.match(cid):
        return
    if ck == "offer":
        to, sdp = users_by_id.get(data.get("to")), data.get("sdp")
        if cid in calls or not to or not to["username"] or to["id"] == uid or not isinstance(sdp, dict):
            return
        if not has_history(uid, to["id"]):  # istenmeyen aramaları önlemek için önce mesajlaşmış olmak gerekir
            await send_to(uid, {"type": "call", "kind": "denied", "callId": cid})
            return
        calls[cid] = {"from": uid, "to": to["id"], "ts": now_ms(), "answered": None}
        if not online.get(to["id"]):
            await finish_call(cid, "unavailable")
            await send_to(uid, {"type": "call", "kind": "unavailable", "callId": cid})
            return
        await send_to(to["id"], {"type": "call", "kind": "offer", "callId": cid, "from": uid,
                                 "fromUser": pub(user), "sdp": sdp})
        t = asyncio.create_task(ring_timeout(cid))
        bg_tasks.add(t)
        t.add_done_callback(bg_tasks.discard)
        return
    c = calls.get(cid)
    if not c or uid not in (c["from"], c["to"]):
        return
    other = c["to"] if uid == c["from"] else c["from"]
    if ck == "answer":
        if uid != c["to"] or c["answered"] or not isinstance(data.get("sdp"), dict):
            return
        c["answered"] = now_ms()
        await send_to(other, {"type": "call", "kind": "answer", "callId": cid, "sdp": data["sdp"]})
        await send_to(uid, {"type": "call", "kind": "taken", "callId": cid}, exclude=ws)  # diğer cihazlarda çalmayı durdur
    elif ck == "ice":
        cand = data.get("candidate")
        if isinstance(cand, dict) and len(json.dumps(cand)) < 2000:
            await send_to(other, {"type": "call", "kind": "ice", "callId": cid, "candidate": cand})
    else:  # end / reject / busy
        await finish_call(cid, ck, by=uid)
        await send_to(other, {"type": "call", "kind": ck, "callId": cid})
        if uid == c["to"]:
            await send_to(uid, {"type": "call", "kind": "taken", "callId": cid}, exclude=ws)


@app.get("/api/rtc-config")
async def rtc_config(request: Request):
    require_auth(request)
    try:
        custom = json.loads(os.environ.get("ICE_SERVERS_JSON", "null"))
        if isinstance(custom, list) and custom:
            return {"iceServers": custom}
    except Exception:
        pass
    servers = [{"urls": ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"]}]
    urls = [u.strip() for u in os.environ.get("TURN_URLS", "").split(",") if u.strip()]
    if urls:
        servers.append({"urls": urls, "username": os.environ.get("TURN_USERNAME", ""),
                        "credential": os.environ.get("TURN_CREDENTIAL", "")})
    return {"iceServers": servers}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    user = auth_user(ws.query_params.get("token"))
    if not user or not user["username"]:
        await ws.accept()
        await ws.close(code=4001)
        return
    await ws.accept()
    uid = user["id"]
    first = not online.get(uid)
    online.setdefault(uid, set()).add(ws)

    # Bana gelip henüz "iletildi" olmayanları işaretle
    by_sender = {}
    for m in db["messages"]:
        if m["to"] == uid and not m.get("dl"):
            m["dl"] = True
            by_sender.setdefault(m["from"], []).append(m["id"])
    if by_sender:
        mark()
        for sid, ids in by_sender.items():
            await send_to(sid, {"type": "delivered", "ids": ids})
    if first:
        await broadcast_presence(user)

    bucket, cbucket, window_start = 0, 0, time.time()
    try:
        while True:
            raw = await ws.receive_text()
            now = time.time()
            if now - window_start > 10:
                window_start, bucket, cbucket = now, 0, 0
            if len(raw) > 40000:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            kind = data.get("type")
            if kind == "call":
                cbucket += 1
                if cbucket > 300:
                    continue
                await handle_call(user, ws, data)
                continue
            bucket += 1
            if bucket > 60 or len(raw) > 8192:
                continue

            if kind == "message":
                text = str(data.get("text") or "").strip()[:2000]
                img = data.get("img")
                if img is not None:
                    if not (isinstance(img, str) and IMG_RE.match(img) and await image_exists(img.split("/")[-1])):
                        continue
                to = users_by_id.get(data.get("to"))
                if not to or not to["username"] or to["id"] == uid or (not text and not img):
                    continue
                msg = {"id": new_id(), "from": uid, "to": to["id"], "text": text, "ts": now_ms(),
                       "dl": bool(online.get(to["id"])), "rd": False, "del": False}
                if img:
                    msg["img"] = img
                db["messages"].append(msg)
                mark()
                payload = {"type": "message", "msg": msg, "fromUser": pub(user)}
                await send_to(uid, payload)
                await send_to(to["id"], payload)

            elif kind == "typing":
                to = users_by_id.get(data.get("to"))
                if to:
                    await send_to(to["id"], {"type": "typing", "from": uid})

            elif kind == "read":
                peer = data.get("peer")
                ids = []
                for m in db["messages"]:
                    if m["from"] == peer and m["to"] == uid and not m.get("rd"):
                        m["rd"] = True
                        m["dl"] = True
                        ids.append(m["id"])
                if ids:
                    mark()
                payload = {"type": "read", "reader": uid, "peer": peer, "ids": ids}
                await send_to(uid, payload)
                if ids:
                    await send_to(peer, payload)

            elif kind == "delete":
                mid = data.get("id")
                m = next((x for x in reversed(db["messages"]) if x["id"] == mid), None)
                if not m or m["from"] != uid or m.get("del") or m.get("call"):
                    continue
                img = m.pop("img", None)
                m["text"], m["del"] = "", True
                if img and IMG_RE.match(img):
                    await remove_image(img.split("/")[-1])
                mark()
                payload = {"type": "deleted", "id": mid, "from": m["from"], "to": m["to"]}
                await send_to(m["from"], payload)
                await send_to(m["to"], payload)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception as e:
        print("WS hatası:", e, flush=True)
    finally:
        s = online.get(uid)
        if s:
            s.discard(ws)
            if not s:
                online.pop(uid, None)
                user["last"] = now_ms()
                mark()
                for cid, c in list(calls.items()):  # bağlantısı kopan kişinin aramalarını bitir
                    if uid in (c["from"], c["to"]):
                        other = c["to"] if uid == c["from"] else c["from"]
                        await finish_call(cid, "end", by=uid)
                        await send_to(other, {"type": "call", "kind": "end", "callId": cid})
                await broadcast_presence(user)


# Statik dosyalar en sonda: API ve WebSocket yolları önce eşleşir
app.mount("/", StaticFiles(directory=str(BASE / "public"), html=True), name="static")


def banner():
    print(f"\n=== {APP_NAME} ===")
    print(f"Adres: http://localhost:{PORT}")
    for p in ENV_LOADED:
        print(f".env okundu: {p}")
    for w in ENV_WARNINGS:
        print("UYARI:", w)
    mode = mail_mode()
    if mode == "smtp":
        print(f"E-posta: SMTP ({os.environ['SMTP_HOST']}) - kodlar gerçekten mail olarak gider")
    elif mode == "brevo":
        print("E-posta: Brevo API - kodlar gerçekten mail olarak gider")
    elif mode == "gmail":
        print("E-posta: Gmail API - kodlar gerçekten mail olarak gider")
    else:
        print("E-posta: AYARLI DEĞİL. Giriş kodları mail olarak GİTMEZ, bu pencereye yazılır.")
        if not ENV_LOADED:
            print("  .env dosyası bulunamadı. Bu klasörde .env.example dosyasını kopyalayıp adını .env yap ve doldur.")
        else:
            print("  .env okundu ama SMTP_HOST ya da BREVO_API_KEY yok.")
    if os.environ.get("ICE_SERVERS_JSON") or os.environ.get("TURN_URLS"):
        print("Sesli arama: STUN + TURN ayarlı")
    else:
        print("Sesli arama: yalnızca STUN (çoğu ağda çalışır; bazı mobil/şirket ağlarında bağlanmayabilir, TURN gerekir)")
    print("Veriler: Postgres veritabanı (kalıcı)\n" if DATABASE_URL else f"Veriler: {DATA_FILE}\n", flush=True)


def test_mail(to: str):
    banner()
    if mail_mode() is None:
        print("Mail ayarı yok, test yapılamaz.")
        return
    try:
        MAILERS[mail_mode()](to, "123456")
        print(f"BAŞARILI: {to} adresine test maili gönderildi. Gelen kutusu ve spam klasörüne bak.")
    except Exception as e:
        print(f"BAŞARISIZ: {explain_mail_error(e)}\nAyrıntı: {e}")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "testmail":
        test_mail(sys.argv[2])
    else:
        banner()
        uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
