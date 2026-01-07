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
from typing import Optional, Dict, Any, List

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
LOCK_FILE = Path("db.json.lock")
VIEW_GUARD_FILE = Path("view_guard.json")

# Zona horaria Bogota (sin DST)
BOGOTA_TZ = timezone(timedelta(hours=-5))

# TTL del guard (segundos)
VIEW_GUARD_TTL_SECONDS = 48 * 3600

# CREDENCIALES DE ADMIN (ideal: usar env vars)
ADMIN_USER = "andrew19f"
ADMIN_PASS = "1003.Pazw"

UPLOAD_DIR.mkdir(exist_ok=True)
THUMB_DIR.mkdir(exist_ok=True)

DEFAULT_PROJECT_ID = "general"


# -----------------------------
# Helpers JSON + migración DB
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


def _now_ts() -> float:
    return time.time()


def _ensure_db_schema(db_raw: Any) -> Dict[str, Any]:
    """
    Nuevo esquema:
      {
        "projects": [{"id": "general", "name": "General", "created_at": <ts>}...],
        "videos": [{...,"project_id":"general"}...]
      }

    Migra automáticamente si db.json era una lista antigua de videos.
    """
    # Caso 1: ya es dict con keys
    if isinstance(db_raw, dict) and "projects" in db_raw and "videos" in db_raw:
        # asegurar default project existe
        projects = db_raw.get("projects") or []
        if not any(p.get("id") == DEFAULT_PROJECT_ID for p in projects):
            projects.insert(0, {"id": DEFAULT_PROJECT_ID, "name": "General", "created_at": _now_ts()})
        db_raw["projects"] = projects

        # asegurar project_id en videos
        videos = db_raw.get("videos") or []
        for v in videos:
            if not v.get("project_id"):
                v["project_id"] = DEFAULT_PROJECT_ID
            if "views" not in v:
                v["views"] = 0
            if "timestamp" not in v:
                v["timestamp"] = 0
        db_raw["videos"] = videos
        return db_raw

    # Caso 2: esquema viejo -> lista de videos
    if isinstance(db_raw, list):
        videos_old = db_raw
        for v in videos_old:
            v["project_id"] = DEFAULT_PROJECT_ID
            if "views" not in v:
                v["views"] = 0
            if "timestamp" not in v:
                v["timestamp"] = v.get("timestamp", 0) or 0

        new_db = {
            "projects": [{"id": DEFAULT_PROJECT_ID, "name": "General", "created_at": _now_ts()}],
            "videos": videos_old
        }
        return new_db

    # Caso 3: vacío/corrupto
    return {
        "projects": [{"id": DEFAULT_PROJECT_ID, "name": "General", "created_at": _now_ts()}],
        "videos": []
    }


def load_db() -> Dict[str, Any]:
    """Lectura segura + migración si hace falta"""
    with FileLock(LOCK_FILE):
        raw = _read_json_file_nolock(DB_FILE, {"projects": [], "videos": []})
        db = _ensure_db_schema(raw)
        # si migró desde lista u otra cosa, persistimos para que quede fijo
        _write_json_file_nolock(DB_FILE, db)
        return db


def save_db(db: Dict[str, Any]) -> None:
    """Escritura segura DB"""
    with FileLock(LOCK_FILE):
        _write_json_file_nolock(DB_FILE, db)


def _find_project(db: Dict[str, Any], project_id: str) -> Optional[Dict[str, Any]]:
    for p in db.get("projects") or []:
        if p.get("id") == project_id:
            return p
    return None


def _all_projects_sorted(db: Dict[str, Any]) -> List[Dict[str, Any]]:
    projects = list(db.get("projects") or [])
    # default primero, luego por created_at
    projects.sort(key=lambda p: (0 if p.get("id") == DEFAULT_PROJECT_ID else 1, float(p.get("created_at", 0) or 0)))
    return projects


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
# Fingerprint + HTTPS helper
# -----------------------------
def _get_client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "0.0.0.0"


def _client_fingerprint(request: Request) -> str:
    ip = _get_client_ip(request)
    ua = request.headers.get("user-agent", "")
    al = request.headers.get("accept-language", "")
    raw = f"{ip}|{ua}|{al}".encode("utf-8", errors="ignore")
    return hashlib.sha256(raw).hexdigest()[:24]


def _today_bogota_str() -> str:
    return datetime.now(BOGOTA_TZ).date().isoformat()


def _is_https_request(request: Request) -> bool:
    xfproto = request.headers.get("x-forwarded-proto", "").lower()
    if xfproto == "https":
        return True
    return request.url.scheme == "https"


# -----------------------------
# Conteo atómico y dedupe por día
# -----------------------------
def increment_view_atomic_once_per_day(video_id: str, fingerprint: str, today_str: str) -> Optional[int]:
    now_ts = int(time.time())

    with FileLock(LOCK_FILE):
        db_raw = _read_json_file_nolock(DB_FILE, {"projects": [], "videos": []})
        db = _ensure_db_schema(db_raw)

        vid = None
        for v in db.get("videos", []):
            if v.get("id") == video_id:
                vid = v
                break
        if not vid:
            _write_json_file_nolock(DB_FILE, db)
            return None

        current_views = int(vid.get("views", 0) or 0)

        guard: Dict[str, Any] = _read_json_file_nolock(VIEW_GUARD_FILE, {})

        cutoff = now_ts - VIEW_GUARD_TTL_SECONDS
        if guard:
            to_del = []
            for k, info in guard.items():
                ts = int((info or {}).get("ts", 0) or 0)
                if ts < cutoff:
                    to_del.append(k)
            for k in to_del:
                guard.pop(k, None)

        key = f"{video_id}|{fingerprint}"
        info = guard.get(key)
        if info and info.get("date") == today_str:
            guard[key] = {"date": today_str, "ts": now_ts}
            _write_json_file_nolock(VIEW_GUARD_FILE, guard)
            _write_json_file_nolock(DB_FILE, db)
            return None

        new_views = current_views + 1
        vid["views"] = new_views
        guard[key] = {"date": today_str, "ts": now_ts}

        _write_json_file_nolock(DB_FILE, db)
        _write_json_file_nolock(VIEW_GUARD_FILE, guard)

        return new_views


# -----------------------------
# RUTAS
# -----------------------------

# 1) MANAGER (PROTEGIDO)
@router.get("/manager", response_class=HTMLResponse)
async def video_manager(request: Request, username: str = Depends(get_current_username)):
    db = load_db()
    projects = _all_projects_sorted(db)
    videos = db.get("videos", [])

    # agrupar por proyectos
    by_project: Dict[str, List[Dict[str, Any]]] = {}
    for v in videos:
        pid = v.get("project_id") or DEFAULT_PROJECT_ID
        by_project.setdefault(pid, []).append(v)

    # mantener orden interno (por timestamp desc)
    for pid in by_project:
        by_project[pid].sort(key=lambda x: float(x.get("timestamp", 0) or 0), reverse=True)

    projects_with_videos = []
    for p in projects:
        pid = p.get("id")
        projects_with_videos.append({
            "project": p,
            "videos": by_project.get(pid, [])
        })

    total_videos = len(videos)

    return templates.TemplateResponse(
        "manager.html",
        {
            "request": request,
            "projects": projects,
            "projects_with_videos": projects_with_videos,
            "total_videos": total_videos,
            "user": username
        }
    )


# 2) WATCH (PÚBLICO)
@router.get("/watch/{video_id}", response_class=HTMLResponse)
async def watch_video(request: Request, video_id: str):
    db = load_db()
    videos = db.get("videos", [])
    video = next((v for v in videos if v.get("id") == video_id), None)
    if not video:
        return HTMLResponse("<h1>Video no encontrado</h1>", status_code=404)

    today_str = _today_bogota_str()

    cookie_name = f"viewed_{video_id}"
    cookie_val = request.cookies.get(cookie_name)

    response = templates.TemplateResponse("watch.html", {"request": request, "video": video})

    if cookie_val != today_str:
        fp = _client_fingerprint(request)
        new_views = increment_view_atomic_once_per_day(video_id, fp, today_str)
        if new_views is not None:
            try:
                video["views"] = new_views
            except Exception:
                pass

    response.set_cookie(
        key=cookie_name,
        value=today_str,
        max_age=400 * 24 * 3600,
        samesite="lax",
        secure=_is_https_request(request),
        httponly=False,
    )
    return response


# 3) CREATE PROJECT (PROTEGIDO)
@router.post("/projects/create")
async def create_project(data: dict = Body(...), username: str = Depends(get_current_username)):
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Nombre requerido")

    # id corto
    new_id = uuid.uuid4().hex[:8]

    with FileLock(LOCK_FILE):
        raw = _read_json_file_nolock(DB_FILE, {"projects": [], "videos": []})
        db = _ensure_db_schema(raw)

        # evitar duplicar nombre exacto (opcional)
        for p in db.get("projects", []):
            if (p.get("name") or "").strip().lower() == name.lower():
                raise HTTPException(status_code=400, detail="Ya existe un proyecto con ese nombre")

        db["projects"].append({"id": new_id, "name": name, "created_at": _now_ts()})
        _write_json_file_nolock(DB_FILE, db)

    return JSONResponse({"status": "ok", "project": {"id": new_id, "name": name}})


# 4) DELETE PROJECT (PROTEGIDO) -> borra proyecto + sus videos + archivos
@router.post("/projects/delete")
async def delete_project(data: dict = Body(...), username: str = Depends(get_current_username)):
    project_id = (data.get("project_id") or "").strip()
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id requerido")
    if project_id == DEFAULT_PROJECT_ID:
        raise HTTPException(status_code=400, detail="No se puede borrar el proyecto base")

    files_to_delete = []
    thumbs_to_delete = []
    video_ids_deleted = []

    with FileLock(LOCK_FILE):
        raw = _read_json_file_nolock(DB_FILE, {"projects": [], "videos": []})
        db = _ensure_db_schema(raw)

        project = _find_project(db, project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Proyecto no existe")

        # quitar proyecto
        db["projects"] = [p for p in db.get("projects", []) if p.get("id") != project_id]

        # sacar videos de ese proyecto
        new_videos = []
        for v in db.get("videos", []):
            if (v.get("project_id") or DEFAULT_PROJECT_ID) == project_id:
                video_ids_deleted.append(v.get("id"))
                files_to_delete.append(v.get("filename"))
                thumbs_to_delete.append(v.get("thumb"))
            else:
                new_videos.append(v)

        db["videos"] = new_videos
        _write_json_file_nolock(DB_FILE, db)

        # limpiar guard (opcional)
        guard = _read_json_file_nolock(VIEW_GUARD_FILE, {})
        if guard and video_ids_deleted:
            to_remove = []
            for k in guard.keys():
                vid = k.split("|", 1)[0]
                if vid in video_ids_deleted:
                    to_remove.append(k)
            for k in to_remove:
                guard.pop(k, None)
            _write_json_file_nolock(VIEW_GUARD_FILE, guard)

    # borrar archivos fuera del lock
    for fn in files_to_delete:
        if fn:
            try:
                os.remove(UPLOAD_DIR / fn)
            except Exception:
                pass
    for tn in thumbs_to_delete:
        if tn:
            try:
                os.remove(THUMB_DIR / tn)
            except Exception:
                pass

    return JSONResponse({"status": "deleted", "project_id": project_id, "videos_deleted": len(video_ids_deleted)})


# 5) MOVE VIDEO (PROTEGIDO)
@router.post("/move")
async def move_video(data: dict = Body(...), username: str = Depends(get_current_username)):
    video_id = (data.get("id") or "").strip()
    project_id = (data.get("project_id") or "").strip()

    if not video_id or not project_id:
        raise HTTPException(status_code=400, detail="id y project_id requeridos")

    with FileLock(LOCK_FILE):
        raw = _read_json_file_nolock(DB_FILE, {"projects": [], "videos": []})
        db = _ensure_db_schema(raw)

        if not _find_project(db, project_id):
            raise HTTPException(status_code=404, detail="Proyecto destino no existe")

        found = False
        for v in db.get("videos", []):
            if v.get("id") == video_id:
                v["project_id"] = project_id
                found = True
                break

        if not found:
            raise HTTPException(status_code=404, detail="Video no existe")

        _write_json_file_nolock(DB_FILE, db)

    return JSONResponse({"status": "ok", "id": video_id, "project_id": project_id})


# 6) UPLOAD (PROTEGIDO)
@router.post("/upload")
async def upload_video(
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    project_id: Optional[str] = Form(None),
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

    with open(video_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    try:
        generate_thumbnail(str(video_path), str(thumb_path))
    except Exception:
        pass

    # validar proyecto
    project_id = (project_id or DEFAULT_PROJECT_ID).strip() or DEFAULT_PROJECT_ID

    with FileLock(LOCK_FILE):
        raw = _read_json_file_nolock(DB_FILE, {"projects": [], "videos": []})
        db = _ensure_db_schema(raw)

        if not _find_project(db, project_id):
            project_id = DEFAULT_PROJECT_ID

        new_entry = {
            "id": unique_hash,
            "title": title,
            "filename": new_filename,
            "thumb": thumb_filename,
            "twitter_link": "",
            "original_name": file.filename,
            "views": 0,
            "timestamp": os.path.getmtime(video_path),
            "project_id": project_id,
        }

        # al inicio
        db["videos"].insert(0, new_entry)
        _write_json_file_nolock(DB_FILE, db)

    return JSONResponse(content={"message": "Subido con éxito", "video": new_entry})


# 7) UPDATE SOCIAL (PROTEGIDO)
@router.post("/update_social")
async def update_social_link(data: dict = Body(...), username: str = Depends(get_current_username)):
    video_id = (data.get("id") or "").strip()
    link = data.get("link", "")

    with FileLock(LOCK_FILE):
        raw = _read_json_file_nolock(DB_FILE, {"projects": [], "videos": []})
        db = _ensure_db_schema(raw)

        found = False
        for video in db.get("videos", []):
            if video.get("id") == video_id:
                video["twitter_link"] = link
                found = True
                break

        if found:
            _write_json_file_nolock(DB_FILE, db)

    return JSONResponse(content={"status": "ok", "link": link})


# 8) DELETE VIDEO (PROTEGIDO)
@router.get("/delete/{video_id}")
async def delete_video(video_id: str, username: str = Depends(get_current_username)):
    filename_to_del = None
    thumb_to_del = None

    with FileLock(LOCK_FILE):
        raw = _read_json_file_nolock(DB_FILE, {"projects": [], "videos": []})
        db = _ensure_db_schema(raw)

        new_videos = []
        for v in db.get("videos", []):
            if v.get("id") == video_id:
                filename_to_del = v.get("filename")
                thumb_to_del = v.get("thumb")
            else:
                new_videos.append(v)

        db["videos"] = new_videos
        _write_json_file_nolock(DB_FILE, db)

        # limpiar guard del video (opcional)
        guard = _read_json_file_nolock(VIEW_GUARD_FILE, {})
        if guard:
            prefix = f"{video_id}|"
            keys = [k for k in guard.keys() if k.startswith(prefix)]
            for k in keys:
                guard.pop(k, None)
            _write_json_file_nolock(VIEW_GUARD_FILE, guard)

    # borrar archivos fuera del lock
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
