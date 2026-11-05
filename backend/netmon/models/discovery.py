from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Identity, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from netmon.models.base import Base, TimestampMixin


class DiscoveryProfile(TimestampMixin, Base):
    __tablename__ = "discovery_profiles"

    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    targets: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), nullable=False
    )
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    schedule_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    schedule_every_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    notify_new_hosts: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notify_channel_ids: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, default=list
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_scan_id: Mapped[int | None] = mapped_column(Integer)


class DiscoveryScan(Base):
    __tablename__ = "discovery_scans"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("discovery_profiles.id", ondelete="SET NULL"), nullable=True
    )
    profile_name: Mapped[str | None] = mapped_column(String(128))
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("groups.id", ondelete="SET NULL"), nullable=True
    )
    targets: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    credentials: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    total_hosts: Mapped[int] = mapped_column(Integer, nullable=False)
    scanned_hosts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    found_hosts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_hosts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DiscoveryResult(Base):
    __tablename__ = "discovery_results"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    scan_id: Mapped[int] = mapped_column(
        ForeignKey("discovery_scans.id", ondelete="CASCADE"), nullable=False
    )
    ip: Mapped[str] = mapped_column(INET, nullable=False)
    hostname: Mapped[str | None] = mapped_column(String(255))
    mac: Mapped[str | None] = mapped_column(String(17))
    ping_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    ping_rtt_ms: Mapped[float | None] = mapped_column(Float)
    snmp_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    snmp_version: Mapped[str | None] = mapped_column(Text)
    snmp_credential_index: Mapped[int | None] = mapped_column(Integer)
    sys_name: Mapped[str | None] = mapped_column(String(255))
    sys_descr: Mapped[str | None] = mapped_column(Text)
    sys_object_id: Mapped[str | None] = mapped_column(String(255))
    open_ports: Mapped[list[int]] = mapped_column(ARRAY(Integer), nullable=False, default=list)
    device_class: Mapped[str] = mapped_column(Text, nullable=False)
    vendor: Mapped[str | None] = mapped_column(String(32))
    suggested_template: Mapped[str] = mapped_column(String(32), nullable=False)
    is_new: Mapped[bool] = mapped_column(Boolean, nullable=False)
    imported_device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), nullable=True
    )
    fingerprint: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    model: Mapped[str | None] = mapped_column(String(64))
    classified_by: Mapped[str] = mapped_column(Text, nullable=False, default="ports")
    confidence: Mapped[str] = mapped_column(Text, nullable=False, default="low")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DiscoveryKnownHost(Base):
    __tablename__ = "discovery_known_hosts"

    ip: Mapped[str] = mapped_column(INET, primary_key=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_scan_id: Mapped[int | None] = mapped_column(Integer)
