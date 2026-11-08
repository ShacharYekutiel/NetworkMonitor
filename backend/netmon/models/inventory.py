from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from netmon.models.base import Base, TimestampMixin


class Group(TimestampMixin, Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    snmp_credentials: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dependency_mode: Mapped[str] = mapped_column(Text, nullable=False, default="inherit")
    dependency_device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL", use_alter=True), nullable=True
    )
    dependency_sensor_id: Mapped[int | None] = mapped_column(
        ForeignKey("sensors.id", ondelete="SET NULL", use_alter=True), nullable=True
    )


class Device(TimestampMixin, Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    snmp_credentials: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dependency_mode: Mapped[str] = mapped_column(Text, nullable=False, default="inherit")
    dependency_device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL", use_alter=True), nullable=True
    )
    dependency_sensor_id: Mapped[int | None] = mapped_column(
        ForeignKey("sensors.id", ondelete="SET NULL", use_alter=True), nullable=True
    )
    identity: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    lldp_chassis_id: Mapped[str | None] = mapped_column(String(255))
    lldp_sys_name: Mapped[str | None] = mapped_column(String(255))
    # [2b-2] auto | on | off
    port_inventory: Mapped[str] = mapped_column(Text, nullable=False, default="auto")
    arp_collect: Mapped[str] = mapped_column(Text, nullable=False, default="auto")


class Sensor(TimestampMixin, Base):
    __tablename__ = "sensors"

    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    interval_s: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    status_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status_change_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ack_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ack_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    ack_by: Mapped[str | None] = mapped_column(String(64))
    ack_comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    ack_status: Mapped[str | None] = mapped_column(Text)

    channels: Mapped[list[SensorChannel]] = relationship(
        back_populates="sensor",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
        order_by="SensorChannel.id",
    )


class SensorChannel(Base):
    __tablename__ = "sensor_channels"

    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    sensor_id: Mapped[int] = mapped_column(
        ForeignKey("sensors.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    warning_min: Mapped[float | None] = mapped_column(Float)
    warning_max: Mapped[float | None] = mapped_column(Float)
    down_min: Mapped[float | None] = mapped_column(Float)
    down_max: Mapped[float | None] = mapped_column(Float)
    last_value: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")

    sensor: Mapped[Sensor] = relationship(back_populates="channels")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    sensor_id: Mapped[int | None] = mapped_column(
        ForeignKey("sensors.id", ondelete="SET NULL"), nullable=True
    )
    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), nullable=True
    )
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("groups.id", ondelete="SET NULL"), nullable=True
    )
    sensor_name: Mapped[str | None] = mapped_column(String(128))
    device_name: Mapped[str | None] = mapped_column(String(128))
    old_status: Mapped[str | None] = mapped_column(Text)
    new_status: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    in_maintenance: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
