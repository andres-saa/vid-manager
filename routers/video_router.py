import os
import json
import uuid
import cv2
import shutil
import secrets
from pathlib import Path
from typing import Optional

# Nuevas importaciones necesarias
from fastapi import APIRouter, UploadFile, File, Request, Form, Body, Depends, HTTPException, status, Response
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

# CREDENCIALES DE ADMIN
ADMIN_USER = "andrew19f"
ADMIN_PASS = "1003.Pazw"

UPLOAD_DIR.mkdir(exist_ok=True)
THUMB_DIR.mkdir(exist_ok=True)

# --- FUNCIONES AUXILIARES ---

def load_db():
    """Lee la base de datos (Solo lectura segura)"""
    with FileLock(LOCK_FILE):
        if not DB_FILE.exists(): return []
        try:
            with open(DB_FILE, "r") as f:
                return json.load(f)
        except: return []

def save_db(data):
    """Guarda en la base de datos (Escritura segura)"""
    with FileLock(LOCK_FILE):
        with open(DB_FILE, "w") as f:
            json.dump(data, f, indent=4)

def increment_view_atomic(video_id: str):
    """
    IMPORTANTE: Esta función maneja la concurrencia de los 8 workers.
    Bloquea el archivo, lee, actualiza y guarda en una sola transacción.
    Evita que dos visitas simultáneas se sobrescriban.
    """
    with FileLock(LOCK_FILE):
        data = []
        if DB_FILE.exists():
            try:
                with open(DB_FILE, "r") as f:
                    data = json.load(f)
            except: 
                data = []
        
        # Buscar y actualizar dentro del bloqueo
        for video in data:
            if video["id"] == video_id:
                # Asegurar que existe el campo y sumar
                current_views = video.get("views", 0)
                video["views"] = current_views + 1
                break
        
        # Guardar inmediatamente antes de soltar el bloqueo
        with open(DB_FILE, "w") as f:
            json.dump(data, f, indent=4)

def get_current_username(credentials: HTTPBasicCredentials = Depends(security)):
    """Verifica usuario y contraseña de forma segura"""
    correct_user = secrets.compare_digest(credentials.username, ADMIN_USER)
    correct_pass = secrets.compare_digest(credentials.password, ADMIN_PASS)
    
    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales incorrectas",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

def generate_thumbnail(video_path: str, thumb_path: str):
    cap = cv2.VideoCapture(video_path)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.set(cv2.CAP_PROP_POS_FRAMES, total_frames // 2) # Frame central
    ret, frame = cap.read()
    if ret:
        cv2.imwrite(thumb_path, frame)
    cap.release()

# --- RUTAS ---

# 1. MANAGER (PROTEGIDO)
@router.get("/manager", response_class=HTMLResponse)
async def video_manager(request: Request, username: str = Depends(get_current_username)):
    videos = load_db()
    # Aseguramos que todos tengan el campo views para visualización
    for v in videos:
        if "views" not in v: v["views"] = 0
            
    return templates.TemplateResponse("manager.html", {"request": request, "videos": videos, "user": username})

# 2. WATCH (PÚBLICO - LÓGICA CORREGIDA)
@router.get("/watch/{video_id}", response_class=HTMLResponse)
async def watch_video(request: Request, video_id: str):
    # Primero cargamos datos solo para mostrar el video (Lectura rápida)
    videos = load_db()
    video = next((v for v in videos if v["id"] == video_id), None)
    
    if not video:
        return HTMLResponse("<h1>Video no encontrado</h1>", status_code=404)
    
    # Preparamos la respuesta
    response = templates.TemplateResponse("watch.html", {"request": request, "video": video})
    
    # --- LOGICA DE VISTA ÚNICA ---
    # Nombre de la cookie única para este video
    cookie_name = f"viewed_{video_id}"
    
    # Verificamos si la cookie YA existe en el navegador del usuario
    if cookie_name not in request.cookies:
        # 1. El usuario NO ha visto el video recientemente.
        # 2. Ejecutamos la actualización atómica (segura para workers).
        increment_view_atomic(video_id)
        
        # 3. Le ponemos la cookie para marcarlo como "Visto".
        # max_age=86400 segundos equivale a 24 horas.
        response.set_cookie(key=cookie_name, value="true", max_age=86400)
    else:
        # El usuario YA tiene la cookie, no hacemos nada (no sumamos la vista).
        pass
    
    return response

# 3. UPLOAD (PROTEGIDO)
@router.post("/upload")
async def upload_video(
    file: UploadFile = File(...), 
    title: Optional[str] = Form(None), 
    username: str = Depends(get_current_username)
):
    unique_hash = uuid.uuid4().hex[:5]
    
    if not title:
        title = unique_hash

    extension = file.filename.split(".")[-1]
    new_filename = f"{unique_hash}.{extension}"
    thumb_filename = f"{unique_hash}.jpg"
    
    video_path = UPLOAD_DIR / new_filename
    thumb_path = THUMB_DIR / thumb_filename

    # Guardar video
    with open(video_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # Generar miniatura
    try: generate_thumbnail(str(video_path), str(thumb_path))
    except: pass

    # Actualizar BD (Usamos la función atómica save_db que ya tiene lock)
    with FileLock(LOCK_FILE): # Bloqueo manual para Insertar al principio
        db = []
        if DB_FILE.exists():
            try:
                with open(DB_FILE, "r") as f: db = json.load(f)
            except: pass
            
        new_entry = {
            "id": unique_hash,
            "title": title,
            "filename": new_filename,
            "thumb": thumb_filename,
            "twitter_link": "",
            "original_name": file.filename,
            "views": 0,
            "timestamp": os.path.getmtime(video_path)
        }
        db.insert(0, new_entry) 
        
        with open(DB_FILE, "w") as f:
            json.dump(db, f, indent=4)

    return JSONResponse(content={"message": "Subido con éxito", "video": new_entry})

# 4. UPDATE SOCIAL (PROTEGIDO)
@router.post("/update_social")
async def update_social_link(data: dict = Body(...), username: str = Depends(get_current_username)):
    video_id = data.get("id")
    link = data.get("link")
    
    # Bloqueo atómico para evitar corrupción si se actualiza mientras alguien ve videos
    with FileLock(LOCK_FILE):
        db = []
        if DB_FILE.exists():
            with open(DB_FILE, "r") as f: db = json.load(f)
            
        found = False
        for video in db:
            if video["id"] == video_id:
                video["twitter_link"] = link
                found = True
                break
        
        if found:
            with open(DB_FILE, "w") as f: json.dump(db, f, indent=4)
    
    return JSONResponse(content={"status": "ok", "link": link})

# 5. DELETE (PROTEGIDO)
@router.get("/delete/{video_id}")
async def delete_video(video_id: str, username: str = Depends(get_current_username)):
    filename_to_del = None
    thumb_to_del = None
    
    with FileLock(LOCK_FILE):
        db = []
        if DB_FILE.exists():
            with open(DB_FILE, "r") as f: db = json.load(f)
        
        # Encontrar y sacar de la lista
        new_db = []
        for v in db:
            if v["id"] == video_id:
                filename_to_del = v["filename"]
                thumb_to_del = v["thumb"]
            else:
                new_db.append(v)
        
        # Guardar la lista nueva
        with open(DB_FILE, "w") as f: json.dump(new_db, f, indent=4)

    # Borrar archivos físicos fuera del bloqueo (para no detener la DB)
    if filename_to_del:
        try: os.remove(UPLOAD_DIR / filename_to_del)
        except: pass
    if thumb_to_del:
        try: os.remove(THUMB_DIR / thumb_to_del)
        except: pass
        
    return JSONResponse(content={"status": "deleted"})