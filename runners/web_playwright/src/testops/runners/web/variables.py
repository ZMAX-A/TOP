"""Resolve runtime variables without placing secret values in a Run Snapshot."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Protocol

from testops.contracts import RunSnapshot, SecretBinding

_VARIABLE_PATTERN = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


class SecretResolutionError(RuntimeError):
    """A secret reference could not be resolved by the Runner."""


class SecretProvider(Protocol):
    def resolve(self, binding: SecretBinding) -> str:
        """Return a secret value without logging it."""


class MappingSecretProvider:
    """In-memory provider for tests and embedding; its representation is redacted."""

    def __init__(self, values_by_ref: Mapping[str, str]):
        self._values_by_ref = dict(values_by_ref)

    def __repr__(self) -> str:
        return "MappingSecretProvider(<redacted>)"

    def resolve(self, binding: SecretBinding) -> str:
        try:
            value = self._values_by_ref[binding.ref]
        except KeyError as exc:
            raise SecretResolutionError(
                f"secret binding '{binding.name}' could not be resolved"
            ) from exc
        if not value:
            raise SecretResolutionError(
                f"secret binding '{binding.name}' resolved to an empty value"
            )
        return value


class EnvironmentSecretProvider:
    """Resolve bindings from TESTOPS_SECRET_<BINDING_NAME> environment variables."""

    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        *,
        prefix: str = "TESTOPS_SECRET_",
    ):
        self._environ = os.environ if environ is None else environ
        self._prefix = prefix

    def resolve(self, binding: SecretBinding) -> str:
        environment_name = f"{self._prefix}{binding.name}"
        value = self._environ.get(environment_name, "")
        if not value:
            raise SecretResolutionError(
                f"secret binding '{binding.name}' requires environment variable "
                f"'{environment_name}'"
            )
        return value


class VariableResolver:
    """Resolve placeholders and redact all resolved secret values from errors."""

    def __init__(self, job: RunSnapshot, secret_provider: SecretProvider):
        values = {binding.name: binding.value for binding in job.variables}
        secret_values: list[str] = []
        for binding in job.secret_bindings:
            value = secret_provider.resolve(binding)
            values[binding.name] = value
            secret_values.append(value)
        self._values = values
        self._secret_values = tuple(sorted(set(secret_values), key=len, reverse=True))

    def __repr__(self) -> str:
        return "VariableResolver(<redacted>)"

    def resolve_text(self, value: str) -> str:
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            try:
                return self._values[name]
            except KeyError as exc:
                raise SecretResolutionError(f"runtime variable '{name}' is not bound") from exc

        return _VARIABLE_PATTERN.sub(replace, value)

    def redact(self, value: str) -> str:
        redacted = value
        for secret in self._secret_values:
            if secret:
                redacted = redacted.replace(secret, "***")
        return redacted
