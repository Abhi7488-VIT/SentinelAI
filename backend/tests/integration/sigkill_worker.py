"""Child process for the SIGKILL resume test: runs an investigation and blocks forever right
after the telemetry investigator's first tool, so the parent can kill it mid-node.

Usage: python -m tests.integration.sigkill_worker <org_id> <investigation_id> <marker_path>
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.investigation.checkpoint import open_checkpointer
from app.investigation.runner import run_investigation
from tests.conftest import TEST_DATABASE_URL
from tests.integration.test_investigation_graph import llm, scripted


async def main(org: uuid.UUID, inv: uuid.UUID, marker: Path) -> None:
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    async def block() -> None:
        marker.write_text("after-first-tool")  # noqa: ASYNC240 (tiny, once, test-only)
        await asyncio.sleep(3600)

    async with open_checkpointer(TEST_DATABASE_URL) as cp:
        await run_investigation(
            org_id=org,
            investigation_id=inv,
            sessionmaker=sm,
            llm=llm(sm, org, scripted()),
            checkpointer=cp,
            owner="doomed-worker",
            hooks={"telemetry.after_first_tool": block},
        )


if __name__ == "__main__":
    asyncio.run(main(uuid.UUID(sys.argv[1]), uuid.UUID(sys.argv[2]), Path(sys.argv[3])))
