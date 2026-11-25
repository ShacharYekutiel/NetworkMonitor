"""[profiles] Connection profiles (data-model "[profiles] Connection profiles")."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from netmon.models.base import Base, TimestampMixin


class ConnectionProfile(TimestampMixin, Base):
    __tablename__ = "connection_profiles"
    __table_args__ = (UniqueConstraint("id", "kind", name="uq_connection_profiles_id_kind"),)

    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # the credential object in the stored form of its kind (secrets encrypted)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class ConnectionProfileAssignment(Base):
    __tablename__ = "connection_profile_assignments"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        CheckConstraint("num_nonnulls(group_id, device_id) = 1", name="ck_cpa_one_owner"),
        ForeignKeyConstraint(
            ["profile_id", "kind"],
            ["connection_profiles.id", "connection_profiles.kind"],
            ondelete="RESTRICT",
            name="fk_cpa_profile_kind",
        ),
        UniqueConstraint("group_id", "kind", name="ux_cpa_group"),
        UniqueConstraint("device_id", "kind", name="ux_cpa_device"),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    profile_id: Mapped[int] = mapped_column(Integer, nullable=False)
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), nullable=True
    )
    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
