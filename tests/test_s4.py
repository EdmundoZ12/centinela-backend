import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from sqlalchemy import select

import test_analyst as fixtures
from app.analyst.discount import build_discount_investigation
from app.analyst.schemas import AnalysisExplanation, AnalysisFact
from app.models.core import Alert, AlertStatus, Area, AuditLog, ExecutionAction, Role
from app.strategist.schemas import ActionType, ModelProposal, ModelStrategy


class S4Tests(unittest.TestCase):
    request = fixtures.AnalystTests.request
    logs = fixtures.AnalystTests.logs

    @classmethod
    def setUpClass(cls):
        fixtures.AnalystTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixtures.AnalystTests.tearDownClass.__func__(cls)

    def setUp(self):
        fixtures.AnalystTests.setUp(self)
        self.seller = "VEN-FIXTURE"
        self.alert.type = "DISCOUNT_POLICY_VIOLATION"
        self.alert.evidence = {
            "vendedor_id": self.seller, "semana": self.week.isoformat(),
            "cantidad_lineas_fuera_politica": 1,
            "descuento_exceso_total": "40",
            "segmentos_afectados": ["Minoristas"],
            "clientes_afectados": 1, "pedidos_afectados": 1,
            "reincidencia_semana_anterior": True,
            "semanas_consecutivas": 2,
            "trigger_fuera_politica": True, "trigger_reincidencia": True,
        }
        self.users[Role.LIDER_PROCESO].area = Area.COMERCIAL
        self.db.commit()
        row = {
            "pedido_id": "PED-FIXTURE", "linea_n": 1,
            "cliente_id": "CLI-FIXTURE", "segmento": "Minoristas",
            "sku": "SKU-FIXTURE", "descuento_pct": Decimal("14"),
            "tope_normal_pct": Decimal("10"),
            "tope_especial_pct": Decimal("13"),
            "aprobacion_especial": "N", "descuento_en_exceso": Decimal("40"),
            "venta_debajo_costo": False,
        }
        self.investigation = build_discount_investigation(
            self.seller, "Vendedor fixture", self.week, self.cutoff,
            [row], [], 2,
        )
        self.investigation_patch = patch(
            "app.analyst.service.investigate_discount",
            return_value=self.investigation,
        )
        self.investigation_patch.start()
        self.openai.responses.parse.return_value.output_parsed = AnalysisExplanation(
            summary="Se observaron descuentos fuera de política.",
            root_cause="El patrón corresponde a descuentos sin aprobación especial.",
            facts=[AnalysisFact(
                statement="Hay líneas con descuento superior al tope normal sin aprobación especial.",
                source="DATA",
            )],
            policy_findings=[], interpretation="La evidencia respalda revisión humana.",
            confidence=0.9, insufficient_evidence=False,
        )
        self.strategy_client = MagicMock()
        self.strategy_client.responses.parse.return_value.status = "completed"
        self.strategy_client.responses.parse.return_value.output_parsed = ModelStrategy(
            summary="Revisión comercial respaldada por evidencia",
            proposals=[
                ModelProposal(
                    id="discount-review",
                    action_type=ActionType.CREATE_DISCOUNT_REVIEW_TASK,
                    title="Revisión comercial",
                    description="Revisar la evidencia con el responsable comercial.",
                    reason="Se observaron descuentos fuera de política.", priority="HIGH",
                ),
                ModelProposal(
                    id="permission-review",
                    action_type=ActionType.CREATE_QUOTING_PERMISSION_REVIEW,
                    title="Revisión de autorización",
                    description="Revisar la autorización de cotización.",
                    reason="La evidencia demuestra reincidencia.", priority="HIGH",
                ),
            ],
        )
        self.strategy_patch = patch(
            "app.strategist.service.create_client", return_value=self.strategy_client,
        )
        self.strategy_patch.start()

    def tearDown(self):
        self.strategy_patch.stop()
        self.investigation_patch.stop()
        fixtures.AnalystTests.tearDown(self)

    def post(self, suffix, role=Role.GERENTE, body=None):
        return self.client.post(
            f"/alertas/{self.alert_id}/{suffix}", json=body,
            headers={"X-User-Id": str(self.users[role].id)},
        )

    def actions(self):
        return self.db.scalars(select(ExecutionAction).where(
            ExecutionAction.alert_id == self.alert_id
        )).all()

    def test_complete_mocked_lifecycle_and_audit(self):
        analysis = self.request()
        self.assertEqual(analysis.status_code, 200, analysis.text)
        alert = self.db.get(Alert, self.alert_id)
        self.assertEqual(alert.status, AlertStatus.ANALYZING)
        self.assertEqual(alert.root_cause["evidence"]["seller"]["vendedor_id"], self.seller)

        strategy = self.post("estrategia")
        self.assertEqual(strategy.status_code, 200, strategy.text)
        alert = self.db.get(Alert, self.alert_id)
        self.assertEqual(alert.status, AlertStatus.PROPOSED)
        self.assertEqual(alert.amount_at_risk, Decimal("40.00"))
        self.assertEqual(len(alert.proposals["proposals"]), 2)

        self.assertEqual(self.post("ejecutar").status_code, 409)
        self.assertEqual(self.post("decision", body={"decision": "APPROVED"}).status_code, 200)
        execution = self.post("ejecutar")
        self.assertEqual(execution.status_code, 200, execution.text)
        self.assertEqual(self.db.get(Alert, self.alert_id).status, AlertStatus.EXECUTED)
        self.assertEqual({action.result["status"] for action in self.actions()}, {"TASK_CREATED"})
        self.assertEqual({action.result["seller_id"] for action in self.actions()}, {self.seller})
        self.assertEqual([event.event_type for event in self.logs()], [
            "ANALYSIS_STARTED", "ANALYSIS_COMPLETED",
            "STRATEGY_STARTED", "STRATEGY_COMPLETED",
            "ALERT_DECISION_APPROVED", "EXECUTION_STARTED", "EXECUTION_COMPLETED",
        ])

    def test_quoting_review_requires_backend_recurrence(self):
        self.alert.evidence = {
            **self.alert.evidence,
            "semanas_consecutivas": 1,
            "reincidencia_semana_anterior": False,
            "trigger_reincidencia": False,
        }
        self.db.commit()
        no_recurrence = self.investigation.model_copy(deep=True)
        no_recurrence.summary.reincidencia = False
        no_recurrence.summary.semanas_consecutivas = 1
        self.investigation_patch.stop()
        self.investigation_patch = patch(
            "app.analyst.service.investigate_discount", return_value=no_recurrence,
        )
        self.investigation_patch.start()
        self.assertEqual(self.request().status_code, 200)
        self.assertEqual(self.post("estrategia").status_code, 503)
        self.assertEqual(self.db.get(Alert, self.alert_id).status, AlertStatus.ANALYZING)

    def test_discount_executor_permissions_and_scenario_allowlist(self):
        self.assertEqual(self.request().status_code, 200)
        self.assertEqual(self.post("estrategia").status_code, 200)
        self.assertEqual(self.post("decision", body={"decision": "APPROVED"}).status_code, 200)
        for role in (Role.ANALISTA, Role.AUDITOR):
            self.assertEqual(self.post("ejecutar", role).status_code, 403)
        alert = self.db.get(Alert, self.alert_id)
        proposals = {**alert.proposals, "proposals": [dict(item) for item in alert.proposals["proposals"]]}
        proposals["proposals"][0]["action_type"] = ActionType.CREATE_PRICE_REVIEW_DRAFT.value
        alert.proposals = proposals
        self.db.commit()
        self.assertEqual(self.post("ejecutar").status_code, 422)
        self.assertEqual(self.actions(), [])


if __name__ == "__main__":
    unittest.main()
