"""Registro del Vigía: cada detector se ejecuta en su propia transacción.

Referencia funcional obligatoria: resources/metricas.yaml. Consultar sus vistas,
dimensiones y umbral_alerta antes de implementar margen, cartera, días de pago,
cobertura de inventario, descuentos o actividad de cliente. No inventar umbrales.
"""

import logging
from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.core import AuditLog
from app.schemas.vigil import VigilSummary
from app.vigil.detectors.margin import MarginDetector


logger = logging.getLogger(__name__)


class Detector(Protocol):
    name: str

    def run(self, db: Session, user_id: UUID | None = None) -> int: ...


DETECTORS: tuple[Detector, ...] = (MarginDetector(),)


def rollback_detector(db: Session) -> None:
    try:
        db.rollback()
    except Exception:
        logger.error("No se pudo cerrar la transacción del detector.")


def run_vigil(db: Session, user_id: UUID | None = None, detectors=None) -> VigilSummary:
    result = VigilSummary()
    for detector in DETECTORS if detectors is None else detectors:
        result.detectores_ejecutados += 1
        try:
            count = detector.run(db, user_id)
            db.commit()  # Alerta y ALERT_DETECTED se confirman juntos.
            result.alertas_nuevas += count
        except Exception:
            rollback_detector(db)
            result.errores.append(detector.name)
            logger.error("Falló el detector %s; sus cambios se revirtieron.", detector.name)
            try:
                db.add(AuditLog(
                    user_id=user_id, event_type="DETECTOR_FAILED",
                    payload={"detector": detector.name, "error": "No se pudo completar la detección."},
                ))
                db.commit()
            except Exception:
                rollback_detector(db)
                logger.error("No se pudo guardar el error del detector %s en bitácora.", detector.name)
    return result
