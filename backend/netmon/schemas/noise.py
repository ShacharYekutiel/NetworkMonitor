"""[if-noise] `PUT /api/settings` keys: sensor event debounce / flapping and the important-port
criteria (data-model "[if-noise] Settings keys")."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ImportantCriterion = Literal["uplink", "lag", "trunk", "irf", "critical_sensor"]
CRITERIA: tuple[str, ...] = ("uplink", "lag", "trunk", "irf", "critical_sensor")


class NoiseSettingsMixin(BaseModel):
    sensor_event_debounce_enabled: bool | None = None
    sensor_event_debounce_polls: int | None = Field(default=None, ge=1, le=10)
    sensor_event_debounce_min_s: int | None = Field(default=None, ge=0, le=3600)
    sensor_event_flap_enabled: bool | None = None
    important_port_criteria: list[ImportantCriterion] | None = Field(default=None, max_length=5)
    important_port_description_pattern: str | None = Field(default=None, max_length=200)

    @field_validator("important_port_criteria")
    @classmethod
    def _criteria(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        return [c for c in CRITERIA if c in set(v)]  # canonical order, no duplicates

    @field_validator("important_port_description_pattern")
    @classmethod
    def _pattern(cls, v: str | None) -> str | None:
        if v is None:
            return v
        from netmon.util.saferegex import SafePatternError, check_safe

        v = v.strip()
        if v:
            try:
                check_safe(v, max_len=200)
            except SafePatternError as exc:
                raise ValueError(str(exc)) from None
        return v


NoiseMode = Literal["inherit", "on", "off"]


class EventNoiseIn(BaseModel):
    """`event_noise` of a sensor / device / group (create, PATCH, bulk). An omitted key keeps the
    stored value; `debounce` / `flap`: `inherit` | `on` | `off` (null is invalid); the values:
    null = inherit."""

    model_config = ConfigDict(extra="forbid")
    debounce: NoiseMode | None = None
    flap: NoiseMode | None = None
    debounce_polls: int | None = Field(default=None, ge=1, le=10)
    debounce_min_s: int | None = Field(default=None, ge=0, le=3600)

    @model_validator(mode="after")
    def _modes_not_null(self) -> EventNoiseIn:
        for key in ("debounce", "flap"):
            if key in self.model_fields_set and getattr(self, key) is None:
                raise ValueError(f"{key} must be one of 'inherit', 'on', 'off'")
        return self

    def changes(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.model_fields_set}


class BulkEventNoiseIn(BaseModel):
    """POST /api/sensors/bulk-event-noise"""

    model_config = ConfigDict(extra="forbid")
    sensor_ids: list[int] = Field(min_length=1, max_length=1000)
    event_noise: EventNoiseIn

    @model_validator(mode="after")
    def _something(self) -> BulkEventNoiseIn:
        if not self.event_noise.model_fields_set:
            raise ValueError(
                "event_noise: give at least one of debounce, flap, debounce_polls, debounce_min_s"
            )
        return self
