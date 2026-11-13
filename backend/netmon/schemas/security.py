"""[1.4.1] Security settings in `PUT /api/settings` (data-model "[1.4.1] Security settings")."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from netmon.services.security import KEYS, SESSION_MAX_AGE_H_MAX, valid_ancestor

SECURITY_KEYS = KEYS


class SecuritySettingsMixin(BaseModel):
    egress_allow_loopback: bool | None = None
    session_max_age_h: int | None = Field(default=None, ge=1, le=SESSION_MAX_AGE_H_MAX)
    login_ip_max_failures: int | None = Field(default=None, ge=0, le=10000)
    frame_ancestors: list[str] | None = Field(default=None, max_length=20)

    @field_validator("frame_ancestors")
    @classmethod
    def _ancestors(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        out: list[str] = []
        for item in v:
            origin = str(item).strip().rstrip("/")
            if not origin:
                continue
            if not valid_ancestor(origin):
                raise ValueError(
                    "each entry must be an origin such as https://wall.example.com "
                    "(scheme, host, optional port; the host may start with *.)"
                )
            origin = origin.lower()
            if origin not in out:
                out.append(origin)
        return out
