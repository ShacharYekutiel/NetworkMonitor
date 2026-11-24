"""[if-noise] `PUT /api/settings` keys: sensor event debounce / flapping and the important-port
criteria (data-model "[if-noise] Settings keys")."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

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
