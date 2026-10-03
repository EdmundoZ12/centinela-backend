import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

from pydantic import ValidationError

from app.analyst.discount import build_discount_investigation
from app.analyst.schemas import AnalysisExplanation, AnalysisFact
from app.analyst.service import contains_unproved_accusation, discount_facts_catalog, explain_discount
from app.core.config import settings
from app.executor.service import sandbox_result
from app.rag.service import PolicyReference
from app.strategist.schemas import ActionType, ModelProposal, ModelStrategy, Proposal
from app.strategist.service import calculate_amount_at_risk, propose_strategy
from app.vigil.detectors.discount import DISCOUNT_QUERY, DiscountDetector, discount_evidence
from app.vigil.service import DETECTORS


def violation(**changes):
    row = {
        "pedido_id": "PED-X", "linea_n": 1, "cliente_id": "CLI-X",
        "segmento": "Minoristas", "sku": "SKU-X",
        "descuento_pct": Decimal("14"), "tope_normal_pct": Decimal("10"),
        "tope_especial_pct": Decimal("13"), "aprobacion_especial": "N",
        "descuento_en_exceso": Decimal("40"), "venta_debajo_costo": False,
    }
    row.update(changes)
    return row


class DiscountTests(unittest.TestCase):
    def test_detector_evidence_groups_week_and_recurrence(self):
        evidence = discount_evidence({
            "vendedor_id": "VEN-X", "semana": date(2026, 7, 6),
            "cantidad_lineas_fuera_politica": 3,
            "descuento_exceso_total": Decimal("125.50"),
            "segmentos_afectados": ["Mayoristas", "Minoristas"],
            "clientes_afectados": 2, "pedidos_afectados": 2,
            "semanas_consecutivas": 2,
        })
        self.assertEqual(evidence["cantidad_lineas_fuera_politica"], 3)
        self.assertEqual(evidence["descuento_exceso_total"], "125.50")
        self.assertTrue(evidence["reincidencia_semana_anterior"])
        self.assertTrue(evidence["trigger_reincidencia"])

    def test_detector_is_registered_after_margin(self):
        self.assertEqual([detector.name for detector in DETECTORS], [
            "margin_detector", "discount_detector",
        ])
        self.assertIsInstance(DETECTORS[1], DiscountDetector)

    def test_detector_uses_semantic_view_cutoff_and_dedupe_insert(self):
        cutoff = date(2026, 7, 10)
        row = {
            "vendedor_id": "VEN-X", "semana": date(2026, 7, 6),
            "cantidad_lineas_fuera_politica": 2,
            "descuento_exceso_total": Decimal("80"),
            "segmentos_afectados": ["Minoristas"],
            "clientes_afectados": 1, "pedidos_afectados": 1,
            "semanas_consecutivas": 1,
        }
        db = MagicMock()
        db.scalar.side_effect = [cutoff, uuid4(), cutoff, None]
        db.execute.return_value.mappings.return_value.all.return_value = [row]
        detector = DiscountDetector()
        self.assertEqual(detector.run(db), 1)
        self.assertEqual(detector.run(db), 0)
        self.assertEqual(db.add.call_count, 1)
        query_text = str(DISCOUNT_QUERY)
        self.assertIn("centinela.v_descuentos_fuera_politica", query_text)
        self.assertIn("violation.fecha <= :cutoff", query_text)
        query_calls = [call for call in db.execute.call_args_list if call.args and call.args[0] is DISCOUNT_QUERY]
        self.assertEqual(len(query_calls), 2)
        self.assertTrue(all(call.args[1]["cutoff"] == cutoff for call in query_calls))

    def test_investigation_aggregates_only_supplied_violations(self):
        rows = [violation(), violation(
            pedido_id="PED-Y", linea_n=2, cliente_id="CLI-Y",
            descuento_en_exceso=Decimal("60"), venta_debajo_costo=True,
        )]
        evidence = build_discount_investigation(
            "VEN-X", "Vendedor", date(2026, 7, 6), date(2026, 7, 10),
            rows, [violation(aprobacion_especial="S")], 2,
        )
        self.assertEqual(evidence.summary.lineas_fuera_politica, 2)
        self.assertEqual(evidence.summary.pedidos_afectados, 2)
        self.assertEqual(evidence.summary.clientes_afectados, 2)
        self.assertEqual(evidence.summary.descuento_exceso_total, Decimal("100"))
        self.assertTrue(evidence.summary.reincidencia)
        self.assertEqual(len(evidence.excesos_con_aprobacion_especial), 1)
        self.assertIn("datos no demuestran", " ".join(discount_facts_catalog(evidence)).lower())

    def test_amount_at_risk_comes_from_discount_rows(self):
        self.assertEqual(calculate_amount_at_risk({"violations": [
            {"descuento_en_exceso": "12.345"},
            {"descuento_en_exceso": "7.655"},
        ]}), Decimal("20.00"))
        with self.assertRaises(ValueError):
            calculate_amount_at_risk({"violations": [{"descuento_en_exceso": "NaN"}]})

    def test_discount_strategy_allowlist_and_recurrence_gate(self):
        client = MagicMock()
        client.responses.parse.return_value.status = "completed"
        client.responses.parse.return_value.output_parsed = ModelStrategy(
            summary="Revisión comercial respaldada por evidencia",
            proposals=[ModelProposal(
                id="review", action_type=ActionType.CREATE_DISCOUNT_REVIEW_TASK,
                title="Revisión comercial", description="Revisar evidencia persistida.",
                reason="Existen descuentos fuera de política.", priority="HIGH",
            )],
        )
        with patch.object(settings, "openai_model_reasoning", "test-model"):
            result = propose_strategy(
                {"evidence": {"summary": {"reincidencia": False}}},
                Decimal("20"), client, "DISCOUNT_POLICY_VIOLATION",
            )
        self.assertEqual(result.proposals[0].financial_reference_cop, Decimal("20"))

        client.responses.parse.return_value.output_parsed.proposals[0].action_type = ActionType.CREATE_QUOTING_PERMISSION_REVIEW
        with patch.object(settings, "openai_model_reasoning", "test-model"), self.assertRaises(ValueError):
            propose_strategy(
                {"evidence": {"summary": {"reincidencia": False}}},
                Decimal("20"), client, "DISCOUNT_POLICY_VIOLATION",
            )

    def test_discount_explanation_rejects_unproved_accusation(self):
        evidence = build_discount_investigation(
            "VEN-X", "Vendedor", date(2026, 7, 6), date(2026, 7, 10),
            [violation()], [], 1,
        )
        policy = PolicyReference(
            document_name="policy.pdf", page_number=1, chunk_index=0,
            content="Los descuentos requieren aprobación especial.",
        )
        client = MagicMock()
        client.responses.parse.return_value.status = "completed"
        client.responses.parse.return_value.output_parsed = AnalysisExplanation(
            summary="Se demuestra fraude comercial.", root_cause="La causa es deliberada.",
            facts=[AnalysisFact(
                statement="Hay líneas con descuento superior al tope normal sin aprobación especial.",
                source="DATA",
            )], policy_findings=[], interpretation="Requiere revisión humana.",
            confidence=0.5, insufficient_evidence=False,
        )
        with patch.object(settings, "openai_model_reasoning", "test-model"), self.assertRaises(ValueError):
            explain_discount(evidence, client, [policy])
        self.assertTrue(contains_unproved_accusation("Fraude comercial."))
        self.assertTrue(contains_unproved_accusation("El vendedor fraccionó los pedidos."))
        self.assertTrue(contains_unproved_accusation(
            "Existe ausencia de aprobación escrita de Gerencia General.",
        ))
        self.assertFalse(contains_unproved_accusation("No hay evidencia de fraude comercial."))
        self.assertFalse(contains_unproved_accusation(
            "Los datos no demuestran intención de evadir la política.",
        ))

    def test_discount_sandbox_action_is_internal_task(self):
        proposal = Proposal(
            id="review", action_type=ActionType.CREATE_QUOTING_PERMISSION_REVIEW,
            title="Revisión", description="Revisar autorización.",
            reason="Existe reincidencia.", priority="HIGH",
            financial_reference_cop=Decimal("20"),
        )
        result = sandbox_result(proposal, {
            "seller": {"vendedor_id": "VEN-X"}, "violations": [],
        }, Decimal("20"))
        self.assertEqual(result["status"], "TASK_CREATED")
        self.assertEqual(result["task_type"], "QUOTING_PERMISSION_REVIEW")
        self.assertEqual(result["seller_id"], "VEN-X")


if __name__ == "__main__":
    unittest.main()
