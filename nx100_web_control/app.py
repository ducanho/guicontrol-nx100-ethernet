"""Local-only web control. Run python app.py or python app.py --mock."""

import argparse
import ipaddress
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from robot.nx100_controller import ControlError, NX100Controller

BASE = Path(__file__).resolve().parent


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Connection(Input):
    ip: str = "192.168.255.1"
    port: int = Field(default=80, ge=1, le=65535, strict=True)

    @field_validator("ip")
    @classmethod
    def validate_ip(cls, value):
        return str(ipaddress.IPv4Address(value))


class CartesianMove(Input):
    motion: Literal["MOVL", "MOVJ"]
    speed: float = Field(gt=0, le=100)
    x: float
    y: float
    z: float
    rx: float
    ry: float
    rz: float
    reference_id: str | None = Field(default=None, min_length=1, max_length=64)


class JointMove(Input):
    speed: float = Field(ge=0.01, le=100)
    s: int = Field(strict=True)
    l: int = Field(strict=True)  # noqa: E741 — NX100 L-axis API name
    u: int = Field(strict=True)
    r: int = Field(strict=True)
    b: int = Field(strict=True)
    t: int = Field(strict=True)


def create_app(mock=False, controller=None):
    config = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    config.setdefault("auto_send_delay_ms", 50)
    if not 100 <= config["auto_send_delay_ms"] <= 2000:
        raise ValueError("auto_send_delay_ms must be 100..2000")
    if not 500 <= config["poll_interval_ms"] <= 10000:
        raise ValueError("poll_interval_ms must be 500..10000")
    robot = controller or NX100Controller(config, mock=mock)

    @asynccontextmanager
    async def lifespan(app):
        robot.start_monitor()
        yield
        robot.close()

    app = FastAPI(title="NX100 Web Control", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.robot = robot

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # Bind loopback AND reject foreign Host/Origin (including DNS rebinding).
        host = urlsplit("http://" + request.headers.get("host", "")).hostname
        if host not in {"127.0.0.1", "localhost"}:
            return JSONResponse(
                {"code": "LOCAL_ONLY", "message": "Local host required"}, status_code=403
            )
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
            return JSONResponse(
                {"code": "ORIGIN_REJECTED", "message": "Same-origin required"}, status_code=403
            )
        if request.method == "POST" and request.headers.get("x-nx100-control") != "local":
            return JSONResponse(
                {"code": "HEADER_REQUIRED", "message": "X-NX100-Control: local required"},
                status_code=403,
            )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; connect-src 'self'; style-src 'self'; script-src 'self'; "
            "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(ControlError)
    async def control_error(request, exc):
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=exc.status)

    @app.exception_handler(ValueError)
    async def bad_value(request, exc):
        return JSONResponse({"code": "INVALID_INPUT", "message": str(exc)}, status_code=422)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Omit raw inputs/contexts: nonfinite JSON must also produce a valid JSON error.
        messages = [
            ".".join(map(str, error["loc"])) + ": " + error["msg"] for error in exc.errors()
        ]
        return JSONResponse(
            {"code": "INVALID_INPUT", "message": "; ".join(messages)}, status_code=422
        )

    @app.get("/")
    def index():
        return FileResponse(BASE / "templates" / "index.html")

    @app.get("/api/config")
    def get_config():
        return {**config, "mode": robot.state["mode"], "move_joint_supported": False}

    @app.get("/api/status")
    def status(refresh: bool = False):
        return robot.read_status() if refresh else robot.snapshot()

    @app.get("/api/position/cartesian")
    def cartesian():
        return robot.read_cartesian()

    @app.get("/api/position/joint")
    def joint():
        return robot.read_joint()

    @app.get("/api/logs")
    def logs(after: int = Query(default=0, ge=0)):
        return {"events": robot.events(after)}

    @app.post("/api/connect")
    def connect(body: Connection):
        return robot.connect(body.ip, body.port)

    @app.post("/api/disconnect")
    def disconnect():
        return robot.disconnect()

    @app.post("/api/move/cartesian")
    def move(body: CartesianMove):
        return robot.move_cartesian(
            body.motion,
            body.speed,
            [body.x, body.y, body.z, body.rx, body.ry, body.rz],
            body.reference_id,
        )

    @app.post("/api/move/joint")
    def move_joint(body: JointMove):
        return robot.move_joint(**body.model_dump())

    @app.post("/api/hold")
    def hold():
        return robot.hold()

    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock", action="store_true", help="Simulator only; never accesses TCP")
    args = parser.parse_args()
    application = create_app(mock=args.mock)
    print("NX100 Web Control\nLocal URL: http://127.0.0.1:8000", flush=True)
    print(
        f"Robot default: {application.state.robot.config['robot_ip']}:{application.state.robot.config['robot_port']}",
        flush=True,
    )
    print("MODE: " + application.state.robot.state["mode"] + " | No auto-connect", flush=True)
    import uvicorn

    uvicorn.run(application, host="127.0.0.1", port=8000, workers=1)
