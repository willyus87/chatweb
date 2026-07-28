"""
Chat interno - Backend
-----------------------
Servidor FastAPI que maneja:
  - Registro/login de usuarios con contraseña (hasheada, nunca en texto plano)
  - Mensajería en tiempo real vía WebSocket (chats 1 a 1), autenticada por token de sesión
  - Historial persistente en SQLite
  - Subida de archivos, audios (notas de voz) y GIFs vía HTTP

Para correrlo:
    pip install -r requirements.txt
    uvicorn main:app --host 0.0.0.0 --port 8000

Tus compañeros entran desde el navegador a:
    http://TU_IP_LOCAL:8000
"""

import sqlite3
import uuid
import os
import hashlib
import secrets
import hmac
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Dict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, Form, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "chat.db"
UPLOADS_DIR = BASE_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB por archivo

app = FastAPI()
app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")


# ---------- Protección básica contra floods ----------
# Límite simple por IP: máximo N pedidos HTTP y N intentos de conexión WS por ventana de tiempo.
# No reemplaza un firewall (eso protege contra floods a nivel de red), pero evita que un solo
# compañero sature el proceso mandando muchísimos pedidos/conexiones seguidas.

RATE_WINDOW_SECONDS = 10
RATE_MAX_REQUESTS = 60      # pedidos HTTP por IP cada RATE_WINDOW_SECONDS
RATE_MAX_WS_CONNECTS = 20   # intentos de conexión WebSocket por IP cada RATE_WINDOW_SECONDS

_http_hits: Dict[str, deque] = defaultdict(deque)
_ws_hits: Dict[str, deque] = defaultdict(deque)


def _rate_limited(bucket: Dict[str, deque], key: str, limit: int) -> bool:
    now = time.monotonic()
    hits = bucket[key]
    while hits and now - hits[0] > RATE_WINDOW_SECONDS:
        hits.popleft()
    if len(hits) >= limit:
        return True
    hits.append(now)
    return False


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    client_ip = request.client.host if request.client else "desconocido"
    if _rate_limited(_http_hits, client_ip, RATE_MAX_REQUESTS):
        return JSONResponse({"detail": "Demasiados pedidos, esperá un momento."}, status_code=429)
    return await call_next(request)


# ---------- Base de datos ----------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,
            recipient TEXT NOT NULL,
            msg_type TEXT NOT NULL,       -- text | file | audio | gif | image
            content TEXT,                 -- texto del mensaje, o nombre original del archivo
            file_url TEXT,                -- URL para descargar/reproducir (si aplica)
            timestamp TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'sent'  -- sent | delivered | read
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            avatar_url TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS group_members (
            group_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            PRIMARY KEY (group_id, username)
        )
    """)
    # Migraciones: columnas agregadas en versiones posteriores a la tabla original
    existing_user_cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "avatar_url" not in existing_user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN avatar_url TEXT")

    existing_msg_cols = {row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "status" not in existing_msg_cols:
        conn.execute("ALTER TABLE messages ADD COLUMN status TEXT NOT NULL DEFAULT 'sent'")
    if "group_id" not in existing_msg_cols:
        conn.execute("ALTER TABLE messages ADD COLUMN group_id INTEGER")

    conn.commit()
    conn.close()


init_db()


# ---------- Contraseñas ----------

def hash_password(password: str, salt: str = None) -> str:
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 100_000)
    return f"{salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt, _ = stored.split("$", 1)
    return hmac.compare_digest(hash_password(password, salt), stored)


def user_exists(username: str) -> bool:
    conn = get_db()
    row = conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    return row is not None


def create_user(username: str, password: str):
    conn = get_db()
    conn.execute(
        "INSERT INTO users (username, password_hash) VALUES (?, ?)",
        (username, hash_password(password)),
    )
    conn.commit()
    conn.close()


def check_login(username: str, password: str) -> bool:
    conn = get_db()
    row = conn.execute("SELECT password_hash FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    if row is None:
        return False
    return verify_password(password, row["password_hash"])


# Tokens de sesión en memoria: token -> username (se resetean si se reinicia el servidor)
sessions: Dict[str, str] = {}


def create_session(username: str) -> str:
    token = secrets.token_hex(24)
    sessions[token] = username
    return token


def save_message(sender, recipient, msg_type, content, file_url=None, status="sent"):
    conn = get_db()
    ts = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO messages (sender, recipient, msg_type, content, file_url, timestamp, status) VALUES (?,?,?,?,?,?,?)",
        (sender, recipient, msg_type, content, file_url, ts, status),
    )
    conn.commit()
    msg_id = cur.lastrowid
    conn.close()
    return {
        "id": msg_id,
        "sender": sender,
        "recipient": recipient,
        "type": msg_type,
        "content": content,
        "file_url": file_url,
        "timestamp": ts,
        "status": status,
    }


def get_history(user_a, user_b, limit=200):
    conn = get_db()
    rows = conn.execute(
        """SELECT * FROM messages
           WHERE (sender=? AND recipient=?) OR (sender=? AND recipient=?)
           ORDER BY id ASC LIMIT ?""",
        (user_a, user_b, user_b, user_a, limit),
    ).fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "sender": r["sender"],
            "recipient": r["recipient"],
            "type": r["msg_type"],
            "content": r["content"],
            "file_url": r["file_url"],
            "timestamp": r["timestamp"],
            "status": r["status"],
        }
        for r in rows
    ]


def mark_delivered_on_connect(username: str):
    """Al conectarse alguien, todos los mensajes pendientes que le mandaron pasan a 'entregado'.
    Devuelve la lista de remitentes a avisar (para actualizar sus tics en tiempo real)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT sender FROM messages WHERE recipient=? AND status='sent'", (username,)
    ).fetchall()
    conn.execute("UPDATE messages SET status='delivered' WHERE recipient=? AND status='sent'", (username,))
    conn.commit()
    conn.close()
    return [r["sender"] for r in rows]


def mark_conversation_read(reader: str, other: str):
    """El usuario 'reader' abrió la conversación con 'other': todo lo que 'other' le mandó pasa a 'leído'."""
    conn = get_db()
    changed = conn.execute(
        "SELECT COUNT(*) as c FROM messages WHERE sender=? AND recipient=? AND status!='read'",
        (other, reader),
    ).fetchone()["c"]
    conn.execute(
        "UPDATE messages SET status='read' WHERE sender=? AND recipient=? AND status!='read'",
        (other, reader),
    )
    conn.commit()
    conn.close()
    return changed > 0


# ---------- Grupos ----------

def create_group(name: str, creator: str, members: list) -> dict:
    conn = get_db()
    ts = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO groups (name, created_by, created_at) VALUES (?,?,?)", (name, creator, ts)
    )
    group_id = cur.lastrowid
    all_members = set(members) | {creator}
    for m in all_members:
        conn.execute(
            "INSERT OR IGNORE INTO group_members (group_id, username) VALUES (?,?)", (group_id, m)
        )
    conn.commit()
    conn.close()
    return {"id": group_id, "name": name, "members": sorted(all_members)}


def get_group_members(group_id: int):
    conn = get_db()
    rows = conn.execute(
        "SELECT username FROM group_members WHERE group_id=?", (group_id,)
    ).fetchall()
    conn.close()
    return [r["username"] for r in rows]


def is_group_member(group_id: int, username: str) -> bool:
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM group_members WHERE group_id=? AND username=?", (group_id, username)
    ).fetchone()
    conn.close()
    return row is not None


def add_group_member(group_id: int, username: str):
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO group_members (group_id, username) VALUES (?,?)", (group_id, username)
    )
    conn.commit()
    conn.close()


def get_user_groups(username: str):
    conn = get_db()
    rows = conn.execute(
        """SELECT g.id, g.name FROM groups g
           JOIN group_members gm ON gm.group_id = g.id
           WHERE gm.username = ?""",
        (username,),
    ).fetchall()
    conn.close()
    return [{"id": r["id"], "name": r["name"], "members": get_group_members(r["id"])} for r in rows]


def save_group_message(sender, group_id, msg_type, content, file_url=None):
    conn = get_db()
    ts = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        """INSERT INTO messages (sender, recipient, msg_type, content, file_url, timestamp, status, group_id)
           VALUES (?,?,?,?,?,?,?,?)""",
        (sender, "", msg_type, content, file_url, ts, "sent", group_id),
    )
    conn.commit()
    msg_id = cur.lastrowid
    conn.close()
    return {
        "id": msg_id,
        "sender": sender,
        "group_id": group_id,
        "type": msg_type,
        "content": content,
        "file_url": file_url,
        "timestamp": ts,
        "status": "sent",
    }


def get_group_history(group_id: int, limit=200):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM messages WHERE group_id=? ORDER BY id ASC LIMIT ?", (group_id, limit)
    ).fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "sender": r["sender"],
            "group_id": r["group_id"],
            "type": r["msg_type"],
            "content": r["content"],
            "file_url": r["file_url"],
            "timestamp": r["timestamp"],
            "status": r["status"],
        }
        for r in rows
    ]


def delete_group_history(group_id: int):
    conn = get_db()
    conn.execute("DELETE FROM messages WHERE group_id=?", (group_id,))
    conn.commit()
    conn.close()


def get_all_users():
    """Todos los usuarios registrados, con su foto de perfil si tienen."""
    conn = get_db()
    rows = conn.execute("SELECT username, avatar_url FROM users").fetchall()
    conn.close()
    return {r["username"]: r["avatar_url"] for r in rows}


def set_avatar(username: str, avatar_url: str):
    conn = get_db()
    conn.execute("UPDATE users SET avatar_url=? WHERE username=?", (avatar_url, username))
    conn.commit()
    conn.close()


def delete_conversation(user_a: str, user_b: str):
    conn = get_db()
    conn.execute(
        "DELETE FROM messages WHERE (sender=? AND recipient=?) OR (sender=? AND recipient=?)",
        (user_a, user_b, user_b, user_a),
    )
    conn.commit()
    conn.close()


# ---------- Conexiones activas ----------

class ConnectionManager:
    def __init__(self):
        self.active: Dict[str, WebSocket] = {}

    async def connect(self, username: str, ws: WebSocket):
        await ws.accept()
        self.active[username] = ws

    def register(self, username: str, ws: WebSocket):
        """Registra una conexión ya aceptada (el accept() se hizo antes, para poder
        rechazar con un código de cierre legible por el cliente si el token es inválido)."""
        self.active[username] = ws

    def disconnect(self, username: str):
        self.active.pop(username, None)

    def is_online(self, username: str) -> bool:
        return username in self.active

    async def send_to(self, username: str, payload: dict):
        ws = self.active.get(username)
        if ws is not None:
            await ws.send_json(payload)

    async def broadcast(self, payload: dict):
        for ws in list(self.active.values()):
            await ws.send_json(payload)

    def roster(self):
        users = get_all_users()
        return [
            {
                "username": u,
                "status": "online" if self.is_online(u) else "offline",
                "avatar_url": avatar_url,
            }
            for u, avatar_url in sorted(users.items())
        ]


manager = ConnectionManager()


# ---------- Rutas HTTP ----------

@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


class Credentials(BaseModel):
    username: str
    password: str


@app.post("/api/register")
async def register(creds: Credentials):
    username = creds.username.strip()
    if not username or not creds.password:
        raise HTTPException(400, "Faltan datos")
    if len(creds.password) < 4:
        raise HTTPException(400, "La contraseña debe tener al menos 4 caracteres")
    if user_exists(username):
        raise HTTPException(409, "Ese nombre de usuario ya existe")
    create_user(username, creds.password)
    token = create_session(username)
    return {"ok": True, "token": token, "username": username}


@app.post("/api/login")
async def login(creds: Credentials):
    username = creds.username.strip()
    if not check_login(username, creds.password):
        raise HTTPException(401, "Usuario o contraseña incorrectos")
    token = create_session(username)
    return {"ok": True, "token": token, "username": username}


@app.post("/api/avatar")
async def upload_avatar(file: UploadFile, token: str = Form(...)):
    username = sessions.get(token)
    if username is None:
        raise HTTPException(401, "Sesión inválida, volvé a iniciar sesión")

    ext = os.path.splitext(file.filename or "")[1] or ".jpg"
    saved_name = f"avatar_{uuid.uuid4().hex}{ext}"
    dest = UPLOADS_DIR / saved_name

    total = 0
    try:
        with open(dest, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > 5 * 1024 * 1024:  # 5 MB máximo para fotos de perfil
                    f.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(413, "La imagen supera el tamaño máximo permitido (5 MB)")
                f.write(chunk)
    except HTTPException:
        raise
    except Exception:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "No se pudo procesar la imagen")

    avatar_url = f"/uploads/{saved_name}"
    set_avatar(username, avatar_url)

    await manager.broadcast({"type": "avatar_updated", "username": username, "avatar_url": avatar_url})
    return {"ok": True, "avatar_url": avatar_url}


class DeleteConversation(BaseModel):
    token: str
    with_user: str


@app.post("/api/delete_conversation")
async def delete_conversation_endpoint(payload: DeleteConversation):
    username = sessions.get(payload.token)
    if username is None:
        raise HTTPException(401, "Sesión inválida, volvé a iniciar sesión")

    delete_conversation(username, payload.with_user)

    # avisarle a ambas partes (cada una ve al otro como "with") para que limpien la vista si la tienen abierta
    await manager.send_to(username, {"type": "conversation_deleted", "with": payload.with_user})
    await manager.send_to(payload.with_user, {"type": "conversation_deleted", "with": username})
    return {"ok": True}


class CreateGroup(BaseModel):
    token: str
    name: str
    members: list[str] = []


@app.post("/api/groups/create")
async def create_group_endpoint(payload: CreateGroup):
    username = sessions.get(payload.token)
    if username is None:
        raise HTTPException(401, "Sesión inválida, volvé a iniciar sesión")
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "El grupo necesita un nombre")

    group = create_group(name, username, payload.members)

    # avisarle a todos los miembros (conectados) que hay un grupo nuevo, para que actualicen su lista
    for member in group["members"]:
        await manager.send_to(member, {"type": "group_created", "group": group})

    return {"ok": True, "group": group}


class AddGroupMember(BaseModel):
    token: str
    group_id: int
    username: str


@app.post("/api/groups/add_member")
async def add_group_member_endpoint(payload: AddGroupMember):
    requester = sessions.get(payload.token)
    if requester is None:
        raise HTTPException(401, "Sesión inválida, volvé a iniciar sesión")
    if not is_group_member(payload.group_id, requester):
        raise HTTPException(403, "No pertenecés a ese grupo")
    if not user_exists(payload.username):
        raise HTTPException(404, "Ese usuario no existe")

    add_group_member(payload.group_id, payload.username)
    members = get_group_members(payload.group_id)
    group_name = next(
        (g["name"] for g in get_user_groups(requester) if g["id"] == payload.group_id), ""
    )
    group = {"id": payload.group_id, "name": group_name, "members": members}

    for member in members:
        await manager.send_to(member, {"type": "group_updated", "group": group})

    return {"ok": True, "group": group}


class DeleteGroupHistory(BaseModel):
    token: str
    group_id: int


@app.post("/api/groups/delete_history")
async def delete_group_history_endpoint(payload: DeleteGroupHistory):
    username = sessions.get(payload.token)
    if username is None:
        raise HTTPException(401, "Sesión inválida, volvé a iniciar sesión")
    if not is_group_member(payload.group_id, username):
        raise HTTPException(403, "No pertenecés a ese grupo")

    delete_group_history(payload.group_id)
    for member in get_group_members(payload.group_id):
        await manager.send_to(member, {"type": "group_history_deleted", "group_id": payload.group_id})
    return {"ok": True}


@app.post("/upload")
async def upload(
    file: UploadFile,
    token: str = Form(...),
    msg_type: str = Form(...),  # file | audio | gif | image
    recipient: str = Form(None),
    group_id: int = Form(None),
):
    sender = sessions.get(token)
    if sender is None:
        raise HTTPException(401, "Sesión inválida, volvé a iniciar sesión")
    if not recipient and not group_id:
        raise HTTPException(400, "Falta destinatario o grupo")
    if group_id and not is_group_member(group_id, sender):
        raise HTTPException(403, "No pertenecés a ese grupo")

    ext = os.path.splitext(file.filename or "")[1]
    saved_name = f"{uuid.uuid4().hex}{ext}"
    dest = UPLOADS_DIR / saved_name

    total = 0
    try:
        with open(dest, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    f.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(413, "El archivo supera el tamaño máximo permitido (25 MB)")
                f.write(chunk)
    except HTTPException:
        raise
    except Exception:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "No se pudo procesar el archivo")

    file_url = f"/uploads/{saved_name}"

    if group_id:
        message = save_group_message(sender, group_id, msg_type, file.filename, file_url)
        payload = {"type": "group_message", "message": message}
        for member in get_group_members(group_id):
            await manager.send_to(member, payload)
    else:
        initial_status = "delivered" if manager.is_online(recipient) else "sent"
        message = save_message(sender, recipient, msg_type, file.filename, file_url, status=initial_status)
        payload = {"type": "message", "message": message}
        await manager.send_to(sender, payload)
        if recipient != sender:
            await manager.send_to(recipient, payload)

    return {"ok": True, "message": message}


# ---------- WebSocket ----------

@app.websocket("/ws/{token}")
async def websocket_endpoint(websocket: WebSocket, token: str):
    # Aceptamos siempre primero: así, si rechazamos después (token inválido, rate limit),
    # el navegador recibe un cierre de WebSocket normal con código -- puede reaccionar
    # (por ejemplo, pedir login de nuevo) en vez de ver solo un error de conexión genérico.
    await websocket.accept()

    client_ip = websocket.client.host if websocket.client else "desconocido"
    if _rate_limited(_ws_hits, client_ip, RATE_MAX_WS_CONNECTS):
        await websocket.close(code=4429)  # demasiados intentos de conexión
        return

    username = sessions.get(token)
    if username is None:
        await websocket.close(code=4401)  # token inválido o sesión vencida (server reiniciado)
        return

    manager.register(username, websocket)
    await manager.broadcast({"type": "presence", "username": username, "status": "online"})
    await websocket.send_json({"type": "roster", "users": manager.roster()})
    await websocket.send_json({"type": "groups", "groups": get_user_groups(username)})

    # Los mensajes que le mandaron mientras estaba desconectado ahora se marcan "entregado"
    senders_to_notify = mark_delivered_on_connect(username)
    for s in senders_to_notify:
        await manager.send_to(s, {"type": "delivered_receipt", "to": username})

    try:
        while True:
            try:
                data = await websocket.receive_json()
            except WebSocketDisconnect:
                raise
            except Exception:
                # mensaje que no es JSON válido u otro dato inesperado: se ignora, no se cae la conexión
                continue

            try:
                action = data.get("type") if isinstance(data, dict) else None

                if action == "open":
                    other = data.get("with")
                    if isinstance(other, str):
                        history = get_history(username, other)
                        await websocket.send_json({"type": "history", "with": other, "messages": history})
                        had_unread = mark_conversation_read(username, other)
                        if had_unread:
                            await manager.send_to(other, {"type": "read_receipt", "by": username})

                elif action == "text":
                    to = data.get("to")
                    text = (data.get("text") or "").strip() if isinstance(data.get("text"), str) else ""
                    if not isinstance(to, str) or not to or not text:
                        continue
                    if len(text) > 5000:
                        text = text[:5000]
                    initial_status = "delivered" if manager.is_online(to) else "sent"
                    message = save_message(username, to, "text", text, status=initial_status)
                    payload = {"type": "message", "message": message}
                    await manager.send_to(username, payload)
                    if to != username:
                        await manager.send_to(to, payload)

                elif action == "mark_read":
                    other = data.get("with")
                    if isinstance(other, str):
                        had_unread = mark_conversation_read(username, other)
                        if had_unread:
                            await manager.send_to(other, {"type": "read_receipt", "by": username})

                elif action == "roster_request":
                    await websocket.send_json({"type": "roster", "users": manager.roster()})

                elif action == "open_group":
                    group_id = data.get("group_id")
                    if isinstance(group_id, int) and is_group_member(group_id, username):
                        history = get_group_history(group_id)
                        await websocket.send_json({"type": "group_history", "group_id": group_id, "messages": history})

                elif action == "group_text":
                    group_id = data.get("group_id")
                    text = (data.get("text") or "").strip() if isinstance(data.get("text"), str) else ""
                    if not isinstance(group_id, int) or not text or not is_group_member(group_id, username):
                        continue
                    if len(text) > 5000:
                        text = text[:5000]
                    message = save_group_message(username, group_id, "text", text)
                    payload = {"type": "group_message", "message": message}
                    for member in get_group_members(group_id):
                        await manager.send_to(member, payload)

                # --- Señalización de videollamada (WebRTC) ---
                # El servidor solo reenvía estos mensajes entre los dos usuarios (no se guardan
                # en la base de datos); la conexión de audio/video real es directa entre navegadores.
                elif action == "call_offer":
                    to = data.get("to")
                    sdp = data.get("sdp")
                    if isinstance(to, str):
                        if manager.is_online(to):
                            await manager.send_to(to, {"type": "call_offer", "from": username, "sdp": sdp})
                        else:
                            await websocket.send_json({"type": "call_unavailable", "to": to})

                elif action == "call_answer":
                    to = data.get("to")
                    sdp = data.get("sdp")
                    if isinstance(to, str):
                        await manager.send_to(to, {"type": "call_answer", "from": username, "sdp": sdp})

                elif action == "call_ice":
                    to = data.get("to")
                    candidate = data.get("candidate")
                    if isinstance(to, str):
                        await manager.send_to(to, {"type": "call_ice", "from": username, "candidate": candidate})

                elif action == "call_end":
                    to = data.get("to")
                    if isinstance(to, str):
                        await manager.send_to(to, {"type": "call_end", "from": username})

                elif action == "call_reject":
                    to = data.get("to")
                    if isinstance(to, str):
                        await manager.send_to(to, {"type": "call_reject", "from": username})

            except WebSocketDisconnect:
                raise
            except Exception:
                # cualquier error procesando un mensaje puntual no debe tumbar la conexión completa
                continue

    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(username)
        await manager.broadcast({"type": "presence", "username": username, "status": "offline"})
