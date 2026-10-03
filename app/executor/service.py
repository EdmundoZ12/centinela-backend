import json
import logging
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.core.permissions import require_alert_access, require_decision_role
from app.executor.schemas import ExecutionResponse, ExecutionResult
from app.models.core import Alert, AlertStatus, AuditLog, ExecutionAction
from app.strategist.schemas import ACTIONS_BY_ALERT_TYPE, ActionType, Proposal, Strategy
from app.strategist.service import SCENARIOS as STRATEGY_SCENARIOS, persisted_investigation

logger = logging.getLogger(__name__)


def approved_proposals(raw) -> list[Proposal]:
    # EDITED conserva el contrato existente: objeto o lista. Validar en la barrera.
    if isinstance(raw, list):
        raw = {"summary": "Propuestas editadas", "proposals": raw}
    elif isinstance(raw, dict) and "action_type" in raw:
        raw = {"summary": "Propuesta editada", "proposals": [raw]}
    return Strategy.model_validate(raw).proposals


def inventory_supplier(evidence: dict) -> dict | None:
    # Proveedor de órdenes retrasadas si existen; si no, el del catálogo. Nunca inventado.
    late = [o for o in evidence["purchase_orders"] if o["estado_al_corte"] == "RETRASADA"]
    if late:
        return {"proveedor_id": late[-1]["proveedor_id"], "nombre": late[-1]["proveedor"], "origen": "orden_retrasada"}
    catalog = evidence.get("proveedor_catalogo")
    if catalog:
        return {"proveedor_id": catalog["proveedor_id"], "nombre": catalog["nombre"], "origen": "catalogo"}
    return None


def inventory_sandbox_result(proposal: Proposal, evidence: dict) -> dict:
    orders = {state: [o["orden_id"] for o in evidence["purchase_orders"] if o["estado_al_corte"] == state]
              for state in ("RETRASADA", "EN_TRANSITO")}
    base = {"sku": evidence["sku"], "bodega_id": evidence["bodega_id"], "supplier": inventory_supplier(evidence),
            "title": proposal.title, "message": proposal.description, "sandbox": True}
    if proposal.action_type == ActionType.CREATE_SUPPLIER_FOLLOWUP_DRAFT:
        # Borrador interno: no se envía ni se contacta al proveedor.
        return {"status": "DRAFT_CREATED", "draft_type": "SUPPLIER_FOLLOWUP", "sent": False,
                "delayed_orders": orders["RETRASADA"], **base}
    tasks = {ActionType.CREATE_PURCHASE_ORDER_REVIEW_TASK: "PURCHASE_ORDER_REVIEW",
             ActionType.CREATE_ALTERNATE_SUPPLIER_REVIEW_TASK: "ALTERNATE_SUPPLIER_REVIEW",
             ActionType.CREATE_URGENT_PARTIAL_DELIVERY_REVIEW: "URGENT_PARTIAL_DELIVERY_REVIEW"}
    return {"status": "TASK_CREATED", "task_type": tasks[proposal.action_type],
            "delayed_orders": orders["RETRASADA"], "in_transit_orders": orders["EN_TRANSITO"], **base}


def sandbox_result(proposal: Proposal, evidence: dict, amount) -> dict:
    if proposal.action_type.value in ACTIONS_BY_ALERT_TYPE["INVENTORY_RISK"]:
        return inventory_sandbox_result(proposal, evidence)
    if proposal.action_type == ActionType.CREATE_PRICE_REVIEW_DRAFT:
        skus = [s["sku"] for s in evidence["sku_afectados"]
                if s.get("contribucion_perdida_margen") is not None
                and Decimal(str(s["contribucion_perdida_margen"])) > 0]
        return {"status": "DRAFT_CREATED", "title": proposal.title,
                "affected_skus": skus, "financial_reference_cop": str(amount) if amount is not None else None}
    tasks = {ActionType.CREATE_MARGIN_FOLLOWUP_TASK: "MARGIN_FOLLOWUP",
             ActionType.CREATE_SUPPLIER_REVIEW_TASK: "SUPPLIER_COST_REVIEW"}
    return {"status": "TASK_CREATED", "task_type": tasks[proposal.action_type], "description": proposal.description}


def execute_alert(db, alert_id: UUID, user) -> ExecutionResult:
    require_decision_role(user)
    started = False
    try:
        alert = db.scalar(select(Alert).where(Alert.id == alert_id).with_for_update().execution_options(populate_existing=True))
        if alert is None:
            raise HTTPException(404, "Alerta no encontrada.")
        require_alert_access(user, alert)
        scenario = STRATEGY_SCENARIOS.get(alert.type)
        if scenario is None or alert.area != scenario.area:
            raise HTTPException(400, "El Ejecutor no admite este tipo de alerta o área.")
        # Barrera obligatoria de aplicación; no depende de prompts ni del cliente.
        if alert.status != AlertStatus.APPROVED:
            raise HTTPException(409, "Solo se pueden ejecutar alertas APPROVED.")
        if alert.proposals is None:
            raise HTTPException(409, "La alerta no tiene propuestas aprobadas.")
        try:
            proposals = approved_proposals(alert.proposals)
            evidence = persisted_investigation(alert)
            # Una edición humana tampoco puede introducir acciones de otro escenario.
            if any(p.action_type.value not in ACTIONS_BY_ALERT_TYPE[alert.type] for p in proposals):
                raise ValueError("Acción fuera del allowlist del escenario")
        except (ValidationError, ValueError, TypeError, KeyError):
            raise HTTPException(422, "Las propuestas aprobadas o la evidencia no cumplen el contrato sandbox.") from None
        db.add(AuditLog(alert_id=alert_id, user_id=user.id, event_type="EXECUTION_STARTED", payload={"sandbox": True}))
        db.flush()
        started = True
        actions = []
        for proposal in proposals:
            # Importes editados por humanos tampoco pueden sustituir los calculados.
            proposal.financial_reference_cop = alert.amount_at_risk
            key = json.dumps([str(alert_id), proposal.id], separators=(",", ":"))
            payload = proposal.model_dump(mode="json")
            action = db.scalar(select(ExecutionAction).where(ExecutionAction.dedupe_key == key))
            if action is not None:
                if action.payload != payload:
                    raise ValueError("La acción existente no coincide con la propuesta aprobada")
            else:
                action = ExecutionAction(alert_id=alert_id, proposal_id=proposal.id,
                    action_type=proposal.action_type.value, payload=payload,
                    result=sandbox_result(proposal, evidence, alert.amount_at_risk), created_by=user.id, dedupe_key=key)
                db.add(action)
                db.flush()
            actions.append(ExecutionResponse.model_validate(action))
        alert.status = AlertStatus.EXECUTED
        db.add(AuditLog(alert_id=alert_id, user_id=user.id, event_type="EXECUTION_COMPLETED", payload={"sandbox": True, "action_count": len(actions)}))
        result = ExecutionResult(alert_status=AlertStatus.EXECUTED, actions=actions)
        db.commit()
        return result
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.error("Falló la ejecución sandbox; acciones revertidas.")
        if started:
            try:
                db.add_all([AuditLog(alert_id=alert_id, user_id=user.id, event_type=name,
                    payload={"sandbox": True, "error": "Fallo controlado de ejecución"} if name.endswith("FAILED") else {"sandbox": True})
                    for name in ("EXECUTION_STARTED", "EXECUTION_FAILED")])
                db.commit()
            except Exception:
                db.rollback()
                logger.error("No se pudo registrar el fallo de ejecución.")
        raise HTTPException(503, "La ejecución falló; la aprobación se conserva para reintentar.") from None
