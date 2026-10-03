import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from product_crawler.server import create_app
from product_crawler.store import ProductStore
from product_crawler.tavily_search import search_product, search_query


RUN_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"


class _Response:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = "bad"

    def json(self) -> dict:
        return self._payload


class _Client:
    def __init__(self, response: _Response) -> None:
        self.response = response

    def post(self, url: str, headers: dict, json: dict) -> _Response:
        self.url = url
        self.headers = headers
        self.body = json
        return self.response


class TavilyTests(unittest.TestCase):
    def test_query_includes_the_catalog_product(self) -> None:
        query = search_query(
            {
                "name": "Trail Shoe",
                "brand": "HOKA",
                "barcode": "100301256016",
                "source_url": "https://example.com/products/shoe",
            }
        )
        self.assertIn("Trail Shoe", query)
        self.assertIn("https://example.com/products/shoe", query)

    def test_search_returns_the_tavily_json(self) -> None:
        payload = {
            "query": "Trail Shoe",
            "answer": "A cushioned trail shoe.",
            "results": [
                {
                    "title": "Trail Shoe",
                    "url": "https://example.com/shoe",
                    "content": "Sold by Hoka.",
                    "score": 0.91,
                }
            ],
            "response_time": 0.4,
        }
        client = _Client(_Response(200, payload))
        result = search_product(
            {"name": "Trail Shoe", "source_url": "https://example.com/shoe"},
            client=client,
            api_key="tvly-test",
        )
        self.assertEqual(result["answer"], "A cushioned trail shoe.")
        self.assertEqual(client.body["max_results"], 5)
        self.assertTrue(client.body["include_images"])
        self.assertEqual(client.headers["Authorization"], "Bearer tvly-test")
        self.assertNotIn("tvly-test", str(result))

    def test_route_returns_the_search_json(self) -> None:
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

            def fake(_product: dict) -> dict:
                return {
                    "query": "Trail Shoe",
                    "answer": "A cushioned trail shoe.",
                    "results": [{"title": "Trail Shoe", "url": "https://example.com/shoe"}],
                }

            with TestClient(app) as client, patch("product_crawler.server.search_product", fake), patch(
                "product_crawler.server.publish_tavily"
            ) as band:
                response = client.post("/tavily/search", json={"id": RUN_ID})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["answer"], "A cushioned trail shoe.")
                band.assert_called_once()
