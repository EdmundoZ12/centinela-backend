# Centinela backend

Esqueleto del backend con Python 3.12, FastAPI, Pydantic y Uvicorn.

## Estructura

```text
app/
  __init__.py
  main.py
  api/
    __init__.py
    router.py
  core/
    __init__.py
  schemas/
    __init__.py
    health.py
```

## Ejecutar en PowerShell

Desde la raiz del repositorio, con Python 3.12 instalado:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

- API: http://127.0.0.1:8000
- Swagger: http://127.0.0.1:8000/docs
- Estado: `GET /health` devuelve `{"status":"ok"}`.

Las rutas se definen en `app/api/` y los esquemas Pydantic en `app/schemas/`.
