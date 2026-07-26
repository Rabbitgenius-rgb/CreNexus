from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

REQUIRED_CI_COMMANDS = [
    "python -m compileall .",
    "python -m unittest discover -s tests",
    "python scripts/security_check.py",
    "python scripts/collect_bridge_status.py --json",
    "python examples/bridge_status.py --json --redact-paths --soft-exit",
    "python -m starbridge_mcp.server tools --json --safe-only",
    "python -m starbridge_mcp.server evidence --init --json",
    "python -m starbridge_mcp.server evidence --validate --json",
    "python -m starbridge_mcp.server job-status --json",
]


def workflow_job(workflow: str, job_name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(job_name)}:\n.*?(?=^  [a-z0-9][a-z0-9-]*:\n|\Z)",
        workflow,
    )
    if match is None:
        raise AssertionError(f"CI job is missing: {job_name}")
    return match.group(0)


class CiCommandsTest(unittest.TestCase):
    def test_normal_ci_covers_desktop_frontend_and_tauri_shell(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        for command in (
            "npm ci --prefix apps/starbridge-desktop",
            "npm test --prefix apps/starbridge-desktop",
            "npm run build --prefix apps/starbridge-desktop",
            "cargo fmt --manifest-path apps/starbridge-desktop/src-tauri/Cargo.toml",
            "cargo test --manifest-path apps/starbridge-desktop/src-tauri/Cargo.toml",
        ):
            with self.subTest(command=command):
                self.assertIn(command, workflow)

    def test_macos_arm64_producer_builds_and_archives_real_sidecar(self) -> None:
        workflow = CI_WORKFLOW.read_text(encoding="utf-8")
        producer = workflow_job(workflow, "test-macos-python-sidecar")

        self.assertIn("permissions:\n  contents: read", workflow)
        for contract in (
            "name: macOS 15 arm64 Python and real sidecar producer",
            "runs-on: macos-15",
            'python-version: "3.12"',
            "python -m venv .venv",
            '.venv/bin/python -m pip install -e ".[dev]"',
            ".venv/bin/python -m compileall -q starbridge_mcp scripts examples tests",
            ".venv/bin/python -m unittest discover -s tests",
            ".venv/bin/python scripts/security_check.py",
            ".venv/bin/python scripts/check_product_facts.py",
            ".venv/bin/python scripts/check_text_encoding.py",
            ".venv/bin/python scripts/starbridge_preflight.py --markdown",
            "./apps/starbridge-desktop/scripts/Build-Sidecar.sh",
            '--target-triple "$target"',
            "--verify-staged",
            'test "$(uname -m)" = "arm64"',
            "/usr/bin/file",
            "/usr/bin/lipo -archs",
            "/usr/bin/otool -L",
            "--vector60-runtime-check",
            "cv2/.dylibs",
            "PIL/.dylibs",
            ".venv/bin/python -I apps/starbridge-desktop/scripts/sidecar_artifact.py pack",
            '--manifest "$archive.manifest.json"',
            "actions/upload-artifact@v7",
            "retention-days: 7",
            "compression-level: 0",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, producer)

        self.assertIn(
            "apps/starbridge-desktop/src-tauri/binaries/"
            "starbridge-sidecar-aarch64-apple-darwin.tar\n",
            producer,
        )
        self.assertIn(
            "apps/starbridge-desktop/src-tauri/binaries/"
            "starbridge-sidecar-aarch64-apple-darwin.tar.sha256\n",
            producer,
        )
        self.assertIn(
            "apps/starbridge-desktop/src-tauri/binaries/"
            "starbridge-sidecar-aarch64-apple-darwin.tar.manifest.json\n",
            producer,
        )
        self.assertNotRegex(
            producer,
            r"(?m)^\s+apps/starbridge-desktop/src-tauri/binaries/"
            r"(?:starbridge-sidecar-aarch64-apple-darwin|"
            r"_internal-aarch64-apple-darwin/?)\s*$",
        )

    def test_macos_arm64_consumer_verifies_sidecar_and_real_app_bundle(self) -> None:
        workflow = CI_WORKFLOW.read_text(encoding="utf-8")
        consumer = workflow_job(workflow, "test-macos-tauri-app")

        for contract in (
            "name: macOS 15 arm64 sidecar consumer and real Tauri app",
            "needs: test-macos-python-sidecar",
            "runs-on: macos-15",
            "actions/download-artifact@v8",
            "python -m venv .venv",
            ".venv/bin/python -I apps/starbridge-desktop/scripts/sidecar_artifact.py",
            "verify-extract",
            '--digest "$archive.sha256"',
            '--manifest "$archive.manifest.json"',
            '--destination "$binaries"',
            "--verify-staged",
            "--vector60-runtime-check",
            "./apps/starbridge-desktop/scripts/Test-Sidecar.sh",
            "--skip-build",
            "actions/setup-node@v7",
            'node-version: "22"',
            "dtolnay/rust-toolchain@stable",
            "npm ci --prefix apps/starbridge-desktop",
            "cargo fmt --manifest-path apps/starbridge-desktop/src-tauri/Cargo.toml",
            "cargo test --manifest-path apps/starbridge-desktop/src-tauri/Cargo.toml",
            "working-directory: apps/starbridge-desktop",
            "npm run tauri -- build --bundles app",
            'resources="$app_bundle/Contents/Resources"',
            'cmp -s "$source_root/starbridge-sidecar-$target" "$sidecar"',
            "cv2/.dylibs",
            "PIL/.dylibs",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, consumer)

        self.assertLess(
            consumer.index("verify-extract"),
            consumer.index("--verify-staged"),
        )
        self.assertNotIn("chmod 0755", consumer)

    def test_macos_artifact_jobs_preserve_no_placeholder_contract(self) -> None:
        workflow = CI_WORKFLOW.read_text(encoding="utf-8")
        producer = workflow_job(workflow, "test-macos-python-sidecar")
        consumer = workflow_job(workflow, "test-macos-tauri-app")
        macos_jobs = producer + consumer

        self.assertEqual(1, workflow.count("actions/upload-artifact@v7"))
        self.assertEqual(1, workflow.count("actions/download-artifact@v8"))
        self.assertNotRegex(workflow, r"actions/upload-artifact@v[1-6]\b")
        self.assertNotRegex(workflow, r"actions/download-artifact@v[1-7]\b")
        for forbidden_creation in (
            "New-Item",
            "touch ",
            "Set-Content",
            "starbridge-sidecar-x86_64-pc-windows-msvc.exe",
        ):
            with self.subTest(forbidden_creation=forbidden_creation):
                self.assertNotIn(forbidden_creation, macos_jobs)
        self.assertGreaterEqual(macos_jobs.count('-name ".ci-placeholder"'), 3)
        self.assertGreaterEqual(macos_jobs.count('-name "*.exe"'), 3)

    def test_readme_mentions_release_candidate_commands(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        release_doc = (REPO_ROOT / "docs" / "RELEASE_V0_1_ALPHA.md").read_text(encoding="utf-8")

        for command in REQUIRED_CI_COMMANDS:
            with self.subTest(command=command):
                self.assertIn(command, release_doc)
        for command in REQUIRED_CI_COMMANDS[2:]:
            with self.subTest(readme_command=command):
                self.assertIn(command, readme)

    def test_package_scripts_cover_public_shortcuts(self) -> None:
        package = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
        scripts = package["scripts"]

        expected = {
            "test": "python -m unittest discover -s tests",
            "security:check": "python scripts/security_check.py",
            "product:facts:check": "python scripts/check_product_facts.py",
            "bridge:status:json": "python scripts/collect_bridge_status.py --json",
            "bridge:status:safe": "python examples/bridge_status.py --json --redact-paths --soft-exit",
            "starbridge:tools:safe": "python -m starbridge_mcp.server tools --json --safe-only",
            "starbridge:evidence:init": "python -m starbridge_mcp.server evidence --init --json",
            "starbridge:evidence:validate": "python -m starbridge_mcp.server evidence --validate --json",
            "starbridge:job-status": "python -m starbridge_mcp.server job-status --json",
            "comfy:workflow:validate": "python examples/comfy_bridge/validate_workflow.py --json",
            "comfy:templates:list": "python examples/comfy_bridge/workflow_templates.py list --json",
            "comfy:templates:get": "python examples/comfy_bridge/workflow_templates.py get --template-id txt2img_basic_v1 --json",
            "comfy:templates:from": "python examples/comfy_bridge/workflow_templates.py from-template --template-id txt2img_basic_v1 --json",
            "comfy:lifecycle:template": "python examples/comfy_bridge/workflow_lifecycle.py --template-id txt2img_basic_v1 --json",
            "cad:dxf:dry-run": "python examples/cad/generate_dxf_plan.py",
            "photoshop:layers": "python -m starbridge_mcp.adapters.photoshop.semantic_layers.cli",
            "desktop:install": "npm ci --prefix apps/starbridge-desktop",
        }
        for name, command in expected.items():
            with self.subTest(script=name):
                self.assertEqual(command, scripts.get(name))

    def test_ci_safe_commands_run_without_local_apps(self) -> None:
        commands = [
            [sys.executable, "scripts/security_check.py"],
            [sys.executable, "scripts/collect_bridge_status.py", "--json"],
            [
                sys.executable,
                "examples/bridge_status.py",
                "--json",
                "--redact-paths",
                "--soft-exit",
            ],
            [sys.executable, "-m", "starbridge_mcp.server", "tools", "--json", "--safe-only"],
            [sys.executable, "-m", "starbridge_mcp.server", "evidence", "--init", "--json"],
            [sys.executable, "-m", "starbridge_mcp.server", "evidence", "--validate", "--json"],
            [sys.executable, "-m", "starbridge_mcp.server", "job-status", "--json"],
            [sys.executable, "examples/comfy_bridge/workflow_templates.py", "list", "--json"],
            [
                sys.executable,
                "examples/comfy_bridge/workflow_templates.py",
                "from-template",
                "--template-id",
                "txt2img_basic_v1",
                "--json",
            ],
            [
                sys.executable,
                "examples/comfy_bridge/workflow_lifecycle.py",
                "--template-id",
                "txt2img_basic_v1",
                "--json",
            ],
        ]
        for command in commands:
            with self.subTest(command=" ".join(command)):
                completed = subprocess.run(
                    command, cwd=REPO_ROOT, capture_output=True, text=True, check=False, timeout=30
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                if "--json" in command:
                    json.loads(completed.stdout)

    def test_ci_has_required_image_to_psd_runtime_job(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("test-image-to-psd-headless:", workflow)
        self.assertIn('STARBRIDGE_REQUIRE_IMAGE_TO_PSD_RUNTIME: "1"', workflow)
        self.assertIn('pip install -e ".[dev,image-to-psd]"', workflow)
        for test_module in (
            "tests.test_photoshop_semantic_layers",
            "tests.test_photoshop_github_feedback",
            "tests.test_photoshop_public_dataset",
            "tests.test_photoshop_public_experiment",
            "tests.test_photoshop_training",
        ):
            with self.subTest(test_module=test_module):
                self.assertIn(test_module, workflow)
        self.assertIn("cli regression", workflow)
        self.assertIn("ci-synthetic-regression", workflow)


if __name__ == "__main__":
    unittest.main()
