import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from pydantic import ValidationError
from sqlalchemy import select

import test_analyst as fixtures
from app.models.core import Alert, AlertStatus, Area, AuditLog, ExecutionAction, Role
from app.strategist.schemas import ActionType, ModelProposal, ModelStrategy
from app.strategist.service import calculate_amount_at_risk, propose_strategy


def model_strategy(count=3):
    return ModelStrategy(summary="Revisión interna respaldada por evidencia", proposals=[
        ModelProposal(id=f"proposal-{i}", action_type=action, title="Revisión interna",
            description="Revisar la evidencia persistida con el equipo responsable.",
            reason="El costo observado creció sin un ajuste proporcional de precio.", priority="HIGH")
        for i, action in enumerate(list(ActionType)[:count])
    ])


class S1Tests(unittest.TestCase):
    sale = fixtures.AnalystTests.sale
    logs = fixtures.AnalystTests.logs
    request = fixtures.AnalystTests.request

    @classmethod
    def setUpClass(cls):
        fixtures.AnalystTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixtures.AnalystTests.tearDownClass.__func__(cls)

    def setUp(self):
        fixtures.AnalystTests.setUp(self)
        self.users[Role.LIDER_PROCESO].area = Area.COMERCIAL
        self.db.commit()
        self.strategy_client = MagicMock()
        self.strategy_client.responses.parse.return_value.status = "completed"
        self.strategy_client.responses.parse.return_value.output_parsed = model_strategy()
        self.strategy_patch = patch("app.strategist.service.create_client", return_value=self.strategy_client)
        self.strategy_patch.start()

    def tearDown(self):
        self.strategy_patch.stop()
        fixtures.AnalystTests.tearDown(self)

    def post(self, suffix, role=Role.GERENTE, body=None):
        return self.client.post(f"/alertas/{self.alert_id}/{suffix}", json=body,
                                headers={"X-User-Id": str(self.users[role].id)})

    def prepare(self):
        response = self.request()
        self.assertEqual(response.status_code, 200, response.text)

    def strategy(self):
        self.prepare()
        response = self.post("estrategia")
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def approve(self):
        self.strategy()
        response = self.post("decision", body={"decision": "APPROVED"})
        self.assertEqual(response.status_code, 200, response.text)

    def actions(self):
        return self.db.scalars(select(ExecutionAction).where(ExecutionAction.alert_id == self.alert_id)).all()

    def test_strategy_persists_amount_proposals_state_and_events(self):
        response = self.strategy()
        alert = self.db.get(Alert, self.alert_id)
        self.assertEqual(alert.status, AlertStatus.PROPOSED)
        self.assertEqual(alert.amount_at_risk, Decimal("35.00"))
        self.assertEqual(len(response.json()["proposals"]["proposals"]), 3)
        self.assertTrue(all(Decimal(p["financial_reference_cop"]) == 35 for p in alert.proposals["proposals"]))
        self.assertEqual([e.event_type for e in self.logs()], ["ANALYSIS_STARTED", "ANALYSIS_COMPLETED", "STRATEGY_STARTED", "STRATEGY_COMPLETED"])
        kwargs = self.strategy_client.responses.parse.call_args.kwargs
        self.assertEqual(kwargs["text_format"], ModelStrategy)
        self.assertNotIn("financial_reference_cop", str(ModelStrategy.model_json_schema()))
        self.assertNotIn("test-key", kwargs["input"])

    def test_amount_uses_only_positive_contributions_and_missing_is_null(self):
        self.assertEqual(calculate_amount_at_risk({"sku_afectados": [
            {"contribucion_perdida_margen": "1.125"}, {"contribucion_perdida_margen": "-9"},
            {"contribucion_perdida_margen": "2.125"}, {"contribucion_perdida_margen": None}]}), Decimal("3.25"))
        self.assertIsNone(calculate_amount_at_risk({"sku_afectados": [{"contribucion_perdida_margen": None}]}))
        self.assertEqual(calculate_amount_at_risk({"sku_afectados": [{"contribucion_perdida_margen": "-3"}]}), 0)
        with self.assertRaises(ValueError):
            calculate_amount_at_risk({"sku_afectados": [{"contribucion_perdida_margen": "NaN"}]})

    def test_structured_output_limits_allowlist_and_model_financial_input(self):
        raw = model_strategy().model_dump(mode="json")
        for count in (0, 4):
            candidate = {**raw, "proposals": [raw["proposals"][0]] * count}
            with self.assertRaises(ValidationError):
                ModelStrategy.model_validate(candidate)
        for key, value in [("action_type", "SEND_EMAIL"), ("financial_reference_cop", 999999)]:
            candidate = model_strategy(1).model_dump(mode="json")
            candidate["proposals"][0][key] = value
            self.strategy_client.responses.parse.return_value.output_parsed = candidate
            with self.assertRaises(ValidationError):
                propose_strategy({}, Decimal("35"), self.strategy_client)
        self.strategy_client.responses.parse.return_value.output_parsed = model_strategy(1)
        self.assertEqual(len(propose_strategy({}, None, self.strategy_client).proposals), 1)

    def test_model_cannot_invent_money_or_guarantee_recovery(self):
        for statement in ("Ahorrará 999 pesos", "Recuperación garantizada del margen", "Ahorro de un millón de pesos"):
            raw = model_strategy(1).model_dump(mode="json")
            raw["summary"] = statement
            self.strategy_client.responses.parse.return_value.output_parsed = raw
            with self.assertRaises(ValueError):
                propose_strategy({}, Decimal("35"), self.strategy_client)

    def test_strategy_failure_preserves_analysis_and_retry(self):
        self.prepare()
        before = self.db.get(Alert, self.alert_id).root_cause
        self.strategy_client.responses.parse.side_effect = RuntimeError("secret")
        with self.assertLogs("app.strategist.service", level="ERROR") as output:
            response = self.post("estrategia")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret", response.text + str(output.output))
        alert = self.db.get(Alert, self.alert_id)
        self.assertEqual(alert.status, AlertStatus.ANALYZING)
        self.assertEqual(alert.root_cause, before)
        self.assertIsNone(alert.proposals)
        self.assertEqual(self.logs()[-1].event_type, "STRATEGY_FAILED")
        self.strategy_client.responses.parse.side_effect = None
        self.assertEqual(self.post("estrategia").status_code, 200)

    def test_strategy_guards_new_incomplete_analysis_and_duplicate(self):
        self.assertEqual(self.post("estrategia").status_code, 409)
        self.strategy_client.responses.parse.assert_not_called()
        self.prepare()
        self.db.get(Alert, self.alert_id).root_cause = None
        self.db.commit()
        self.assertEqual(self.post("estrategia").status_code, 409)
        self.db.get(Alert, self.alert_id).root_cause = {"evidence": self.db.get(Alert, self.alert_id).evidence["investigation"]}
        self.db.commit()
        self.assertEqual(self.post("estrategia").status_code, 200)
        self.assertEqual(self.post("estrategia").status_code, 409)
        self.strategy_client.responses.parse.assert_called_once()

    def test_strategy_roles(self):
        self.prepare()
        for role in (Role.AUDITOR, Role.LIDER_PROCESO):
            self.assertEqual(self.post("estrategia", role).status_code, 403)
        self.assertEqual(self.post("estrategia", Role.ANALISTA).status_code, 200)

    def test_rejection_and_proposed_cannot_execute(self):
        self.strategy()
        self.assertEqual(self.post("ejecutar").status_code, 409)
        self.assertEqual(self.post("decision", body={"decision": "REJECTED"}).status_code, 200)
        self.assertEqual(self.post("ejecutar").status_code, 409)
        self.assertEqual(self.actions(), [])

    def test_edited_proposals_remain_proposed_and_use_backend_finance(self):
        self.strategy()
        edited = self.db.get(Alert, self.alert_id).proposals
        edited = {**edited, "proposals": [dict(p) for p in edited["proposals"]]}
        edited["proposals"][0]["title"] = "Revisión editada"
        edited["proposals"][0]["financial_reference_cop"] = "999999"
        self.assertEqual(self.post("decision", body={"decision": "EDITED", "edited_proposal": edited}).status_code, 200)
        self.assertEqual(self.db.get(Alert, self.alert_id).status, AlertStatus.PROPOSED)
        self.assertEqual(self.post("decision", body={"decision": "APPROVED"}).status_code, 200)
        response = self.post("ejecutar")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(Decimal(response.json()["actions"][0]["payload"]["financial_reference_cop"]), 35)

    def test_execution_roles_area_and_read_permissions(self):
        self.approve()
        for role in (Role.ANALISTA, Role.AUDITOR):
            self.assertEqual(self.post("ejecutar", role).status_code, 403)
        self.users[Role.LIDER_PROCESO].area = Area.CARTERA
        self.db.commit()
        self.assertEqual(self.post("ejecutar", Role.LIDER_PROCESO).status_code, 403)
        path = f"/alertas/{self.alert_id}/ejecuciones"
        self.assertEqual(self.client.get(path, headers={"X-User-Id":str(self.users[Role.LIDER_PROCESO].id)}).status_code, 403)
        self.users[Role.LIDER_PROCESO].area = Area.COMERCIAL
        self.db.commit()
        self.assertEqual(self.post("ejecutar", Role.LIDER_PROCESO).status_code, 200)
        self.assertEqual(len(self.client.get(path, headers={"X-User-Id":str(self.users[Role.AUDITOR].id)}).json()), 3)

    def test_execution_creates_sandbox_results_and_is_idempotent(self):
        self.approve()
        response = self.post("ejecutar")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.db.get(Alert, self.alert_id).status, AlertStatus.EXECUTED)
        actions = self.actions()
        self.assertEqual(len(actions), 3)
        draft = next(a for a in actions if a.action_type == ActionType.CREATE_PRICE_REVIEW_DRAFT)
        self.assertEqual(set(draft.result["affected_skus"]), {self.main_sku, self.other_sku})
        self.assertEqual(draft.result["status"], "DRAFT_CREATED")
        self.assertEqual(len({a.dedupe_key for a in actions}), 3)
        self.assertEqual(self.post("ejecutar").status_code, 409)
        self.assertEqual(len(self.actions()), 3)
        self.assertEqual([e.event_type for e in self.logs()], ["ANALYSIS_STARTED", "ANALYSIS_COMPLETED", "STRATEGY_STARTED", "STRATEGY_COMPLETED", "ALERT_DECISION_APPROVED", "EXECUTION_STARTED", "EXECUTION_COMPLETED"])

    def test_execution_failure_rolls_back_actions_preserves_approved_and_retries(self):
        self.approve()
        from app.executor.service import sandbox_result
        count = 0
        def fail_second(*args):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError("secret")
            return sandbox_result(*args)
        with patch("app.executor.service.sandbox_result", side_effect=fail_second), self.assertLogs("app.executor.service", level="ERROR"):
            response = self.post("ejecutar")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.actions(), [])
        self.assertEqual(self.db.get(Alert, self.alert_id).status, AlertStatus.APPROVED)
        self.assertEqual(self.logs()[-1].event_type, "EXECUTION_FAILED")
        self.assertEqual(self.post("ejecutar").status_code, 200)
        self.assertEqual(len(self.actions()), 3)

    def test_invalid_approved_action_type_is_never_executed(self):
        self.approve()
        self.db.get(Alert, self.alert_id).proposals = {"summary":"Unsafe", "proposals":[{"action_type":"SEND_EMAIL"}]}
        self.db.commit()
        self.assertEqual(self.post("ejecutar").status_code, 422)
        self.assertEqual(self.actions(), [])
        self.assertEqual(self.db.get(Alert, self.alert_id).status, AlertStatus.APPROVED)


if __name__ == "__main__":
    unittest.main()
