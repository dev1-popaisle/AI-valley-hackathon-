"""Ask a ZooWork agent to search the public web for one catalog product."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from zoowork import (
    SessionEvent,
    ZooworkError,
    assistant_text,
    create_zoowork_client,
    is_run_finished,
    run_outcome,
)

AGENT_NAME = "catalog-product-researcher"
_AGENT_LOCK = asyncio.Lock()
ClientFactory = Callable[[], Any]


def load_zoowork_key() -> str | None:
    for path in (Path("api_config.json"), Path(__file__).resolve().parents[2] / "api_config.json"):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        key = payload.get("ZOOWORK_API_KEY2") or payload.get("ZOOWORK_API_KEY")
        if isinstance(key, str) and key.strip():
            return key.strip()
    return None


def briefing_request(product: dict[str, Any]) -> str:
    lines = [
        "Search the public web for this catalog product and write a short briefing.",
        f"Name: {product.get('name') or 'unknown'}",
        f"Brand: {product.get('brand') or 'unknown'}",
        f"Barcode: {product.get('barcode') or 'unknown'}",
        f"Catalog URL: {product.get('source_url') or 'unknown'}",
        "Use web_search, then open the most relevant pages with web_fetch.",
        "Say what the product is, who sells it, and a price only when a page states one.",
        "List the source URLs you opened.",
        "If the search does not find this product, say so. Do not invent prices or specifications.",
    ]
    return "\n".join(lines)


def answer_from_events(events: list[SessionEvent]) -> tuple[str, str | None]:
    parts: list[str] = []
    outcome: str | None = None
    for event in events:
        text = assistant_text(event).strip()
        if text:
            parts.append(text)
        if is_run_finished(event):
            outcome = run_outcome(event)
            break
    return "\n\n".join(parts).strip(), outcome


async def ask_product(
    product: dict[str, Any],
    *,
    client_factory: ClientFactory | None = None,
    state_path: Path | None = None,
) -> str:
    key = ""
    if client_factory is None:
        key = load_zoowork_key() or ""
        if not key:
            raise RuntimeError("ZOOWORK_API_KEY2 is not in api_config.json")
        timeout = httpx.Timeout(180.0, connect=30.0)
        client_factory = lambda: create_zoowork_client(api_key=key, timeout=timeout)
    path = state_path or _default_state_path()
    try:
        async with client_factory() as client:
            async with _AGENT_LOCK:
                agent_id = await _agent_id(client, path)
            session = await client.create_session(agent_id)
            session_id = str(session["session_id"])
            receipt = await client.post_events(
                agent_id,
                session_id,
                [{"type": "user.message", "content": briefing_request(product)}],
            )
            if not receipt or receipt[0].get("accepted") is False:
                raise RuntimeError("ZooWork did not accept the product question")
            parts: list[str] = []
            outcome: str | None = None
            async for event in client.stream_events(agent_id, session_id):
                text = assistant_text(event).strip()
                if text:
                    parts.append(text)
                if is_run_finished(event):
                    outcome = run_outcome(event)
                    break
    except ZooworkError as exc:
        raise RuntimeError(f"ZooWork request failed: {_public_error(exc, key)}") from None
    answer = "\n\n".join(parts).strip()
    if outcome != "succeeded":
        detail = answer or "ZooWork finished without an answer"
        raise RuntimeError(f"ZooWork run {outcome or 'did not finish'}: {detail}")
    if not answer:
        raise RuntimeError("ZooWork finished without an answer")
    return answer


async def _agent_id(client: Any, state_path: Path) -> str:
    saved = _read_id(state_path)
    if saved:
        try:
            agent = await client.get_agent(saved)
        except ZooworkError as exc:
            if exc.status != 404:
                raise
        else:
            await _ensure_running(client, saved, agent)
            return saved
    page = await client.list_agents(labels={"app": "product-crawler"})
    async for agent in page:
        declared = agent.get("declared") or {}
        if declared.get("name") == AGENT_NAME:
            agent_id = str(agent["agent_id"])
            await _ensure_running(client, agent_id, agent)
            _write_id(state_path, agent_id)
            return agent_id
    created = await client.create_agent(_agent_resource())
    agent_id = str(created["agent_id"])
    await client.start_agent(agent_id)
    await client.wait_until_running(agent_id, timeout=90)
    _write_id(state_path, agent_id)
    return agent_id


def _agent_resource() -> dict[str, Any]:
    return {
        "name": AGENT_NAME,
        "labels": {"app": "product-crawler"},
        "tool_policy": {"allow": ["web_search", "web_fetch"]},
        "persona": {
            "docs": [
                {
                    "name": "AGENTS.md",
                    "content": (
                        "You research one catalog product at a time. "
                        "Search the public web, open the relevant pages, and write a short factual briefing. "
                        "Include a price only when a page states one, and list the source URLs you opened. "
                        "If you cannot find the product, say so. Do not invent prices or specifications."
                    ),
                }
            ]
        },
    }


async def _ensure_running(client: Any, agent_id: str, agent: dict[str, Any]) -> None:
    status = agent.get("status") or {}
    if status.get("desired_state") == "running":
        return
    await client.start_agent(agent_id)
    await client.wait_until_running(agent_id, timeout=90)


def _default_state_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "zoowork-agent.json"


def _read_id(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    agent_id = payload.get("agent_id")
    return agent_id.strip() if isinstance(agent_id, str) else ""


def _write_id(path: Path, agent_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"agent_id": agent_id}) + "\n", encoding="utf-8")


def _public_error(exc: BaseException, key: str) -> str:
    text = str(exc) or exc.__class__.__name__
    if key:
        text = text.replace(key, "ZOOWORK_API_KEY2")
    return text
