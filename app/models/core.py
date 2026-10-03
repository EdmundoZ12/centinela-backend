from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, Enum as SqlEnum, ForeignKey, MetaData, Numeric, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Role(str, Enum):
    GERENTE = "GERENTE"
    LIDER_PROCESO = "LIDER_PROCESO"
    ANALISTA = "ANALISTA"
    AUDITOR = "AUDITOR"


class AlertStatus(str, Enum):
    NEW = "NEW"
    ANALYZING = "ANALYZING"
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"


class Base(DeclarativeBase):
    metadata = MetaData(schema="app")


class User(Base):
    __tablename__ = "users"
    id: Mapped[UUID] = mapped_column(PgUUID, primary_key=True, server_default=text("gen_random_uuid()"))
    name: Mapped[str] = mapped_column(Text)
    email: Mapped[str] = mapped_column(Text, unique=True)
    role: Mapped[Role] = mapped_column(SqlEnum(Role, native_enum=False, validate_strings=True))
    area: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[UUID] = mapped_column(PgUUID, primary_key=True, server_default=text("gen_random_uuid()"))
    type: Mapped[str] = mapped_column(Text)
    area: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric)
    amount_at_risk: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    status: Mapped[AlertStatus] = mapped_column(SqlEnum(AlertStatus, native_enum=False, validate_strings=True), server_default=text("'NEW'"))
    simulated_date: Mapped[date]
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    evidence: Mapped[Any] = mapped_column(JSONB(none_as_null=True), nullable=False, server_default=text("'{}'::jsonb"))
    root_cause: Mapped[Any | None] = mapped_column(JSONB(none_as_null=True))
    proposals: Mapped[Any | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[UUID] = mapped_column(PgUUID, primary_key=True, server_default=text("gen_random_uuid()"))
    alert_id: Mapped[UUID | None] = mapped_column(PgUUID, ForeignKey("app.alerts.id"))
    user_id: Mapped[UUID | None] = mapped_column(PgUUID, ForeignKey("app.users.id"))
    event_type: Mapped[str] = mapped_column(Text)
    payload: Mapped[Any] = mapped_column(JSONB(none_as_null=True), nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
