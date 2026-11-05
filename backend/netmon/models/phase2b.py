"""[2b-1] Maintenance windows and topology tables."""

from __future__ import annotations

from datetime import datetime, time

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Integer,
    SmallInteger,
    String,
    Text,
    Time,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from netmon.models.base import Base, TimestampMixin


class MaintenanceWindow(TimestampMixin, Base):
    __tablename__ = "maintenance_windows"

    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), nullable=True
    )
    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    weekdays: Mapped[list[int] | None] = mapped_column(ARRAY(SmallInteger))
    start_time: Mapped[time | None] = mapped_column(Time)
    duration_min: Mapped[int | None] = mapped_column(Integer)
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class TopologyNeighbor(Base):
    __tablename__ = "topology_neighbors"

    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    chassis_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    sys_name: Mapped[str | None] = mapped_column(String(255))
    sys_descr: Mapped[str | None] = mapped_column(Text)
    mgmt_address: Mapped[str | None] = mapped_column(String(64))
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TopologyLink(Base):
    __tablename__ = "topology_links"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    a_device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    a_if_index: Mapped[int | None] = mapped_column(Integer)
    a_port: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    b_device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), nullable=True
    )
    b_neighbor_id: Mapped[int | None] = mapped_column(
        ForeignKey("topology_neighbors.id", ondelete="CASCADE"), nullable=True
    )
    b_if_index: Mapped[int | None] = mapped_column(Integer)
    b_port: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    missed_refreshes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class TopologyLayout(Base):
    __tablename__ = "topology_layout"

    node_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    x: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
