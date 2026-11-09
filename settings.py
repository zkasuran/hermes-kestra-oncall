"""Strict configuration and per-call runtime construction for profile isolation."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlsplit

PLUGIN = "kestra-oncall"
SECRET_NAMES = ("KESTRA_USERNAME", "KESTRA_PASSWORD", "KESTRA_API_TOKEN")
DEFAULTS: dict[str, Any] = {
    "kestra_url": "http://127.0.0.1:8080", "tenant": "main", "namespaces": [],
    "protected_flows": [], "rollback_on_failure": True,
    "replay_timeout_seconds": 300, "max_log_lines": 200,
}
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\Z")


class SettingsError(ValueError):
    """Invalid configuration must never silently widen the namespace policy."""


def _names(value: Any, key: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 100:
        raise SettingsError(f"{key} must be a JSON list of at most 100 identifiers.")
    if any(not isinstance(v, str) or not _IDENTIFIER.fullmatch(v) for v in value):
        raise SettingsError(f"{key} requires nonempty dot-separated identifiers, without wildcards.")
    return list(dict.fromkeys(value))


@dataclass(frozen=True)
class Settings:
    kestra_url: str = "http://127.0.0.1:8080"
    tenant: str = "main"
    namespaces: list[str] = field(default_factory=list)
    protected_flows: list[str] = field(default_factory=list)
    rollback_on_failure: bool = True
    replay_timeout_seconds: int = 300
    max_log_lines: int = 200

    @classmethod
    def parse(cls, values: Mapping[str, Any]) -> Settings:
        """Reject bad types rather than coercing strings such as 'false' to True."""
        data = {**DEFAULTS, **{k: v for k, v in values.items() if k in DEFAULTS}}
        url = data["kestra_url"]
        if not isinstance(url, str) or len(url) > 2048 or any(c.isspace() for c in url):
            raise SettingsError("kestra_url must be an HTTP(S) origin without credentials.")
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            raise SettingsError("kestra_url has an invalid host or port.") from None
        if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.query or parts.fragment or parts.path not in {"", "/"}
                or port == 0):
            raise SettingsError("kestra_url must be an HTTP(S) origin without path, query or credentials.")
        if parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise SettingsError("Use HTTPS for a remote Kestra server; HTTP is allowed only on loopback.")
        data["kestra_url"] = url.rstrip("/")
        if not isinstance(data["tenant"], str) or not _IDENTIFIER.fullmatch(data["tenant"]):
            raise SettingsError("tenant must be a nonempty identifier.")
        for key in ("namespaces", "protected_flows"):
            data[key] = _names(data[key], key)
        if any("." not in v for v in data["protected_flows"]):
            raise SettingsError("protected_flows entries must include namespace.flowId.")
        if type(data["rollback_on_failure"]) is not bool:
            raise SettingsError("rollback_on_failure must be true or false, not a string.")
        for key, maximum in (("replay_timeout_seconds", 300), ("max_log_lines", 200)):
            if type(data[key]) is not int or not 1 <= data[key] <= maximum:
                raise SettingsError(f"{key} must be an integer from 1 to {maximum}.")
        return cls(**data)

    @classmethod
    def from_context(cls, ctx: Any) -> Settings:
        return cls.parse({key: ctx.get_config(key, default) for key, default in DEFAULTS.items()})

    def public(self) -> dict[str, Any]:
        return asdict(self)


def read_secrets() -> dict[str, str]:
    """Resolve every credential at invocation time through the active Hermes profile."""
    from agent.secret_scope import get_secret

    return {name: get_secret(name) or "" for name in SECRET_NAMES}


def session_context() -> dict[str, str]:
    """Task-local markers prevent a webhook from borrowing another chat's presence."""
    from gateway.session_context import get_session_env

    return {name: get_session_env(name, "") or "" for name in (
        "HERMES_SESSION_PLATFORM", "HERMES_CRON_SESSION", "HERMES_SINGLE_QUERY_SESSION",
        "HERMES_SESSION_ID", "HERMES_SESSION_CHAT_ID",
    )}


def require_attended(context: Mapping[str, str]) -> None:
    """A prompt or a permissive host override never authorizes unattended writes."""
    platform = context.get("HERMES_SESSION_PLATFORM", "").lower()
    if platform in {"webhook", "msgraph_webhook", "api_server", "cron"} or any(
        str(context.get(key, "")).lower() in {"true", "1", "yes", "on"}
        for key in ("HERMES_CRON_SESSION", "HERMES_SINGLE_QUERY_SESSION")
    ):
        raise SettingsError("Unattended writes are blocked. Review this incident in an interactive chat.")


def redact(value: Any, secrets: Mapping[str, str]) -> Any:
    """Keep known credentials and credential-valued fields out of every surface."""
    if isinstance(value, str):
        for secret in sorted((v for v in secrets.values() if v), key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if re.search(
            r"(?i)(?:^|_)(?:password|token|secret|authorization|api_key)(?:$|_)", str(k)
        ) else redact(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, secrets) for v in value]
    return value


def error_payload(exc: Exception, secrets: Mapping[str, str]) -> dict[str, str]:
    """Transport bodies and unexpected exception strings may contain credentials."""
    name = type(exc).__name__
    if name == "KestraError":
        status = getattr(exc, "status", None)
        message = f"Kestra request failed{f' (HTTP {status})' if isinstance(status, int) else ''}."
        hint = "Check the Kestra URL, network and this profile's credentials; then retry."
    elif isinstance(exc, (SettingsError, ValueError, KeyError)) or name in {"PolicyError", "StaleError"}:
        message = str(exc)[:1500]
        hint = "Check settings or refresh the incident/pause and review a fresh proposal."
    else:
        message = f"Kestra on-call could not complete this request ({name})."
        hint = "Run hermes kestra-oncall doctor and retry after fixing the reported issue."
    return redact({"error": message, "hint": hint}, secrets)


@dataclass
class Runtime:
    settings: Settings
    client: Any
    store: Any
    oncall: Any
    pauses: Any
    conn: Any


@contextmanager
def open_runtime(ctx: Any, secrets: Mapping[str, str]) -> Iterator[Runtime]:
    """One connection per call avoids thread and profile state bleeding across runs."""
    from plugins.plugin_storage import plugin_db
    from .client import KestraClient
    from .incidents import Oncall, Policy
    from .pauses import PauseDesk
    from .store import Store

    settings = Settings.from_context(ctx)
    token = secrets.get("KESTRA_API_TOKEN") or None
    username = secrets.get("KESTRA_USERNAME") or None
    password = secrets.get("KESTRA_PASSWORD") or None
    if not token and bool(username) != bool(password):
        raise SettingsError("Basic authentication requires both KESTRA_USERNAME and KESTRA_PASSWORD.")
    client = KestraClient(settings.kestra_url, settings.tenant, username=None if token else username,
                          password=None if token else password, token=token, timeout=20.0)
    conn = plugin_db(PLUGIN)
    try:
        store = Store(conn)
        policy = Policy(namespaces=settings.namespaces, protected_flows=settings.protected_flows,
                        rollback_on_failure=settings.rollback_on_failure,
                        replay_timeout_s=settings.replay_timeout_seconds, max_log_lines=settings.max_log_lines)
        yield Runtime(settings, client, store, Oncall(client, store, policy), PauseDesk(client, store, policy), conn)
    finally:
        conn.close()
