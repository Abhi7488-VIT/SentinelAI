"""Controller HTTP API (:8090, testbed network; published on 127.0.0.1 only).

Every endpoint except /healthz needs `Authorization: Bearer <CONTROLLER_TOKEN>`.

POST /scenarios/{id}/start   full run in the background (same as `run`); 202 + run id.
                             Idempotent: if that scenario is already running, returns its run.
POST /scenarios/{id}/stop    ends the active run early (stop + recovery still run)
GET  /runs/{run_id}          the run record (updated as the run progresses)
GET  /scenarios              definitions and plugins
"""

from __future__ import annotations

import asyncio
import hmac
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from harness.controller import groundtruth, service
from harness.controller.lock import RunLock
from harness.controller.primitives import ControllerSettings
from harness.controller.runner import RunOptions


class StartIn(BaseModel):
    seed: int | None = None
    baseline_s: int = Field(default=120, ge=30, le=900)
    observe_s: int = Field(default=300, ge=30, le=900)
    recovery_s: int = Field(default=180, ge=60, le=900)
    dry_run: bool = False


def build_app(settings: ControllerSettings) -> FastAPI:
    if not settings.controller_token:
        raise RuntimeError("CONTROLLER_TOKEN is required to serve the HTTP API")
    app = FastAPI(title="testbed-controller", docs_url=None, redoc_url=None)
    tasks: set[asyncio.Task[Any]] = set()

    def auth(authorization: Annotated[str | None, Header()] = None) -> None:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            token.encode(), settings.controller_token.encode()
        ):
            raise HTTPException(status_code=401, detail="unauthorized")

    authed = [Depends(auth)]

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/scenarios", dependencies=authed)
    async def scenarios() -> list[dict[str, Any]]:
        return service.list_scenarios(settings)

    @app.post("/scenarios/{scenario_id}/start", status_code=202, dependencies=authed)
    async def start(scenario_id: str, body: StartIn) -> dict[str, Any]:
        if scenario_id not in groundtruth.definition_ids(settings.scenarios_dir):
            raise HTTPException(status_code=404, detail=f"unknown scenario {scenario_id}")
        options = RunOptions(
            seed=settings.default_seed if body.seed is None else body.seed,
            baseline_s=body.baseline_s,
            observe_s=body.observe_s,
            recovery_s=body.recovery_s,
            mode="dry_run" if body.dry_run else "full",
        )
        try:
            run, lock = service.prepare_run(settings, scenario_id, options)
        except service.BusyError as busy:
            if busy.holder.get("scenario_id") == scenario_id:
                return {"run_id": busy.holder["run_id"], "already_running": True}
            raise HTTPException(status_code=409, detail="another scenario is running") from None
        task = asyncio.create_task(service.execute(run, lock))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {"run_id": run.run_id, "already_running": False}

    @app.post("/scenarios/{scenario_id}/stop", dependencies=authed)
    async def stop(scenario_id: str) -> dict[str, Any]:
        holder = RunLock(settings.runs_dir).current()
        if not holder or holder.get("scenario_id") != scenario_id:
            raise HTTPException(status_code=404, detail="that scenario is not running")
        return await service.manual_stop(settings, scenario_id)

    @app.get("/runs/{run_id}", dependencies=authed)
    async def get_run(run_id: str) -> dict[str, Any]:
        try:
            record = groundtruth.find_record(settings.runs_dir, run_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="not found") from None
        if record is None:
            raise HTTPException(status_code=404, detail="not found")
        return record

    return app
