from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, Date, DateTime, Enum as SqlEnum, ForeignKey, Index, MetaData, Numeric, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Role(str, Enum):
    GERENTE = "GERENTE"
    LIDER_PROCESO = "LIDER_PROCESO"
    ANALISTA = "ANALISTA"
    AUDITOR = "AUDITOR"


class Area(str, Enum):
    COMERCIAL = "COMERCIAL"
    CARTERA = "CARTERA"
    COMPRAS = "COMPRAS"
    INVENTARIO = "INVENTARIO"


class AlertStatus(str, Enum):
    NEW = "NEW"
    ANALYZING = "ANALYZING"
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"


class DecisionType(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EDITED = "EDITED"


DEMO_EMAILS = (
    "gerente@centinela.demo", "lider.comercial@centinela.demo",
    "lider.cartera@centinela.demo", "lider.compras@centinela.demo",
    "lider.inventario@centinela.demo", "analista@centinela.demo", "auditor@centinela.demo",
)


def enum_column(enum: type[Enum], name: str) -> SqlEnum:
    return SqlEnum(enum, native_enum=False, create_constraint=True, validate_strings=True, name=name)


class Base(DeclarativeBase):
    metadata = MetaData(schema="app")


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    name: Mapped[str] = mapped_column(Text)
    email: Mapped[str] = mapped_column(Text, unique=True)
    role: Mapped[Role] = mapped_column(enum_column(Role, "users_role_check"))
    area: Mapped[Area | None] = mapped_column(enum_column(Area, "users_area_check"))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (Index("ux_alerts_dedupe_key", "dedupe_key", unique=True),)

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    type: Mapped[str] = mapped_column(Text)
    dedupe_key: Mapped[str | None] = mapped_column(Text)
    area: Mapped[Area] = mapped_column(enum_column(Area, "alerts_area_check"))
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric)
    amount_at_risk: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    status: Mapped[AlertStatus] = mapped_column(enum_column(AlertStatus, "alerts_status_check"), server_default=text("'NEW'"))
    simulated_date: Mapped[date] = mapped_column(Date, index=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    evidence: Mapped[Any] = mapped_column(JSONB(none_as_null=True), nullable=False, server_default=text("'{}'::jsonb"))
    root_cause: Mapped[Any | None] = mapped_column(JSONB(none_as_null=True))
    proposals: Mapped[Any | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Decision(Base):
    __tablename__ = "decisions"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    alert_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("app.alerts.id"), index=True)
    user_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("app.users.id"))
    decision: Mapped[DecisionType] = mapped_column(enum_column(DecisionType, "decisions_decision_check"))
    reason: Mapped[str | None] = mapped_column(Text)
    edited_proposal: Mapped[Any | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    alert_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("app.alerts.id"))
    user_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("app.users.id"))
    event_type: Mapped[str] = mapped_column(Text)
    payload: Mapped[Any] = mapped_column(JSONB(none_as_null=True), nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class ExecutionAction(Base):
    __tablename__ = "execution_actions"
    __table_args__ = (UniqueConstraint("alert_id", "proposal_id"),)
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    alert_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("app.alerts.id"))
    proposal_id: Mapped[str] = mapped_column(Text)
    action_type: Mapped[str] = mapped_column(Text)
    payload: Mapped[Any] = mapped_column(JSONB(none_as_null=True), nullable=False)
    result: Mapped[Any] = mapped_column(JSONB(none_as_null=True), nullable=False)
    created_by: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("app.users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    dedupe_key: Mapped[str] = mapped_column(Text, unique=True)
