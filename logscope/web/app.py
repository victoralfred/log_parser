"""FastAPI application factory."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from logscope.core.registry import Registry
from logscope.core.store import Store
from logscope.web.api import router

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(db_path: str = "logscope.db", plugin_dirs=None,
               uploads_root: str | Path = "uploads") -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        registry = Registry(plugin_dirs)
        registry.load()
        app.state.registry = registry
        app.state.store = Store(db_path)
        app.state.uploads_root = Path(uploads_root)
        yield

    app = FastAPI(title="logscope", lifespan=lifespan)
    app.include_router(router)
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
