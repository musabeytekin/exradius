"""Desk's HTTP side: sign-in, the API the frontend uses, the live stream and the frontend
itself (design §3, §8, §9)."""


import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict

from exradius_desk.auth import COOKIE, ProviderProblem, SignIn, SignInFailed
from exradius_desk.desk import Desk, Refused, Upload
from exradius_desk.mentions import EXRADIUS
from exradius_desk.models import Person

STATIC = Path(__file__).parent / "static"
HEARTBEAT = 25.0
UNSAFE = {"POST", "PATCH", "PUT", "DELETE"}


@dataclass(frozen=True)
class Web:
    """What the HTTP side needs besides the service."""

    origin: str
    secure_cookies: bool
    session_hours: float
    static: Path | None = STATIC


class TaskEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = None
    description: str | None = None
    status: str | None = None


class Assignment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assignee: str


def make_app(desk: Desk, signin: SignIn, web: Web) -> FastAPI:
    app = FastAPI(title="Exradius Desk", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        """Cross-site requests that change state are refused (design §3)."""
        path = request.url.path
        if request.method in UNSAFE and (path.startswith("/api/") or path == "/auth/logout"):
            origin = request.headers.get("origin")
            if origin is not None and origin != web.origin:
                return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(Refused)
    async def refused(_: Request, exc: Refused):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    async def current(request: Request) -> Person:
        person = await signin.person(request.cookies.get(COOKIE))
        if person is None:
            raise HTTPException(401, "sign in")
        return person

    Me = Annotated[Person, Depends(current)]

    async def uploads(files: list[UploadFile]) -> list[Upload]:
        out: list[Upload] = []
        total = 0
        for upload in files:
            data = await upload.read(desk.max_attachment_bytes - total + 1)
            total += len(data)
            if total > desk.max_attachment_bytes:
                raise Refused(413, f"the files total more than {desk.max_attachment_bytes / (1024 * 1024):g} MB")
            out.append(Upload(name=upload.filename or "file", content_type=upload.content_type or "", data=data))
        return out

    # -- sign-in ------------------------------------------------------------------------------------

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/auth/login")
    async def login(next: str | None = None):
        try:
            return RedirectResponse(await signin.start(next), status_code=303)
        except ProviderProblem as exc:
            return RedirectResponse("/signin?" + urlencode({"error": str(exc)}), status_code=303)

    @app.get("/auth/callback")
    async def callback(code: str | None = None, state: str | None = None, error: str | None = None,
                       error_description: str | None = None):
        try:
            signed_in, next_path = await signin.finish(code, state, error, error_description)
        except (SignInFailed, ProviderProblem) as exc:
            return RedirectResponse("/signin?" + urlencode({"error": str(exc)}), status_code=303)
        response = RedirectResponse(next_path, status_code=303)
        response.set_cookie(COOKIE, signed_in.token, max_age=int(web.session_hours * 3600), httponly=True,
                            secure=web.secure_cookies, samesite="lax", path="/")
        return response

    @app.post("/auth/logout", status_code=204)
    async def logout(request: Request):
        await signin.end(request.cookies.get(COOKIE))
        response = Response(status_code=204)
        response.delete_cookie(COOKIE, path="/")
        return response

    # -- the API ------------------------------------------------------------------------------------

    @app.get("/api/me")
    async def me(person: Me):
        return {"id": person.id, "name": person.name, "email": person.email, "groups": person.groups,
                "exradius": EXRADIUS}

    @app.get("/api/projects")
    async def projects(person: Me):
        return desk.projects(person)

    @app.get("/api/projects/{slug}/people")
    async def people(slug: str, person: Me):
        if not desk.allowed(person, slug):
            raise Refused(404, f"no project {slug}")
        return [{"id": p.id, "name": p.name} for p in await desk.people(slug)]

    @app.get("/api/tasks")
    async def tasks(person: Me, project: str | None = None, assignee: str | None = None, status: str | None = None,
                    limit: int = 100, offset: int = 0):
        return await desk.list_tasks(person, project=project, assignee=assignee, status=status, limit=limit,
                                     offset=offset)

    @app.post("/api/tasks", status_code=201)
    async def create(person: Me, project: Annotated[str, Form()], title: Annotated[str, Form()],
                     description: Annotated[str, Form()] = "", assignee: Annotated[str, Form()] = "none",
                     files: Annotated[list[UploadFile], File()] = []):  # noqa: B006 - FastAPI's form default
        return await desk.create_task(person, project=project, title=title, description=description,
                                      assignee=assignee, uploads=await uploads(files))

    @app.get("/api/tasks/{key}")
    async def task(key: str, person: Me):
        return await desk.get_task(person, key)

    @app.patch("/api/tasks/{key}", status_code=204)
    async def edit(key: str, body: TaskEdit, person: Me):
        await desk.edit_task(person, key, title=body.title, description=body.description, status=body.status)

    @app.post("/api/tasks/{key}/assignee", status_code=204)
    async def assign(key: str, body: Assignment, person: Me):
        await desk.assign(person, key, body.assignee)

    @app.post("/api/tasks/{key}/files", status_code=204)
    async def add_files(key: str, person: Me, files: Annotated[list[UploadFile], File()]):
        await desk.add_files(person, key, await uploads(files))

    @app.post("/api/tasks/{key}/comments", status_code=201)
    async def comment(key: str, person: Me, body: Annotated[str, Form()] = "",
                      files: Annotated[list[UploadFile], File()] = []):  # noqa: B006
        return await desk.comment(person, key, body, await uploads(files))

    @app.post("/api/tasks/{key}/cancel", status_code=204)
    async def cancel(key: str, person: Me):
        await desk.cancel(person, key)

    def attachment_headers(name: str) -> dict[str, str]:
        return {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
                "X-Content-Type-Options": "nosniff"}

    @app.get("/api/attachments/{attachment_id}")
    async def attachment(attachment_id: int, person: Me):
        row = await desk.attachment(person, attachment_id)
        return Response(row.data, media_type=row.content_type, headers=attachment_headers(row.name))

    @app.get("/api/artifacts/{artifact_id}")
    async def artifact(artifact_id: int, person: Me):
        row, upstream = await desk.artifact(person, artifact_id)

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(body(), media_type=row.content_type, headers=attachment_headers(row.name))

    @app.get("/api/stream")
    async def stream(request: Request, person: Me):
        listener = desk.hub.subscribe(lambda project: desk.allowed(person, project))

        async def events() -> AsyncIterator[str]:
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        change: dict[str, Any] = await asyncio.wait_for(listener.queue.get(), HEARTBEAT)
                    except TimeoutError:
                        if await request.is_disconnected():
                            return
                        yield ": keep-alive\n\n"
                        continue
                    yield f"event: task\ndata: {json.dumps(change)}\n\n"
            finally:
                desk.hub.unsubscribe(listener)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"])
    async def unknown(rest: str):
        raise HTTPException(404, "not found")

    # -- the frontend ---------------------------------------------------------------------------------

    static = web.static
    if static is not None and (static / "index.html").is_file():
        root = static.resolve()

        @app.get("/{path:path}")
        async def frontend(path: str):
            candidate = (root / path).resolve()
            if path and candidate.is_file() and root in candidate.parents:
                cache = "public, max-age=31536000, immutable" if path.startswith("assets/") else "no-cache"
                return FileResponse(candidate, headers={"Cache-Control": cache})
            return FileResponse(root / "index.html", headers={"Cache-Control": "no-cache"})

    return app
