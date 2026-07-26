from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any

from starbridge_mcp.adapters.base import (
    AdapterContext,
    AdapterResult,
    CreativeAdapter,
    ProbeResult,
    ValidationReport,
)
from starbridge_mcp.adapters.photoshop.node_proxy_client import bridge_status, rpc
from starbridge_mcp.domain.models import Artifact, JobError, validate_id, validate_relative_path
from starbridge_mcp.storage.artifact_store import ArtifactStore
from starbridge_mcp.storage.asset_store import file_sha256
from starbridge_mcp.storage.atomic_json import atomic_write_json, read_json

ProxyStatusReader = Callable[[], dict[str, Any]]
ProxyRpcRunner = Callable[..., dict[str, Any]]

_OPERATIONS = frozenset(
    {
        "validate-source",
        "probe-session",
        "inspect-session",
        "execute-production",
        "verify-output",
        "validate-batch",
        "execute-batch",
        "verify-batch",
    }
)
_OUTPUT_SPECS = {
    "png": ("photoshop-preview.png", "photoshop-preview"),
    "jpeg": ("photoshop-preview.jpg", "photoshop-preview"),
    "psd": ("photoshop-copy.psd", "photoshop-document"),
    "subject": ("photoshop-subject.png", "photoshop-subject"),
}
_SAFE_VERSION = re.compile(r"^[0-9A-Za-z._ -]{1,32}$")


class PhotoshopWorkflowAdapter(CreativeAdapter):
    """Fixed Photoshop production recipe routed through the local UXP proxy.

    The adapter never accepts arbitrary BatchPlay descriptors. Paths are derived from
    managed Project and Artifact roots, while the proxy performs an independent path
    check before forwarding the fixed recipe to UXP.
    """

    adapter_id = "photoshop-production"

    def __init__(
        self,
        *,
        status_reader: ProxyStatusReader = bridge_status,
        rpc_runner: ProxyRpcRunner = rpc,
    ) -> None:
        self.status_reader = status_reader
        self.rpc_runner = rpc_runner

    @staticmethod
    def _operation(context: AdapterContext) -> str:
        return str(context.step.input_data.get("operation") or "validate-source")

    @staticmethod
    def _state_path(context: AdapterContext) -> Path:
        batch_item_id = str(context.step.input_data.get("batchItemId") or "")
        suffix = f"-{validate_id(batch_item_id, 'batchItemId')}" if batch_item_id else ""
        return context.app_paths.jobs / context.job_id / f"photoshop-runtime{suffix}.json"

    @staticmethod
    def _batch_state_path(context: AdapterContext) -> Path:
        return context.app_paths.jobs / context.job_id / "photoshop-batch-runtime.json"

    @staticmethod
    def _managed_source_values(
        context: AdapterContext, relative_value: object, sha256_value: object
    ) -> Path:
        relative = validate_relative_path(str(relative_value or ""))
        candidate = (context.app_paths.root / relative).resolve(strict=True)
        projects_root = context.app_paths.projects.resolve(strict=True)
        try:
            candidate.relative_to(projects_root)
        except ValueError as exc:
            raise ValueError("Photoshop source must stay inside the managed project root") from exc
        if not candidate.is_file() or candidate.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            raise ValueError("Photoshop source must be one managed PNG or JPEG file")
        expected_hash = str(sha256_value or "")
        if expected_hash and file_sha256(candidate) != expected_hash:
            raise ValueError("Photoshop source hash no longer matches the approved plan")
        PhotoshopWorkflowAdapter._raster_info(candidate)
        return candidate

    @staticmethod
    def _raster_info(path: Path) -> dict[str, Any]:
        try:
            from PIL import Image

            with Image.open(path) as image:
                width, height = image.size
                mode = str(image.mode)
                image_format = str(image.format or "").upper()
                bands = tuple(str(value) for value in image.getbands())
                has_transparency = "A" in bands or "transparency" in image.info
                image.verify()
        except (ImportError, OSError, ValueError) as exc:
            raise ValueError("Photoshop raster input or output is not decodable") from exc
        if width < 1 or height < 1:
            raise ValueError("Photoshop raster dimensions are invalid")
        return {
            "width": int(width),
            "height": int(height),
            "mode": mode,
            "format": image_format,
            "hasTransparency": has_transparency,
        }

    @classmethod
    def _managed_source(cls, context: AdapterContext) -> Path:
        return cls._managed_source_values(
            context,
            context.step.input_data.get("sourceAssetRelativePath"),
            context.step.input_data.get("sourceAssetSha256"),
        )

    @staticmethod
    def _batch_items(context: AdapterContext) -> list[dict[str, str]]:
        raw_items = context.step.input_data.get("items")
        if not isinstance(raw_items, list) or not 2 <= len(raw_items) <= 32:
            raise ValueError("Photoshop batch requires between 2 and 32 items")
        items: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                raise ValueError("Photoshop batch item must be an object")
            item_id = validate_id(str(raw_item.get("itemId") or ""), "batchItemId")
            if item_id in seen:
                raise ValueError("Photoshop batch item IDs must be unique")
            seen.add(item_id)
            items.append(
                {
                    "itemId": item_id,
                    "sourceAssetRelativePath": validate_relative_path(
                        str(raw_item.get("sourceAssetRelativePath") or "")
                    ),
                    "sourceAssetSha256": str(raw_item.get("sourceAssetSha256") or ""),
                }
            )
        return items

    def _batch_request_fingerprint(
        self, context: AdapterContext, items: list[dict[str, str]]
    ) -> str:
        request = {
            "items": [
                {
                    "itemId": item["itemId"],
                    "sourceAssetSha256": item["sourceAssetSha256"],
                }
                for item in items
            ],
            "outputFormats": self._requested_formats(context),
            "canvas": dict(context.step.input_data.get("canvas") or {}),
            "adjustment": dict(context.step.input_data.get("adjustment") or {}),
            "exportSubject": bool(context.step.input_data.get("exportSubject")),
        }
        encoded = json.dumps(
            request, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    def _new_batch_state(
        self, context: AdapterContext, items: list[dict[str, str]]
    ) -> dict[str, Any]:
        return {
            "schemaVersion": 2,
            "requestFingerprint": self._batch_request_fingerprint(context, items),
            "singleHostFifo": True,
            "sourcePathsPersisted": False,
            "items": [
                {
                    "itemId": item["itemId"],
                    "status": "queued",
                    "attempts": 0,
                    "artifacts": [],
                }
                for item in items
            ],
        }

    def _batch_state_matches_request(
        self,
        context: AdapterContext,
        items: list[dict[str, str]],
        state: dict[str, Any],
    ) -> bool:
        rows = state.get("items")
        if (
            state.get("schemaVersion") != 2
            or state.get("sourcePathsPersisted") is not False
            or state.get("requestFingerprint") != self._batch_request_fingerprint(context, items)
            or not isinstance(rows, list)
            or len(rows) != len(items)
        ):
            return False
        expected_ids = [item["itemId"] for item in items]
        actual_ids = [str(row.get("itemId") or "") if isinstance(row, dict) else "" for row in rows]
        return actual_ids == expected_ids

    @staticmethod
    def _safe_host(raw: dict[str, Any]) -> dict[str, str]:
        version = str(raw.get("version") or "unknown")
        return {
            "app": "Photoshop",
            "version": version if _SAFE_VERSION.fullmatch(version) else "unknown",
        }

    @staticmethod
    def _result_payload(response: dict[str, Any]) -> dict[str, Any] | None:
        payload = response.get("result")
        return dict(payload) if isinstance(payload, dict) else None

    def probe(self, context: AdapterContext) -> ProbeResult:
        return ProbeResult(
            available=True,
            connection_state="adapter_available",
            message="Photoshop 固定工作流适配器可用；真实会话将在只读步骤中探测。",
        )

    def plan(self, context: AdapterContext) -> dict[str, Any]:
        operation = self._operation(context)
        return {
            "operation": operation,
            "writes": operation in {"execute-production", "execute-batch"},
            "safeRootRef": "starbridge-app-data/artifacts",
            "fixedRecipe": True,
            "arbitraryBatchPlay": False,
            "sourceOverwrite": False,
        }

    def validate(self, context: AdapterContext) -> ValidationReport:
        operation = self._operation(context)
        if operation not in _OPERATIONS:
            return ValidationReport(
                ok=False,
                error=JobError(
                    code="invalid_photoshop_operation", message="Photoshop 工作流步骤无效。"
                ),
            )
        try:
            if operation in {"validate-batch", "execute-batch", "verify-batch"}:
                for item in self._batch_items(context):
                    self._managed_source_values(
                        context,
                        item["sourceAssetRelativePath"],
                        item["sourceAssetSha256"],
                    )
            else:
                self._managed_source(context)
        except (OSError, ValueError):
            return ValidationReport(
                ok=False,
                error=JobError(
                    code="photoshop_source_invalid",
                    message="Photoshop 源素材缺失、已变化或不在项目安全目录。",
                    next_steps=("重新导入 PNG 或 JPEG 后建立新任务。",),
                ),
            )
        if operation == "verify-output" and not self._state_path(context).is_file():
            return ValidationReport(
                ok=False,
                error=JobError(
                    code="photoshop_runtime_missing", message="没有可验证的 Photoshop 执行记录。"
                ),
            )
        if operation == "verify-batch" and not self._batch_state_path(context).is_file():
            return ValidationReport(
                ok=False,
                error=JobError(
                    code="photoshop_batch_runtime_missing",
                    message="没有可验证的 Photoshop 批量执行记录。",
                ),
            )
        return ValidationReport(ok=True)

    def _validate_source(self, context: AdapterContext) -> AdapterResult:
        source = self._managed_source(context)
        return AdapterResult(
            status="completed",
            output={
                "sourceHashVerified": True,
                "managedProjectSource": True,
                "mediaType": "image/png" if source.suffix.lower() == ".png" else "image/jpeg",
            },
        )

    def _validate_batch(self, context: AdapterContext) -> AdapterResult:
        items = self._batch_items(context)
        for item in items:
            self._managed_source_values(
                context,
                item["sourceAssetRelativePath"],
                item["sourceAssetSha256"],
            )
        return AdapterResult(
            status="completed",
            output={
                "itemCount": len(items),
                "sourceHashesVerified": True,
                "managedProjectSources": True,
                "deterministicItemIds": True,
            },
        )

    def _probe_session(self) -> AdapterResult:
        status = self.status_reader()
        connected = bool(
            status.get("ok")
            and status.get("node_proxy_running")
            and status.get("uxp_client_connected")
            and status.get("photoshop_host_seen")
        )
        if not connected:
            return AdapterResult(
                status="needs_user",
                output={
                    "message": "尚未连接 Photoshop UXP。请启动本机代理、打开已授权 Photoshop 并连接 StarBridge 插件后继续。",
                    "nodeProxyRunning": bool(status.get("node_proxy_running")),
                    "uxpClientConnected": bool(status.get("uxp_client_connected")),
                },
                warnings=("未执行任何 Photoshop 写入。",),
            )
        return AdapterResult(
            status="completed",
            output={
                "connectionVerified": True,
                "host": self._safe_host(dict(status.get("photoshop_host") or {})),
            },
        )

    def _inspect_session(self, context: AdapterContext) -> AdapterResult:
        response = self.rpc_runner("ps.document.info", {"job_id": context.job_id}, timeout=8)
        payload = self._result_payload(response)
        document = dict((payload or {}).get("document") or {})
        if not payload or not payload.get("ok"):
            return AdapterResult(
                status="needs_user",
                output={"message": "无法读取 Photoshop 会话状态，请重新连接插件后继续。"},
                warnings=("未执行 Photoshop 写入。",),
            )
        summary = {
            "width": max(0, int(document.get("width") or 0)),
            "height": max(0, int(document.get("height") or 0)),
            "layerCount": max(0, int(document.get("layer_count") or 0)),
            "resolution": max(0, int(document.get("resolution") or 0)),
            "activeDocument": bool(payload.get("active_document", bool(document))),
            "host": self._safe_host(dict(payload.get("photoshop_host") or {})),
        }
        atomic_write_json(
            self._state_path(context),
            {
                "schemaVersion": 1,
                "session": summary,
                "outputs": [],
                "sourcePathPersisted": False,
                "documentNamePersisted": False,
                "layerNamesPersisted": False,
            },
        )
        return AdapterResult(status="completed", output={"session": summary})

    @staticmethod
    def _requested_formats(context: AdapterContext) -> tuple[str, ...]:
        values = context.step.input_data.get("outputFormats") or ["png", "jpeg", "psd"]
        return tuple(str(item) for item in values)

    @classmethod
    def _validate_production_output(
        cls,
        path: Path,
        output_format: str,
        *,
        expected_width: int,
        expected_height: int,
        source_has_transparency: bool,
        native_payload: dict[str, Any],
    ) -> dict[str, Any]:
        if not path.is_file() or path.stat().st_size < 64:
            raise ValueError("Photoshop output is missing or too small")
        if output_format == "psd":
            with path.open("rb") as stream:
                if stream.read(4) != b"8BPS":
                    raise ValueError("Photoshop PSD signature is invalid")
            if (
                native_payload.get("native_reopen_validated") is not True
                or int(native_payload.get("native_reopen_width") or 0) != expected_width
                or int(native_payload.get("native_reopen_height") or 0) != expected_height
                or int(native_payload.get("native_reopen_layer_count") or 0) < 1
            ):
                raise ValueError("Photoshop PSD native reopen evidence is invalid")
            return {
                "signatureValidated": True,
                "width": expected_width,
                "height": expected_height,
                "layerCount": int(native_payload.get("native_reopen_layer_count") or 0),
            }

        raster = cls._raster_info(path)
        expected_format = "JPEG" if output_format == "jpeg" else "PNG"
        if raster["format"] != expected_format:
            raise ValueError("Photoshop raster format does not match the requested output")
        if (raster["width"], raster["height"]) != (expected_width, expected_height):
            raise ValueError("Photoshop raster dimensions do not match the recipe")
        if output_format == "jpeg" and raster["mode"] not in {"RGB", "CMYK", "L"}:
            raise ValueError("Photoshop JPEG color mode is invalid")
        if output_format in {"png", "subject"} and raster["mode"] not in {
            "RGB",
            "RGBA",
            "L",
            "LA",
            "P",
        }:
            raise ValueError("Photoshop PNG color mode is invalid")
        if (
            output_format == "subject" or (output_format == "png" and source_has_transparency)
        ) and not raster["hasTransparency"]:
            raise ValueError("Photoshop PNG transparency does not match the recipe")
        return {
            "signatureValidated": True,
            "width": raster["width"],
            "height": raster["height"],
            "mode": raster["mode"],
            "hasTransparency": raster["hasTransparency"],
        }

    @staticmethod
    def _batch_item_context(
        context: AdapterContext,
        item: dict[str, str],
        *,
        operation: str,
    ) -> AdapterContext:
        item_input = {
            **context.step.input_data,
            **item,
            "operation": operation,
            "batchItemId": item["itemId"],
        }
        return replace(context, step=replace(context.step, input_data=item_input))

    @staticmethod
    def _transient_proxy_failure(response: dict[str, Any]) -> bool:
        error = response.get("error")
        if not isinstance(error, dict):
            return False
        message = str(error.get("message") or "").lower()
        return any(
            token in message
            for token in (
                "uxp_client_not_connected",
                "uxp_timeout",
                "photoshop_busy",
                "host_busy",
                "document_busy",
                "connection",
                "disconnected",
                "timeout",
            )
        )

    def _execute_production(self, context: AdapterContext) -> AdapterResult:
        source = self._managed_source(context)
        store = ArtifactStore(context.app_paths.artifacts)
        formats = self._requested_formats(context)
        requested = list(formats)
        if bool(context.step.input_data.get("exportSubject")):
            requested.append("subject")
        batch_item_id = str(context.step.input_data.get("batchItemId") or "")
        output_paths: dict[str, Path] = {}
        for output_format in requested:
            basename, _kind = _OUTPUT_SPECS[output_format]
            if batch_item_id:
                basename = f"{validate_id(batch_item_id, 'batchItemId')}-{basename}"
            desired = store.job_directory(context.project_id, context.job_id) / basename
            output_paths[output_format] = (
                desired
                if desired.exists()
                else store.allocate_path(context.project_id, context.job_id, basename)
            )
        params = {
            "job_id": context.job_id,
            "confirm_write": True,
            "source_path": str(source),
            "source_sha256": str(context.step.input_data.get("sourceAssetSha256") or ""),
            "outputs": {key: str(value) for key, value in output_paths.items()},
            "canvas": dict(context.step.input_data.get("canvas") or {}),
            "adjustment": dict(context.step.input_data.get("adjustment") or {}),
            "export_subject": bool(context.step.input_data.get("exportSubject")),
        }
        if batch_item_id:
            params["batch_item_id"] = batch_item_id
        try:
            response = self.rpc_runner("ps.production.execute_confirmed", params, timeout=60)
        except (OSError, TimeoutError):
            return AdapterResult(
                status="needs_user",
                output={
                    "message": "Photoshop 连接已中断；本项已保留在安全检查点，重新连接后可继续。",
                    "batchItemId": batch_item_id or None,
                },
                warnings=("未把未确认的临时输出登记为交付文件。",),
            )
        if self._transient_proxy_failure(response):
            return AdapterResult(
                status="needs_user",
                output={
                    "message": "Photoshop 暂时不可用；本项已保留在安全检查点，重新连接后可继续。",
                    "batchItemId": batch_item_id or None,
                },
                warnings=("未把未确认的临时输出登记为交付文件。",),
            )
        payload = self._result_payload(response)
        if not payload or not payload.get("executed") or payload.get("success") is False:
            errors = list((payload or {}).get("errors") or ())
            code = str(errors[0].get("code") if errors and isinstance(errors[0], dict) else "")
            return AdapterResult(
                status="cancelled" if code == "user_cancelled" else "failed",
                error=(
                    None
                    if code == "user_cancelled"
                    else JobError(
                        code="photoshop_execution_failed",
                        message="Photoshop 受控副本没有完成，原始文档未被覆盖。",
                        retryable=False,
                        next_steps=("检查 Photoshop 模态状态和插件连接后建立新任务。",),
                    )
                ),
                warnings=("代理会清理应用拥有的临时输出；不会删除源文件。",),
            )
        source_raster = self._raster_info(source)
        canvas = dict(context.step.input_data.get("canvas") or {})
        expected_width = (
            int(canvas.get("width") or 0)
            if canvas.get("resize") is True
            else int(source_raster["width"])
        )
        expected_height = (
            int(canvas.get("height") or 0)
            if canvas.get("resize") is True
            else int(source_raster["height"])
        )
        artifacts: list[Artifact] = []
        output_evidence: dict[str, dict[str, Any]] = {}
        for output_format, path in output_paths.items():
            try:
                output_evidence[output_format] = self._validate_production_output(
                    path,
                    output_format,
                    expected_width=expected_width,
                    expected_height=expected_height,
                    source_has_transparency=bool(source_raster["hasTransparency"]),
                    native_payload=payload,
                )
            except (OSError, ValueError):
                return AdapterResult(
                    status="failed",
                    error=JobError(
                        code="photoshop_output_validation_failed",
                        message="Photoshop 报告完成，但输出格式、尺寸、透明度或原生重开证据不合格。",
                    ),
                )
            artifacts.append(
                store.register(
                    context.project_id,
                    context.job_id,
                    path,
                    kind=_OUTPUT_SPECS[output_format][1],
                )
            )
        state = read_json(self._state_path(context))
        state["outputs"] = [
            {
                "artifactId": artifact.artifact_id,
                "basename": artifact.basename,
                "sha256": artifact.sha256,
                "sizeBytes": artifact.size_bytes,
                **output_evidence[output_format],
            }
            for output_format, artifact in zip(output_paths, artifacts, strict=True)
        ]
        state["sandboxCopy"] = bool(payload.get("sandbox_copy"))
        state["nativeReopenValidated"] = bool(payload.get("native_reopen_validated"))
        state["nativeReopenWidth"] = int(payload.get("native_reopen_width") or 0)
        state["nativeReopenHeight"] = int(payload.get("native_reopen_height") or 0)
        state["nativeReopenLayerCount"] = int(payload.get("native_reopen_layer_count") or 0)
        state["rollbackSupported"] = bool(payload.get("rollback_supported"))
        state["idempotentReplay"] = bool(payload.get("idempotent_replay"))
        atomic_write_json(self._state_path(context), state)
        return AdapterResult(
            status="completed",
            output={
                "sandboxCopy": bool(payload.get("sandbox_copy")),
                "sourceOverwritten": False,
                "artifactCount": len(artifacts),
                "nativeReopenValidated": bool(payload.get("native_reopen_validated")),
                "rollbackSupported": bool(payload.get("rollback_supported")),
                "idempotentReplay": bool(payload.get("idempotent_replay")),
                "dimensionsValidated": True,
                "colorModesValidated": True,
                "transparencyValidated": True,
            },
            artifacts=tuple(artifacts),
            warnings=tuple(str(item) for item in payload.get("warnings") or ()),
        )

    def _execute_batch(self, context: AdapterContext) -> AdapterResult:
        items = self._batch_items(context)
        batch_path = self._batch_state_path(context)
        if batch_path.is_file():
            batch_state = read_json(batch_path)
            if not self._batch_state_matches_request(context, items, batch_state):
                return AdapterResult(
                    status="failed",
                    error=JobError(
                        code="photoshop_batch_state_mismatch",
                        message="Photoshop 批量续跑状态与当前请求不一致；未执行任何写入。",
                    ),
                )
        else:
            batch_state = self._new_batch_state(context, items)
            atomic_write_json(batch_path, batch_state)

        state_rows = {
            str(row.get("itemId") or ""): row
            for row in list(batch_state.get("items") or ())
            if isinstance(row, dict)
        }
        artifacts: list[Artifact] = []
        warnings: list[str] = []
        for item in items:
            if context.cancellation.cancelled:
                atomic_write_json(batch_path, batch_state)
                return AdapterResult(
                    status="cancelled",
                    artifacts=tuple(artifacts),
                    warnings=("仅保留已经验收完成的批量项目。",),
                )
            row = state_rows.get(item["itemId"])
            if row is None:
                row = {
                    "itemId": item["itemId"],
                    "status": "queued",
                    "attempts": 0,
                    "artifacts": [],
                }
                batch_state.setdefault("items", []).append(row)
                state_rows[item["itemId"]] = row
            if row.get("status") == "completed":
                artifacts.extend(
                    Artifact.from_dict(raw)
                    for raw in row.get("artifacts") or ()
                    if isinstance(raw, dict)
                )
                continue

            item_context = self._batch_item_context(context, item, operation="execute-production")
            item_state_path = self._state_path(item_context)
            if not item_state_path.is_file():
                shared_state = (
                    read_json(context.app_paths.jobs / context.job_id / "photoshop-runtime.json")
                    if (
                        context.app_paths.jobs / context.job_id / "photoshop-runtime.json"
                    ).is_file()
                    else {}
                )
                atomic_write_json(
                    item_state_path,
                    {
                        "schemaVersion": 1,
                        "session": dict(shared_state.get("session") or {}),
                        "outputs": [],
                        "sourcePathPersisted": False,
                        "documentNamePersisted": False,
                        "layerNamesPersisted": False,
                    },
                )
            row["status"] = "running"
            row["attempts"] = int(row.get("attempts") or 0) + 1
            row.pop("errorCode", None)
            atomic_write_json(batch_path, batch_state)
            result = self._execute_production(item_context)
            if result.status == "needs_user":
                row["status"] = "needs_user"
                atomic_write_json(batch_path, batch_state)
                return AdapterResult(
                    status="needs_user",
                    output={
                        **result.output,
                        "completedItemCount": sum(
                            1
                            for current in state_rows.values()
                            if current.get("status") == "completed"
                        ),
                        "pendingItemId": item["itemId"],
                    },
                    artifacts=tuple(artifacts),
                    warnings=result.warnings,
                )
            if result.status == "cancelled":
                row["status"] = "cancelled"
                atomic_write_json(batch_path, batch_state)
                return AdapterResult(
                    status="cancelled",
                    artifacts=tuple(artifacts),
                    warnings=result.warnings,
                )
            if result.status == "failed":
                row["status"] = "failed"
                row["errorCode"] = (
                    result.error.code if result.error else "photoshop_execution_failed"
                )
                warnings.append(f"{item['itemId']} 未完成；批次继续处理其余项目。")
                atomic_write_json(batch_path, batch_state)
                continue
            row["status"] = "completed"
            row["artifacts"] = [artifact.to_dict() for artifact in result.artifacts]
            artifacts.extend(result.artifacts)
            atomic_write_json(batch_path, batch_state)

        completed_count = sum(1 for row in state_rows.values() if row.get("status") == "completed")
        failed_count = sum(1 for row in state_rows.values() if row.get("status") == "failed")
        if completed_count < 1:
            return AdapterResult(
                status="failed",
                error=JobError(
                    code="photoshop_batch_all_failed",
                    message="Photoshop 批量没有任何项目完成；原始素材未被修改。",
                ),
                warnings=tuple(warnings),
            )
        return AdapterResult(
            status="completed",
            output={
                "itemCount": len(items),
                "completedItemCount": completed_count,
                "failedItemCount": failed_count,
                "singleHostFifo": True,
                "checkpointed": True,
                "sourceOverwritten": False,
            },
            artifacts=tuple(artifacts),
            warnings=tuple(warnings),
        )

    def _verify_output(self, context: AdapterContext) -> AdapterResult:
        state = read_json(self._state_path(context))
        rows = list(state.get("outputs") or ())
        requires_native_reopen = any(
            str(row.get("basename") or "").lower().endswith(".psd")
            for row in rows
            if isinstance(row, dict)
        )
        if requires_native_reopen and state.get("nativeReopenValidated") is not True:
            return AdapterResult(
                status="failed",
                error=JobError(
                    code="photoshop_native_reopen_unverified",
                    message="Photoshop PSD delivery was not verified by a native reopen.",
                ),
            )
        artifact_dir = ArtifactStore(context.app_paths.artifacts).job_directory(
            context.project_id, context.job_id
        )
        source_raster = self._raster_info(self._managed_source(context))
        canvas = dict(context.step.input_data.get("canvas") or {})
        expected_width = (
            int(canvas.get("width") or 0)
            if canvas.get("resize") is True
            else int(source_raster["width"])
        )
        expected_height = (
            int(canvas.get("height") or 0)
            if canvas.get("resize") is True
            else int(source_raster["height"])
        )
        native_payload = {
            "native_reopen_validated": state.get("nativeReopenValidated"),
            "native_reopen_width": state.get("nativeReopenWidth"),
            "native_reopen_height": state.get("nativeReopenHeight"),
            "native_reopen_layer_count": state.get("nativeReopenLayerCount"),
        }
        verified = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            basename = str(row.get("basename") or "")
            candidate = artifact_dir / basename
            if not candidate.is_file() or file_sha256(candidate) != row.get("sha256"):
                return AdapterResult(
                    status="failed",
                    error=JobError(
                        code="photoshop_output_hash_mismatch",
                        message="Photoshop 交付文件与执行证据不一致。",
                    ),
                )
            output_format = (
                "subject"
                if basename.endswith("photoshop-subject.png")
                else "jpeg"
                if basename.endswith(".jpg")
                else "psd"
                if basename.endswith(".psd")
                else "png"
            )
            try:
                self._validate_production_output(
                    candidate,
                    output_format,
                    expected_width=expected_width,
                    expected_height=expected_height,
                    source_has_transparency=bool(source_raster["hasTransparency"]),
                    native_payload=native_payload,
                )
            except (OSError, ValueError):
                return AdapterResult(
                    status="failed",
                    error=JobError(
                        code="photoshop_output_structure_invalid",
                        message="Photoshop 交付文件的格式、尺寸、色彩或透明度验收失败。",
                    ),
                )
            verified += 1
        if verified < 1:
            return AdapterResult(
                status="failed",
                error=JobError(
                    code="photoshop_output_empty", message="没有可验证的 Photoshop 输出。"
                ),
            )
        return AdapterResult(
            status="completed",
            output={
                "verifiedArtifactCount": verified,
                "hashesVerified": True,
                "nativeReopenValidated": bool(state.get("nativeReopenValidated")),
                "sourcePathPersisted": False,
                "documentNamePersisted": False,
                "layerNamesPersisted": False,
            },
        )

    def _verify_batch(self, context: AdapterContext) -> AdapterResult:
        items = self._batch_items(context)
        batch_path = self._batch_state_path(context)
        batch_state = read_json(batch_path)
        if not self._batch_state_matches_request(context, items, batch_state):
            return AdapterResult(
                status="failed",
                error=JobError(
                    code="photoshop_batch_state_mismatch",
                    message="Photoshop 批量验收状态与当前请求不一致。",
                ),
            )
        state_rows = {
            str(row.get("itemId") or ""): row
            for row in list(batch_state.get("items") or ())
            if isinstance(row, dict)
        }
        verified = 0
        failed = 0
        warnings: list[str] = []
        for item in items:
            row = state_rows.get(item["itemId"])
            if row is None or row.get("status") != "completed":
                failed += 1
                continue
            item_context = self._batch_item_context(context, item, operation="verify-output")
            result = self._verify_output(item_context)
            if result.status != "completed":
                row["status"] = "failed"
                row["errorCode"] = (
                    result.error.code if result.error else "photoshop_verification_failed"
                )
                failed += 1
                warnings.append(f"{item['itemId']} 的输出验收失败。")
                continue
            verified += 1
        atomic_write_json(batch_path, batch_state)
        if verified < 1:
            return AdapterResult(
                status="failed",
                error=JobError(
                    code="photoshop_batch_verification_failed",
                    message="Photoshop 批量没有可通过哈希和原生重开验收的项目。",
                ),
                warnings=tuple(warnings),
            )
        return AdapterResult(
            status="completed",
            output={
                "verifiedItemCount": verified,
                "failedItemCount": failed,
                "hashesVerified": True,
                "nativeReopenRequiredForPsd": "psd" in self._requested_formats(context),
                "sourceHashesVerified": True,
            },
            warnings=tuple(warnings),
        )

    def execute(self, context: AdapterContext) -> AdapterResult:
        if context.cancellation.cancelled:
            return AdapterResult(status="cancelled")
        operation = self._operation(context)
        if operation == "validate-source":
            return self._validate_source(context)
        if operation == "validate-batch":
            return self._validate_batch(context)
        if operation == "probe-session":
            return self._probe_session()
        if operation == "inspect-session":
            return self._inspect_session(context)
        if operation == "execute-production":
            return self._execute_production(context)
        if operation == "execute-batch":
            return self._execute_batch(context)
        if operation == "verify-output":
            return self._verify_output(context)
        if operation == "verify-batch":
            return self._verify_batch(context)
        raise ValueError("unsupported Photoshop workflow operation")

    def collect_evidence(self, context: AdapterContext, result: AdapterResult) -> dict[str, Any]:
        return {
            "adapter": self.adapter_id,
            "stepId": context.step.step_id,
            "status": result.status,
            "artifactIds": [artifact.artifact_id for artifact in result.artifacts],
            "artifactHashes": [artifact.sha256 for artifact in result.artifacts],
            "sourcePathPersisted": False,
            "documentNamePersisted": False,
            "layerNamesPersisted": False,
            "arbitraryBatchPlayAccepted": False,
        }

    def rollback(self, context: AdapterContext, result: AdapterResult) -> bool:
        # The UXP modal handler owns rollback of the duplicate document; final files are
        # promoted only after the handler reports success.
        return bool(result.output.get("rollbackSupported", True))
