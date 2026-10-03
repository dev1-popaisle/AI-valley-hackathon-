"""Search the public web for one catalog product with Tavily."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

SEARCH_URL = "https://api.tavily.com/search"


def load_tavily_key() -> str | None:
    for path in (Path("api_config.json"), Path(__file__).resolve().parents[2] / "api_config.json"):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        key = payload.get("TAVILY_API_KEY")
        if isinstance(key, str) and key.strip():
            return key.strip()
    return None


def search_query(product: dict[str, Any]) -> str:
    parts = [
        str(product.get("name") or "").strip(),
        str(product.get("brand") or "").strip(),
        str(product.get("barcode") or "").strip(),
        str(product.get("source_url") or "").strip(),
    ]
    return " ".join(part for part in parts if part) or "catalog product"


def search_product(
    product: dict[str, Any],
    *,
    client: httpx.Client | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    key = api_key if api_key is not None else (load_tavily_key() or "")
    if not key:
        raise RuntimeError("TAVILY_API_KEY is not in api_config.json")
    body = {
        "query": search_query(product),
        "search_depth": "basic",
        "max_results": 5,
        "include_answer": "basic",
        "include_images": True,
        "include_image_descriptions": True,
    }
    headers = {"Authorization": f"Bearer {key}"}
    try:
        if client is None:
            with httpx.Client(timeout=60.0) as owned:
                response = owned.post(SEARCH_URL, headers=headers, json=body)
        else:
            response = client.post(SEARCH_URL, headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Tavily request failed: {_public(str(exc), key)}") from None
    if response.status_code != 200:
        detail = _public(response.text[:300], key)
        raise RuntimeError(f"Tavily request failed ({response.status_code}): {detail}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Tavily returned an unexpected response")
    return payload


def _public(text: str, key: str) -> str:
    if key:
        text = text.replace(key, "TAVILY_API_KEY")
    return text
