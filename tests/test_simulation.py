import json
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from sqlalchemy.exc import OperationalError

from app.db.session import get_db
from app.main import app
from app.schemas.vigil import VigilSummary


class SimulationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = MagicMock()

        def session():
            yield self.db

        app.dependency_overrides[get_db] = session
        self.vigil_patch = patch("app.api.simulation.run_vigil", return_value=VigilSummary(detectores_ejecutados=1))
        self.vigil = self.vigil_patch.start()

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        self.vigil_patch.stop()

    async def request(self, method="GET", query=""):
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        path = "/simulacion" if method == "GET" else "/simulacion/avanzar"
        await app({
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
            "query_string": query.encode(), "headers": [],
            "server": ("test", 80), "client": ("test", 123),
        }, receive, send)
        code = next(m["status"] for m in messages if m["type"] == "http.response.start")
        body = json.loads(b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body"))
        return code, body

    async def test_get_initial_date(self):
        self.db.execute.return_value.scalar_one_or_none.return_value = date(2026, 6, 30)
        self.assertEqual(await self.request(), (200, {
            "fecha_actual": "2026-06-30", "fecha_maxima": "2026-09-30",
        }))
        self.db.commit.assert_not_called()

    async def test_advance_default_and_explicit_days(self):
        for query, days in [("", 1), ("dias=5", 5)]:
            with self.subTest(query=query):
                self.db.reset_mock()
                self.db.execute.return_value.scalar_one_or_none.return_value = date(2026, 7, 5)
                code, body = await self.request("POST", query)
                self.assertEqual(code, 200)
                self.assertEqual(body["fecha_actual"], "2026-07-05")
                self.assertEqual(self.db.execute.call_args.args[1]["days"], days)
                self.db.commit.assert_called_once()

    async def test_invalid_days_do_not_access_database(self):
        for query in ["dias=0", "dias=-1", "dias=1.5", "dias=abc"]:
            with self.subTest(query=query):
                code, _ = await self.request("POST", query)
                self.assertEqual(code, 422)
        self.db.execute.assert_not_called()

    async def test_huge_advance_is_bounded_and_max_date_returned(self):
        self.db.execute.return_value.scalar_one_or_none.return_value = date(2026, 9, 30)
        code, body = await self.request("POST", "dias=999999999999999999999999")
        self.assertEqual(code, 200)
        self.assertEqual(body["fecha_actual"], "2026-09-30")
        parameters = self.db.execute.call_args.args[1]
        self.assertEqual(parameters, {"days": 92, "maximum": date(2026, 9, 30)})

    async def test_missing_state_does_not_commit(self):
        self.db.execute.return_value.scalar_one_or_none.return_value = None
        for method in ["GET", "POST"]:
            code, _ = await self.request(method)
            self.assertEqual(code, 503)
        self.db.commit.assert_not_called()

    async def test_database_error_hides_credentials(self):
        self.db.execute.side_effect = OperationalError("secret-url", {}, Exception("secret-password"))
        for method in ["GET", "POST"]:
            code, body = await self.request(method)
            self.assertEqual(code, 503)
            self.assertNotIn("secret", json.dumps(body))
        self.db.rollback.assert_called_once()

    async def test_commit_error_rolls_back(self):
        self.db.execute.return_value.scalar_one_or_none.return_value = date(2026, 7, 1)
        self.db.commit.side_effect = OperationalError("secret-url", {}, Exception("secret-password"))
        code, body = await self.request("POST")
        self.assertEqual(code, 503)
        self.assertNotIn("secret", json.dumps(body))
        self.db.rollback.assert_called_once()

    async def test_clock_commits_before_vigil_runs(self):
        self.db.execute.return_value.scalar_one_or_none.return_value = date(2026, 7, 1)

        def execute(db):
            db.commit.assert_called_once()
            return VigilSummary(detectores_ejecutados=1, alertas_nuevas=2)

        self.vigil.side_effect = execute
        code, body = await self.request("POST")
        self.assertEqual(code, 200)
        self.assertEqual(body["vigia"], {"detectores_ejecutados": 1, "alertas_nuevas": 2})
        self.vigil.assert_called_once_with(self.db)

    async def test_controlled_detector_failure_keeps_successful_clock_response(self):
        self.db.execute.return_value.scalar_one_or_none.return_value = date(2026, 7, 1)
        self.vigil.return_value = VigilSummary(detectores_ejecutados=1, errores=["margin_detector"])
        code, body = await self.request("POST")
        self.assertEqual(code, 200)
        self.assertEqual(body["fecha_actual"], "2026-07-01")
        self.assertEqual(body["vigia"]["errores"], ["margin_detector"])
        self.db.commit.assert_called_once()
        self.db.rollback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
