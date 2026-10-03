import tempfile
import unittest
from pathlib import Path

from product_crawler.band_agent import (
    catalog_review,
    jev_review,
    publish_catalog,
    publish_tavily,
    tavily_review,
    zoowork_review,
)


PRODUCT = {
    "id": "11111111-1111-1111-1111-111111111111",
    "name": "Trail Shoe",
    "source_url": "https://example.com/products/shoe",
}


class _Response:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = "band_secret failed"

    def json(self) -> dict:
        return self._payload


class _Band:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.rooms: list[dict] = []
        self.participants: list[dict] = []

    def request(self, method: str, url: str, headers: dict | None = None, json: dict | None = None, params=None):
        self.calls.append((method, url, json, params))
        if url.endswith("/agent/me"):
            return _Response(
                200,
                {
                    "data": {
                        "handle": "umit/shopping-agent",
                        "owner_uuid": "owner-1",
                        "id": "agent-1",
                        "name": "Shopping agent",
                    }
                },
            )
        if url.endswith("/agent/chats") and method == "GET":
            return _Response(200, {"data": list(self.rooms)})
        if url.endswith("/agent/chats") and method == "POST":
            room = {"id": "room-1", "title": json["chat"]["title"]}
            self.rooms.append(room)
            return _Response(201, {"data": room})
        if url.endswith("/participants") and method == "GET":
            return _Response(200, {"data": list(self.participants)})
        if method == "GET" and "/agent/chats/" in url:
            room = self.rooms[0] if self.rooms else {"id": "room-1", "title": "Product catalog log"}
            return _Response(200, {"data": room})
        if method == "PATCH" and "/agent/chats/" in url:
            if self.rooms:
                self.rooms[0]["title"] = json["chat"]["title"]
            return _Response(200, {"data": self.rooms[0] if self.rooms else {}})
        if url.endswith("/participants") and method == "POST":
            person = {"id": json["participant"]["participant_id"], "type": "User"}
            self.participants.append(person)
            return _Response(201, {"data": person})
        if url.endswith("/messages"):
            return _Response(201, {"data": {"id": "msg-1", "success": True, "recipients": []}})
        if url.endswith("/events"):
            return _Response(201, {"data": {"id": "evt-1", "success": True, "message_type": "attention"}})
        if "band_secret" in url:
            return _Response(500, {})
        return _Response(404, {})


class BandReviewTests(unittest.TestCase):
    def test_reviews_name_the_catalog_url(self) -> None:
        self.assertIn("https://example.com/products/shoe", catalog_review([PRODUCT]))
        tavily = tavily_review(
            PRODUCT,
            {
                "answer": "Trail Shoe is a cushioned shoe.",
                "results": [{"title": "Trail Shoe", "url": "https://example.com/shoe"}],
            },
        )
        self.assertIn("https://example.com/products/shoe", tavily)
        self.assertIn("The Tavily result names this catalog product.", tavily)
        zoowork = zoowork_review(PRODUCT, "Trail Shoe is listed for sale.")
        self.assertIn("The ZooWork briefing names this catalog product.", zoowork)
        jev = jev_review(
            "Is this a trail shoe?",
            [PRODUCT],
            {
                "product_name": "Trail Shoe",
                "confidence": 0.9,
                "noul": 0.8,
                "probabilities": [{"name": "Trail Shoe", "probability": 0.9}],
                "score": {
                    "score": 1.8,
                    "confidence": 0.7,
                    "levels": [
                        {"description": "Does not match the question", "probability": 0.05},
                        {"description": "Partly matches the question", "probability": 0.15},
                        {"description": "Clearly matches the question", "probability": 0.8},
                    ],
                },
                "insight": "Yes means this shoe fits the question.",
            },
        )
        self.assertIn("Is this a trail shoe?", jev)
        self.assertIn("https://example.com/products/shoe", jev)
        self.assertIn("Selected: Trail Shoe", jev)
        self.assertIn("Confidence: 90%", jev)
        self.assertIn("- Trail Shoe: 90%", jev)
        self.assertIn("Yes: 80%", jev)
        self.assertIn("No: 20%", jev)
        self.assertIn("Score: 1.80 of 2", jev)
        self.assertIn("Clearly matches the question: 80%", jev)
        self.assertIn("LLM (with Jev input) :", jev)
        self.assertIn("Yes means this shoe fits the question.", jev)
        self.assertIn("Jev clearly matches the question with Trail Shoe.", jev)

    def test_publish_opens_a_room_and_reviews_for_the_owner(self) -> None:
        band = _Band()
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "band-room.json"
            publish_tavily(
                PRODUCT,
                {"answer": "Trail Shoe is a cushioned shoe.", "results": []},
                api_key="band_secret",
                http=band,
                state_path=state,
            )
            posted = band.calls[-2][2]["message"]
            event = band.calls[-1][2]["event"]
            self.assertIn("@umit", posted["content"])
            self.assertEqual(posted["mentions"][0]["handle"], "umit")
            self.assertEqual(posted["mentions"][0]["id"], "owner-1")
            self.assertEqual(event["message_type"], "attention")
            self.assertEqual(event["metadata"]["kind"], "review")
            self.assertEqual(event["metadata"]["source"], "tavily")
            self.assertEqual(band.participants[0]["id"], "owner-1")
            self.assertIn("room-1", state.read_text(encoding="utf-8"))

    def test_catalog_urls_are_announced_once(self) -> None:
        band = _Band()
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "band-room.json"
            first = publish_catalog([PRODUCT], api_key="band_secret", http=band, state_path=state)
            calls = len(band.calls)
            second = publish_catalog([PRODUCT], api_key="band_secret", http=band, state_path=state)
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(band.calls), calls)

    def test_band_errors_hide_the_key(self) -> None:
        band = _Band()

        def request(method, url, headers=None, json=None, params=None):
            return _Response(401, {})

        band.request = request
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError) as caught:
                publish_tavily(
                    PRODUCT,
                    {"answer": "Trail Shoe"},
                    api_key="band_secret",
                    http=band,
                    state_path=Path(tmp) / "band-room.json",
                )
        self.assertNotIn("band_secret", str(caught.exception))
        self.assertIn("BAND_API_KEY", str(caught.exception))
