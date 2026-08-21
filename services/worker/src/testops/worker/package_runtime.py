"""Fail-closed catalog for immutable automation package runtimes."""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import ValidationError

from testops.contracts import AutomationPackageRef, AutomationPackageRuntimeRef

MAX_PACKAGE_RUNTIMES = 50


class PackageRuntimeUnavailable(RuntimeError):
    """The worker does not host the exact immutable runtime requested by a Run."""


@dataclass(frozen=True, slots=True)
class PackageRuntimeCatalog:
    packages: tuple[AutomationPackageRuntimeRef, ...]

    @classmethod
    def from_json(cls, raw_catalog: str) -> PackageRuntimeCatalog:
        try:
            document = json.loads(raw_catalog)
        except json.JSONDecodeError as exc:
            raise RuntimeError("RUNNER_PACKAGE_CATALOG must be valid JSON") from exc
        if not isinstance(document, list):
            raise RuntimeError("RUNNER_PACKAGE_CATALOG must be a JSON array")
        if len(document) > MAX_PACKAGE_RUNTIMES:
            raise RuntimeError(
                f"RUNNER_PACKAGE_CATALOG cannot contain more than {MAX_PACKAGE_RUNTIMES} entries"
            )
        try:
            packages = tuple(AutomationPackageRuntimeRef.model_validate(item) for item in document)
        except ValidationError as exc:
            raise RuntimeError(
                "RUNNER_PACKAGE_CATALOG contains an invalid runtime reference"
            ) from exc
        keys = tuple(_runtime_key(package) for package in packages)
        if len(keys) != len(set(keys)):
            raise RuntimeError("RUNNER_PACKAGE_CATALOG contains duplicate runtime references")
        return cls(packages=packages)

    def capability_payload(self) -> list[dict[str, str]]:
        return [package.model_dump(mode="json") for package in self.packages]

    def require(self, package: AutomationPackageRef) -> AutomationPackageRuntimeRef:
        requested_key = _runtime_key(package)
        for available in self.packages:
            if _runtime_key(available) == requested_key:
                return available
        immutable_reference = f"{package.image_repository}@{package.digest}"
        raise PackageRuntimeUnavailable(
            "worker does not host automation package runtime "
            f"{package.runner_type}:{immutable_reference}"
        )


def _runtime_key(package: AutomationPackageRuntimeRef) -> tuple[str, str, str]:
    return package.runner_type, package.image_repository, package.digest.lower()
