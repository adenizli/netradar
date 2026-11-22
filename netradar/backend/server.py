"""Local FastAPI snapshot, event ingestion and WebSocket streaming service."""
import asyncio
from contextlib import asynccontextmanager
from ipaddress import ip_address
import os

from anyio import CancelScope

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import ValidationError

from netradar.backend.diagnostic_schema import Telemetry
from netradar.backend.diagnostics import Diagnostics, MAX_BODY_BYTES, prometheus
from netradar.telemetry import process_resources

from netradar.backend.schemas import EventInput
from netradar.backend.stream import TrafficService


DEFAULT_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


def create_app(*, service: TrafficService | None = None,
               allowed_origins: tuple[str, ...] | None = None,
               send_timeout: float = 5.0, diagnostics: Diagnostics | None = None) -> FastAPI:
    if send_timeout <= 0:
        raise ValueError("Send timeout must be positive")
    if allowed_origins is None:
        configured = os.environ.get("NETRADAR_ALLOWED_ORIGINS")
        allowed_origins = (
            tuple(o.strip() for o in configured.split(",") if o.strip())
            if configured is not None else DEFAULT_ORIGINS
        )
    # Demo only: "*" lets any web page read the feed and POST events (no auth).
    allow_any_origin = "*" in allowed_origins

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.traffic = service if service is not None else TrafficService()
        app.state.diagnostics = diagnostics if diagnostics is not None else Diagnostics(
            expected_producer=os.getenv("NETRADAR_EXPECT_RECEIVER") == "1",
        )
        try:
            yield
        finally:
            await app.state.traffic.close()

    app = FastAPI(title="NetRadar live backend", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"] if allow_any_origin else list(allowed_origins),
        allow_methods=["GET", "POST"], allow_headers=["Content-Type"],
    )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        if request.url.path == "/api/events":
            request.app.state.diagnostics.rejected += 1
        # Do not echo arbitrary input/NaN or exception objects into JSON errors.
        return JSONResponse(status_code=422, content={"detail": [
            {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
            for error in exc.errors()
        ]})

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "schema_version": "1.1"}

    async def diagnostic_report(request):
        resources = await asyncio.to_thread(process_resources)
        return request.app.state.diagnostics.report(resources, request.app.state.traffic.subscriber_count)

    @app.get("/api/diagnostics")
    async def diagnostic_snapshot(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return await diagnostic_report(request)

    @app.get("/metrics")
    async def metrics(request: Request):
        return PlainTextResponse(
            prometheus(await diagnostic_report(request)),
            media_type="text/plain; version=0.0.4",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/diagnostics", status_code=204)
    async def ingest_diagnostics(request: Request):
        try:
            loopback = request.client is not None and ip_address(request.client.host).is_loopback
        except ValueError:
            loopback = False
        if not loopback or any(
            header in request.headers for header in ("origin", "sec-fetch-site", "forwarded", "x-forwarded-for")
        ):
            raise HTTPException(403, "Diagnostics ingestion requires a local producer")
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise HTTPException(415, "Diagnostics require JSON")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_BODY_BYTES:
                raise HTTPException(413, "Diagnostics snapshot exceeds the size limit")
            body.extend(chunk)
        try:
            update = Telemetry.model_validate_json(body)
        except ValidationError:
            raise HTTPException(422, "Invalid diagnostics snapshot") from None
        try:
            request.app.state.diagnostics.ingest(update.model_dump(exclude_unset=True))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return Response(status_code=204)

    @app.get("/api/aircraft")
    async def aircraft(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return await request.app.state.traffic.snapshot()

    @app.post("/api/events")
    async def ingest(payload: EventInput, request: Request, response: Response):
        origin = request.headers.get("origin")
        diagnostics = request.app.state.diagnostics
        if origin is not None and not allow_any_origin and origin not in allowed_origins:
            diagnostics.rejected += 1
            raise HTTPException(403, "Origin is not allowed")
        try:
            event = payload.to_event()
        except (ValueError, TypeError, OverflowError):
            diagnostics.rejected += 1
            raise HTTPException(422, "Invalid decoded event") from None
        response.headers["Cache-Control"] = "no-store"
        update = await request.app.state.traffic.ingest(event)
        diagnostics.accepted += 1
        return update

    @app.websocket("/ws/aircraft")
    async def aircraft_stream(websocket: WebSocket):
        origin = websocket.headers.get("origin")
        if origin is not None and not allow_any_origin and origin not in allowed_origins:
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
