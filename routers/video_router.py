import os
import json
import uuid
import cv2
import shutil
import secrets
import time
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any

from fastapi import APIRouter, UploadFile, File, Request, Form, Body, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from filelock import FileLock

router = APIRouter(prefix="/videos", tags=["Videos"])
templates = Jinja2Templates(directory="templates")
security = HTTPBasic()

# --- CONFIGURACIÓN ---
UPLOAD_DIR = Path("uploads")
THUMB_DIR = Path("thumbnails")
DB_FILE = Path("db.json")

# Lock único para TODO lo que toque archivos (db + guard)
LOCK_FILE = Path("db.json.lock")

# Guard para evitar duplicados por concurrencia / clientes sin cookies
VIEW_GUARD_FILE = Path("view_guard.json")

# Zona horaria Bogota (sin DST)
BOGOTA_TZ = timezone(timedelta(hours=-5))

# TTL del guard (segundos). 48h recomendado para cubrir “mismo día” + márgen.
VIEW_GUARD_TTL_SECONDS = 48 * 3600

# CREDENCIALES DE ADMIN (ideal: usar env vars)
ADMIN_USER = "andrew19f"
ADMIN_PASS = "1003.Pazw"

UPLOAD_DIR.mkdir(exist_ok=True)
THUMB_DIR.mkdir(exist_ok=True)


# -----------------------------
# Helpers de lectura/escritura
# -----------------------------
def _read_json_file_nolock(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json_file_nolock(path: Path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


def load_db():
    """Lectura segura de DB"""
    with FileLock(LOCK_FILE):
        return _read_json_file_nolock(DB_FILE, [])


def save_db(data):
    """Escritura segura de DB"""
    with FileLock(LOCK_FILE):
        _write_json_file_nolock(DB_FILE, data)


# -----------------------------
# Seguridad Basic Auth
# -----------------------------
def get_current_username(credentials: HTTPBasicCredentials = Depends(security)):
    correct_user = secrets.compare_digest(credentials.username, ADMIN_USER)
    correct_pass = secrets.compare_digest(credentials.password, ADMIN_PASS)

    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales incorrectas",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# -----------------------------
# Thumbnail
# -----------------------------
def generate_thumbnail(video_path: str, thumb_path: str):
    cap = cv2.VideoCapture(video_path)
    try:
        if not cap.isOpened():
            return

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total_frames > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, total_frames // 2)
        else:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

        ret, frame = cap.read()
        if ret and frame is not None:
            cv2.imwrite(thumb_path, frame)
    finally:
        cap.release()


# -----------------------------
# Fingerprint (respaldo)
# -----------------------------
def _get_client_ip(request: Request) -> str:
    """
    Saca IP real si Nginx envía X-Forwarded-For.
    Si no, cae a request.client.host.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # Primer IP de la cadena
        return xff.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "0.0.0.0"


def _client_fingerprint(request: Request) -> str:
    """
    Fingerprint estable (no perfecto, pero útil para dedupe):
    IP + User-Agent + Accept-Language.
    """
    ip = _get_client_ip(request)
    ua = request.headers.get("user-agent", "")
    al = request.headers.get("accept-language", "")
    raw = f"{ip}|{ua}|{al}".encode("utf-8", errors="ignore")
    return hashlib.sha256(raw).hexdigest()[:24]


def _today_bogota_str() -> str:
    return datetime.now(BOGOTA_TZ).date().isoformat()


def _is_https_request(request: Request) -> bool:
    """
    Útil para set_cookie(secure=True) cuando vas por HTTPS detrás de Nginx.
    """
    xfproto = request.headers.get("x-forwarded-proto", "").lower()
    if xfproto == "https":
        return True
    return request.url.scheme == "https"


# -----------------------------
# Conteo atómico y dedupe por día
# -----------------------------
def increment_view_atomic_once_per_day(video_id: str, fingerprint: str, today_str: str) -> Optional[int]:
    """
    Cuenta 1 vista SOLO si este fingerprint no ha contado HOY para este video.
    Todo ocurre dentro de un único lock -> seguro con 8 workers.

    Retorna el nuevo total de views si contó, o None si NO contó.
    """
    now_ts = int(time.time())

    with FileLock(LOCK_FILE):
        # 1) Leer DB
        db = _read_json_file_nolock(DB_FILE, [])
        vid = None
        for v in db:
            if v.get("id") == video_id:
                vid = v
                break
        if not vid:
            return None

        # Asegurar campo views
        current_views = int(vid.get("views", 0) or 0)

        # 2) Leer guard
        guard: Dict[str, Any] = _read_json_file_nolock(VIEW_GUARD_FILE, {})

        # 3) Limpiar guard viejo (TTL)
        # guard estructura: { "<video_id>|<fingerprint>": {"date": "YYYY-MM-DD", "ts": 123} }
        cutoff = now_ts - VIEW_GUARD_TTL_SECONDS
        if guard:
            to_del = []
            for k, info in guard.items():
                ts = int((info or {}).get("ts", 0) or 0)
                if ts < cutoff:
                    to_del.append(k)
            for k in to_del:
                guard.pop(k, None)

        # 4) Verificar si ya contó hoy
        key = f"{video_id}|{fingerprint}"
        info = guard.get(key)
        if info and info.get("date") == today_str:
            # Ya contado hoy -> no incrementa
            # Igual actualizamos ts para extender TTL un poco con actividad
            guard[key] = {"date": today_str, "ts": now_ts}
            _write_json_file_nolock(VIEW_GUARD_FILE, guard)
            return None

        # 5) Contar + guardar guard
        new_views = current_views + 1
        vid["views"] = new_views
        guard[key] = {"date": today_str, "ts": now_ts}

        # 6) Persistir DB + guard
        _write_json_file_nolock(DB_FILE, db)
        _write_json_file_nolock(VIEW_GUARD_FILE, guard)

        return new_views


# -----------------------------
# RUTAS
# -----------------------------

# 1. MANAGER (PROTEGIDO)
@router.get("/manager", response_class=HTMLResponse)
async def video_manager(request: Request, username: str = Depends(get_current_username)):
    videos = load_db()
    for v in videos:
        if "views" not in v:
            v["views"] = 0
    return templates.TemplateResponse("manager.html", {"request": request, "videos": videos, "user": username})


# 2. WATCH (PÚBLICO - CORREGIDO)
@router.get("/watch/{video_id}", response_class=HTMLResponse)
async def watch_video(request: Request, video_id: str):
    # Cargar datos para mostrar video (lectura rápida)
    videos = load_db()
    video = next((v for v in videos if v.get("id") == video_id), None)
    if not video:
        return HTMLResponse("<h1>Video no encontrado</h1>", status_code=404)

    today_str = _today_bogota_str()

    # Cookie por video, valor = fecha (YYYY-MM-DD)
    cookie_name = f"viewed_{video_id}"
    cookie_val = request.cookies.get(cookie_name)

    # Preparar response
    response = templates.TemplateResponse("watch.html", {"request": request, "video": video})

    # Si cookie ya dice "hoy", no intentamos contar (barato)
    counted = False
    if cookie_val != today_str:
        fp = _client_fingerprint(request)
        new_views = increment_view_atomic_once_per_day(video_id, fp, today_str)
        if new_views is not None:
            counted = True
            # Para que el template muestre el número actualizado en esta misma carga:
            try:
                video["views"] = new_views
            except Exception:
                pass

    # Setear/actualizar cookie SIEMPRE al día actual para bloquear re-conteo
    # (aunque no haya contado por guard, igual evita que el navegador lo intente otra vez)
    response.set_cookie(
        key=cookie_name,
        value=today_str,
        max_age=400 * 24 * 3600,   # ~400 días, pero el valor cambia cada día que visite
        samesite="lax",
        secure=_is_https_request(request),
        httponly=False,
    )

    return response


# 3. UPLOAD (PROTEGIDO)
@router.post("/upload")
async def upload_video(
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    username: str = Depends(get_current_username),
):
    unique_hash = uuid.uuid4().hex[:5]
    if not title:
        title = unique_hash

    extension = (file.filename.split(".")[-1] if file.filename and "." in file.filename else "mp4").lower()
    new_filename = f"{unique_hash}.{extension}"
    thumb_filename = f"{unique_hash}.jpg"

    video_path = UPLOAD_DIR / new_filename
    thumb_path = THUMB_DIR / thumb_filename

    # Guardar video
    with open(video_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # Generar miniatura
    try:
        generate_thumbnail(str(video_path), str(thumb_path))
    except Exception:
        pass

    # Insertar en DB al inicio (atómico)
    with FileLock(LOCK_FILE):
        db = _read_json_file_nolock(DB_FILE, [])
        new_entry = {
            "id": unique_hash,
            "title": title,
            "filename": new_filename,
            "thumb": thumb_filename,
            "twitter_link": "",
            "original_name": file.filename,
            "views": 0,
            "timestamp": os.path.getmtime(video_path),
        }
        db.insert(0, new_entry)
        _write_json_file_nolock(DB_FILE, db)

    return JSONResponse(content={"message": "Subido con éxito", "video": new_entry})


# 4. UPDATE SOCIAL (PROTEGIDO)
@router.post("/update_social")
async def update_social_link(data: dict = Body(...), username: str = Depends(get_current_username)):
    video_id = data.get("id")
    link = data.get("link", "")

    with FileLock(LOCK_FILE):
        db = _read_json_file_nolock(DB_FILE, [])
        found = False
        for video in db:
            if video.get("id") == video_id:
                video["twitter_link"] = link
                found = True
                break
        if found:
            _write_json_file_nolock(DB_FILE, db)

    return JSONResponse(content={"status": "ok", "link": link})


# 5. DELETE (PROTEGIDO)
@router.get("/delete/{video_id}")
async def delete_video(video_id: str, username: str = Depends(get_current_username)):
    filename_to_del = None
    thumb_to_del = None

    with FileLock(LOCK_FILE):
        db = _read_json_file_nolock(DB_FILE, [])
        new_db = []
        for v in db:
            if v.get("id") == video_id:
                filename_to_del = v.get("filename")
                thumb_to_del = v.get("thumb")
            else:
                new_db.append(v)
        _write_json_file_nolock(DB_FILE, new_db)

        # También limpiamos entradas del guard de ese video (opcional)
        guard = _read_json_file_nolock(VIEW_GUARD_FILE, {})
        if guard:
            prefix = f"{video_id}|"
            keys = [k for k in guard.keys() if k.startswith(prefix)]
            for k in keys:
                guard.pop(k, None)
            _write_json_file_nolock(VIEW_GUARD_FILE, guard)

    # Borrar archivos fuera del lock
    if filename_to_del:
        try:
            os.remove(UPLOAD_DIR / filename_to_del)
        except Exception:
            pass
    if thumb_to_del:
        try:
            os.remove(THUMB_DIR / thumb_to_del)
        except Exception:
            pass

    return JSONResponse(content={"status": "deleted"})
