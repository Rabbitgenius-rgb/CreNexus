from __future__ import annotations

import copy
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "apps" / "starbridge-desktop" / "scripts"
if os.fspath(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, os.fspath(SCRIPTS_DIR))

import sidecar_builder  # noqa: E402
import sidecar_tester  # noqa: E402


class DarwinSidecarPackagingTest(unittest.TestCase):
    def make_layout_fixture(self, root: Path) -> sidecar_builder.BuildLayout:
        scripts = root / "apps" / "starbridge-desktop" / "scripts"
        binaries = root / "apps" / "starbridge-desktop" / "src-tauri" / "binaries"
        scripts.mkdir(parents=True)
        binaries.mkdir(parents=True)
        (root / "pyproject.toml").write_text("[project]\nname='fixture'\n")
        for name in (
            "requirements-sidecar-build.txt",
            "sidecar_entry.py",
            "sidecar_builder.py",
            "sidecar_tester.py",
            "starbridge-sidecar.spec",
        ):
            (scripts / name).write_text("# fixture\n", encoding="utf-8")
        return sidecar_builder.layout_for(root, "aarch64-apple-darwin")

    def make_exact_fixture(
        self,
        root: Path,
        *,
        svg_text: str = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1">'
            '<rect width="1" height="1" fill="#ffffff"/></svg>'
        ),
        report: dict[str, object] | None = None,
    ) -> tuple[Path, Path, Path, Path]:
        data_root = root / "app data"
        source = data_root / "private source.png"
        output = data_root / "data" / "vectorization" / "fixture" / "exact"
        output.mkdir(parents=True)
        source.write_bytes(b"png")
        (output / "vector.svg").write_text(svg_text, encoding="utf-8")
        payload = report or {
            "validation": {
                "svg_verified": True,
                "image_trace_used": False,
                "embedded_raster_count": 0,
                "external_reference_count": 0,
            },
            "exact_validation": {
                "pixel_match": True,
                "different_pixel_count": 0,
                "maximum_channel_difference": 0,
            },
        }
        (output / "vector_report.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
        return data_root / "data" / "vectorization", data_root, source, output

    def test_supported_target_triples_are_explicit_and_path_safe(self) -> None:
        for target in ("aarch64-apple-darwin", "x86_64-apple-darwin"):
            with self.subTest(target=target):
                self.assertEqual(target, sidecar_builder.validate_target_triple(target))

        for invalid in (
            "",
            "../../aarch64-apple-darwin",
            "aarch64 apple darwin",
            "arm64-apple-darwin",
            "x86_64-unknown-linux-gnu",
        ):
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(sidecar_builder.SidecarBuildError),
            ):
                sidecar_builder.validate_target_triple(invalid)

    def test_cargo_target_override_uses_the_same_validation(self) -> None:
        self.assertEqual(
            "x86_64-apple-darwin",
            sidecar_builder.resolve_target_triple(
                None,
                {"CARGO_BUILD_TARGET": "x86_64-apple-darwin"},
            ),
        )
        with self.assertRaises(sidecar_builder.SidecarBuildError):
            sidecar_builder.resolve_target_triple(
                None,
                {"CARGO_BUILD_TARGET": "../private-target"},
            )

    def test_target_layouts_isolate_environment_build_and_support_files(self) -> None:
        arm = sidecar_builder.layout_for(REPO_ROOT, "aarch64-apple-darwin")
        intel = sidecar_builder.layout_for(REPO_ROOT, "x86_64-apple-darwin")

        for layout in (arm, intel):
            target = layout.target_triple
            self.assertEqual(
                REPO_ROOT / ".venv-build" / "sidecar" / target,
                layout.build_environment,
            )
            self.assertEqual(
                REPO_ROOT / "apps" / "starbridge-desktop" / "build" / "sidecar" / target,
                layout.build_root,
            )
            self.assertEqual(
                layout.build_root / "pyinstaller-config",
                layout.pyinstaller_config_root,
            )
            self.assertEqual(
                f"starbridge-sidecar-{target}",
                layout.staged_executable.name,
            )
            self.assertEqual(
                f"_internal-{target}",
                layout.staged_support_directory.name,
            )

        self.assertNotEqual(arm.build_environment, intel.build_environment)
        self.assertNotEqual(arm.build_root, intel.build_root)
        self.assertNotEqual(arm.staged_executable, intel.staged_executable)
        self.assertNotEqual(
            arm.staged_support_directory,
            intel.staged_support_directory,
        )

    def test_plan_remains_read_only_and_testable_on_non_darwin_hosts(self) -> None:
        with mock.patch.object(
            sidecar_builder,
            "detect_host_target",
            side_effect=sidecar_builder.SidecarBuildError("non-Darwin host"),
        ):
            plan = sidecar_builder.build_plan(
                REPO_ROOT,
                "x86_64-apple-darwin",
            )

        self.assertIsNone(plan["host_triple"])
        self.assertFalse(plan["buildable_on_current_host"])
        self.assertEqual("x86_64-apple-darwin", plan["target_triple"])
        self.assertEqual(
            "src-tauri/binaries/starbridge-sidecar-x86_64-apple-darwin",
            plan["executable"],
        )

    def test_pyinstaller_contents_directory_is_target_isolated_on_darwin(self) -> None:
        spec = (SCRIPTS_DIR / "starbridge-sidecar.spec").read_text(encoding="utf-8")
        builder = (SCRIPTS_DIR / "sidecar_builder.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("STARBRIDGE_SIDECAR_TARGET_TRIPLE", "")', spec)
        self.assertIn('CONTENTS_DIRECTORY = f"_internal-{TARGET_TRIPLE}"', spec)
        self.assertIn("contents_directory=CONTENTS_DIRECTORY", spec)
        self.assertIn('CONTENTS_DIRECTORY = "_internal"', spec)
        self.assertIn('"--clean"', builder)
        self.assertIn('"PYINSTALLER_CONFIG_DIR"', builder)

    @unittest.skipIf(os.name == "nt", "POSIX shell checks do not run on Windows")
    def test_posix_wrappers_are_executable_and_parse_as_sh(self) -> None:
        for name in ("Build-Sidecar.sh", "Test-Sidecar.sh"):
            path = SCRIPTS_DIR / name
            with self.subTest(path=path):
                self.assertTrue(path.stat().st_mode & stat.S_IXUSR)
                subprocess.run(["sh", "-n", path], check=True)
                text = path.read_text(encoding="utf-8")
                self.assertIn('exec "$RUNNER" -I', text)
                self.assertIn("PYTHON[A-Za-z0-9_]*", text)
                self.assertIn("unset __PYVENV_LAUNCHER__", text)

    def test_python_and_pip_environments_remove_injection_and_install_redirects(
        self,
    ) -> None:
        inherited = {
            "PATH": "/trusted/bin",
            "PYTHONPATH": "/attacker",
            "PYTHONHOME": "/outside",
            "PYTHONUSERBASE": "/outside-user",
            "__PYVENV_LAUNCHER__": "/outside-launcher",
            "PYINSTALLER_CONFIG_DIR": "/outside-pyinstaller-cache",
            "PIP_TARGET": "/outside-target",
            "PIP_PREFIX": "/outside-prefix",
            "PIP_ROOT": "/outside-root",
            "PIP_USER": "1",
            "PIP_CONFIG_FILE": "/outside/pip.conf",
            "PIP_PROXY": "http://proxy.invalid:8080",
            "PIP_CERT": "/certs/pip.pem",
            "HTTPS_PROXY": "http://proxy.invalid:8443",
        }

        python_environment = sidecar_builder.sanitized_environment(inherited)
        self.assertEqual("/trusted/bin", python_environment["PATH"])
        self.assertEqual("http://proxy.invalid:8443", python_environment["HTTPS_PROXY"])
        self.assertFalse(
            any(
                name.upper().startswith(("PYTHON", "PIP_")) or name.upper() == "__PYVENV_LAUNCHER__"
                for name in python_environment
            )
        )
        self.assertNotIn("PYINSTALLER_CONFIG_DIR", python_environment)

        pip_environment = sidecar_builder.sanitized_environment(
            inherited,
            for_pip=True,
        )
        for dangerous in (
            "PYTHONPATH",
            "PYTHONHOME",
            "PYTHONUSERBASE",
            "__PYVENV_LAUNCHER__",
            "PYINSTALLER_CONFIG_DIR",
            "PIP_TARGET",
            "PIP_PREFIX",
            "PIP_ROOT",
            "PIP_USER",
        ):
            self.assertNotIn(dangerous, pip_environment)
        self.assertEqual(os.devnull, pip_environment["PIP_CONFIG_FILE"])
        self.assertEqual("1", pip_environment["PIP_NO_INPUT"])
        self.assertEqual("1", pip_environment["PIP_DISABLE_PIP_VERSION_CHECK"])
        self.assertEqual("http://proxy.invalid:8080", pip_environment["PIP_PROXY"])
        self.assertEqual("/certs/pip.pem", pip_environment["PIP_CERT"])

    @unittest.skipIf(os.name == "nt", "POSIX wrapper checks do not run on Windows")
    def test_wrapper_python_isolation_blocks_startup_injection(self) -> None:
        with tempfile.TemporaryDirectory(prefix="KORYAO sidecar python injection ") as temporary:
            root = Path(temporary)
            attacker = root / "attacker"
            attacker.mkdir()
            trace = root / "sitecustomize.trace"
            (attacker / "sitecustomize.py").write_text(
                f"from pathlib import Path\nPath({os.fspath(trace)!r}).write_text('executed')\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "PYTHONPATH": os.fspath(attacker),
                    "PYTHONHOME": os.fspath(root / "invalid-home"),
                    "PYTHONUSERBASE": os.fspath(root / "outside-user"),
                    "__PYVENV_LAUNCHER__": os.fspath(root / "outside-launcher"),
                }
            )

            completed = subprocess.run(
                [
                    "sh",
                    SCRIPTS_DIR / "Build-Sidecar.sh",
                    "--print-plan",
                    "--target-triple",
                    "aarch64-apple-darwin",
                ],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertFalse(trace.exists())

    @unittest.skipIf(os.name == "nt", "symlink checks require POSIX")
    def test_write_roots_and_input_files_reject_symlink_replacement(self) -> None:
        cases = (
            ".venv-build",
            "apps/starbridge-desktop/build",
            "apps/starbridge-desktop/src-tauri/binaries",
            "apps/starbridge-desktop/scripts/starbridge-sidecar.spec",
        )
        for relative in cases:
            with (
                self.subTest(relative=relative),
                tempfile.TemporaryDirectory(prefix="KORYAO sidecar path guard ") as temporary,
            ):
                temporary_root = Path(temporary)
                repo_root = temporary_root / "repo"
                layout = self.make_layout_fixture(repo_root)
                target = repo_root / relative
                if target.is_dir():
                    target.rmdir()
                elif target.exists():
                    target.unlink()
                outside = temporary_root / "outside"
                if relative.endswith(".spec"):
                    outside.write_text("# outside\n", encoding="utf-8")
                else:
                    outside.mkdir()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(outside, target_is_directory=outside.is_dir())

                with self.assertRaises(sidecar_builder.SidecarBuildError):
                    sidecar_builder._validate_layout_roots(layout)

    @unittest.skipIf(os.name == "nt", "symlink checks require POSIX")
    def test_symlinked_repository_root_is_rejected_without_resolving_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="KORYAO sidecar repo guard ") as temporary:
            root = Path(temporary)
            physical = root / "physical"
            self.make_layout_fixture(physical)
            alias = root / "repo-link"
            alias.symlink_to(physical, target_is_directory=True)
            layout = sidecar_builder.layout_for(alias, "aarch64-apple-darwin")

            with self.assertRaises(sidecar_builder.SidecarBuildError):
                sidecar_builder._validate_layout_roots(layout)

    def test_support_tree_requires_native_regular_non_pe_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="KORYAO sidecar support ") as temporary:
            support = Path(temporary) / "_internal-aarch64-apple-darwin"
            support.mkdir()
            native = support / "runtime.cpython-313-darwin.so"
            native.write_bytes(b"\xcf\xfa\xed\xfe")

            summary = sidecar_builder._scan_support_tree(support)
            self.assertEqual(1, summary["support_file_count"])
            self.assertEqual(1, summary["native_extension_count"])

            (support / "foreign.DLL").write_bytes(b"MZpayload")
            with self.assertRaises(sidecar_builder.SidecarBuildError):
                sidecar_builder._scan_support_tree(support)
            (support / "foreign.DLL").unlink()
            (support / "renamed-payload.bin").write_bytes(b"MZpayload")
            with self.assertRaisesRegex(
                sidecar_builder.SidecarBuildError,
                "Windows executable",
            ):
                sidecar_builder._scan_support_tree(support)
            (support / "renamed-payload.bin").unlink()
            (support / "restored 2.bin").write_bytes(b"payload")
            with self.assertRaisesRegex(
                sidecar_builder.SidecarBuildError,
                "collision-copy",
            ):
                sidecar_builder._scan_support_tree(support)
            (support / "restored 2.bin").unlink()
            (support / "package 2").mkdir()
            with self.assertRaisesRegex(
                sidecar_builder.SidecarBuildError,
                "collision-copy",
            ):
                sidecar_builder._scan_support_tree(support)

    @unittest.skipIf(os.name == "nt", "symlink checks require POSIX")
    def test_support_symlinks_must_resolve_to_internal_regular_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="KORYAO sidecar support symlink ") as temporary:
            root = Path(temporary)
            support = root / "_internal-aarch64-apple-darwin"
            support.mkdir()
            native = support / "runtime.so"
            native.write_bytes(b"\xcf\xfa\xed\xfe")
            (support / "alias.dylib").symlink_to(native.name)

            summary = sidecar_builder._scan_support_tree(
                support,
                allow_internal_file_symlinks=True,
            )
            self.assertEqual(1, summary["support_symlink_count"])
            with self.assertRaises(sidecar_builder.SidecarBuildError):
                sidecar_builder._scan_support_tree(support)

            outside = root / "outside.dylib"
            outside.write_bytes(b"\xcf\xfa\xed\xfe")
            (support / "escaping.dylib").symlink_to(outside)
            with self.assertRaises(sidecar_builder.SidecarBuildError):
                sidecar_builder._scan_support_tree(
                    support,
                    allow_internal_file_symlinks=True,
                )

    def test_target_families_reject_extensionless_numbered_copies(self) -> None:
        cases = (
            (
                "apps/starbridge-desktop/src-tauri/binaries/"
                "starbridge-sidecar-aarch64-apple-darwin 2",
                False,
            ),
            (
                "apps/starbridge-desktop/src-tauri/binaries/_internal-aarch64-apple-darwin 2",
                True,
            ),
            (
                "apps/starbridge-desktop/build/sidecar/aarch64-apple-darwin 2",
                True,
            ),
            (".venv-build/sidecar/aarch64-apple-darwin 2", True),
        )
        for relative, is_directory in cases:
            with (
                self.subTest(relative=relative),
                tempfile.TemporaryDirectory(prefix="KORYAO sidecar numbered copy ") as temporary,
            ):
                repo_root = Path(temporary) / "repo"
                layout = self.make_layout_fixture(repo_root)
                collision = repo_root / relative
                collision.parent.mkdir(parents=True, exist_ok=True)
                if is_directory:
                    collision.mkdir()
                else:
                    collision.write_bytes(b"collision")

                with self.assertRaisesRegex(
                    sidecar_builder.SidecarBuildError,
                    "numbered collision-copy",
                ):
                    sidecar_builder._validate_layout_roots(layout)

    def test_macho_dependency_verifier_rejects_non_system_absolute_paths(
        self,
    ) -> None:
        def fake_run(
            arguments: object,
            **_: object,
        ) -> subprocess.CompletedProcess[str]:
            command = list(arguments)  # type: ignore[arg-type]
            if command[0] == "file-tool":
                stdout = "Mach-O 64-bit bundle arm64\n"
            elif command[0] == "lipo-tool":
                stdout = "arm64\n"
            else:
                stdout = (
                    "runtime.so:\n"
                    "\t/Users/private/build-venv/libunsafe.dylib "
                    "(compatibility version 1.0.0, current version 1.0.0)\n"
                )
            return subprocess.CompletedProcess(command, 0, stdout, "")

        with (
            mock.patch.object(sidecar_builder, "_run", side_effect=fake_run),
            self.assertRaisesRegex(
                sidecar_builder.SidecarBuildError,
                "non-system absolute dependency",
            ),
        ):
            sidecar_builder._verify_macho_file(
                Path("runtime.so"),
                label="test native extension",
                expected_architecture="arm64",
                file_tool="file-tool",
                lipo_tool="lipo-tool",
                otool_tool="otool-tool",
                environment={},
                require_linked_library=False,
            )

    def test_macho_dependency_verifier_rejects_traversing_install_names(
        self,
    ) -> None:
        unsafe_references = (
            "/usr/lib/../../Users/private/libunsafe.dylib",
            "/System/Library/../../../private/tmp/libunsafe.dylib",
            "@loader_path/../../private/libunsafe.dylib",
            "@executable_path/../private/libunsafe.dylib",
            "@rpath/../../private/libunsafe.dylib",
        )
        for reference in unsafe_references:
            with self.subTest(reference=reference):

                def fake_run(
                    arguments: object,
                    unsafe_reference: str = reference,
                    **_: object,
                ) -> subprocess.CompletedProcess[str]:
                    command = list(arguments)  # type: ignore[arg-type]
                    if command[0] == "file-tool":
                        stdout = "Mach-O 64-bit bundle arm64\n"
                    elif command[0] == "lipo-tool":
                        stdout = "arm64\n"
                    elif command[1] == "-L":
                        stdout = (
                            "runtime.so:\n"
                            f"\t{unsafe_reference} "
                            "(compatibility version 1.0.0, current version 1.0.0)\n"
                        )
                    else:
                        stdout = "runtime.so:\n"
                    return subprocess.CompletedProcess(command, 0, stdout, "")

                with (
                    mock.patch.object(
                        sidecar_builder,
                        "_run",
                        side_effect=fake_run,
                    ),
                    self.assertRaises(sidecar_builder.SidecarBuildError),
                ):
                    sidecar_builder._verify_macho_file(
                        Path("runtime.so"),
                        label="test native extension",
                        expected_architecture="arm64",
                        file_tool="file-tool",
                        lipo_tool="lipo-tool",
                        otool_tool="otool-tool",
                        environment={},
                        require_linked_library=False,
                    )

    def test_macho_verifier_rejects_absolute_non_system_lc_rpath(self) -> None:
        def fake_run(
            arguments: object,
            **_: object,
        ) -> subprocess.CompletedProcess[str]:
            command = list(arguments)  # type: ignore[arg-type]
            if command[0] == "file-tool":
                stdout = "Mach-O 64-bit bundle arm64\n"
            elif command[0] == "lipo-tool":
                stdout = "arm64\n"
            elif command[1] == "-L":
                stdout = (
                    "runtime.so:\n"
                    "\t/usr/lib/libSystem.B.dylib "
                    "(compatibility version 1.0.0, current version 1.0.0)\n"
                )
            else:
                stdout = (
                    "runtime.so:\n"
                    "Load command 1\n"
                    "          cmd LC_RPATH\n"
                    "      cmdsize 64\n"
                    "         path /private/tmp/build-venv/lib (offset 12)\n"
                )
            return subprocess.CompletedProcess(command, 0, stdout, "")

        with (
            mock.patch.object(sidecar_builder, "_run", side_effect=fake_run),
            self.assertRaisesRegex(
                sidecar_builder.SidecarBuildError,
                "non-system absolute LC_RPATH",
            ),
        ):
            sidecar_builder._verify_macho_file(
                Path("runtime.so"),
                label="test native extension",
                expected_architecture="arm64",
                file_tool="file-tool",
                lipo_tool="lipo-tool",
                otool_tool="otool-tool",
                environment={},
                require_linked_library=False,
            )

    def test_macho_rpath_parent_segments_must_remain_inside_bundle(self) -> None:
        safe_rpath = "@loader_path/../.."
        escaping_rpath = "@loader_path/" + "/".join([".."] * 64) + "/private/tmp/build-venv/evil"
        with tempfile.TemporaryDirectory(prefix="KORYAO sidecar rpath containment ") as temporary:
            pair_root = Path(temporary) / "bundle"
            support_root = pair_root / "_internal"
            native = support_root / "cv2" / ".dylibs" / "runtime.so"
            native.parent.mkdir(parents=True)
            native.write_bytes(b"\xcf\xfa\xed\xfe")

            def run_verifier(rpath: str) -> tuple[list[str], int, int]:
                def fake_run(
                    arguments: object,
                    **_: object,
                ) -> subprocess.CompletedProcess[str]:
                    command = list(arguments)  # type: ignore[arg-type]
                    if command[0] == "file-tool":
                        stdout = "Mach-O 64-bit bundle arm64\n"
                    elif command[0] == "lipo-tool":
                        stdout = "arm64\n"
                    elif command[1] == "-L":
                        stdout = (
                            "runtime.so:\n"
                            "\t/usr/lib/libSystem.B.dylib "
                            "(compatibility version 1.0.0, current version 1.0.0)\n"
                        )
                    else:
                        stdout = (
                            "runtime.so:\n"
                            "Load command 1\n"
                            "          cmd LC_RPATH\n"
                            "      cmdsize 64\n"
                            f"         path {rpath} (offset 12)\n"
                        )
                    return subprocess.CompletedProcess(command, 0, stdout, "")

                with mock.patch.object(
                    sidecar_builder,
                    "_run",
                    side_effect=fake_run,
                ):
                    return sidecar_builder._verify_macho_file(
                        native,
                        label="test native extension",
                        expected_architecture="arm64",
                        file_tool="file-tool",
                        lipo_tool="lipo-tool",
                        otool_tool="otool-tool",
                        environment={},
                        require_linked_library=False,
                        bundle_root=support_root,
                        executable_directory=pair_root,
                    )

            self.assertEqual((["arm64"], 1, 1), run_verifier(safe_rpath))
            with self.assertRaisesRegex(
                sidecar_builder.SidecarBuildError,
                "bundle-escaping LC_RPATH",
            ):
                run_verifier(escaping_rpath)

            other_target = pair_root / "_internal-x86_64-apple-darwin"
            other_target.mkdir()
            with self.assertRaisesRegex(
                sidecar_builder.SidecarBuildError,
                "bundle-escaping LC_RPATH",
            ):
                run_verifier("@executable_path/_internal-x86_64-apple-darwin")

    def test_pair_transaction_restores_previous_pair_after_final_verify_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="KORYAO sidecar pair rollback ") as temporary:
            repo_root = Path(temporary) / "repo"
            layout = self.make_layout_fixture(repo_root)
            layout.source_folder.mkdir(parents=True)
            layout.source_executable.write_bytes(b"new executable")
            layout.source_executable.chmod(0o755)
            layout.source_support_directory.mkdir()
            (layout.source_support_directory / "runtime.so").write_bytes(b"\xcf\xfa\xed\xfe")
            layout.staged_executable.write_bytes(b"old executable")
            layout.staged_executable.chmod(0o755)
            layout.staged_support_directory.mkdir()
            (layout.staged_support_directory / "old.txt").write_text(
                "old support",
                encoding="utf-8",
            )

            with (
                mock.patch.object(
                    sidecar_builder,
                    "verify_staged_artifact",
                    side_effect=(
                        {"candidate_verified": True},
                        sidecar_builder.SidecarBuildError("final verify failed"),
                    ),
                ),
                self.assertRaisesRegex(
                    sidecar_builder.SidecarBuildError,
                    "final verify failed",
                ),
            ):
                sidecar_builder._replace_staged_pair(
                    layout.source_executable,
                    layout.source_support_directory,
                    layout,
                )

            self.assertEqual(b"old executable", layout.staged_executable.read_bytes())
            self.assertEqual(
                "old support",
                (layout.staged_support_directory / "old.txt").read_text(encoding="utf-8"),
            )
            self.assertFalse(
                any(
                    path.name.startswith(".sidecar-stage-")
                    for path in layout.binaries_root.iterdir()
                )
            )

    def test_staged_artifact_requires_execution_bits_before_tooling(self) -> None:
        with tempfile.TemporaryDirectory(prefix="KORYAO sidecar staged ") as temporary:
            layout = self.make_layout_fixture(Path(temporary) / "repo")
            layout.staged_support_directory.mkdir()
            (layout.staged_support_directory / "runtime.so").write_bytes(b"\xcf\xfa\xed\xfe")
            layout.staged_executable.write_bytes(b"\xcf\xfa\xed\xfe")
            layout.staged_executable.chmod(0o644)

            with self.assertRaisesRegex(
                sidecar_builder.SidecarBuildError,
                "not executable",
            ):
                sidecar_builder.verify_staged_artifact(layout)

    def test_safe_exact_svg_and_zero_difference_report_are_read_from_disk(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="KORYAO exact artifact ") as temporary:
            output_root, data_root, source, _ = self.make_exact_fixture(Path(temporary))

            evidence = sidecar_tester._validate_exact_artifacts(
                output_root,
                data_root,
                source,
            )

            self.assertTrue(evidence["svg_parsed"])
            self.assertTrue(evidence["report_verified"])
            self.assertEqual(0, evidence["embedded_raster_count"])
            self.assertEqual(0, evidence["external_reference_count"])
            self.assertTrue(evidence["pixel_match"])

    def test_exact_svg_rejects_raster_script_and_external_references(self) -> None:
        unsafe_svgs = {
            "image": ('<svg xmlns="http://www.w3.org/2000/svg"><image href="#embedded"/></svg>'),
            "data_uri": (
                '<svg xmlns="http://www.w3.org/2000/svg">'
                '<rect style="fill:url(data:image/png;base64,AA==)"/></svg>'
            ),
            "script": ('<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>'),
            "external_href": (
                '<svg xmlns="http://www.w3.org/2000/svg">'
                '<use href="https://example.invalid/shape.svg#x"/></svg>'
            ),
            "external_css": (
                '<svg xmlns="http://www.w3.org/2000/svg">'
                '<rect style="fill:url(https://example.invalid/a.svg#x)"/></svg>'
            ),
            "processing_instruction": (
                '<?xml-stylesheet type="text/css" '
                'href="https://example.invalid/x.css"?>'
                '<svg xmlns="http://www.w3.org/2000/svg"/>'
            ),
            "malformed": '<svg xmlns="http://www.w3.org/2000/svg">',
        }
        for name, svg_text in unsafe_svgs.items():
            with (
                self.subTest(name=name),
                tempfile.TemporaryDirectory(prefix="KORYAO unsafe SVG ") as temporary,
            ):
                output_root, data_root, source, _ = self.make_exact_fixture(
                    Path(temporary),
                    svg_text=svg_text,
                )
                with self.assertRaises(sidecar_tester.SidecarTestError):
                    sidecar_tester._validate_exact_artifacts(
                        output_root,
                        data_root,
                        source,
                    )

    def test_exact_report_rejects_nonzero_or_unproven_validation(self) -> None:
        base: dict[str, object] = {
            "validation": {
                "svg_verified": True,
                "image_trace_used": False,
                "embedded_raster_count": 0,
                "external_reference_count": 0,
            },
            "exact_validation": {
                "pixel_match": True,
                "different_pixel_count": 0,
                "maximum_channel_difference": 0,
            },
        }
        mutations = (
            ("svg_verified", ("validation", "svg_verified"), False),
            ("image_trace", ("validation", "image_trace_used"), True),
            ("embedded", ("validation", "embedded_raster_count"), 1),
            ("external", ("validation", "external_reference_count"), 1),
            ("pixel_match", ("exact_validation", "pixel_match"), False),
            ("different", ("exact_validation", "different_pixel_count"), 1),
            (
                "maximum_difference",
                ("exact_validation", "maximum_channel_difference"),
                1,
            ),
        )
        for name, (section, field), value in mutations:
            with (
                self.subTest(name=name),
                tempfile.TemporaryDirectory(prefix="KORYAO unsafe report ") as temporary,
            ):
                report = copy.deepcopy(base)
                report[section][field] = value  # type: ignore[index]
                output_root, data_root, source, _ = self.make_exact_fixture(
                    Path(temporary),
                    report=report,
                )
                with self.assertRaises(sidecar_tester.SidecarTestError):
                    sidecar_tester._validate_exact_artifacts(
                        output_root,
                        data_root,
                        source,
                    )

    def test_exact_artifacts_reject_multiple_outputs_and_private_path_leaks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="KORYAO duplicate exact ") as temporary:
            output_root, data_root, source, _ = self.make_exact_fixture(Path(temporary))
            duplicate = output_root / "other" / "exact"
            duplicate.mkdir(parents=True)
            (duplicate / "vector.svg").write_text("<svg/>", encoding="utf-8")
            with self.assertRaises(sidecar_tester.SidecarTestError):
                sidecar_tester._validate_exact_artifacts(
                    output_root,
                    data_root,
                    source,
                )

        with tempfile.TemporaryDirectory(prefix="KORYAO leaked exact ") as temporary:
            output_root, data_root, source, output = self.make_exact_fixture(Path(temporary))
            report_path = output / "vector_report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["source"] = os.fspath(source.resolve())
            report_path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaises(sidecar_tester.SidecarTestError):
                sidecar_tester._validate_exact_artifacts(
                    output_root,
                    data_root,
                    source,
                )

    def test_every_exact_job_poll_rejects_private_path_leaks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="KORYAO leaking job poll ") as temporary:
            data_root = Path(temporary) / "app data"
            data_root.mkdir()
            source = data_root / "community vector source.png"
            responses = (
                {"ok": True, "data": {"selectionId": "selection-1"}},
                {"ok": True, "data": {"jobId": "job-1"}},
                {
                    "ok": True,
                    "data": {
                        "status": "running",
                        "source": os.fspath(source.resolve()),
                    },
                },
            )

            with (
                mock.patch.object(
                    sidecar_tester,
                    "_expect_status",
                    side_effect=responses,
                ),
                self.assertRaisesRegex(
                    sidecar_tester.SidecarTestError,
                    "poll response exposed a private absolute path",
                ),
            ):
                sidecar_tester._verify_exact_svg(
                    4567,
                    "credential",
                    data_root,
                    timeout=1,
                )

    @unittest.skipIf(os.name == "nt", "POSIX wrapper checks do not run on Windows")
    def test_arm_and_x86_plan_paths_preserve_argument_boundaries(self) -> None:
        build_wrapper = SCRIPTS_DIR / "Build-Sidecar.sh"
        test_wrapper = SCRIPTS_DIR / "Test-Sidecar.sh"
        for target in ("aarch64-apple-darwin", "x86_64-apple-darwin"):
            with self.subTest(target=target):
                build = subprocess.run(
                    [
                        "sh",
                        build_wrapper,
                        "--print-plan",
                        "--target-triple",
                        target,
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                )
                build_plan = json.loads(build.stdout)
                self.assertEqual(target, build_plan["target_triple"])
                self.assertEqual(
                    f"src-tauri/binaries/starbridge-sidecar-{target}",
                    build_plan["executable"],
                )
                self.assertEqual(
                    f"src-tauri/binaries/_internal-{target}",
                    build_plan["support_directory"],
                )
                self.assertEqual(
                    f"build/sidecar/{target}/pyinstaller-config",
                    build_plan["pyinstaller_config_root"],
                )

                test = subprocess.run(
                    [
                        "sh",
                        test_wrapper,
                        "--print-plan",
                        "--target-triple",
                        target,
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                )
                test_plan = json.loads(test.stdout)
                self.assertEqual(build_plan["executable"], test_plan["executable"])
                self.assertEqual(
                    build_plan["support_directory"],
                    test_plan["support_directory"],
                )

    @unittest.skipIf(os.name == "nt", "POSIX wrapper checks do not run on Windows")
    def test_unknown_arguments_invalid_ports_and_traversal_fail_closed(self) -> None:
        cases = (
            (SCRIPTS_DIR / "Build-Sidecar.sh", ["--unknown"]),
            (
                SCRIPTS_DIR / "Build-Sidecar.sh",
                ["--print-plan", "--verify-staged"],
            ),
            (
                SCRIPTS_DIR / "Build-Sidecar.sh",
                ["--print-plan", "--target-triple", "../../private"],
            ),
            (
                SCRIPTS_DIR / "Test-Sidecar.sh",
                ["--print-plan", "--port", "65536"],
            ),
        )
        for script, arguments in cases:
            with self.subTest(script=script.name, arguments=arguments):
                completed = subprocess.run(
                    ["sh", script, *arguments],
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                )
                self.assertNotEqual(0, completed.returncode)


if __name__ == "__main__":
    unittest.main()
