"""Direct reproduction of the orchestrate background job to capture real errors."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv  # noqa


async def main():
    from app.core.config import settings
    settings.debug = False
    from app.background_jobs import _make_session_factory
    from app.models.task import Task, TaskStatus as TS
    from app.models.agent import Agent

    factory = _make_session_factory()
    tid = open('/tmp/aieos_tid').read().strip()
    ceo = open('/tmp/aieos_ceo').read().strip()

    async with factory() as session:
        p = await session.get(Task, tid)
        coord = await session.get(Agent, ceo)
        try:
            from app.orchestrator import orchestrate
            from app.task_engine import transition
            await transition(session, p, TS.RUNNING, "system", None)
            await orchestrate(session, p, coord)
            print("ORCHESTRATE OK:", p.status)
        except Exception:
            import traceback
            traceback.print_exc()


asyncio.run(main())
