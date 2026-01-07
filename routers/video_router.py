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
from typing import Optional, Dict, Any, List, Tuple

from fastapi import (
    APIRouter, UploadFile, File, Request, Form, Body, Depends,
    HTTPException, status, Query
)
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from filelock import FileLock

# ============================================================
# ROUTER
# ============================================================
router = APIRouter(prefix="/courses", tags=["Courses"])
templates = Jinja2Templates(directory="templates")
security = HTTPBasic()

# ============================================================
# CONFIG
# ============================================================
UPLOAD_DIR = Path("uploads")
THUMB_DIR = Path("thumbnails")
DB_FILE = Path("db.json")
LOCK_FILE = Path("db.json.lock")
VIEW_GUARD_FILE = Path("view_guard.json")

# Zona horaria Bogota (sin DST)
BOGOTA_TZ = timezone(timedelta(hours=-5))

# TTL del guard (segundos)
VIEW_GUARD_TTL_SECONDS = 48 * 3600

# CREDENCIALES ADMIN (ideal: env vars)
ADMIN_USER = "andrew19f"
ADMIN_PASS = "1003.Pazw"

UPLOAD_DIR.mkdir(exist_ok=True)
THUMB_DIR.mkdir(exist_ok=True)

DEFAULT_SCHOOL_ID = "general"
DEFAULT_COURSE_ID = "general_course"


# ============================================================
# JSON helpers + schema migration
# ============================================================
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


def _ensure_default_entities(db: Dict[str, Any]) -> Dict[str, Any]:
    db.setdefault("schools", [])
    db.setdefault("courses", [])
    db.setdefault("classes", [])

    # Escuela base
    if not any(s.get("id") == DEFAULT_SCHOOL_ID for s in db["schools"]):
        db["schools"].insert(
            0, {"id": DEFAULT_SCHOOL_ID, "name": "General", "created_at": _now_ts()}
        )

    # Curso base
    if not any(c.get("id") == DEFAULT_COURSE_ID for c in db["courses"]):
        db["courses"].insert(
            0,
            {
                "id": DEFAULT_COURSE_ID,
                "school_id": DEFAULT_SCHOOL_ID,
                "name": "Curso General",
                "created_at": _now_ts(),
            },
        )

    # Normalizar clases
    for cls in db["classes"]:
        if not cls.get("course_id"):
            cls["course_id"] = DEFAULT_COURSE_ID
        if "views" not in cls:
            cls["views"] = 0
        if "timestamp" not in cls:
            cls["timestamp"] = 0
        if "social_link" not in cls:
            cls["social_link"] = ""

    return db


def _migrate_from_videos_projects_schema(old: Dict[str, Any]) -> Dict[str, Any]:
    """
    Migra del esquema anterior:
      {
        "projects": [{"id","name","created_at"}...],
        "videos": [{"id","title","filename","thumb","twitter_link","timestamp","views","project_id"}...]
      }
    a:
      {
        "schools": [...],
        "courses": [...],
        "classes": [...]
      }

    Estrategia:
    - Cada project -> school (mismo id/nombre)
    - Para cada school: crear 1 curso automático "Curso 1"
    - Cada video -> class dentro del curso de su school
    """
    projects = old.get("projects") or []
    videos = old.get("videos") or []

    schools: List[Dict[str, Any]] = []
    courses: List[Dict[str, Any]] = []
    classes: List[Dict[str, Any]] = []

    # Mapa project_id -> school_id (igual)
    for p in projects:
        pid = (p.get("id") or "").strip() or uuid.uuid4().hex[:8]
        pname = (p.get("name") or "Escuela").strip() or "Escuela"
        schools.append(
            {"id": pid, "name": pname, "created_at": float(p.get("created_at", 0) or 0) or _now_ts()}
        )

    # asegurar existencia de default school
    if not any(s.get("id") == DEFAULT_SCHOOL_ID for s in schools):
        schools.insert(0, {"id": DEFAULT_SCHOOL_ID, "name": "General", "created_at": _now_ts()})

    # crear curso por escuela
    course_for_school: Dict[str, str] = {}
    for s in schools:
        sid = s["id"]
        if sid == DEFAULT_SCHOOL_ID:
            cid = DEFAULT_COURSE_ID
            cname = "Curso General"
        else:
            cid = f"{sid}_course1"
            cname = "Curso 1"
        course_for_school[sid] = cid
        courses.append({"id": cid, "school_id": sid, "name": cname, "created_at": _now_ts()})

    # videos -> classes
    for v in videos:
        pid = (v.get("project_id") or DEFAULT_SCHOOL_ID).strip() or DEFAULT_SCHOOL_ID
        if pid not in course_for_school:
            # si no existe, caemos en default
            pid = DEFAULT_SCHOOL_ID
        cid = course_for_school[pid]

        cls = {
            "id": (v.get("id") or uuid.uuid4().hex[:5]).strip(),
            "title": (v.get("title") or "Clase").strip(),
            "filename": v.get("filename"),
            "thumb": v.get("thumb"),
            "original_name": v.get("original_name", ""),
            "views": int(v.get("views", 0) or 0),
            "timestamp": float(v.get("timestamp", 0) or 0),
            "course_id": cid,
            "social_link": v.get("twitter_link", "") or "",
        }
        classes.append(cls)

    new_db = {"schools": schools, "courses": courses, "classes": classes}
    return _ensure_default_entities(new_db)


def _ensure_db_schema(db_raw: Any) -> Dict[str, Any]:
    """
    Nuevo esquema:
      {
        "schools": [{"id","name","created_at"}...],
        "courses": [{"id","school_id","name","created_at"}...],
        "classes": [{"id","course_id","title","filename","thumb","social_link","views","timestamp","original_name"}...]
      }

    Migra automáticamente si:
    - era lista vieja (videos)
    - era dict viejo (projects/videos)
    """
    # Caso 1: nuevo esquema OK
    if isinstance(db_raw, dict) and "schools" in db_raw and "courses" in db_raw and "classes" in db_raw:
        return _ensure_default_entities(db_raw)

    # Caso 2: esquema anterior projects/videos
    if isinstance(db_raw, dict) and "projects" in db_raw and "videos" in db_raw:
        return _migrate_from_videos_projects_schema(db_raw)

    # Caso 3: esquema viejo -> lista de videos
    if isinstance(db_raw, list):
        # Lo tratamos como videos "sueltos": todo al curso general
        classes = []
        for v in db_raw:
            classes.append({
                "id": (v.get("id") or uuid.uuid4().hex[:5]).strip(),
                "title": (v.get("title") or "Clase").strip(),
                "filename": v.get("filename"),
                "thumb": v.get("thumb"),
                "original_name": v.get("original_name", ""),
                "views": int(v.get("views", 0) or 0),
                "timestamp": float(v.get("timestamp", 0) or 0),
                "course_id": DEFAULT_COURSE_ID,
                "social_link": v.get("twitter_link", "") or "",
            })

        db = {
            "schools": [{"id": DEFAULT_SCHOOL_ID, "name": "General", "created_at": _now_ts()}],
            "courses": [{
                "id": DEFAULT_COURSE_ID,
                "school_id": DEFAULT_SCHOOL_ID,
                "name": "Curso General",
                "created_at": _now_ts()
            }],
            "classes": classes
        }
        return _ensure_default_entities(db)

    # Caso 4: vacío/corrupto
    return _ensure_default_entities({
        "schools": [{"id": DEFAULT_SCHOOL_ID, "name": "General", "created_at": _now_ts()}],
        "courses": [{
            "id": DEFAULT_COURSE_ID,
            "school_id": DEFAULT_SCHOOL_ID,
            "name": "Curso General",
            "created_at": _now_ts()
        }],
        "classes": []
    })


def load_db() -> Dict[str, Any]:
    with FileLock(LOCK_FILE):
        raw = _read_json_file_nolock(DB_FILE, {"schools": [], "courses": [], "classes": []})
        db = _ensure_db_schema(raw)
        _write_json_file_nolock(DB_FILE, db)
        return db


def save_db(db: Dict[str, Any]) -> None:
    with FileLock(LOCK_FILE):
        _write_json_file_nolock(DB_FILE, db)


# ============================================================
# Find / list helpers
# ============================================================
def _find_school(db: Dict[str, Any], school_id: str) -> Optional[Dict[str, Any]]:
    for s in db.get("schools") or []:
        if s.get("id") == school_id:
            return s
    return None


def _find_course(db: Dict[str, Any], course_id: str) -> Optional[Dict[str, Any]]:
    for c in db.get("courses") or []:
        if c.get("id") == course_id:
            return c
    return None


def _schools_sorted(db: Dict[str, Any]) -> List[Dict[str, Any]]:
    schools = list(db.get("schools") or [])
    schools.sort(key=lambda s: (0 if s.get("id") == DEFAULT_SCHOOL_ID else 1, float(s.get("created_at", 0) or 0)))
    return schools


def _courses_sorted(db: Dict[str, Any]) -> List[Dict[str, Any]]:
    courses = list(db.get("courses") or [])
    # general_course primero
    courses.sort(key=lambda c: (0 if c.get("id") == DEFAULT_COURSE_ID else 1, float(c.get("created_at", 0) or 0)))
    return courses


# ============================================================
# Basic Auth
# ============================================================
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


# ============================================================
# Thumbnail
# ============================================================
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


# ============================================================
# Fingerprint + HTTPS helper
# ============================================================
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


# ============================================================
# Atomic view count + dedupe per day
# ============================================================
def increment_view_atomic_once_per_day(class_id: str, fingerprint: str, today_str: str) -> Optional[int]:
    now_ts = int(time.time())

    with FileLock(LOCK_FILE):
        db_raw = _read_json_file_nolock(DB_FILE, {"schools": [], "courses": [], "classes": []})
        db = _ensure_db_schema(db_raw)

        cls = None
        for c in db.get("classes", []):
            if c.get("id") == class_id:
                cls = c
                break
        if not cls:
            _write_json_file_nolock(DB_FILE, db)
            return None

        current_views = int(cls.get("views", 0) or 0)

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

        key = f"{class_id}|{fingerprint}"
        info = guard.get(key)
        if info and info.get("date") == today_str:
            guard[key] = {"date": today_str, "ts": now_ts}
            _write_json_file_nolock(VIEW_GUARD_FILE, guard)
            _write_json_file_nolock(DB_FILE, db)
            return None

        new_views = current_views + 1
        cls["views"] = new_views
        guard[key] = {"date": today_str, "ts": now_ts}

        _write_json_file_nolock(DB_FILE, db)
        _write_json_file_nolock(VIEW_GUARD_FILE, guard)

        return new_views


# ============================================================
# Build tree for manager
# ============================================================
def _build_tree(
    db: Dict[str, Any],
    selected_school_id: str,
    selected_course_id: str
) -> Tuple[List[Dict[str, Any]], int]:
    schools = _schools_sorted(db)
    courses = _courses_sorted(db)
    classes = list(db.get("classes") or [])

    # index courses by school_id
    courses_by_school: Dict[str, List[Dict[str, Any]]] = {}
    for c in courses:
        sid = c.get("school_id") or DEFAULT_SCHOOL_ID
        courses_by_school.setdefault(sid, []).append(c)

    # index classes by course_id
    classes_by_course: Dict[str, List[Dict[str, Any]]] = {}
    for cls in classes:
        cid = cls.get("course_id") or DEFAULT_COURSE_ID
        classes_by_course.setdefault(cid, []).append(cls)

    # default order: populares desc, luego timestamp desc
    for cid in classes_by_course:
        classes_by_course[cid].sort(
            key=lambda x: (int(x.get("views", 0) or 0), float(x.get("timestamp", 0) or 0)),
            reverse=True
        )

    # apply filters server-side
    def school_ok(sid: str) -> bool:
        return selected_school_id == "__ALL__" or sid == selected_school_id

    def course_ok(cid: str) -> bool:
        return selected_course_id == "__ALL__" or cid == selected_course_id

    tree: List[Dict[str, Any]] = []
    total_classes = len(classes)

    for s in schools:
        sid = s.get("id")
        if not sid or not school_ok(sid):
            continue

        course_blocks = []
        for c in courses_by_school.get(sid, []):
            cid = c.get("id")
            if not cid or not course_ok(cid):
                continue

            course_blocks.append({
                "course": c,
                "classes": classes_by_course.get(cid, [])
            })

        # (opcional) ocultar escuelas sin cursos en el filtro actual
        # pero como tú quieres "escuela/curso/clase", dejamos visible si coincide el filtro
        tree.append({
            "school": s,
            "courses": course_blocks
        })

    return tree, total_classes


# ============================================================
# ROUTES
# ============================================================

# 1) MANAGER (PROTEGIDO) + filtro por escuela/curso
@router.get("/manager", response_class=HTMLResponse)
async def courses_manager(
    request: Request,
    school_id: Optional[str] = Query(default=None),
    course_id: Optional[str] = Query(default=None),
    username: str = Depends(get_current_username),
):
    db = load_db()

    school_id = (school_id or "").strip()
    course_id = (course_id or "").strip()

    schools = _schools_sorted(db)
    courses = _courses_sorted(db)

    valid_school_ids = {s.get("id") for s in schools if s.get("id")}
    valid_course_ids = {c.get("id") for c in courses if c.get("id")}

    # Resolve selected school
    if school_id == "__ALL__":
        selected_sid = "__ALL__"
    elif school_id and school_id in valid_school_ids:
        selected_sid = school_id
    else:
        # default: primera escuela que tenga clases, si no: general
        class_counts_by_school: Dict[str, int] = {}
        course_by_id = {c["id"]: c for c in courses if c.get("id")}
        for cls in db.get("classes", []):
            cid = cls.get("course_id") or DEFAULT_COURSE_ID
            c = course_by_id.get(cid)
            sid = (c.get("school_id") if c else DEFAULT_SCHOOL_ID) or DEFAULT_SCHOOL_ID
            class_counts_by_school[sid] = class_counts_by_school.get(sid, 0) + 1

        first_with_classes = None
        for s in schools:
            sid = s.get("id")
            if class_counts_by_school.get(sid, 0) > 0:
                first_with_classes = sid
                break
        selected_sid = first_with_classes or DEFAULT_SCHOOL_ID

    # Resolve selected course
    if course_id == "__ALL__" or not course_id:
        selected_cid = "__ALL__"
    elif course_id in valid_course_ids:
        selected_cid = course_id
    else:
        selected_cid = "__ALL__"

    # Si seleccionaron una escuela específica, pero el course_id es de otra escuela -> lo reseteamos
    if selected_sid != "__ALL__" and selected_cid != "__ALL__":
        c = _find_course(db, selected_cid)
        if c and (c.get("school_id") or DEFAULT_SCHOOL_ID) != selected_sid:
            selected_cid = "__ALL__"

    schools_tree, total_classes = _build_tree(db, selected_sid, selected_cid)

    # Render (usa tu nuevo HTML como manager.html)
    return templates.TemplateResponse(
        "manager.html",
        {
            "request": request,
            "user": username,

            # data para el template nuevo
            "schools_tree": schools_tree,
            "total_classes": total_classes,

            # selection
            "selected_school_id": selected_sid,
            "selected_course_id": selected_cid,
        }
    )


# 2) WATCH (PÚBLICO) clase
@router.get("/classes/watch/{class_id}", response_class=HTMLResponse)
async def watch_class(request: Request, class_id: str):
    db = load_db()
    classes = db.get("classes", [])
    cls = next((c for c in classes if c.get("id") == class_id), None)
    if not cls:
        return HTMLResponse("<h1>Clase no encontrada</h1>", status_code=404)

    today_str = _today_bogota_str()

    cookie_name = f"viewed_{class_id}"
    cookie_val = request.cookies.get(cookie_name)

    response = templates.TemplateResponse("watch.html", {"request": request, "class": cls})

    if cookie_val != today_str:
        fp = _client_fingerprint(request)
        new_views = increment_view_atomic_once_per_day(class_id, fp, today_str)
        if new_views is not None:
            try:
                cls["views"] = new_views
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


# 3) CREATE SCHOOL (PROTEGIDO)
@router.post("/schools/create")
async def create_school(data: dict = Body(...), username: str = Depends(get_current_username)):
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Nombre requerido")

    new_id = uuid.uuid4().hex[:8]

    with FileLock(LOCK_FILE):
        raw = _read_json_file_nolock(DB_FILE, {"schools": [], "courses": [], "classes": []})
        db = _ensure_db_schema(raw)

        for s in db.get("schools", []):
            if (s.get("name") or "").strip().lower() == name.lower():
                raise HTTPException(status_code=400, detail="Ya existe una escuela con ese nombre")

        db["schools"].append({"id": new_id, "name": name, "created_at": _now_ts()})

        # Opcional: crear curso 1 automático
        course_id = f"{new_id}_course1"
        db["courses"].append({
            "id": course_id,
            "school_id": new_id,
            "name": "Curso 1",
            "created_at": _now_ts()
        })

        _write_json_file_nolock(DB_FILE, db)

    return JSONResponse({"status": "ok", "school": {"id": new_id, "name": name}})


# 4) DELETE SCHOOL (PROTEGIDO) -> borra escuela + cursos + clases + archivos
@router.post("/schools/delete")
async def delete_school(data: dict = Body(...), username: str = Depends(get_current_username)):
    school_id = (data.get("school_id") or "").strip()
    if not school_id:
        raise HTTPException(status_code=400, detail="school_id requerido")
    if school_id == DEFAULT_SCHOOL_ID:
        raise HTTPException(status_code=400, detail="No se puede borrar la escuela base")

    files_to_delete = []
    thumbs_to_delete = []
    class_ids_deleted = []

    with FileLock(LOCK_FILE):
        raw = _read_json_file_nolock(DB_FILE, {"schools": [], "courses": [], "classes": []})
        db = _ensure_db_schema(raw)

        school = _find_school(db, school_id)
        if not school:
            raise HTTPException(status_code=404, detail="Escuela no existe")

        # cursos de esa escuela
        course_ids = [c.get("id") for c in db.get("courses", []) if (c.get("school_id") or DEFAULT_SCHOOL_ID) == school_id]
        course_ids = [cid for cid in course_ids if cid]

        # borrar escuela
        db["schools"] = [s for s in db.get("schools", []) if s.get("id") != school_id]
        # borrar cursos
        db["courses"] = [c for c in db.get("courses", []) if (c.get("school_id") or DEFAULT_SCHOOL_ID) != school_id]

        # borrar clases de esos cursos
        new_classes = []
        for cls in db.get("classes", []):
            if (cls.get("course_id") or DEFAULT_COURSE_ID) in course_ids:
                class_ids_deleted.append(cls.get("id"))
                files_to_delete.append(cls.get("filename"))
                thumbs_to_delete.append(cls.get("thumb"))
            else:
                new_classes.append(cls)

        db["classes"] = new_classes
        _write_json_file_nolock(DB_FILE, db)

        # limpiar guard
        guard = _read_json_file_nolock(VIEW_GUARD_FILE, {})
        if guard and class_ids_deleted:
            to_remove = []
            for k in list(guard.keys()):
                cid = k.split("|", 1)[0]
                if cid in class_ids_deleted:
                    to_remove.append(k)
            for k in to_remove:
                guard.pop(k, None)
            _write_json_file_nolock(VIEW_GUARD_FILE, guard)

    # borrar archivos
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

    return JSONResponse({
        "status": "deleted",
        "school_id": school_id,
        "courses_deleted": len(set(course_ids)),
        "classes_deleted": len(class_ids_deleted),
    })


# 5) CREATE COURSE (PROTEGIDO)
@router.post("/courses/create")
async def create_course(data: dict = Body(...), username: str = Depends(get_current_username)):
    school_id = (data.get("school_id") or "").strip()
    name = (data.get("name") or "").strip()

    if not school_id:
        raise HTTPException(status_code=400, detail="school_id requerido")
    if not name:
        raise HTTPException(status_code=400, detail="Nombre requerido")

    new_id = uuid.uuid4().hex[:10]

    with FileLock(LOCK_FILE):
        raw = _read_json_file_nolock(DB_FILE, {"schools": [], "courses": [], "classes": []})
        db = _ensure_db_schema(raw)

        if not _find_school(db, school_id):
            raise HTTPException(status_code=404, detail="Escuela no existe")

        # evitar nombre duplicado dentro de la misma escuela
        for c in db.get("courses", []):
            if (c.get("school_id") or DEFAULT_SCHOOL_ID) == school_id and (c.get("name") or "").strip().lower() == name.lower():
                raise HTTPException(status_code=400, detail="Ya existe un curso con ese nombre en esa escuela")

        db["courses"].append({"id": new_id, "school_id": school_id, "name": name, "created_at": _now_ts()})
        _write_json_file_nolock(DB_FILE, db)

    return JSONResponse({"status": "ok", "course": {"id": new_id, "school_id": school_id, "name": name}})


# 6) DELETE COURSE (PROTEGIDO) -> borra curso + clases + archivos
@router.post("/courses/delete")
async def delete_course(data: dict = Body(...), username: str = Depends(get_current_username)):
    course_id = (data.get("course_id") or "").strip()
    if not course_id:
        raise HTTPException(status_code=400, detail="course_id requerido")
    if course_id == DEFAULT_COURSE_ID:
        raise HTTPException(status_code=400, detail="No se puede borrar el curso base")

    files_to_delete = []
    thumbs_to_delete = []
    class_ids_deleted = []

    with FileLock(LOCK_FILE):
        raw = _read_json_file_nolock(DB_FILE, {"schools": [], "courses": [], "classes": []})
        db = _ensure_db_schema(raw)

        course = _find_course(db, course_id)
        if not course:
            raise HTTPException(status_code=404, detail="Curso no existe")

        # borrar curso
        db["courses"] = [c for c in db.get("courses", []) if c.get("id") != course_id]

        # borrar clases del curso
        new_classes = []
        for cls in db.get("classes", []):
            if (cls.get("course_id") or DEFAULT_COURSE_ID) == course_id:
                class_ids_deleted.append(cls.get("id"))
                files_to_delete.append(cls.get("filename"))
                thumbs_to_delete.append(cls.get("thumb"))
            else:
                new_classes.append(cls)

        db["classes"] = new_classes
        _write_json_file_nolock(DB_FILE, db)

        # limpiar guard
        guard = _read_json_file_nolock(VIEW_GUARD_FILE, {})
        if guard and class_ids_deleted:
            to_remove = []
            for k in list(guard.keys()):
                cid = k.split("|", 1)[0]
                if cid in class_ids_deleted:
                    to_remove.append(k)
            for k in to_remove:
                guard.pop(k, None)
            _write_json_file_nolock(VIEW_GUARD_FILE, guard)

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

    return JSONResponse({"status": "deleted", "course_id": course_id, "classes_deleted": len(class_ids_deleted)})


# 7) MOVE CLASS (PROTEGIDO)
@router.post("/classes/move")
async def move_class(data: dict = Body(...), username: str = Depends(get_current_username)):
    class_id = (data.get("id") or "").strip()
    course_id = (data.get("course_id") or "").strip()

    if not class_id or not course_id:
        raise HTTPException(status_code=400, detail="id y course_id requeridos")

    if course_id == "__ALL__":
        course_id = DEFAULT_COURSE_ID

    with FileLock(LOCK_FILE):
        raw = _read_json_file_nolock(DB_FILE, {"schools": [], "courses": [], "classes": []})
        db = _ensure_db_schema(raw)

        if not _find_course(db, course_id):
            raise HTTPException(status_code=404, detail="Curso destino no existe")

        found = False
        for cls in db.get("classes", []):
            if cls.get("id") == class_id:
                cls["course_id"] = course_id
                found = True
                break

        if not found:
            raise HTTPException(status_code=404, detail="Clase no existe")

        _write_json_file_nolock(DB_FILE, db)

    return JSONResponse({"status": "ok", "id": class_id, "course_id": course_id})


# 8) UPLOAD CLASS (PROTEGIDO)
@router.post("/classes/upload")
async def upload_class(
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    course_id: Optional[str] = Form(None),
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

    course_id = (course_id or DEFAULT_COURSE_ID).strip() or DEFAULT_COURSE_ID
    if course_id == "__ALL__":
        course_id = DEFAULT_COURSE_ID

    with FileLock(LOCK_FILE):
        raw = _read_json_file_nolock(DB_FILE, {"schools": [], "courses": [], "classes": []})
        db = _ensure_db_schema(raw)

        if not _find_course(db, course_id):
            course_id = DEFAULT_COURSE_ID

        new_entry = {
            "id": unique_hash,
            "title": title,
            "filename": new_filename,
            "thumb": thumb_filename,
            "social_link": "",
            "original_name": file.filename,
            "views": 0,
            "timestamp": os.path.getmtime(video_path),
            "course_id": course_id,
        }

        db["classes"].insert(0, new_entry)
        _write_json_file_nolock(DB_FILE, db)

    return JSONResponse(content={"message": "Subida con éxito", "class": new_entry})


# 9) UPDATE SOCIAL (PROTEGIDO)
@router.post("/classes/update_social")
async def update_social_link(data: dict = Body(...), username: str = Depends(get_current_username)):
    class_id = (data.get("id") or "").strip()
    link = data.get("link", "") or ""

    with FileLock(LOCK_FILE):
        raw = _read_json_file_nolock(DB_FILE, {"schools": [], "courses": [], "classes": []})
        db = _ensure_db_schema(raw)

        found = False
        for cls in db.get("classes", []):
            if cls.get("id") == class_id:
                cls["social_link"] = link
                found = True
                break

        if found:
            _write_json_file_nolock(DB_FILE, db)

    return JSONResponse(content={"status": "ok", "link": link})


# 10) DELETE CLASS (PROTEGIDO)
@router.get("/classes/delete/{class_id}")
async def delete_class(class_id: str, username: str = Depends(get_current_username)):
    filename_to_del = None
    thumb_to_del = None

    with FileLock(LOCK_FILE):
        raw = _read_json_file_nolock(DB_FILE, {"schools": [], "courses": [], "classes": []})
        db = _ensure_db_schema(raw)

        new_classes = []
        for cls in db.get("classes", []):
            if cls.get("id") == class_id:
                filename_to_del = cls.get("filename")
                thumb_to_del = cls.get("thumb")
            else:
                new_classes.append(cls)

        db["classes"] = new_classes
        _write_json_file_nolock(DB_FILE, db)

        guard = _read_json_file_nolock(VIEW_GUARD_FILE, {})
        if guard:
            prefix = f"{class_id}|"
            keys = [k for k in list(guard.keys()) if k.startswith(prefix)]
            for k in keys:
                guard.pop(k, None)
            _write_json_file_nolock(VIEW_GUARD_FILE, guard)

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
