"""Post catalog watches and agent reviews to the Band shopping agent.

The shopping agent writes into one chat room. The owner sees that room in the
Band dashboard at https://app.band.ai/.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "https://api.band.ai/api/v1"
ROOM_TITLE = "Product catalog log"
_LOCK = threading.RLock()


def load_band_key() -> str | None:
    for path in (Path("api_config.json"), Path(__file__).resolve().parents[2] / "api_config.json"):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        key = payload.get("BAND_API_KEY") if isinstance(payload, dict) else None
        if isinstance(key, str) and key.strip():
            return key.strip()
    return None


def catalog_review(products: list[dict[str, Any]]) -> str:
    lines = ["Catalog watch. These products are monitored by URL:"]
    for product in products:
        lines.append(f"- {_label(product)}: {_url(product)}")
    return _clip("\n".join(lines))


def tavily_review(product: dict[str, Any], result: dict[str, Any]) -> str:
    answer = str(result.get("answer") or "").strip()
    sources = result.get("results") if isinstance(result.get("results"), list) else []
    lines = [
        "Tavily review",
        f"Product: {_label(product)}",
        f"URL: {_url(product)}",
        f"Tavily answer: {answer or 'Tavily did not return an answer.'}",
        "Sources:",
    ]
    if not sources:
        lines.append("- none")
    for item in sources[:5]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "Untitled page").strip()
        url = str(item.get("url") or "").strip()
        lines.append(f"- {title} ({url})" if url else f"- {title}")
    lines.append(f"Assessment: {_tavily_assessment(product, answer, sources)}")
    return _clip("\n".join(lines))


def zoowork_review(product: dict[str, Any], answer: str) -> str:
    briefing = answer.strip() or "ZooWork did not return a briefing."
    lines = [
        "ZooWork review",
        f"Product: {_label(product)}",
        f"URL: {_url(product)}",
        "ZooWork briefing:",
        briefing[:1500],
        f"Assessment: {_zoowork_assessment(product, answer)}",
    ]
    return _clip("\n".join(lines))


def jev_review(
    question: str,
    products: list[dict[str, Any]],
    decision: dict[str, Any],
) -> str:
    """Same Choice, Noul, and Score figures the Decide button shows on the page."""
    score = decision.get("score") if isinstance(decision.get("score"), dict) else {}
    levels = score.get("levels") if isinstance(score.get("levels"), list) else []
    position = score.get("score")
    if isinstance(position, (int, float)) and levels:
        position_text = f"{float(position):.2f} of {max(len(levels) - 1, 0)}"
    elif isinstance(position, (int, float)):
        position_text = f"{float(position):.2f}"
    else:
        position_text = "not returned"
    noul = decision.get("noul")
    no = _percent(1 - float(noul)) if isinstance(noul, (int, float)) else "not returned"
    lines = [
        "Jev output",
        f"Question: {question.strip()}",
        "Checked products:",
    ]
    for product in products:
        lines.append(f"- {_label(product)}: {_url(product)}")
    lines.extend(
        [
            "Choice",
            f"Selected: {decision.get('product_name') or decision.get('choice') or ''}",
            f"Confidence: {_percent(decision.get('confidence'))}",
            "Probability for each option",
        ]
    )
    options = decision.get("probabilities") if isinstance(decision.get("probabilities"), list) else []
    if not options:
        lines.append("- none")
    for option in options:
        if isinstance(option, dict):
            lines.append(f"- {option.get('name') or option.get('id')}: {_percent(option.get('probability'))}")
    lines.extend(
        [
            "Noul",
            f"Yes: {_percent(noul)}",
            f"No: {no}",
            "Score",
            f"Score: {position_text}",
            f"Confidence: {_percent(score.get('confidence'))}",
            "Probability for each level",
        ]
    )
    if not levels:
        lines.append("- none")
    for level in levels:
        if isinstance(level, dict):
            lines.append(f"- {level.get('description')}: {_percent(level.get('probability'))}")
    lines.append(f"Assessment: {_jev_assessment(decision)}")
    lines.append("LLM (with Jev input) :")
    insight = str(decision.get("insight") or "").strip()
    lines.append(insight or "OpenAI did not return an explanation.")
    return _clip("\n".join(lines))


def ensure_catalog_room(
    *,
    api_key: str | None = None,
    http: Any | None = None,
    state_path: Path | None = None,
) -> str:
    """Open the Product catalog log room and keep its title."""
    key = api_key if api_key is not None else (load_band_key() or "")
    if not key:
        raise RuntimeError("BAND_API_KEY is not in api_config.json")
    owned = http is None
    client = http if http is not None else httpx.Client(timeout=30.0)
    try:
        with _LOCK:
            room = _ensure_room(client, key, _state_file(state_path))
        return str(room["chat_id"])
    finally:
        if owned:
            client.close()


def publish_catalog(
    products: list[dict[str, Any]],
    *,
    api_key: str | None = None,
    http: Any | None = None,
    state_path: Path | None = None,
) -> bool:
    """Post catalog URLs that have not been announced yet."""
    with _LOCK:
        fresh = _unannounced(products, state_path)
        if not fresh:
            return False
        _deliver(
            "catalog",
            catalog_review(fresh),
            {"product_ids": [str(item.get("id") or "") for item in fresh]},
            api_key=api_key,
            http=http,
            state_path=state_path,
        )
        _mark_announced(fresh, state_path)
        return True


def publish_tavily(
    product: dict[str, Any],
    result: dict[str, Any],
    *,
    api_key: str | None = None,
    http: Any | None = None,
    state_path: Path | None = None,
) -> None:
    _deliver(
        "tavily",
        tavily_review(product, result),
        {"product_id": product.get("id"), "source_url": product.get("source_url")},
        api_key=api_key,
        http=http,
        state_path=state_path,
    )


def publish_zoowork(
    product: dict[str, Any],
    answer: str,
    *,
    api_key: str | None = None,
    http: Any | None = None,
    state_path: Path | None = None,
) -> None:
    _deliver(
        "zoowork",
        zoowork_review(product, answer),
        {"product_id": product.get("id"), "source_url": product.get("source_url")},
        api_key=api_key,
        http=http,
        state_path=state_path,
    )


def publish_jev(
    question: str,
    products: list[dict[str, Any]],
    decision: dict[str, Any],
    *,
    api_key: str | None = None,
    http: Any | None = None,
    state_path: Path | None = None,
) -> None:
    _deliver(
        "jev",
        jev_review(question, products, decision),
        {"product_ids": [str(item.get("id") or "") for item in products]},
        api_key=api_key,
        http=http,
        state_path=state_path,
    )


def _deliver(
    source: str,
    review: str,
    metadata: dict[str, Any],
    *,
    api_key: str | None,
    http: Any | None,
    state_path: Path | None,
) -> None:
    key = api_key if api_key is not None else (load_band_key() or "")
    if not key:
        raise RuntimeError("BAND_API_KEY is not in api_config.json")
    owned = http is None
    client = http if http is not None else httpx.Client(timeout=30.0)
    try:
        with _LOCK:
            room = _ensure_room(client, key, _state_file(state_path))
            profile = room["profile"]
            handle = _owner_handle(str(profile.get("handle") or ""))
            content = f"@{handle} {review}"
            _send(
                client,
                key,
                "POST",
                f"/agent/chats/{room['chat_id']}/messages",
                json={
                    "message": {
                        "content": content,
                        "mentions": [
                            {
                                "id": profile["owner_uuid"],
                                "handle": handle,
                                "kind": "mention",
                            }
                        ],
                    }
                },
            )
            _send(
                client,
                key,
                "POST",
                f"/agent/chats/{room['chat_id']}/events",
                json={
                    "event": {
                        "content": content,
                        "message_type": "attention",
                        "metadata": {"kind": "review", "blocking": False, "source": source, **metadata},
                    }
                },
            )
    finally:
        if owned:
            client.close()


def _ensure_room(client: Any, key: str, state_path: Path) -> dict[str, Any]:
    profile = _data(client, key, "GET", "/agent/me")
    if not isinstance(profile, dict) or not profile.get("owner_uuid"):
        raise RuntimeError("Band did not return the shopping agent")
    state = _read_state(state_path)
    chat_id = state.get("chat_id")
    if not isinstance(chat_id, str) or not chat_id:
        chat_id = _find_or_create_room(client, key)
        state["chat_id"] = chat_id
        _write_state(state_path, state)
    _rename_room(client, key, chat_id)
    _ensure_owner(client, key, chat_id, str(profile["owner_uuid"]))
    return {"chat_id": chat_id, "profile": profile}


def _rename_room(client: Any, key: str, chat_id: str) -> None:
    current = _data(client, key, "GET", f"/agent/chats/{chat_id}")
    if isinstance(current, dict) and current.get("title") == ROOM_TITLE:
        return
    _send(client, key, "PATCH", f"/agent/chats/{chat_id}", json={"chat": {"title": ROOM_TITLE}})


def _find_or_create_room(client: Any, key: str) -> str:
    listed = _data(client, key, "GET", "/agent/chats", params={"page_size": 50})
    rooms = listed if isinstance(listed, list) else []
    for room in rooms:
        if isinstance(room, dict) and room.get("title") == ROOM_TITLE and room.get("id"):
            return str(room["id"])
    created = _data(client, key, "POST", "/agent/chats", json={"chat": {"title": ROOM_TITLE}})
    if not isinstance(created, dict) or not created.get("id"):
        raise RuntimeError("Band did not create the catalog review room")
    return str(created["id"])


def _ensure_owner(client: Any, key: str, chat_id: str, owner_id: str) -> None:
    listed = _data(client, key, "GET", f"/agent/chats/{chat_id}/participants")
    people = listed if isinstance(listed, list) else []
    if any(isinstance(person, dict) and person.get("id") == owner_id for person in people):
        return
    _send(
        client,
        key,
        "POST",
        f"/agent/chats/{chat_id}/participants",
        json={"participant": {"participant_id": owner_id}},
    )


def _data(client: Any, key: str, method: str, path: str, **kwargs: Any) -> Any:
    payload = _send(client, key, method, path, **kwargs)
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _send(client: Any, key: str, method: str, path: str, **kwargs: Any) -> Any:
    response = client.request(
        method,
        f"{BASE_URL}{path}",
        headers={"X-API-Key": key, "Content-Type": "application/json"},
        **kwargs,
    )
    if response.status_code >= 400:
        detail = _public(getattr(response, "text", "")[:300], key)
        raise RuntimeError(f"Band request failed ({response.status_code}): {detail}")
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError("Band returned an unexpected response") from exc


def _unannounced(products: list[dict[str, Any]], state_path: Path | None) -> list[dict[str, Any]]:
    announced = set(_read_state(_state_file(state_path)).get("announced") or [])
    fresh = []
    for product in products:
        product_id = str(product.get("id") or "")
        if product_id and product_id not in announced:
            fresh.append(product)
    return fresh


def _mark_announced(products: list[dict[str, Any]], state_path: Path | None) -> None:
    path = _state_file(state_path)
    with _LOCK:
        state = _read_state(path)
        announced = list(state.get("announced") or [])
        known = set(announced)
        for product in products:
            product_id = str(product.get("id") or "")
            if product_id and product_id not in known:
                announced.append(product_id)
                known.add(product_id)
        state["announced"] = announced
        _write_state(path, state)


def _state_file(state_path: Path | None) -> Path:
    return state_path or (Path("data") / "band-room.json")


def _read_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"chat_id": None, "announced": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"chat_id": None, "announced": []}
    if not isinstance(payload, dict):
        return {"chat_id": None, "announced": []}
    return payload


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _owner_handle(agent_handle: str) -> str:
    owner, _slash, _slug = agent_handle.partition("/")
    return owner or "umit"


def _label(product: dict[str, Any]) -> str:
    return str(product.get("name") or product.get("id") or "Untitled product").strip()


def _url(product: dict[str, Any]) -> str:
    return str(product.get("source_url") or "no source URL stored")


def _percent(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "not returned"
    return f"{round(float(value) * 100)}%"


def _tavily_assessment(product: dict[str, Any], answer: str, sources: list[Any]) -> str:
    if not answer and not sources:
        return "Tavily did not find public pages for this catalog product."
    blob = " ".join(
        [answer]
        + [str(item.get("title") or "") for item in sources if isinstance(item, dict)]
    ).lower()
    name = _label(product).lower()
    if name and name != "untitled product" and name in blob:
        return "The Tavily result names this catalog product."
    return "Tavily returned pages, but they do not clearly name this catalog product."


def _zoowork_assessment(product: dict[str, Any], answer: str) -> str:
    text = answer.strip()
    if not text:
        return "ZooWork did not return a briefing."
    lowered = text.lower()
    name = _label(product).lower()
    if name and name != "untitled product" and name in lowered:
        return "The ZooWork briefing names this catalog product."
    url = str(product.get("source_url") or "")
    if url and url in text:
        return "The ZooWork briefing cites this catalog URL."
    return "ZooWork returned a briefing, but it does not clearly name this catalog product."


def _jev_assessment(decision: dict[str, Any]) -> str:
    score = decision.get("score") if isinstance(decision.get("score"), dict) else {}
    position = score.get("score")
    name = str(decision.get("product_name") or "no product")
    if isinstance(position, (int, float)) and position >= 1.5:
        return f"Jev clearly matches the question with {name}."
    if isinstance(position, (int, float)) and position >= 0.5:
        return f"Jev only partly matches the question with {name}."
    if isinstance(position, (int, float)):
        return f"Jev does not see a match for the question. It still named {name}."
    return f"Jev chose {name}."


def _clip(text: str) -> str:
    if len(text) <= 15000:
        return text
    return text[:14999] + "…"


def _public(text: str, key: str) -> str:
    if key:
        text = text.replace(key, "BAND_API_KEY")
    return text
