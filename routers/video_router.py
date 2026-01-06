import os
import json
import uuid
import cv2
import shutil
import secrets  # <--- NUEVO: Para seguridad
from pathlib import Path
from fastapi import APIRouter, UploadFile, File, Request, Form, Body, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials # <--- NUEVO: Autenticación
from filelock import FileLock

router = APIRouter(prefix="/videos", tags=["Videos"])
templates = Jinja2Templates(directory="templates")
security = HTTPBasic() # Instancia de seguridad

# --- CONFIGURACIÓN ---
UPLOAD_DIR = Path("uploads")
THUMB_DIR = Path("thumbnails")
DB_FILE = Path("db.json")
LOCK_FILE = Path("db.json.lock")

# CREDENCIALES DE ADMIN (Cámbialas aquí)
ADMIN_USER = "andrew19f"
ADMIN_PASS = "1003.Pazw"

UPLOAD_DIR.mkdir(exist_ok=True)
THUMB_DIR.mkdir(exist_ok=True)

# --- FUNCIONES AUXILIARES ---

def load_db():
    with FileLock(LOCK_FILE):
        if not DB_FILE.exists(): return []
        try:
            with open(DB_FILE, "r") as f:
                return json.load(f)
        except: return []

def save_db(data):
    with FileLock(LOCK_FILE):
        with open(DB_FILE, "w") as f:
            json.dump(data, f, indent=4)

def get_current_username(credentials: HTTPBasicCredentials = Depends(security)):
    """Verifica usuario y contraseña"""
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

# AHORA PROTEGIDO: Requiere login
@router.get("/manager", response_class=HTMLResponse)
async def video_manager(request: Request, username: str = Depends(get_current_username)):
    videos = load_db()
    # Aseguramos que todos tengan el campo views
    for v in videos:
        if "views" not in v: v["views"] = 0
            
    return templates.TemplateResponse("manager.html", {"request": request, "videos": videos, "user": username})

# PÚBLICO: Aquí contamos las vistas
@router.get("/watch/{video_id}", response_class=HTMLResponse)
async def watch_video(request: Request, video_id: str):
    videos = load_db()
    video = next((v for v in videos if v["id"] == video_id), None)
    
    if not video:
        return HTMLResponse("<h1>Video no encontrado</h1>", status_code=404)
    
    # --- LOGICA DE VISTAS ---
    # Incrementamos vista y guardamos inmediatamente
    video["views"] = video.get("views", 0) + 1
    save_db(videos) # Guardamos el cambio en el JSON
    
    return templates.TemplateResponse("watch.html", {"request": request, "video": video})

# PROTEGIDO
@router.post("/upload")
async def upload_video(
    file: UploadFile = File(...), 
    title: str = Form(...),
    username: str = Depends(get_current_username) # Seguridad
):
    unique_hash = uuid.uuid4().hex[:5]
    extension = file.filename.split(".")[-1]
    new_filename = f"{unique_hash}.{extension}"
    thumb_filename = f"{unique_hash}.jpg"
    
    video_path = UPLOAD_DIR / new_filename
    thumb_path = THUMB_DIR / thumb_filename

    with open(video_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    try: generate_thumbnail(str(video_path), str(thumb_path))
    except: pass

    db = load_db()
    new_entry = {
        "id": unique_hash,
        "title": title,
        "filename": new_filename,
        "thumb": thumb_filename,
        "twitter_link": "",
        "original_name": file.filename,
        "views": 0, # <--- Inicializamos contador
        "timestamp": os.path.getmtime(video_path) # Para ordenar por fecha real
    }
    db.insert(0, new_entry) 
    save_db(db)

    return JSONResponse(content={"message": "Subido con éxito", "video": new_entry})

# PROTEGIDO
@router.post("/update_social")
async def update_social_link(data: dict = Body(...), username: str = Depends(get_current_username)):
    video_id = data.get("id")
    link = data.get("link")
    
    db = load_db()
    for video in db:
        if video["id"] == video_id:
            video["twitter_link"] = link
            break
    
    save_db(db)
    return JSONResponse(content={"status": "ok", "link": link})

# PROTEGIDO
@router.get("/delete/{video_id}")
async def delete_video(video_id: str, username: str = Depends(get_current_username)):
    db = load_db()
    video = next((v for v in db if v["id"] == video_id), None)
    
    if video:
        try:
            os.remove(UPLOAD_DIR / video["filename"])
            os.remove(THUMB_DIR / video["thumb"])
        except: pass
        
        db = [v for v in db if v["id"] != video_id]
        save_db(db)
        
    return JSONResponse(content={"status": "deleted"})