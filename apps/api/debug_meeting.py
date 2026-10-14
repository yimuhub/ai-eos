"""Quick test: simulate a full meeting turn manually."""
import asyncio
from sqlalchemy import select

async def main():
    from app.db.session import async_session
    from app.models.meeting import Meeting, MeetingParticipant
    from app.models.workflow import WorkflowExecution
    from app.meeting_engine import run_meeting_turn, _agents_of, _history

    async with async_session() as db:
        m = (await db.execute(select(Meeting).where(
            Meeting.id == "31a7678e-522a-4ab5-895d-19e70938002a"))).scalars().first()
        print("meeting:", m.id, m.status)
        agents = await _agents_of(db, m)
        print("agents:", [(a.name, a.id[:8]) for a in agents])
        history = await _history(db, m.id)
        print("history len:", len(history))
        print("phase logic: agent_speeches=", len([h for h in history if h["who"].startswith("agent:")]),
              "/ agents=", len(agents), "=> phase =",
              "opening" if len([h for h in history if h["who"].startswith("agent:")]) < len(agents)
              else "closing" if len([h for h in history if h["who"].startswith("agent:")]) >= len(agents) * 2
              else "discussion")

asyncio.run(main())
