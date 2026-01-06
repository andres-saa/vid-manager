# Aquí va tu archivo principal (FastAPI app, etc.)
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

# 1. Importa el router que creamos
from routers.video_router import router as video_router

app = FastAPI()

# 2. Monta las carpetas para que sean accesibles desde la web
# Esto permite que la URL sea /static_videos/hash.mp4
app.mount("/static_videos", StaticFiles(directory="uploads"), name="static_videos")
app.mount("/static_thumbs", StaticFiles(directory="thumbnails"), name="static_thumbs")

# 3. Incluye el router
app.include_router(video_router)

# Corre tu servidor normalmente
# uvicorn main:app --reload