import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from zoowork import SessionEvent

from product_crawler.server import create_app
from product_crawler.store import ProductStore
from product_crawler.zoowork_agent import answer_from_events, ask_product, briefing_request


RUN_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"


class _EmptyPage:
    def __aiter__(self):
        return self._empty()

    async def _empty(self):
        if False:
            yield None


class _Client:
    def __init__(self) -> None:
        self.events: list[SessionEvent] = [
            SessionEvent(
                1,
                "agent.assistant",
                {"message": {"content": "The trail shoe is sold by Hoka."}},
            ),
            SessionEvent(2, "run.finished", {"status": "succeeded"}),
        ]
        self.created = False

    async def __aenter__(self) -> "_Client":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get_agent(self, agent_id: str) -> dict:
        return {"agent_id": agent_id, "status": {"desired_state": "running"}}

    async def create_session(self, agent_id: str) -> dict:
        return {"session_id": "ses_test"}

    async def post_events(self, agent_id: str, session_id: str, events: list) -> list:
        self.posted = events
        return [{"accepted": True}]

    async def stream_events(self, agent_id: str, session_id: str):
        for event in self.events:
            yield event

    async def list_agents(self, labels: dict | None = None) -> _EmptyPage:
        return _EmptyPage()

    async def create_agent(self, resource: dict) -> dict:
        self.created = True
        return {"agent_id": "agt_created"}

    async def start_agent(self, agent_id: str) -> dict:
        return {"warnings": []}

    async def wait_until_running(self, agent_id: str, timeout: float = 30) -> dict:
        return {}


class ZooWorkTests(unittest.TestCase):
    def test_briefing_names_the_catalog_product(self) -> None:
        text = briefing_request(
            {
                "name": "Trail Shoe",
                "brand": "HOKA",
                "barcode": "100301256016",
                "source_url": "https://example.com/products/shoe",
            }
        )
        self.assertIn("Trail Shoe", text)
        self.assertIn("https://example.com/products/shoe", text)
        self.assertIn("web_search", text)

    def test_answer_from_events_stops_at_the_finished_run(self) -> None:
        answer, outcome = answer_from_events(
            [
                SessionEvent(1, "agent.assistant", {"message": {"content": "Found it."}}),
                SessionEvent(2, "run.finished", {"status": "succeeded"}),
                SessionEvent(3, "agent.assistant", {"message": {"content": "ignored"}}),
            ]
        )
        self.assertEqual(answer, "Found it.")
        self.assertEqual(outcome, "succeeded")

    def test_ask_product_returns_the_agent_briefing(self) -> None:
        client = _Client()

        async def run() -> str:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "agent.json"
                answer = await ask_product(
                    {"name": "Trail Shoe", "source_url": "https://example.com/shoe"},
                    client_factory=lambda: client,
                    state_path=path,
                )
                self.assertIn("agt_created", path.read_text(encoding="utf-8"))
                return answer

        import asyncio

        answer = asyncio.run(run())
        self.assertEqual(answer, "The trail shoe is sold by Hoka.")
        self.assertIn("Trail Shoe", client.posted[0]["content"])
        self.assertTrue(client.created)

    def test_saved_agent_is_reused(self) -> None:
        client = _Client()

        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "agent.json"
                path.write_text('{"agent_id": "agt_saved"}\n', encoding="utf-8")
                await ask_product(
                    {"name": "Trail Shoe"},
                    client_factory=lambda: client,
                    state_path=path,
                )

        import asyncio

        asyncio.run(run())
        self.assertFalse(client.created)


class ZooWorkRouteTests(unittest.TestCase):
    def test_route_shows_the_agent_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ProductStore(root / "products.duckdb")
            store.init()
            store.create(
                {
                    "run_id": RUN_ID,
                    "requested_url": "https://example.com/products/shoe",
                    "title": "Trail Shoe",
                    "brand": "Hoka",
                }
            )
            app = create_app(root / "products.duckdb", root / "output")

            async def fake(_product: dict) -> str:
                return "Hoka sells this trail shoe.\n\nSource: https://example.com/shoe"

            with TestClient(app) as client, patch("product_crawler.server.ask_product", fake), patch(
                "product_crawler.server.publish_zoowork"
            ) as band:
                missing = client.post(
                    "/zoowork/ask",
                    json={"id": "dddddddd-dddd-dddd-dddd-dddddddddddd"},
                )
                self.assertEqual(missing.status_code, 404)
                response = client.post("/zoowork/ask", json={"id": RUN_ID})
                self.assertEqual(response.status_code, 200)
                self.assertIn("Hoka sells this trail shoe.", response.json()["answer"])
                band.assert_called_once()
