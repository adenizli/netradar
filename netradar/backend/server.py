"""Local FastAPI snapshot, event ingestion and WebSocket streaming service."""
import asyncio
from contextlib import asynccontextmanager
import os

from anyio import CancelScope

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from netradar.backend.schemas import EventInput
from netradar.backend.stream import TrafficService


DEFAULT_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


def create_app(*, service: TrafficService | None = None,
               allowed_origins: tuple[str, ...] | None = None,
               send_timeout: float = 5.0) -> FastAPI:
    if send_timeout <= 0:
        raise ValueError("Send timeout must be positive")
    if allowed_origins is None:
        configured = os.environ.get("NETRADAR_ALLOWED_ORIGINS")
        allowed_origins = (
            tuple(o.strip() for o in configured.split(",") if o.strip())
            if configured is not None else DEFAULT_ORIGINS
        )
    if "*" in allowed_origins:
        raise ValueError("Configure explicit frontend origins")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.traffic = service if service is not None else TrafficService()
        try:
            yield
        finally:
            await app.state.traffic.close()

    app = FastAPI(title="NetRadar live backend", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=list(allowed_origins),
        allow_methods=["GET", "POST"], allow_headers=["Content-Type"],
    )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # Do not echo arbitrary input/NaN or exception objects into JSON errors.
        return JSONResponse(status_code=422, content={"detail": [
            {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
            for error in exc.errors()
        ]})

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "schema_version": "1.1"}

    @app.get("/api/aircraft")
    async def aircraft(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return await request.app.state.traffic.snapshot()

    @app.post("/api/events")
    async def ingest(payload: EventInput, request: Request, response: Response):
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            raise HTTPException(403, "Origin is not allowed")
        try:
            event = payload.to_event()
        except (ValueError, TypeError, OverflowError):
            raise HTTPException(422, "Invalid decoded event") from None
        response.headers["Cache-Control"] = "no-store"
        return await request.app.state.traffic.ingest(event)

    @app.websocket("/ws/aircraft")
    async def aircraft_stream(websocket: WebSocket):
        origin = websocket.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            await websocket.close(code=1008)
            return
        traffic = websocket.app.state.traffic
        # Register before handshake: the UI subscribes, then fetches its snapshot.
        subscription = traffic.subscribe()
        tasks = []

        async def send_updates():
            while True:
                update = await subscription.queue.get()
                if update is None:
                    await websocket.close(code=subscription.close_code or 1001)
                    return
                try:
                    await asyncio.wait_for(websocket.send_json(update), timeout=send_timeout)
                except asyncio.TimeoutError:
                    await websocket.close(code=1013)
                    return

        async def watch_disconnect():
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                # This endpoint is a server-to-client aircraft feed.
                await websocket.close(code=1008)
                return

        try:
            await websocket.accept()
            tasks = [asyncio.create_task(send_updates()), asyncio.create_task(watch_disconnect())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (WebSocketDisconnect, OSError):
            pass
        finally:
            traffic.unsubscribe(subscription)
            for task in tasks:
                task.cancel()
            with CancelScope(shield=True):
                await asyncio.gather(*tasks, return_exceptions=True)

    return app


app = create_app()
