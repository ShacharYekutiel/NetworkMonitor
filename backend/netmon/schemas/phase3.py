"""[3] Request models of the phase-3 API (api.md "Phase 3", conventions "Phase 3 notes"):
the `PUT /api/settings` mixins (feature toggles, config backup, new-device alerts) and the
config-backup / new-device bodies. `extra="forbid"` for the endpoint bodies."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from netmon.configbackup.credentials import SshCredentialsIn

HHMM_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")

__all__ = [
    "AllowEntryIn",
    "BackupRunIn",
    "ConfigBackupSettingsMixin",
    "HostKeyAcceptIn",
    "NewDeviceAckIn",
    "NewdevSettingsMixin",
    "Phase3FeaturesMixin",
    "SshCredentialsIn",
    "SshTestIn",
]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Phase3FeaturesMixin(BaseModel):
    """[3] the 8 phase-3 feature toggles of `PUT /api/settings` (architecture § 13.0)."""

    feature_config_backup_enabled: bool | None = None
    feature_flows_enabled: bool | None = None
    feature_new_device_alerts_enabled: bool | None = None
    feature_sso_kerberos_enabled: bool | None = None
    feature_sso_oidc_enabled: bool | None = None
    feature_reports_enabled: bool | None = None
    feature_noc_enabled: bool | None = None
    feature_chat_ack_enabled: bool | None = None


class ConfigBackupSettingsMixin(BaseModel):
    config_backup_time: str | None = None
    config_backup_window_min: int | None = Field(default=None, ge=0, le=720)
    config_backup_concurrency: int | None = Field(default=None, ge=1, le=16)
    config_backup_timeout_s: int | None = Field(default=None, ge=30, le=600)
    config_backup_mask_secrets: bool | None = None
    config_backup_max_versions: int | None = Field(default=None, ge=2, le=1000)
    config_backup_min_versions: int | None = Field(default=None, ge=1, le=100)
    config_backup_retention_days: int | None = Field(default=None, ge=7, le=3650)
    config_backup_fail_threshold: int | None = Field(default=None, ge=1, le=10)
    config_change_alert: bool | None = None
    config_change_clear_min: int | None = Field(default=None, ge=5, le=10080)

    @field_validator("config_backup_time")
    @classmethod
    def _hhmm(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not HHMM_RE.match(v.strip()):
            raise ValueError("must be HH:MM (00:00-23:59)")
        return v.strip()


class NewdevSettingsMixin(BaseModel):
    newdev_learning_days: int | None = Field(default=None, ge=0, le=60)
    newdev_scope_group_ids: list[int] | None = Field(default=None, max_length=100)
    newdev_ignore_random_macs: bool | None = None
    newdev_condition_clear_min: int | None = Field(default=None, ge=10, le=10080)
    newdev_forget_after_days: int | None = Field(default=None, ge=0, le=3650)

    @field_validator("newdev_forget_after_days")
    @classmethod
    def _forget(cls, v: int | None) -> int | None:
        if v is not None and 0 < v < 30:
            raise ValueError("must be 0 (never) or 30-3650")
        return v

    @field_validator("newdev_scope_group_ids")
    @classmethod
    def _scope(cls, v: list[int] | None) -> list[int] | None:
        return None if v is None else list(dict.fromkeys(v))


# ------------------------------------------------------------------ config backup bodies
class SshTestIn(_In):
    ssh_credentials: SshCredentialsIn | None = None
    driver: str | None = Field(default=None, max_length=64)


class BackupRunIn(_In):
    device_ids: list[int] | None = Field(default=None, max_length=5000)


class HostKeyAcceptIn(_In):
    fingerprint: str = Field(min_length=1, max_length=64)


# ------------------------------------------------------------------ new-device bodies
class NewDeviceAckIn(_In):
    macs: list[str] = Field(min_length=1, max_length=500)
    comment: str = Field(default="", max_length=500)

    @field_validator("macs")
    @classmethod
    def _macs(cls, v: list[str]) -> list[str]:
        from netmon.newdev.detector import normalize_mac

        out = []
        for m in v:
            n = normalize_mac(m)
            if n is None:
                raise ValueError(f"{m!r} is not a MAC address")
            out.append(n)
        return list(dict.fromkeys(out))


class AllowEntryIn(_In):
    prefix: str = Field(min_length=6, max_length=17)
    comment: str = Field(default="", max_length=200)

    @field_validator("prefix")
    @classmethod
    def _prefix(cls, v: str) -> str:
        from netmon.newdev.detector import parse_prefix

        if parse_prefix(v) is None:
            raise ValueError(
                "must be a MAC address, an OUI (6 hex digits) or a 28/36-bit prefix "
                "(7 or 9 hex digits)"
            )
        return v
