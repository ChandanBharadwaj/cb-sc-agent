"""Agents SDK Session backed by Postgres (``chat_session`` / ``chat_message``) so Q&A conversations
are persistent and traceable per user."""

from __future__ import annotations

import asyncio
from typing import Any

from agents.memory import SessionABC, SessionSettings

from sanctions_agent.db.engine import fetch_all, fetch_one, jsonb, tx


def _role_and_text(item: Any) -> tuple[str, str | None]:
    if isinstance(item, dict):
        role = str(item.get("role") or item.get("type") or "item")
        content = item.get("content")
        if isinstance(content, str):
            return role, content
        if isinstance(content, list):
            texts = [str(c["text"]) for c in content if isinstance(c, dict) and c.get("text")]
            return role, "\n".join(texts) or None
        return role, None
    return "item", None


class PostgresSession(SessionABC):
    session_settings: SessionSettings | None = None

    def __init__(self, session_id: str, user_id: str) -> None:
        self.session_id = session_id
        self.user_id = user_id
        with tx() as conn:
            conn.execute(
                "INSERT INTO chat_session (session_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (session_id, user_id),
            )

    def _get(self, limit: int | None) -> list[Any]:
        with tx() as conn:
            if limit:
                rows = fetch_all(
                    conn,
                    """SELECT item FROM (SELECT message_id, item FROM chat_message WHERE session_id = %s
                                          ORDER BY message_id DESC LIMIT %s) t ORDER BY message_id""",
                    (self.session_id, limit),
                )
            else:
                rows = fetch_all(
                    conn,
                    "SELECT item FROM chat_message WHERE session_id = %s ORDER BY message_id",
                    (self.session_id,),
                )
        return [r["item"] for r in rows]

    def _add(self, items: list[Any]) -> None:
        with tx() as conn:
            for it in items:
                role, text = _role_and_text(it)
                conn.execute(
                    "INSERT INTO chat_message (session_id, role, content, item) VALUES (%s, %s, %s, %s)",
                    (self.session_id, role, text, jsonb(it)),
                )

    def _pop(self) -> Any:
        with tx() as conn:
            row = fetch_one(
                conn,
                """DELETE FROM chat_message WHERE message_id = (SELECT max(message_id) FROM chat_message
                                     WHERE session_id = %s) RETURNING item""",
                (self.session_id,),
            )
        return row["item"] if row else None

    async def get_items(self, limit: int | None = None) -> list[Any]:
        return await asyncio.to_thread(self._get, limit)

    async def add_items(self, items: list[Any]) -> None:
        await asyncio.to_thread(self._add, items)

    async def pop_item(self) -> Any:
        return await asyncio.to_thread(self._pop)

    async def clear_session(self) -> None:
        def _clear() -> None:
            with tx() as conn:
                conn.execute("DELETE FROM chat_message WHERE session_id = %s", (self.session_id,))

        await asyncio.to_thread(_clear)
