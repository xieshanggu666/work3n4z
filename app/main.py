# -*- coding: utf-8 -*-
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from .core.database import ensure_schema
from .api.router import router

BASE = Path(__file__).resolve().parent.parent
STATIC = BASE / "static"

app = FastAPI(title="末日地堡生存", version="1.0.0")

# 建表并对旧库做增量迁移（幂等）
ensure_schema()

app.include_router(router)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")