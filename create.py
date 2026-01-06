import os
from pathlib import Path

def crear_estructura():
    # Nombre de la carpeta raíz
    root_name = "tu_proyecto"
    base_path = Path.cwd() / root_name

    # Definimos la estructura: Carpetas
    directorios = [
        base_path / "uploads",
        base_path / "thumbnails",
        base_path / "routers",
        base_path / "templates",
    ]

    # Definimos los archivos y un contenido inicial básico (opcional)
    archivos = {
        base_path / "main.py": "# Aquí va tu archivo principal (FastAPI app, etc.)\n",
        base_path / "routers/video_router.py": "# Aquí pega el código del router que tienes\n",
        base_path / "templates/manager.html": "\n<h1>Manager Dashboard</h1>",
        base_path / "templates/watch.html": "\n<h1>Video Player</h1>"
    }

    print(f"🚀 Iniciando creación de estructura en: {base_path}")

    # 1. Crear directorios
    if not base_path.exists():
        base_path.mkdir()
        print(f"✅ Carpeta raíz '{root_name}' creada.")
    else:
        print(f"ℹ️ La carpeta raíz '{root_name}' ya existe.")

    for carpeta in directorios:
        try:
            carpeta.mkdir(parents=True, exist_ok=True)
            print(f"   📂 Creado: {carpeta.name}/")
        except Exception as e:
            print(f"   ❌ Error creando {carpeta.name}: {e}")

    # 2. Crear archivos
    for ruta_archivo, contenido in archivos.items():
        try:
            if not ruta_archivo.exists():
                ruta_archivo.write_text(contenido, encoding="utf-8")
                print(f"   📄 Creado: {ruta_archivo.name}")
            else:
                print(f"   ⚠️ Saltado: {ruta_archivo.name} ya existe.")
        except Exception as e:
            print(f"   ❌ Error creando {ruta_archivo.name}: {e}")

    # Nota sobre db.json
    print("\nℹ️ Nota: 'db.json' no se ha creado explícitamente porque indicaste que se creará solo.")
    print("\n✨ ¡Estructura del proyecto lista! A codear. ✨")

if __name__ == "__main__":
    crear_estructura()