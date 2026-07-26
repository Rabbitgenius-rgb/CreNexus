from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from PIL import Image

from starbridge_mcp.adapters.photoshop.workflow_adapter import PhotoshopWorkflowAdapter
from starbridge_mcp.core.app_data import resolve_app_data_paths
from starbridge_mcp.storage import AssetStore, EvidenceStore, JobStore, ProjectStore
from starbridge_mcp.workflows.engine import WorkflowEngine
from starbridge_mcp.workflows.photoshop_production_pipeline import (
    WORKFLOW_ID,
    create_photoshop_production_plan,
    register_photoshop_production_workflow,
)
from starbridge_mcp.workflows.registry import WorkflowRegistry


class FakePhotoshopProxy:
    def __init__(
        self,
        *,
        connected: bool = True,
        native_reopen: bool = True,
        disconnect_on_call: int | None = None,
        fail_on_call: int | None = None,
        active_document: bool = True,
        corrupt_output_format: str | None = None,
        drop_png_transparency: bool = False,
    ) -> None:
        self.connected = connected
        self.native_reopen = native_reopen
        self.production_calls = 0
        self.disconnect_on_call = disconnect_on_call
        self.fail_on_call = fail_on_call
        self.active_document = active_document
        self.corrupt_output_format = corrupt_output_format
        self.drop_png_transparency = drop_png_transparency

    def status(self) -> dict[str, object]:
        return {
            "ok": True,
            "node_proxy_running": True,
            "uxp_client_connected": self.connected,
            "photoshop_host_seen": self.connected,
            "photoshop_host": {"app": "Photoshop", "version": "27.0"},
        }

    def rpc(self, method: str, params: dict[str, object], **_kwargs: object) -> dict[str, object]:
        if method == "ps.document.info":
            if not self.active_document:
                return {
                    "jsonrpc": "2.0",
                    "id": "test",
                    "result": {
                        "ok": True,
                        "active_document": False,
                        "photoshop_host": {"app": "Photoshop", "version": "27.0"},
                    },
                }
            return {
                "jsonrpc": "2.0",
                "id": "test",
                "result": {
                    "ok": True,
                    "document": {
                        "title": "Private Client Campaign.psd",
                        "width": 1600,
                        "height": 900,
                        "resolution": 300,
                        "layer_count": 12,
                    },
                    "photoshop_host": {"app": "Photoshop", "version": "27.0"},
                },
            }
        if method == "ps.production.execute_confirmed":
            self.production_calls += 1
            if self.production_calls == self.disconnect_on_call:
                return {
                    "jsonrpc": "2.0",
                    "id": "test",
                    "error": {"code": -32001, "message": "uxp_client_not_connected"},
                }
            if self.production_calls == self.fail_on_call:
                return {
                    "jsonrpc": "2.0",
                    "id": "test",
                    "result": {
                        "ok": False,
                        "success": False,
                        "executed": True,
                        "errors": [{"code": "invalid_test_item"}],
                    },
                }
            with Image.open(Path(str(params["source_path"]))) as source_image:
                source_size = source_image.size
                source_has_alpha = "A" in source_image.getbands()
            canvas = dict(params["canvas"])
            output_size = (
                (int(canvas["width"]), int(canvas["height"]))
                if canvas.get("resize") is True
                else source_size
            )
            for output_format, output_path in dict(params["outputs"]).items():
                target = Path(str(output_path))
                if output_format == self.corrupt_output_format:
                    target.write_bytes(b"not-a-real-adobe-output" * 8)
                elif output_format == "psd":
                    target.write_bytes(b"8BPS" + b"\0" * 96)
                elif output_format == "jpeg":
                    Image.new("RGB", output_size, (30, 90, 150)).save(target, "JPEG")
                elif output_format == "subject":
                    Image.new("RGBA", output_size, (30, 90, 150, 0)).save(target, "PNG")
                else:
                    mode = "RGBA" if source_has_alpha and not self.drop_png_transparency else "RGB"
                    color = (30, 90, 150, 128) if mode == "RGBA" else (30, 90, 150)
                    Image.new(mode, output_size, color).save(target, "PNG")
            return {
                "jsonrpc": "2.0",
                "id": "test",
                "result": {
                    "ok": True,
                    "success": True,
                    "executed": True,
                    "sandbox_copy": True,
                    "source_overwritten": False,
                    "native_reopen_validated": self.native_reopen and "psd" in params["outputs"],
                    "native_reopen_width": output_size[0]
                    if self.native_reopen and "psd" in params["outputs"]
                    else 0,
                    "native_reopen_height": output_size[1]
                    if self.native_reopen and "psd" in params["outputs"]
                    else 0,
                    "native_reopen_layer_count": 1
                    if self.native_reopen and "psd" in params["outputs"]
                    else 0,
                    "rollback_supported": True,
                    "warnings": [],
                },
            }
        raise AssertionError(f"unexpected method: {method}")


class PhotoshopProductionPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.paths = resolve_app_data_paths(Path(self.temporary.name) / "app-data")
        self.projects = ProjectStore(self.paths.projects)
        self.jobs = JobStore(self.paths.jobs)
        self.evidence = EvidenceStore(self.paths.evidence)
        self.project = self.projects.create("Photoshop 安全副本", WORKFLOW_ID)
        external = Path(self.temporary.name) / "input.png"
        Image.new("RGBA", (320, 180), (30, 90, 150, 128)).save(external, "PNG")
        asset = AssetStore(self.paths.projects).import_source(
            self.project.project_id, external, confirm_import=True
        )
        self.project = self.projects.save(replace(self.project, source_assets=(asset,)))
        self.asset = asset

    def _add_asset(self, name: str, content: bytes):
        external = Path(self.temporary.name) / name
        color_seed = sum(content) % 200
        if external.suffix.lower() in {".jpg", ".jpeg"}:
            Image.new("RGB", (320, 180), (color_seed, 80, 140)).save(external, "JPEG")
        else:
            Image.new("RGBA", (320, 180), (color_seed, 80, 140, 160)).save(external, "PNG")
        asset = AssetStore(self.paths.projects).import_source(
            self.project.project_id, external, confirm_import=True
        )
        self.project = self.projects.save(
            replace(self.project, source_assets=(*self.project.source_assets, asset))
        )
        return asset

    def _engine(self, proxy: FakePhotoshopProxy) -> WorkflowEngine:
        registry = WorkflowRegistry()
        register_photoshop_production_workflow(
            registry,
            adapter=PhotoshopWorkflowAdapter(
                status_reader=proxy.status,
                rpc_runner=proxy.rpc,
            ),
        )
        return WorkflowEngine(
            registry=registry,
            project_store=self.projects,
            job_store=self.jobs,
            evidence_store=self.evidence,
            app_paths=self.paths,
        )

    def _create_job(self, engine: WorkflowEngine):
        return engine.create_job(
            self.project.project_id,
            WORKFLOW_ID,
            {
                "sourceAssetRelativePath": self.asset.relative_path,
                "sourceAssetSha256": self.asset.sha256,
                "outputFormats": ["png", "jpeg", "psd"],
                "resizeCanvas": True,
                "canvasWidth": 1920,
                "canvasHeight": 1080,
                "brightness": 8,
                "contrast": 4,
                "saturation": 6,
                "exportSubject": False,
            },
        )

    def _create_batch_job(self, engine: WorkflowEngine):
        second = self._add_asset("second.jpg", b"second-managed-image")
        third = self._add_asset("third.png", b"third-managed-image")
        return engine.create_job(
            self.project.project_id,
            WORKFLOW_ID,
            {
                "sourceAssets": [
                    {"relativePath": asset.relative_path, "sha256": asset.sha256}
                    for asset in (self.asset, second, third)
                ],
                "outputFormats": ["png", "jpeg", "psd"],
            },
        )

    def test_plan_is_fixed_copy_first_and_contains_no_absolute_path(self) -> None:
        plan = create_photoshop_production_plan(
            {
                "sourceAssetRelativePath": self.asset.relative_path,
                "sourceAssetSha256": self.asset.sha256,
                "outputFormats": ["png", "psd"],
            }
        )
        execute_step = next(step for step in plan.steps if step.step_id == "execute-production")
        self.assertTrue(execute_step.requires_confirmation)
        self.assertTrue(execute_step.rollback_policy["enabled"])
        self.assertIn("duplicate-before-write", execute_step.validation)
        serialized = json.dumps(plan.to_dict(), ensure_ascii=False)
        self.assertNotIn(str(self.paths.root), serialized)
        self.assertNotIn("descriptor", serialized.lower())

    def test_unconnected_proxy_pauses_and_resumes_without_write_approval(self) -> None:
        proxy = FakePhotoshopProxy(connected=False)
        engine = self._engine(proxy)
        job = self._create_job(engine)

        paused = engine.run(job.job_id)
        self.assertEqual("needs_user", paused.job.status)
        self.assertEqual("probe-photoshop", paused.job.current_step)
        self.assertIsNone(paused.approval)
        self.assertEqual(0, proxy.production_calls)

        proxy.connected = True
        resumed = engine.run(job.job_id)
        self.assertEqual("needs_user", resumed.job.status)
        self.assertEqual("execute-production", resumed.job.current_step)
        self.assertIsNotNone(resumed.approval)
        self.assertEqual(0, proxy.production_calls)

    def test_full_simulated_workflow_writes_real_hashed_artifacts_and_redacted_evidence(
        self,
    ) -> None:
        proxy = FakePhotoshopProxy()
        engine = self._engine(proxy)
        job = self._create_job(engine)

        first = engine.run(job.job_id)
        self.assertEqual("execute-production", first.job.current_step)
        self.assertIsNotNone(first.approval)
        second = engine.run(
            job.job_id,
            approval_ref=first.approval.approval_ref,
            confirm_execute=True,
        )
        self.assertEqual("needs_user", second.job.status)
        self.assertEqual("review-result", second.job.current_step)
        self.assertEqual(1, proxy.production_calls)
        self.assertEqual(3, len(second.job.artifacts))
        self.assertEqual(
            {"photoshop-preview.png", "photoshop-preview.jpg", "photoshop-copy.psd"},
            {artifact.basename for artifact in second.job.artifacts},
        )
        for artifact in second.job.artifacts:
            self.assertEqual(64, len(artifact.sha256))

        final = engine.run(job.job_id)
        self.assertIsNotNone(final.approval)
        completed = engine.run(
            job.job_id,
            approval_ref=final.approval.approval_ref,
            confirm_execute=True,
        )
        self.assertEqual("completed", completed.job.status)

        runtime_text = (self.paths.jobs / job.job_id / "photoshop-runtime.json").read_text(
            encoding="utf-8"
        )
        evidence_text = self.evidence.manifest_file(str(completed.job.evidence_id)).read_text(
            encoding="utf-8"
        )
        for text in (runtime_text, evidence_text):
            self.assertNotIn("Private Client Campaign", text)
            self.assertNotIn(str(self.paths.root), text)
            self.assertNotIn(str(Path(self.temporary.name)), text)
        self.assertIn('"sourcePathPersisted": false', runtime_text)
        self.assertIn('"nativeReopenValidated": true', runtime_text)

    def test_managed_source_batch_does_not_require_a_preopened_document(self) -> None:
        proxy = FakePhotoshopProxy(active_document=False)
        engine = self._engine(proxy)
        job = self._create_job(engine)

        first = engine.run(job.job_id)
        self.assertEqual("execute-production", first.job.current_step)
        self.assertIsNotNone(first.approval)
        executed = engine.run(
            job.job_id,
            approval_ref=first.approval.approval_ref,
            confirm_execute=True,
        )

        self.assertEqual("review-result", executed.job.current_step)
        self.assertEqual(1, proxy.production_calls)

    def test_invalid_format_is_rejected_before_job_creation(self) -> None:
        with self.assertRaises(ValueError):
            create_photoshop_production_plan(
                {
                    "sourceAssetRelativePath": self.asset.relative_path,
                    "sourceAssetSha256": self.asset.sha256,
                    "outputFormats": ["tiff"],
                }
            )

    def test_psd_delivery_fails_closed_without_native_reopen(self) -> None:
        proxy = FakePhotoshopProxy(native_reopen=False)
        engine = self._engine(proxy)
        job = self._create_job(engine)
        approval = engine.run(job.job_id).approval
        self.assertIsNotNone(approval)

        result = engine.run(
            job.job_id,
            approval_ref=approval.approval_ref,
            confirm_execute=True,
        )

        self.assertEqual("failed", result.job.status)
        self.assertEqual("photoshop_output_validation_failed", result.job.error.code)

    def test_corrupt_raster_output_is_not_registered_or_published(self) -> None:
        proxy = FakePhotoshopProxy(corrupt_output_format="png")
        engine = self._engine(proxy)
        job = engine.create_job(
            self.project.project_id,
            WORKFLOW_ID,
            {
                "sourceAssetRelativePath": self.asset.relative_path,
                "sourceAssetSha256": self.asset.sha256,
                "outputFormats": ["png"],
            },
        )
        approval = engine.run(job.job_id).approval
        self.assertIsNotNone(approval)

        result = engine.run(
            job.job_id,
            approval_ref=approval.approval_ref,
            confirm_execute=True,
        )

        self.assertEqual("failed", result.job.status)
        self.assertEqual("photoshop_output_validation_failed", result.job.error.code)
        self.assertEqual((), result.job.artifacts)

    def test_transparent_png_recipe_fails_when_output_loses_alpha(self) -> None:
        proxy = FakePhotoshopProxy(drop_png_transparency=True)
        engine = self._engine(proxy)
        job = engine.create_job(
            self.project.project_id,
            WORKFLOW_ID,
            {
                "sourceAssetRelativePath": self.asset.relative_path,
                "sourceAssetSha256": self.asset.sha256,
                "outputFormats": ["png"],
            },
        )
        approval = engine.run(job.job_id).approval
        self.assertIsNotNone(approval)

        result = engine.run(
            job.job_id,
            approval_ref=approval.approval_ref,
            confirm_execute=True,
        )

        self.assertEqual("failed", result.job.status)
        self.assertEqual("photoshop_output_validation_failed", result.job.error.code)
        self.assertEqual((), result.job.artifacts)

    def test_subject_export_registers_only_fixed_subject_and_requested_outputs(self) -> None:
        proxy = FakePhotoshopProxy()
        engine = self._engine(proxy)
        job = engine.create_job(
            self.project.project_id,
            WORKFLOW_ID,
            {
                "sourceAssetRelativePath": self.asset.relative_path,
                "sourceAssetSha256": self.asset.sha256,
                "outputFormats": ["png"],
                "exportSubject": True,
            },
        )

        approval = engine.run(job.job_id).approval
        self.assertIsNotNone(approval)
        executed = engine.run(
            job.job_id,
            approval_ref=approval.approval_ref,
            confirm_execute=True,
        )

        self.assertEqual("review-result", executed.job.current_step)
        self.assertEqual(
            {"photoshop-preview.png", "photoshop-subject.png"},
            {artifact.basename for artifact in executed.job.artifacts},
        )

    def test_batch_runs_in_fifo_and_keeps_a_failed_item_isolated(self) -> None:
        proxy = FakePhotoshopProxy(fail_on_call=2)
        engine = self._engine(proxy)
        job = self._create_batch_job(engine)

        approval = engine.run(job.job_id).approval
        self.assertIsNotNone(approval)
        executed = engine.run(
            job.job_id,
            approval_ref=approval.approval_ref,
            confirm_execute=True,
        )

        self.assertEqual("needs_user", executed.job.status)
        self.assertEqual("review-result", executed.job.current_step)
        self.assertEqual(3, proxy.production_calls)
        self.assertEqual(6, len(executed.job.artifacts))
        self.assertTrue(
            all(artifact.basename.startswith("item-") for artifact in executed.job.artifacts)
        )
        runtime = json.loads(
            (self.paths.jobs / job.job_id / "photoshop-batch-runtime.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            ["completed", "failed", "completed"],
            [item["status"] for item in runtime["items"]],
        )
        self.assertFalse(runtime["sourcePathsPersisted"])

    def test_batch_disconnect_resumes_from_the_next_safe_item_checkpoint(self) -> None:
        proxy = FakePhotoshopProxy(disconnect_on_call=2)
        engine = self._engine(proxy)
        job = self._create_batch_job(engine)

        approval = engine.run(job.job_id).approval
        self.assertIsNotNone(approval)
        interrupted = engine.run(
            job.job_id,
            approval_ref=approval.approval_ref,
            confirm_execute=True,
        )
        self.assertEqual("needs_user", interrupted.job.status)
        self.assertEqual("execute-batch", interrupted.job.current_step)
        self.assertEqual(2, proxy.production_calls)

        resumed_approval = engine.run(job.job_id).approval
        self.assertIsNotNone(resumed_approval)
        resumed = engine.run(
            job.job_id,
            approval_ref=resumed_approval.approval_ref,
            confirm_execute=True,
        )
        self.assertEqual("review-result", resumed.job.current_step)
        self.assertEqual(4, proxy.production_calls)
        self.assertEqual(9, len(resumed.job.artifacts))

        runtime = json.loads(
            (self.paths.jobs / job.job_id / "photoshop-batch-runtime.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual([1, 2, 1], [item["attempts"] for item in runtime["items"]])
        self.assertTrue(all(item["status"] == "completed" for item in runtime["items"]))

    def test_batch_resume_rejects_tampered_request_state_before_writing(self) -> None:
        proxy = FakePhotoshopProxy(disconnect_on_call=1)
        engine = self._engine(proxy)
        job = self._create_batch_job(engine)

        approval = engine.run(job.job_id).approval
        self.assertIsNotNone(approval)
        interrupted = engine.run(
            job.job_id,
            approval_ref=approval.approval_ref,
            confirm_execute=True,
        )
        self.assertEqual("needs_user", interrupted.job.status)
        self.assertEqual(1, proxy.production_calls)

        runtime_path = self.paths.jobs / job.job_id / "photoshop-batch-runtime.json"
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        runtime["requestFingerprint"] = "0" * 64
        runtime_path.write_text(json.dumps(runtime), encoding="utf-8")

        resumed_approval = engine.run(job.job_id).approval
        self.assertIsNotNone(resumed_approval)
        rejected = engine.run(
            job.job_id,
            approval_ref=resumed_approval.approval_ref,
            confirm_execute=True,
        )

        self.assertEqual("failed", rejected.job.status)
        self.assertEqual("photoshop_batch_state_mismatch", rejected.job.error.code)
        self.assertEqual(1, proxy.production_calls)

    def test_same_batch_plan_reuses_the_existing_job_id(self) -> None:
        proxy = FakePhotoshopProxy()
        engine = self._engine(proxy)
        job = self._create_batch_job(engine)
        plan = self.jobs.get_plan(job.job_id)
        execute = next(step for step in plan.steps if step.step_id == "execute-batch")
        same = engine.create_job(
            self.project.project_id,
            WORKFLOW_ID,
            {
                "sourceAssets": [
                    {
                        "relativePath": item["sourceAssetRelativePath"],
                        "sha256": item["sourceAssetSha256"],
                    }
                    for item in execute.input_data["items"]
                ],
                "outputFormats": ["png", "jpeg", "psd"],
            },
        )
        self.assertEqual(job.job_id, same.job_id)
        self.assertEqual(job.idempotency_key, same.idempotency_key)


if __name__ == "__main__":
    unittest.main()
