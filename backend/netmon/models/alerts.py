from __future__ import annotations

from typing import Any

from sqlalchemy import Boolean, ForeignKey, Identity, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from netmon.models.base import Base, TimestampMixin


class NotificationChannel(TimestampMixin, Base):
    __tablename__ = "notification_channels"

    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class AlertRule(TimestampMixin, Base):
    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    sensor_id: Mapped[int | None] = mapped_column(
        ForeignKey("sensors.id", ondelete="CASCADE"), nullable=True
    )
    on_statuses: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    delay_s: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    escalation_after_s: Mapped[int | None] = mapped_column(Integer)
    escalation_channel_ids: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, default=list
    )
    repeat_every_s: Mapped[int | None] = mapped_column(Integer)


class AlertRuleChannel(Base):
    __tablename__ = "alert_rule_channels"

    rule_id: Mapped[int] = mapped_column(
        ForeignKey("alert_rules.id", ondelete="CASCADE"), primary_key=True
    )
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("notification_channels.id", ondelete="CASCADE"), primary_key=True
    )
