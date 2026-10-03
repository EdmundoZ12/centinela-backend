"""Fixtures transaccionales; nunca deja usuarios ni alertas de prueba persistidos.

Por defecto usa SQLite en memoria. CENTINELA_TEST_POSTGRES=1 usa la conexión
administrativa del .env y revierte todas las escrituras, incluido el SQL del core.
"""

import json
import os
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4

from dotenv import dotenv_values
from sqlalchemy import create_engine, event, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable

from app.db.session import get_db
from app.main import app
from app.models.core import Alert, AlertStatus, Area, AuditLog, Base, Decision, Role, User


@compiles(JSONB, "sqlite")
def sqlite_jsonb(type_, compiler, **kwargs):
    return "JSON"


@compiles(CreateTable, "sqlite")
def sqlite_table_defaults(element, compiler, **kwargs):
    return compiler.visit_create_table(element, **kwargs).replace("'{}'::jsonb", "'{}'")


class DecisionTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.postgres = os.environ.get("CENTINELA_TEST_POSTGRES") == "1"
        if cls.postgres:
            root = Path(__file__).resolve().parents[1]
            url = make_url(dotenv_values(root / ".env")["DATABASE_URL_UNPOOLED"])
            cls.engine = create_engine(url.set(drivername="postgresql+psycopg"), connect_args={"connect_timeout": 15})
        else:
            cls.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

            @event.listens_for(cls.engine, "connect")
            def prepare_sqlite(connection, record):
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("ATTACH DATABASE ':memory:' AS app")
                connection.create_function("gen_random_uuid", 0, lambda: uuid4().hex)

            Base.metadata.create_all(cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        if self.postgres:
            root = Path(__file__).resolve().parents[1]
            with self.connection.connection.driver_connection.cursor() as cursor:
                cursor.execute((root / "database/sql/05_core_app.sql").read_text(encoding="utf-8"))
        else:
            # SQLite necesita una transacción real antes de los SAVEPOINT.
            self.connection.exec_driver_sql("BEGIN")
        self.db = Session(bind=self.connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
        self.users = {}
        for name, role, area in [
            ("manager", Role.GERENTE, None),
            ("commercial", Role.LIDER_PROCESO, Area.COMERCIAL),
            ("portfolio", Role.LIDER_PROCESO, Area.CARTERA),
            ("analyst", Role.ANALISTA, None),
            ("auditor", Role.AUDITOR, None),
            ("no_area", Role.LIDER_PROCESO, None),
        ]:
            user = User(name=name, email=f"{uuid4()}@test.invalid", role=role, area=area, active=True)
            self.db.add(user)
            self.users[name] = user
        self.alert = Alert(
            type="TEST", area=Area.COMERCIAL, title="Fixture transaccional", summary="Solo prueba",
            severity="TEST", status=AlertStatus.PROPOSED, simulated_date=date(2026, 6, 30),
            evidence={"test": True}, proposals=[{"action": "original"}],
        )
        self.db.add(self.alert)
        self.db.flush()
        self.alert_id = self.alert.id
        self.db.commit()

        def session():
            yield self.db

        app.dependency_overrides[get_db] = session

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        self.db.close()
        self.transaction.rollback()
        self.connection.close()

    async def request(self, path=None, user="manager", method="POST", body=None, header=None, query=None):
        path = path or f"/alertas/{self.alert_id}/decision"
        if header is None and user is not None:
            header = str(self.users[user].id)
        headers = [(b"content-type", b"application/json")]
        if header is not None:
            headers.append((b"x-user-id", header.encode()))
        payload = json.dumps(body if body is not None else {"decision": "APPROVED"}).encode()
        messages = []

        async def receive():
            return {"type": "http.request", "body": payload, "more_body": False}

        async def send(message):
            messages.append(message)

        await app({
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
            "query_string": urlencode(query or {}).encode(), "headers": headers,
            "server": ("test", 80), "client": ("test", 123),
        }, receive, send)
        code = next(m["status"] for m in messages if m["type"] == "http.response.start")
        result = json.loads(b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body"))
        return code, result

    def decisions(self):
        return self.db.scalars(select(Decision).where(Decision.alert_id == self.alert_id)).all()

    def logs(self):
        return self.db.scalars(select(AuditLog).where(AuditLog.alert_id == self.alert_id)).all()

    async def test_manager_decides_and_persists_decision_and_audit(self):
        code, body = await self.request(body={"decision": "APPROVED", "reason": "Verificado"})
        self.assertEqual(code, 200)
        self.assertEqual(body["alert_status"], "APPROVED")
        self.assertEqual(self.db.get(Alert, self.alert_id).status, AlertStatus.APPROVED)
        decisions, logs = self.decisions(), self.logs()
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].user_id, self.users["manager"].id)
        self.assertEqual(decisions[0].reason, "Verificado")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].event_type, "ALERT_DECISION_APPROVED")
        self.assertEqual(logs[0].payload["decision_id"], str(decisions[0].id))

    async def test_correct_area_leader_can_decide(self):
        self.assertEqual((await self.request(user="commercial"))[0], 200)
        self.assertEqual(len(self.decisions()), 1)

    async def test_other_area_leader_is_forbidden(self):
        self.assertEqual((await self.request(user="portfolio"))[0], 403)
        self.assertEqual(self.decisions(), [])
        self.assertEqual(self.logs(), [])

    async def test_analyst_cannot_decide(self):
        self.assertEqual((await self.request(user="analyst"))[0], 403)
        self.assertEqual(self.decisions(), [])

    async def test_auditor_cannot_decide(self):
        self.assertEqual((await self.request(user="auditor"))[0], 403)
        self.assertEqual(self.decisions(), [])

    async def test_missing_invalid_and_unknown_users(self):
        for header in [None, "invalid", str(uuid4())]:
            with self.subTest(header=header):
                self.assertEqual((await self.request(user=None, header=header))[0], 401)

    async def test_inactive_user_is_forbidden(self):
        self.users["manager"].active = False
        self.db.commit()
        self.assertEqual((await self.request())[0], 403)

    async def test_rejected_changes_status_and_logs(self):
        self.assertEqual((await self.request(body={"decision": "REJECTED"}))[0], 200)
        self.assertEqual(self.db.get(Alert, self.alert_id).status, AlertStatus.REJECTED)
        self.assertEqual(self.logs()[0].event_type, "ALERT_DECISION_REJECTED")

    async def test_duplicate_terminal_decision_is_conflict(self):
        self.assertEqual((await self.request())[0], 200)
        self.assertEqual((await self.request())[0], 409)
        self.assertEqual(len(self.decisions()), 1)
        self.assertEqual(len(self.logs()), 1)

    async def test_non_proposed_state_is_conflict(self):
        for state in AlertStatus:
            if state == AlertStatus.PROPOSED:
                continue
            self.alert.status = state
            self.db.commit()
            self.assertEqual((await self.request())[0], 409)
        self.assertEqual(self.decisions(), [])

    async def test_edit_stays_proposed_can_be_approved_and_identical_edit_conflicts(self):
        body = {"decision": "EDITED", "edited_proposal": [{"action": "edited"}]}
        self.assertEqual((await self.request(body=body))[0], 200)
        self.assertEqual(self.db.get(Alert, self.alert_id).status, AlertStatus.PROPOSED)
        self.assertEqual(self.db.get(Alert, self.alert_id).proposals, body["edited_proposal"])
        self.assertEqual(self.logs()[0].payload["previous_proposals"], [{"action": "original"}])
        self.assertEqual((await self.request(body=body))[0], 409)
        self.assertEqual((await self.request())[0], 200)
        self.assertEqual(len(self.decisions()), 2)

    async def test_edit_requires_nonempty_object_or_list(self):
        for proposal in [None, {}, [], "text", 1]:
            self.assertEqual((await self.request(body={"decision": "EDITED", "edited_proposal": proposal}))[0], 422)
        self.assertEqual(self.decisions(), [])

    async def test_lists_and_detail_obey_area_and_allow_auditor_evidence(self):
        for user in ["manager", "commercial", "analyst", "auditor"]:
            code, body = await self.request(path=f"/alertas/{self.alert_id}", user=user, method="GET")
            self.assertEqual(code, 200)
            self.assertEqual(body["evidence"], {"test": True})
        for user in ["portfolio", "no_area"]:
            code, body = await self.request(path="/alertas", user=user, method="GET")
            self.assertEqual((code, body), (200, []))
            self.assertEqual((await self.request(path=f"/alertas/{self.alert_id}", user=user, method="GET"))[0], 403)

    async def test_audit_permissions_and_filters(self):
        await self.request()
        # Sin alerta: solo gerente y auditor pueden ver estos eventos.
        global_id = uuid4()
        self.db.add(AuditLog(id=global_id, event_type="TEST_GLOBAL", payload={}))
        self.db.commit()
        for user in ["manager", "auditor"]:
            code, body = await self.request(path="/bitacora", user=user, method="GET", query={"event_type": "TEST_GLOBAL"})
            self.assertEqual(code, 200)
            self.assertIn(str(global_id), [row["id"] for row in body])
        for user in ["commercial", "analyst"]:
            code, body = await self.request(path="/bitacora", user=user, method="GET", query={"alert_id": str(self.alert_id), "event_type": "ALERT_DECISION_APPROVED"})
            self.assertEqual(code, 200)
            self.assertEqual(len(body), 1)
            self.assertEqual((await self.request(path="/bitacora", user=user, method="GET", query={"event_type": "TEST_GLOBAL"}))[1], [])
        self.assertEqual((await self.request(path="/bitacora", user="portfolio", method="GET", query={"alert_id": str(self.alert_id)}))[1], [])
        self.assertEqual((await self.request(path="/bitacora", method="GET", query={"event_type": "UNKNOWN"}))[1], [])

    async def test_audit_failure_rolls_back_alert_and_decision(self):
        real_flush = self.db.flush

        def flush(*args, **kwargs):
            if any(isinstance(row, AuditLog) for row in self.db.new):
                raise OperationalError("secret-url", {}, Exception("secret-password"))
            return real_flush(*args, **kwargs)

        with patch.object(self.db, "flush", side_effect=flush):
            code, body = await self.request()
        self.assertEqual(code, 503)
        self.assertNotIn("secret", json.dumps(body))
        self.assertEqual(self.db.get(Alert, self.alert_id).status, AlertStatus.PROPOSED)
        self.assertEqual(self.decisions(), [])
        self.assertEqual(self.logs(), [])


if __name__ == "__main__":
    unittest.main()
