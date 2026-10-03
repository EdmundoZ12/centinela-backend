import json
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import uuid4

from sqlalchemy.exc import OperationalError

from app.db.session import get_db
from app.api.dependencies import get_current_user
from app.main import app
from app.models.core import Area, AuditLog, DEMO_EMAILS, Role, User


class BusinessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = MagicMock()
        self.db.scalars.return_value.all.return_value = []

        def session():
            yield self.db

        app.dependency_overrides[get_db] = session
        app.dependency_overrides[get_current_user] = lambda: User(
            id=uuid4(), role=Role.GERENTE, active=True,
        )

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
        self.db.commit.assert_not_called()

    async def request(self, path):
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await app({
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
            "query_string": b"", "headers": [], "server": ("test", 80), "client": ("test", 123),
        }, receive, send)
        code = next(m["status"] for m in messages if m["type"] == "http.response.start")
        body = json.loads(b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body"))
        return code, body

    async def test_seven_demo_users_with_roles_and_areas(self):
        definitions = [
            ("Gerente General", Role.GERENTE, None),
            ("Líder Comercial", Role.LIDER_PROCESO, Area.COMERCIAL),
            ("Líder Cartera", Role.LIDER_PROCESO, Area.CARTERA),
            ("Líder Compras", Role.LIDER_PROCESO, Area.COMPRAS),
            ("Líder Inventario", Role.LIDER_PROCESO, Area.INVENTARIO),
            ("Analista", Role.ANALISTA, None),
            ("Auditor", Role.AUDITOR, None),
        ]
        self.db.scalars.return_value.all.return_value = [
            User(id=uuid4(), name=name, email=email, role=role, area=area,
                 active=True, created_at=datetime.now(timezone.utc))
            for (name, role, area), email in zip(definitions, DEMO_EMAILS)
        ]
        code, body = await self.request("/users/demo")
        self.assertEqual(code, 200)
        self.assertEqual(len(body), 7)
        self.assertEqual({item["role"] for item in body}, {role.value for role in Role})
        self.assertEqual({item["area"] for item in body if item["area"]}, {area.value for area in Area})
        self.assertTrue(all(item["active"] for item in body))
        statement = self.db.scalars.call_args.args[0]
        self.assertEqual(set(statement.compile().params["email_1"]), set(DEMO_EMAILS))

    async def test_empty_alerts(self):
        self.assertEqual(await self.request("/alertas"), (200, []))

    async def test_empty_audit_log(self):
        self.assertEqual(await self.request("/bitacora"), (200, []))

    async def test_audit_log_serializes_nullable_references_and_json(self):
        # Fixture solo en memoria: no se inserta en la base de datos.
        self.db.scalars.return_value.all.return_value = [AuditLog(
            id=uuid4(), alert_id=None, user_id=None, event_type="TEST_EVENT",
            payload={"details": {"success": True}}, created_at=datetime.now(timezone.utc),
        )]
        code, body = await self.request("/bitacora")
        self.assertEqual(code, 200)
        self.assertIsNone(body[0]["alert_id"])
        self.assertIsNone(body[0]["user_id"])
        self.assertEqual(body[0]["payload"], {"details": {"success": True}})

    async def test_missing_alert_returns_404(self):
        self.db.get.return_value = None
        self.assertEqual((await self.request(f"/alertas/{uuid4()}"))[0], 404)

    async def test_invalid_alert_uuid_returns_422(self):
        self.assertEqual((await self.request("/alertas/not-a-uuid"))[0], 422)
        self.db.get.assert_not_called()

    async def test_database_errors_do_not_expose_credentials(self):
        error = OperationalError("secret-url", {}, Exception("secret-password"))
        self.db.scalars.side_effect = error
        self.db.get.side_effect = error
        for path in ["/users/demo", "/alertas", "/bitacora", f"/alertas/{uuid4()}"]:
            with self.subTest(path=path):
                code, body = await self.request(path)
                self.assertEqual(code, 503)
                self.assertNotIn("secret", json.dumps(body))


if __name__ == "__main__":
    unittest.main()
