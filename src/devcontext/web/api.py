import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sqlite3
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import portalocker
from pydantic import BaseModel, Field, field_validator

from devcontext.config import Settings, project_root
from .manager import RunManager
from .protocol import RunError, TERMINAL
from .store import RunStore


class SessionInput(BaseModel):
    title: str = Field(default="新会话", min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Title must not be blank")
        return value.strip()


class RunInput(BaseModel):
    query: str = Field(min_length=1, max_length=10000)
    profile: Literal["fast", "full"] = "fast"
    client_request_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("query")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Query must not be blank")
        return value.strip()


def create_app(*, database_path=None, settings=None, service=None, max_workers=None, frontend_path=None, shutdown_timeout=30):
    path = Path(database_path or os.environ.get("DEVCONTEXT_WEB_DATABASE", project_root() / "data/web/devcontext.sqlite3")).resolve()
    workers = max_workers or int(os.environ.get("DEVCONTEXT_WEB_WORKERS", "2"))
    if not 1 <= workers <= 16:
        raise ValueError("Web worker count must be between 1 and 16")
    dist = Path(frontend_path or project_root() / "frontend/dist")

    @asynccontextmanager
    async def lifespan(app):
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = portalocker.Lock(str(path) + ".lock", timeout=0)
        await asyncio.to_thread(lock.acquire)
        manager = None
        try:
            store = await asyncio.to_thread(RunStore, path)
            await asyncio.to_thread(store.recover)
            manager = RunManager(store, settings=settings or Settings(), service=service, max_workers=workers)
            app.state.manager = manager
            yield
        finally:
            if manager:
                await asyncio.to_thread(manager.close, shutdown_timeout)
            # An in-flight thread still owns this database until process exit.
            if manager and manager.active:
                app.state.database_lock = lock
            else:
                await asyncio.to_thread(lock.release)

    app = FastAPI(title="DevContext Web V1", lifespan=lifespan)

    @app.exception_handler(RunError)
    async def run_error(request, exc):
        return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code}})

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request, exc):
        return JSONResponse(status_code=503, content={"error": {"code": "storage_unavailable"}})

    # Default validation errors echo rejected input, so omit it from the public response.
    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(status_code=422, content={"error": {"code": "invalid_request"}})

    @app.get("/health")
    def health(request: Request):
        request.app.state.manager.store.health()
        return {"status": "ok"}

    @app.get("/api/v1/sessions")
    def sessions(request: Request, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
        return {"items": request.app.state.manager.store.list_sessions(limit, offset)}

    @app.post("/api/v1/sessions", status_code=201)
    def create_session(body: SessionInput, request: Request):
        return request.app.state.manager.store.create_session(request.app.state.manager.public.clean(body.title))

    @app.get("/api/v1/sessions/{session_id}")
    def session(session_id: str, request: Request):
        manager = request.app.state.manager
        value = manager.store.session(session_id)
        for run in value["runs"]:
            manager.get(run["id"])
        return value

    @app.patch("/api/v1/sessions/{session_id}")
    def rename(session_id: str, body: SessionInput, request: Request):
        return request.app.state.manager.store.rename(session_id, request.app.state.manager.public.clean(body.title))

    @app.delete("/api/v1/sessions/{session_id}", status_code=204)
    def delete(session_id: str, request: Request):
        request.app.state.manager.delete_session(session_id)

    @app.post("/api/v1/sessions/{session_id}/runs", status_code=202)
    def run(session_id: str, body: RunInput, request: Request):
        return request.app.state.manager.submit(session_id, body.query, body.profile, body.client_request_id)

    @app.get("/api/v1/runs/{run_id}")
    def get_run(run_id: str, request: Request):
        return request.app.state.manager.get(run_id)

    @app.post("/api/v1/runs/{run_id}/cancel")
    def cancel(run_id: str, request: Request):
        return request.app.state.manager.cancel(run_id)

    @app.get("/api/v1/runs/{run_id}/events")
    async def events(run_id: str, request: Request, after: int = Query(0, ge=0)):
        manager = request.app.state.manager
        snapshot = await asyncio.to_thread(manager.get, run_id)
        last = request.headers.get("last-event-id")
        try:
            cursor = int(last) if last is not None else after
            if cursor < 0 or cursor > snapshot["last_seq"]:
                raise ValueError()
        except ValueError:
            raise HTTPException(400, detail="invalid_event_cursor") from None

        async def stream():
            nonlocal cursor
            pair = manager.subscribe(run_id)
            event = pair[1]
            try:
                while not await request.is_disconnected():
                    event.clear()
                    batch = await asyncio.to_thread(manager.store.events, run_id, cursor)
                    for value in batch:
                        cursor = value["seq"]
                        yield f"id: {cursor}\nevent: {value['type']}\ndata: {json.dumps(value, ensure_ascii=False)}\n\n"
                    state = await asyncio.to_thread(manager.get, run_id)
                    if cursor >= state["last_seq"] and state["status"] in TERMINAL:
                        break
                    if batch:
                        continue
                    try:
                        await asyncio.wait_for(event.wait(), timeout=15)
                    except TimeoutError:
                        yield ": heartbeat\n\n"
            finally:
                manager.unsubscribe(run_id, pair)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        if path.startswith("api/") or path == "health":
            raise HTTPException(404)
        if not (dist / "index.html").is_file():
            return JSONResponse(status_code=503, content={"error": {"code": "frontend_not_built"}})
        return FileResponse(dist / "index.html")

    return app
