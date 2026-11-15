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
FILE_RE = re.compile(r"^/uploads/[0-9a-f]{32}\.(pdf|bin)$")
MAX_FILE = 8 * 1024 * 1024
try:
    TTL_DAYS = float(os.environ.get("MESSAGE_TTL_DAYS", "0") or 0)  # 0 = eski mesajlar silinmez
except ValueError:
    TTL_DAYS = 0
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "").strip().lower()


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
db.setdefault("reports", [])
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
    if os.environ.get("APPSCRIPT_URL") and os.environ.get("APPSCRIPT_SECRET"):
        return "appscript"
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


def _send_appscript(email: str, code: str):
    """Kendi Gmail'inde çalışan Google Apps Script web uygulaması üzerinden mail gönderir (alan adı gerekmez)."""
    body = json.dumps({"secret": os.environ["APPSCRIPT_SECRET"], "to": email, "name": APP_NAME,
                       "subject": f"{APP_NAME} giriş kodun: {code}", "body": _mail_text(code)}).encode()
    req = urllib.request.Request(os.environ["APPSCRIPT_URL"], data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=40).read().decode(errors="replace")  # Google 302 ile yönlendirir, urllib izler
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Apps Script {e.code}: {e.read().decode(errors='replace')[:200]}")
    if '"ok":true' not in resp.replace(" ", ""):
        raise RuntimeError("Apps Script yanıtı beklenen gibi değil: " + resp[:200].replace("\n", " "))


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
    if "yetkisiz" in str(e):
        return "Apps Script 'yetkisiz' dedi: scriptteki SECRET ile APPSCRIPT_SECRET aynı olmalı."
    if "Apps Script" in str(e):
        return "Apps Script yanıt vermedi. Web uygulaması 'Erişimi olan: Herkes' ve 'Şu kullanıcı olarak çalıştır: Ben' ile dağıtıldı mı, URL /exec ile mi bitiyor?"
    if "invalid_grant" in str(e):
        return ("Google yenileme anahtarı geçersiz ya da süresi dolmuş. OAuth uygulamasını 'Production' (Yayında) durumuna "
                "almadıysan anahtar 7 günde ölür; yenisini al ve GMAIL_REFRESH_TOKEN'ı güncelle.")
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return ("Giriş reddedildi. Gmail kullanıyorsan SMTP_PASS normal şifren değil, 16 haneli UYGULAMA ŞİFRESİ olmalı "
                "(https://myaccount.google.com/apppasswords) ve SMTP_USER tam e-posta adresin olmalı.")
    if isinstance(e, (TimeoutError, ConnectionError, OSError)):
        return "Mail sunucusuna bağlanılamadı (internet, güvenlik duvarı ya da barındırma servisi SMTP'yi engelliyor olabilir)."
    return str(e)


MAILERS = {"gmail": _send_gmail, "brevo": _send_brevo, "appscript": _send_appscript, "smtp": _send_smtp}


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
api_hits = {}  # (user_id, anahtar) -> [timestamps]


def api_limited(uid, key, n, sec) -> bool:
    now = time.time()
    arr = [t for t in api_hits.get((uid, key), []) if now - t < sec]
    if len(arr) >= n:
        api_hits[(uid, key)] = arr
        return True
    arr.append(now)
    api_hits[(uid, key)] = arr
    return False


def view(m, uid):
    """Yıldızlar kişiye özel: başkasının yıldızını gösterme."""
    if "st" not in m:
        return m
    v = {k: x for k, x in m.items() if k != "st"}
    if uid in m["st"]:
        v["star"] = True
    return v


def hidden_for(m, uid) -> bool:
    return bool(m.get("bl")) and m["to"] == uid  # engellenmiş kişinin mesajı alıcıya gösterilmez


def is_blocked(by, who) -> bool:
    u = users_by_id.get(by)
    return bool(u) and who in u.get("blocks", [])


def url_in_use(url, exclude=None) -> bool:
    return any(m["id"] != exclude and (m.get("img") == url or (m.get("file") or {}).get("url") == url)
               for m in db["messages"])


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
        n = 0
        while True:
            await asyncio.sleep(1)
            await flush()
            n += 1
            if n % 3600 == 60:  # açılıştan 1 dk sonra, sonra saatte bir
                api_hits.clear()
                try:
                    await cleanup_old()
                except Exception as e:
                    print("Temizlik hatası:", e, flush=True)

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
    if api_limited(user["id"], "su", 60, 60):
        raise HTTPException(429, "Çok hızlı arıyorsun, biraz bekle")
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
    if api_limited(user["id"], "sm", 30, 60):
        raise HTTPException(429, "Çok hızlı arıyorsun, biraz bekle")
    q = q.strip()
    if len(q) < 2:
        return []
    fq, uid, out = fold(q), user["id"], []
    for m in reversed(db["messages"]):
        if m.get("del") or not m.get("text"):
            continue
        if m["from"] != uid and m["to"] != uid:
            continue
        if hidden_for(m, uid):
            continue
        if fq in fold(m["text"]):
            peer = users_by_id.get(m["to"] if m["from"] == uid else m["from"])
            if peer:
                out.append({"peer": pub(peer), "msg": view(m, uid)})
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
        if hidden_for(m, uid):
            continue
        peer = m["to"] if m["from"] == uid else m["from"]
        last[peer] = view(m, uid)
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
    msgs = [view(m, mid) for m in db["messages"]
            if ((m["from"] == mid and m["to"] == peer) or (m["from"] == peer and m["to"] == mid))
            and not hidden_for(m, mid)]
    p = users_by_id.get(peer)
    return {"peer": pub(p) if p else None, "messages": msgs[-max(1, min(limit, 2000)):],
            "blocked": is_blocked(mid, peer), "blockedMe": is_blocked(peer, mid)}


@app.get("/api/media/{peer}")
async def api_media(peer: str, request: Request):
    user = require_auth(request)
    mid = user["id"]
    out = [view(m, mid) for m in db["messages"]
           if ((m["from"] == mid and m["to"] == peer) or (m["from"] == peer and m["to"] == mid))
           and not m.get("del") and (m.get("img") or m.get("file")) and not hidden_for(m, mid)]
    return out[::-1][:500]


@app.get("/api/starred")
async def api_starred(request: Request):
    user = require_auth(request)
    uid, out = user["id"], []
    for m in reversed(db["messages"]):
        if uid in m.get("st", []) and not m.get("del") and (m["from"] == uid or m["to"] == uid) \
                and not hidden_for(m, uid):
            peer = users_by_id.get(m["to"] if m["from"] == uid else m["from"])
            if peer:
                out.append({"peer": pub(peer), "msg": view(m, uid)})
                if len(out) >= 200:
                    break
    return out


@app.get("/api/blocks")
async def api_blocks(request: Request):
    user = require_auth(request)
    return [pub(users_by_id[i]) for i in user.get("blocks", []) if i in users_by_id]


@app.post("/api/blocks/{pid}")
async def block_user(pid: str, request: Request):
    user = require_auth(request)
    if api_limited(user["id"], "bl", 30, 60):
        raise HTTPException(429, "Çok hızlı, biraz bekle")
    if pid not in users_by_id or pid == user["id"]:
        raise HTTPException(404, "Kullanıcı bulunamadı")
    bl = user.setdefault("blocks", [])
    if pid not in bl:
        bl.append(pid)
        mark()
    return {"ok": True}


@app.delete("/api/blocks/{pid}")
async def unblock_user(pid: str, request: Request):
    user = require_auth(request)
    if pid in user.get("blocks", []):
        user["blocks"].remove(pid)
        mark()
    return {"ok": True}


@app.post("/api/report")
async def report_user(request: Request):
    user = require_auth(request)
    if api_limited(user["id"], "rp", 5, 3600):
        raise HTTPException(429, "Çok fazla şikayet gönderdin, sonra tekrar dene")
    d = await body_of(request)
    peer = users_by_id.get(d.get("peer"))
    if not peer or peer["id"] == user["id"]:
        raise HTTPException(404, "Kullanıcı bulunamadı")
    ctx = [str(m.get("text") or ("[fotoğraf]" if m.get("img") else "[dosya]" if m.get("file") else ""))[:200]
           for m in db["messages"] if m["from"] == peer["id"] and m["to"] == user["id"] and not m.get("del")][-5:]
    db["reports"].append({"id": new_id(), "by": user["id"], "peer": peer["id"], "ts": now_ms(),
                          "reason": str(d.get("reason") or "")[:300], "ctx": ctx})
    del db["reports"][:-1000]
    mark()
    print(f"ŞİKAYET: @{user['username']} -> @{peer['username']}", flush=True)
    return {"ok": True}


@app.get("/api/admin/reports")
async def admin_reports(request: Request):
    user = require_auth(request)
    if not ADMIN_EMAIL or user["email"] != ADMIN_EMAIL:
        raise HTTPException(404, "Bulunamadı")
    nm = lambda i: (users_by_id.get(i) or {}).get("username", "?")
    return [{**r, "by": nm(r["by"]), "peer": nm(r["peer"])} for r in reversed(db["reports"])]


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


@app.post("/api/upload-file")
async def upload_file(request: Request):
    user = require_auth(request)
    if api_limited(user["id"], "uf", 15, 600):
        raise HTTPException(429, "Çok fazla dosya, biraz bekle")
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_FILE:
        raise HTTPException(413, "Dosya çok büyük (en fazla 8 MB)")
    body = await request.body()
    if len(body) > MAX_FILE:
        raise HTTPException(413, "Dosya çok büyük (en fazla 8 MB)")
    if body[:5] == b"%PDF-":
        ext = "pdf"
    elif body[:4] == b"PK\x03\x04":  # docx / xlsx / pptx / zip
        ext = "bin"
    else:
        raise HTTPException(400, "Sadece PDF, Word, Excel, PowerPoint ve ZIP gönderilebilir")
    name = f"{secrets.token_hex(16)}.{ext}"
    await asyncio.to_thread((UPLOAD_DIR / name).write_bytes, body)
    if DATABASE_URL:
        try:
            await asyncio.to_thread(pg_put_file, name, body)
        except Exception as e:
            print("Dosya veritabanına yazılamadı:", e, flush=True)
            (UPLOAD_DIR / name).unlink(missing_ok=True)
            raise HTTPException(500, "Dosya kaydedilemedi, tekrar dene")
    return {"url": f"/uploads/{name}", "size": len(body)}


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


IMG_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(jpg|png|gif|webp|pdf|bin)$")
IMG_MIME = {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp",
            "pdf": "application/pdf", "bin": "application/octet-stream"}
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
    ext = name.rsplit(".", 1)[1]
    mime = IMG_MIME[ext]
    hdr = dict(IMG_CACHE)
    if ext in ("pdf", "bin"):
        hdr["Content-Disposition"] = "attachment"
    p = UPLOAD_DIR / name
    if p.is_file():
        return FileResponse(p, media_type=mime, headers=hdr)
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
            return Response(data, media_type=mime, headers=hdr)
    raise HTTPException(404, "Bulunamadı")


async def cleanup_old():
    """MESSAGE_TTL_DAYS ayarlıysa yıldızsız eski mesajları ve dosyalarını siler."""
    if TTL_DAYS <= 0:
        return
    cutoff = now_ms() - int(TTL_DAYS * 86400000)
    keep = [m for m in db["messages"] if m["ts"] >= cutoff or m.get("st")]
    if len(keep) == len(db["messages"]):
        return
    kept_urls = set()
    for m in keep:
        if m.get("img"):
            kept_urls.add(m["img"])
        if m.get("file"):
            kept_urls.add(m["file"].get("url"))
    gone = {m["id"] for m in db["messages"]} - {m["id"] for m in keep}
    for m in db["messages"]:
        if m["id"] in gone:
            for u in (m.get("img"), (m.get("file") or {}).get("url")):
                if u and u not in kept_urls and (IMG_RE.match(u) or FILE_RE.match(u)):
                    kept_urls.add(u)  # bir kez sil
                    await remove_image(u.split("/")[-1])
    db["messages"][:] = keep
    mark()
    print(f"Temizlik: {len(gone)} eski mesaj silindi", flush=True)


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
        if is_blocked(uid, to["id"]) or is_blocked(to["id"], uid):
            await send_to(uid, {"type": "call", "kind": "denied", "callId": cid})
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
        if m["to"] == uid and not m.get("dl") and not m.get("bl"):
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
            if len(raw) > 60000:
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
            if bucket > 60 or len(raw) > 24000:
                continue

            if kind == "message":
                text = str(data.get("text") or "").strip()[:4000]
                img = data.get("img")
                if img is not None:
                    if not (isinstance(img, str) and IMG_RE.match(img) and await image_exists(img.split("/")[-1])):
                        continue
                fl = data.get("file")
                if fl is not None:
                    if not (isinstance(fl, dict) and isinstance(fl.get("url"), str) and FILE_RE.match(fl["url"])
                            and await image_exists(fl["url"].split("/")[-1])):
                        continue
                    sz = fl.get("size")
                    fl = {"url": fl["url"], "name": str(fl.get("name") or "dosya")[:100],
                          "size": sz if isinstance(sz, int) and 0 <= sz <= MAX_FILE else 0}
                loc = data.get("loc")
                if loc is not None:
                    try:
                        la, lo = float(loc["lat"]), float(loc["lng"])
                        assert -90 <= la <= 90 and -180 <= lo <= 180
                    except Exception:
                        continue
                    loc = {"lat": round(la, 6), "lng": round(lo, 6)}
                to = users_by_id.get(data.get("to"))
                if not to or not to["username"] or to["id"] == uid or not (text or img or fl or loc):
                    continue
                if is_blocked(uid, to["id"]):
                    await send_to(uid, {"type": "error", "msg": "Bu kişiyi engelledin. Mesaj göndermek için engeli kaldır."})
                    continue
                blocked_me = is_blocked(to["id"], uid)
                msg = {"id": new_id(), "from": uid, "to": to["id"], "text": text, "ts": now_ms(),
                       "dl": bool(online.get(to["id"])) and not blocked_me, "rd": False, "del": False}
                if img:
                    msg["img"] = img
                    w, h = data.get("w"), data.get("h")
                    if isinstance(w, int) and isinstance(h, int) and 0 < w <= 10000 and 0 < h <= 10000:
                        msg["w"], msg["h"] = w, h
                if fl:
                    msg["file"] = fl
                if loc:
                    msg["loc"] = loc
                rid = data.get("re")
                if isinstance(rid, str):
                    r = next((x for x in reversed(db["messages"]) if x["id"] == rid), None)
                    if r and not r.get("del") and not r.get("call") and not hidden_for(r, uid) \
                            and {r["from"], r["to"]} == {uid, to["id"]}:
                        snip = (r.get("text") or ("📷 Fotoğraf" if r.get("img") else
                                ("📄 " + r["file"]["name"]) if r.get("file") else "📍 Konum"))[:80]
                        msg["re"] = {"id": r["id"], "t": snip, "f": r["from"]}
                if blocked_me:
                    msg["bl"] = True  # gönderen görür, engelleyen görmez
                db["messages"].append(msg)
                mark()
                await send_to(uid, {"type": "message", "msg": msg, "fromUser": pub(user)})
                if not blocked_me:
                    await send_to(to["id"], {"type": "message", "msg": msg, "fromUser": pub(user)})

            elif kind == "react":
                m = next((x for x in reversed(db["messages"]) if x["id"] == data.get("id")), None)
                e = str(data.get("e") or "")[:8]
                if not m or m.get("del") or m.get("call") or uid not in (m["from"], m["to"]) or hidden_for(m, uid):
                    continue
                if e and (any(ch.isalnum() for ch in e) or is_blocked(m["from"] if m["to"] == uid else m["to"], uid)):
                    continue
                rx = m.setdefault("rx", {})
                if e and rx.get(uid) != e:
                    rx[uid] = e
                else:
                    rx.pop(uid, None)
                if not rx:
                    m.pop("rx", None)
                mark()
                payload = {"type": "reacted", "id": m["id"], "rx": m.get("rx", {})}
                await send_to(m["from"], payload)
                if not m.get("bl"):
                    await send_to(m["to"], payload)

            elif kind == "star":
                m = next((x for x in reversed(db["messages"]) if x["id"] == data.get("id")), None)
                if not m or m.get("del") or uid not in (m["from"], m["to"]) or hidden_for(m, uid):
                    continue
                st = m.setdefault("st", [])
                on = bool(data.get("on"))
                if on and uid not in st:
                    st.append(uid)
                elif not on and uid in st:
                    st.remove(uid)
                if not st:
                    m.pop("st", None)
                mark()
                await send_to(uid, {"type": "starred", "id": m["id"], "on": on})

            elif kind == "typing":
                to = users_by_id.get(data.get("to"))
                if to and not is_blocked(to["id"], uid) and not is_blocked(uid, to["id"]):
                    await send_to(to["id"], {"type": "typing", "from": uid})

            elif kind == "read":
                peer = data.get("peer")
                ids = []
                for m in db["messages"]:
                    if m["from"] == peer and m["to"] == uid and not m.get("rd") and not m.get("bl"):
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
                fl = m.pop("file", None)
                for k in ("loc", "re", "rx", "st", "w", "h"):
                    m.pop(k, None)
                m["text"], m["del"] = "", True
                for u in (img, (fl or {}).get("url")):
                    if u and (IMG_RE.match(u) or FILE_RE.match(u)) and not url_in_use(u, mid):
                        await remove_image(u.split("/")[-1])
                mark()
                payload = {"type": "deleted", "id": mid, "from": m["from"], "to": m["to"]}
                await send_to(m["from"], payload)
                if not m.get("bl"):
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
    elif mode == "appscript":
        print("E-posta: Google Apps Script (Gmail'in) - kodlar gerçekten mail olarak gider")
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
