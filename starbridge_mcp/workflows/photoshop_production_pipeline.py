from __future__ import annotations

import hashlib
import json
from typing import Any

from starbridge_mcp.adapters.local import LocalDeliveryAdapter, UserReviewAdapter
from starbridge_mcp.adapters.photoshop.workflow_adapter import PhotoshopWorkflowAdapter
from starbridge_mcp.domain.models import (
    WorkflowPlan,
    WorkflowStep,
    validate_relative_path,
    validate_sha256,
)
from starbridge_mcp.workflows.registry import WorkflowRegistry, build_workflow_plan

WORKFLOW_ID = "photoshop-production-v1"
OUTPUT_FORMATS = frozenset({"png", "jpeg", "psd"})


def _bounded_number(value: Any, default: int, minimum: int, maximum: int, field_name: str) -> int:
    result = default if value is None else int(value)
    if not minimum <= result <= maximum:
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}")
    return result


def _prepare_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    source_relative_path = validate_relative_path(str(inputs.get("sourceAssetRelativePath") or ""))
    source_sha256 = validate_sha256(str(inputs.get("sourceAssetSha256") or ""))
    formats = tuple(dict.fromkeys(str(item).lower() for item in inputs.get("outputFormats") or ()))
    if not formats:
        formats = ("png", "jpeg", "psd")
    if any(item not in OUTPUT_FORMATS for item in formats):
        raise ValueError("outputFormats may contain only png, jpeg, and psd")
    resize_canvas = bool(inputs.get("resizeCanvas", False))
    canvas = {
        "resize": resize_canvas,
        "width": _bounded_number(inputs.get("canvasWidth"), 1920, 64, 8192, "canvasWidth"),
        "height": _bounded_number(inputs.get("canvasHeight"), 1080, 64, 8192, "canvasHeight"),
    }
    adjustment = {
        "brightness": _bounded_number(inputs.get("brightness"), 0, -150, 150, "brightness"),
        "contrast": _bounded_number(inputs.get("contrast"), 0, -100, 100, "contrast"),
        "saturation": _bounded_number(inputs.get("saturation"), 0, -100, 100, "saturation"),
    }
    return {
        "sourceAssetRelativePath": source_relative_path,
        "sourceAssetSha256": source_sha256,
        "outputFormats": formats,
        "canvas": canvas,
        "adjustment": adjustment,
        "exportSubject": bool(inputs.get("exportSubject", False)),
    }


def _prepare_batch_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    raw_assets = inputs.get("sourceAssets")
    if not isinstance(raw_assets, list) or not 2 <= len(raw_assets) <= 32:
        raise ValueError("sourceAssets must contain between 2 and 32 managed images")
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_asset in raw_assets:
        if not isinstance(raw_asset, dict):
            raise ValueError("each sourceAssets item must be an object")
        relative_path = validate_relative_path(str(raw_asset.get("relativePath") or ""))
        sha256 = validate_sha256(str(raw_asset.get("sha256") or ""))
        item_id = (
            "item-"
            + hashlib.sha256(
                json.dumps(
                    {"relativePath": relative_path, "sha256": sha256},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()[:24]
        )
        if item_id in seen:
            continue
        seen.add(item_id)
        items.append(
            {
                "itemId": item_id,
                "sourceAssetRelativePath": relative_path,
                "sourceAssetSha256": sha256,
            }
        )
    if len(items) < 2:
        raise ValueError("sourceAssets must contain at least two distinct managed images")
    first = _prepare_inputs(
        {
            **inputs,
            "sourceAssetRelativePath": items[0]["sourceAssetRelativePath"],
            "sourceAssetSha256": items[0]["sourceAssetSha256"],
        }
    )
    return {**first, "items": tuple(items)}


def _create_photoshop_batch_plan(inputs: dict[str, Any]) -> WorkflowPlan:
    prepared = _prepare_batch_inputs(inputs)
    first = prepared["items"][0]
    session_common = {
        "sourceAssetRelativePath": first["sourceAssetRelativePath"],
        "sourceAssetSha256": first["sourceAssetSha256"],
    }
    batch_common = {
        "items": list(prepared["items"]),
        "outputFormats": list(prepared["outputFormats"]),
        "canvas": prepared["canvas"],
        "adjustment": prepared["adjustment"],
        "exportSubject": prepared["exportSubject"],
    }
    return build_workflow_plan(
        WORKFLOW_ID,
        (
            WorkflowStep(
                step_id="validate-batch",
                adapter="photoshop-production",
                input_data={**batch_common, "operation": "validate-batch"},
                validation=(
                    "managed-project-sources",
                    "source-hashes",
                    "deterministic-item-ids",
                ),
            ),
            WorkflowStep(
                step_id="probe-photoshop",
                adapter="photoshop-production",
                input_data={**session_common, "operation": "probe-session"},
                validation=("loopback-proxy", "uxp-connected", "licensed-host-only"),
            ),
            WorkflowStep(
                step_id="inspect-session",
                adapter="photoshop-production",
                input_data={**session_common, "operation": "inspect-session"},
                validation=("host-session", "optional-active-document", "redacted-session-summary"),
            ),
            WorkflowStep(
                step_id="execute-batch",
                adapter="photoshop-production",
                input_data={**batch_common, "operation": "execute-batch"},
                validation=(
                    "single-host-fifo",
                    "duplicate-before-write",
                    "per-item-checkpoint",
                    "continue-after-item-failure",
                    "safe-artifact-root",
                    "no-source-overwrite",
                ),
                requires_confirmation=True,
                retry_policy={"maxAttempts": 1},
                rollback_policy={
                    "enabled": True,
                    "closeSandboxOnFailure": True,
                    "cleanupAppOwnedStagingOnly": True,
                },
            ),
            WorkflowStep(
                step_id="verify-batch",
                adapter="photoshop-production",
                input_data={**batch_common, "operation": "verify-batch"},
                validation=(
                    "actual-files",
                    "per-item-sha256",
                    "native-reopen-if-psd-requested",
                ),
            ),
            WorkflowStep(
                step_id="review-result",
                adapter="user-review",
                input_data={"review": "photoshop-batch-output"},
                requires_confirmation=True,
                rollback_policy={"enabled": False, "preserveArtifacts": True},
            ),
            WorkflowStep(
                step_id="collect-delivery",
                adapter="local-delivery",
                input_data={"formats": "from-existing-artifacts-only"},
                validation=("no-fabricated-format", "redacted-evidence"),
            ),
        ),
    )


def create_photoshop_production_plan(inputs: dict[str, Any]) -> WorkflowPlan:
    if isinstance(inputs.get("sourceAssets"), list) and len(inputs["sourceAssets"]) > 1:
        return _create_photoshop_batch_plan(inputs)
    prepared = _prepare_inputs(inputs)
    common = {
        "sourceAssetRelativePath": prepared["sourceAssetRelativePath"],
        "sourceAssetSha256": prepared["sourceAssetSha256"],
    }
    return build_workflow_plan(
        WORKFLOW_ID,
        (
            WorkflowStep(
                step_id="validate-source",
                adapter="photoshop-production",
                input_data={**common, "operation": "validate-source"},
                validation=("managed-project-source", "png-or-jpeg", "source-hash"),
            ),
            WorkflowStep(
                step_id="probe-photoshop",
                adapter="photoshop-production",
                input_data={**common, "operation": "probe-session"},
                validation=("loopback-proxy", "uxp-connected", "licensed-host-only"),
            ),
            WorkflowStep(
                step_id="inspect-session",
                adapter="photoshop-production",
                input_data={**common, "operation": "inspect-session"},
                validation=("host-session", "optional-active-document", "redacted-session-summary"),
            ),
            WorkflowStep(
                step_id="execute-production",
                adapter="photoshop-production",
                input_data={
                    **common,
                    "operation": "execute-production",
                    "outputFormats": list(prepared["outputFormats"]),
                    "canvas": prepared["canvas"],
                    "adjustment": prepared["adjustment"],
                    "exportSubject": prepared["exportSubject"],
                },
                validation=(
                    "fixed-recipe",
                    "duplicate-before-write",
                    "safe-artifact-root",
                    "no-source-overwrite",
                    "staged-output-promotion",
                ),
                requires_confirmation=True,
                retry_policy={"maxAttempts": 1},
                rollback_policy={
                    "enabled": True,
                    "closeSandboxOnFailure": True,
                    "cleanupAppOwnedStagingOnly": True,
                },
            ),
            WorkflowStep(
                step_id="verify-output",
                adapter="photoshop-production",
                input_data={
                    **common,
                    "operation": "verify-output",
                    "outputFormats": list(prepared["outputFormats"]),
                    "canvas": prepared["canvas"],
                    "adjustment": prepared["adjustment"],
                    "exportSubject": prepared["exportSubject"],
                },
                validation=("actual-file", "sha256", "no-private-document-metadata"),
            ),
            WorkflowStep(
                step_id="review-result",
                adapter="user-review",
                input_data={"review": "photoshop-output"},
                requires_confirmation=True,
                rollback_policy={"enabled": False, "preserveArtifacts": True},
            ),
            WorkflowStep(
                step_id="collect-delivery",
                adapter="local-delivery",
                input_data={"formats": "from-existing-artifacts-only"},
                validation=("no-fabricated-format", "redacted-evidence"),
            ),
        ),
    )


def register_photoshop_production_workflow(
    registry: WorkflowRegistry, *, adapter: PhotoshopWorkflowAdapter | None = None
) -> None:
    photoshop_adapter = adapter or PhotoshopWorkflowAdapter()
    if photoshop_adapter.adapter_id not in registry.adapter_ids():
        registry.register_adapter(photoshop_adapter)
    if "user-review" not in registry.adapter_ids():
        registry.register_adapter(UserReviewAdapter())
    if "local-delivery" not in registry.adapter_ids():
        registry.register_adapter(LocalDeliveryAdapter())
    registry.register_workflow(WORKFLOW_ID, create_photoshop_production_plan)
