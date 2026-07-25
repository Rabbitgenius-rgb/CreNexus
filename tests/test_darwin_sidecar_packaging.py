from __future__ import annotations

import contextlib
import copy
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "apps" / "starbridge-desktop" / "scripts"
if os.fspath(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, os.fspath(SCRIPTS_DIR))

import sidecar_builder  # noqa: E402
import sidecar_launcher  # noqa: E402
import sidecar_tester  # noqa: E402


class DarwinSidecarPackagingTest(unittest.TestCase):
    @staticmethod
    def write_thin_macho(path: Path, architecture: str = "arm64") -> None:
        cpu_type = {
            "arm64": 0x0100000C,
            "x86_64": 0x01000007,
        }[architecture]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            b"\xcf\xfa\xed\xfe" + cpu_type.to_bytes(4, byteorder="little", signed=False)
        )

    @staticmethod
    def make_posix_wrapper_fixture(root: Path) -> Path:
        scripts = root / "apps" / "starbridge-desktop" / "scripts"
        runner = root / ".venv" / "bin" / "python"
        scripts.mkdir(parents=True)
        runner.parent.mkdir(parents=True)
        runner.symlink_to(Path(sys.executable).resolve())
        (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
        for name in (
            "sidecar_builder.py",
            "sidecar_launcher.py",
            "sidecar_tester.py",
            "Build-Sidecar.sh",
            "Test-Sidecar.sh",
        ):
            destination = scripts / name
            destination.write_bytes((SCRIPTS_DIR / name).read_bytes())
            if destination.suffix == ".sh":
                destination.chmod(0o755)
        return scripts

    @contextlib.contextmanager
    def allow_windows_darwin_executable_fixture(self, executable: Path):
        if os.name != "nt":
            yield
            return

        real_lstat = os.lstat
        real_access = os.access

        def fixture_lstat(path: object, *args: object, **kwargs: object) -> os.stat_result:
            metadata = real_lstat(path, *args, **kwargs)
            if Path(path) == executable:
                values = list(metadata)
                values[0] |= stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
                return os.stat_result(values)
            return metadata

        def fixture_access(
            path: object,
            mode: int,
            *args: object,
            **kwargs: object,
        ) -> bool:
            if Path(path) == executable and mode & os.X_OK:
                return True
            return real_access(path, mode, *args, **kwargs)

        with (
            mock.patch.object(sidecar_builder.os, "lstat", side_effect=fixture_lstat),
            mock.patch.object(sidecar_builder.os, "access", side_effect=fixture_access),
        ):
            yield

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
            "sidecar_launcher.py",
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

    def test_host_target_detection_does_not_execute_path_rustc(self) -> None:
        with tempfile.TemporaryDirectory(prefix="KORYAO fake rustc ") as temporary:
            root = Path(temporary)
            trace = root / "rustc.trace"
            rustc = root / "rustc"
            rustc.write_text(
                f"#!/bin/sh\nprintf executed > {os.fspath(trace)!r}\n",
                encoding="utf-8",
            )
            rustc.chmod(0o755)
            with (
                mock.patch.dict(os.environ, {"PATH": os.fspath(root)}, clear=False),
                mock.patch.object(sidecar_builder.platform, "system", return_value="Darwin"),
                mock.patch.object(sidecar_builder.platform, "machine", return_value="arm64"),
            ):
                self.assertEqual(
                    "aarch64-apple-darwin",
                    sidecar_builder.detect_host_target(),
                )
            self.assertFalse(trace.exists())

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
    def test_posix_wrappers_use_privileged_minimal_launcher(self) -> None:
        self.assertEqual(
            sidecar_builder.ENVIRONMENT_PASSTHROUGH_ALLOWLIST,
            sidecar_launcher.ENVIRONMENT_PASSTHROUGH_ALLOWLIST,
        )
        self.assertEqual(
            sidecar_builder.PIP_NETWORK_ENVIRONMENT_ALLOWLIST,
            sidecar_launcher.PIP_NETWORK_ENVIRONMENT_ALLOWLIST,
        )
        self.assertEqual(sidecar_builder.SANITIZED_PATH, sidecar_launcher.SANITIZED_PATH)
        with tempfile.TemporaryDirectory(prefix="KORYAO trusted launcher fixture ") as temporary:
            root = Path(temporary) / "repo"
            scripts = self.make_posix_wrapper_fixture(root)
            with mock.patch.object(
                sidecar_launcher,
                "__file__",
                os.fspath(scripts / "sidecar_launcher.py"),
            ):
                repository_runner, launcher_command = sidecar_launcher._launcher_command(
                    ["sidecar_launcher.py", "build", "--print-plan"]
                )
            self.assertEqual(root / ".venv" / "bin" / "python", repository_runner)
            self.assertEqual(os.fspath(repository_runner), launcher_command[0])
        for name in ("Build-Sidecar.sh", "Test-Sidecar.sh"):
            path = SCRIPTS_DIR / name
            with self.subTest(path=path):
                self.assertTrue(path.stat().st_mode & stat.S_IXUSR)
                subprocess.run(["/bin/sh", "-p", "-n", path], check=True)
                text = path.read_text(encoding="utf-8")
                self.assertTrue(text.startswith("#!/bin/sh -p\n"))
                self.assertEqual(1, text.count("/usr/bin/python3 -I"))
                self.assertIn("sidecar_launcher.py", text)
                self.assertIn("case $- in", text)
                self.assertIn("unset DEVELOPER_DIR SDKROOT TOOLCHAINS", text)
                self.assertIn('[ -L "$0" ]', text)
                self.assertIn('[ -L "$launcher_path" ]', text)
                self.assertEqual(1, text.count("exec /usr/bin/python3 -I"))
                for shell_operation in (
                    "\nset ",
                    "\ncd ",
                    "\npwd ",
                    "\nbuiltin ",
                    "\ncommand ",
                    "\n[ ",
                ):
                    self.assertNotIn(shell_operation, text)
                explicit_unprivileged = subprocess.run(
                    ["/bin/sh", path, "--print-plan"],
                    check=False,
                    capture_output=True,
                )
                self.assertNotEqual(0, explicit_unprivileged.returncode)
        staging_readme = (SCRIPTS_DIR.parent / "src-tauri" / "binaries" / "README.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("./scripts/Build-Sidecar.sh", staging_readme)
        self.assertIn("./scripts/Test-Sidecar.sh --skip-build", staging_readme)
        self.assertIn(
            "不要改成 `sh ./scripts/Build-Sidecar.sh` 或 `bash ./scripts/Test-Sidecar.sh`",
            staging_readme,
        )

    @unittest.skipIf(os.name == "nt", "POSIX launcher check")
    def test_launcher_fails_closed_without_repository_python(self) -> None:
        with tempfile.TemporaryDirectory(prefix="stage4-missing-venv-") as temporary:
            root = Path(temporary) / "repo"
            scripts = root / "apps" / "starbridge-desktop" / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "sidecar_launcher.py").write_text(
                (SCRIPTS_DIR / "sidecar_launcher.py").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            for entrypoint in ("sidecar_builder.py", "sidecar_tester.py"):
                (scripts / entrypoint).write_text("# fixture\n", encoding="utf-8")
            for wrapper_name in ("Build-Sidecar.sh", "Test-Sidecar.sh"):
                with self.subTest(wrapper=wrapper_name):
                    wrapper = scripts / wrapper_name
                    wrapper.write_text(
                        (SCRIPTS_DIR / wrapper_name).read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
                    wrapper.chmod(0o755)
                    completed = subprocess.run(
                        [wrapper, "--print-plan"],
                        check=False,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                    )
                    self.assertNotEqual(0, completed.returncode)
                    self.assertIn(
                        "repository Python environment is unavailable",
                        completed.stderr,
                    )
                    self.assertNotIn(os.fspath(root), completed.stderr)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin wrapper symlink check")
    def test_wrappers_reject_symlinked_wrapper_or_sibling_launcher(self) -> None:
        with tempfile.TemporaryDirectory(prefix="stage4-wrapper-symlink-") as temporary:
            root = Path(temporary)
            for wrapper_name in ("Build-Sidecar.sh", "Test-Sidecar.sh"):
                with self.subTest(kind="wrapper", wrapper=wrapper_name):
                    directory = root / f"wrapper-{wrapper_name}"
                    directory.mkdir()
                    trace = directory / "launcher.trace"
                    (directory / "sidecar_launcher.py").write_text(
                        "from pathlib import Path\n"
                        f"Path({os.fspath(trace)!r}).write_text('executed')\n",
                        encoding="utf-8",
                    )
                    wrapper = directory / wrapper_name
                    wrapper.symlink_to(SCRIPTS_DIR / wrapper_name)

                    completed = subprocess.run(
                        [wrapper, "--print-plan"],
                        check=False,
                        capture_output=True,
                    )

                    self.assertNotEqual(0, completed.returncode)
                    self.assertFalse(trace.exists())

                with self.subTest(kind="launcher", wrapper=wrapper_name):
                    directory = root / f"launcher-{wrapper_name}"
                    directory.mkdir()
                    wrapper = directory / wrapper_name
                    wrapper.write_text(
                        (SCRIPTS_DIR / wrapper_name).read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
                    wrapper.chmod(0o755)
                    trace = directory / "launcher.trace"
                    outside_launcher = root / f"outside-{wrapper_name}.py"
                    outside_launcher.write_text(
                        "from pathlib import Path\n"
                        f"Path({os.fspath(trace)!r}).write_text('executed')\n",
                        encoding="utf-8",
                    )
                    (directory / "sidecar_launcher.py").symlink_to(outside_launcher)

                    completed = subprocess.run(
                        [wrapper, "--print-plan"],
                        check=False,
                        capture_output=True,
                    )

                    self.assertNotEqual(0, completed.returncode)
                    self.assertFalse(trace.exists())

    def test_python_and_pip_environments_remove_injection_and_install_redirects(
        self,
    ) -> None:
        safe_environment = {
            "HOME": "/controlled/home",
            "TMPDIR": "/controlled/tmp",
            "LANG": "en_US.UTF-8",
            "LC_CTYPE": "en_US.UTF-8",
            "HTTPS_PROXY": "http://proxy.invalid:8443",
            "http_proxy": "http://proxy.invalid:8081",
            "SSL_CERT_FILE": "/certs/python.pem",
            "REQUESTS_CA_BUNDLE": "/certs/requests.pem",
        }
        pip_environment_allowlist = {
            "PIP_INDEX_URL": "https://packages.invalid/simple",
            "PIP_PROXY": "http://proxy.invalid:8080",
            "PIP_CERT": "/certs/pip.pem",
        }
        dangerous_names = (
            "PATH",
            "DYLD_INSERT_LIBRARIES",
            "DyLd_LIBRARY_PATH",
            "LD_PRELOAD",
            "Ld_LIBRARY_PATH",
            "MAGIC",
            "dEvElOpEr_DiR",
            "SDKROOT",
            "TOOLCHAINS",
            "XCRUN_CACHE_PATH",
            "CODESIGN_ALLOCATE",
            "STARBRIDGE_SESSION_TOKEN",
            "STARBRIDGE_APP_DATA_DIR",
            "CODEX_HOME",
            "pYtHoNpAtH",
            "PYTHONHOME",
            "_PYTHON_SYSCONFIGDATA_NAME",
            "__PYVENV_LAUNCHER__",
            "PYINSTALLER_CONFIG_DIR",
            "PIP_TARGET",
            "PIP_BUILD_TRACKER",
            "PIP_CONFIG_FILE",
            "cC",
            "CXX",
            "CPP",
            "LD",
            "LDSHARED",
            "LDCXXSHARED",
            "AR",
            "ARFLAGS",
            "RANLIB",
            "NM",
            "STRIP",
            "AS",
            "CFLAGS",
            "CXXFLAGS",
            "CPPFLAGS",
            "LDFLAGS",
            "OBJCFLAGS",
            "FCFLAGS",
            "FFLAGS",
            "ARCHFLAGS",
            "MACOSX_DEPLOYMENT_TARGET",
            "CPATH",
            "C_INCLUDE_PATH",
            "CPLUS_INCLUDE_PATH",
            "LIBRARY_PATH",
            "COMPILER_PATH",
            "GCC_EXEC_PREFIX",
            "vIrTuAl_EnV",
            "CONDA_PREFIX",
            "_CE_CONDA",
            "PKG_CONFIG_PATH",
            "SETUPTOOLS_SCM_PRETEND_VERSION",
            "DISTUTILS_USE_SDK",
            "CMAKE_GENERATOR",
            "MESON_ARGS",
            "SKBUILD_CMAKE_ARGS",
            "NINJA_STATUS",
            "MAKEFLAGS",
            "RUSTC",
            "RUSTFLAGS",
            "CARGO_HOME",
            "CARGO_BUILD_TARGET",
            "PYO3_CONFIG_FILE",
            "MATURIN_PEP517_ARGS",
            "CCACHE_PREFIX",
            "SCCACHE_ERROR_LOG",
            "BASH_ENV",
            "ENV",
        )
        inherited = {
            **safe_environment,
            **pip_environment_allowlist,
            **{name: f"/tmp/evil-{index}" for index, name in enumerate(dangerous_names)},
        }

        python_environment = sidecar_builder.sanitized_environment(inherited)
        self.assertEqual(sidecar_builder.SANITIZED_PATH, python_environment["PATH"])
        for name, value in safe_environment.items():
            self.assertEqual(value, python_environment[name])
        for name in (*dangerous_names[1:], *pip_environment_allowlist):
            self.assertNotIn(name, python_environment)

        pip_environment = sidecar_builder.sanitized_environment(
            inherited,
            for_pip=True,
        )
        for name in dangerous_names[1:]:
            if name == "PIP_CONFIG_FILE":
                continue
            self.assertNotIn(name, pip_environment)
        for name, value in safe_environment.items():
            self.assertEqual(value, pip_environment[name])
        for name, value in pip_environment_allowlist.items():
            self.assertEqual(value, pip_environment[name])
        self.assertEqual(os.devnull, pip_environment["PIP_CONFIG_FILE"])
        self.assertEqual("1", pip_environment["PIP_NO_INPUT"])
        self.assertEqual("1", pip_environment["PIP_DISABLE_PIP_VERSION_CHECK"])

        launcher_environment = sidecar_launcher.sanitized_launcher_environment(
            {
                **inherited,
                "CARGO_BUILD_TARGET": "aarch64-apple-darwin",
            }
        )
        for name, value in safe_environment.items():
            self.assertEqual(value, launcher_environment[name])
        for name, value in pip_environment_allowlist.items():
            self.assertEqual(value, launcher_environment[name])
        self.assertEqual(
            "aarch64-apple-darwin",
            launcher_environment["CARGO_BUILD_TARGET"],
        )
        self.assertEqual(sidecar_builder.SANITIZED_PATH, launcher_environment["PATH"])
        self.assertEqual(os.devnull, launcher_environment["PIP_CONFIG_FILE"])
        self.assertEqual("1", launcher_environment["PIP_NO_INPUT"])
        self.assertEqual(
            "1",
            launcher_environment["PIP_DISABLE_PIP_VERSION_CHECK"],
        )
        for name in dangerous_names[1:]:
            if name in {
                "CARGO_BUILD_TARGET",
                "PIP_CONFIG_FILE",
            }:
                continue
            self.assertNotIn(name, launcher_environment)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin compiler isolation test")
    def test_source_build_does_not_execute_inherited_fake_compiler(self) -> None:
        with tempfile.TemporaryDirectory(prefix="KORYAOFakeCompiler") as temporary:
            root = Path(temporary)
            trace = root / "fake-compiler.trace"
            fake_compiler = root / "fake-compiler"
            fake_compiler.write_text(
                f'#!/bin/sh\nprintf executed > "{trace}"\nexit 73\n',
                encoding="utf-8",
            )
            fake_compiler.chmod(0o755)
            source = root / "probe.c"
            object_file = root / "probe.o"
            source.write_text(
                "int sidecar_compiler_probe(void) { return 0; }\n",
                encoding="utf-8",
            )
            inherited = {
                **os.environ,
                "CC": os.fspath(fake_compiler),
                "CXX": os.fspath(fake_compiler),
                "LDSHARED": os.fspath(fake_compiler),
                "CFLAGS": "-DPROBE_INJECTED=1",
                "VIRTUAL_ENV": "/tmp/evil-virtual-environment",
                "CONDA_PREFIX": "/tmp/evil-conda",
            }
            environment = sidecar_builder.sanitized_environment(inherited, for_pip=True)
            compiler = environment.get("CC", "/usr/bin/cc")

            completed = subprocess.run(
                [compiler, "-c", source, "-o", object_file],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertFalse(trace.exists())
            self.assertTrue(object_file.is_file())

    def test_prepare_environment_uses_layout_venv_and_sanitized_children(self) -> None:
        with tempfile.TemporaryDirectory(prefix="KORYAO sidecar environment plan ") as temporary:
            layout = self.make_layout_fixture(Path(temporary) / "repo")
            build_python = layout.build_environment / "bin" / "python"
            build_python.parent.mkdir(parents=True)
            build_python.write_text("# fixture\n", encoding="utf-8")
            inherited = {
                "HOME": "/controlled/home",
                "PIP_INDEX_URL": "https://packages.invalid/simple",
                "CC": "/tmp/evil-cc",
                "CFLAGS": "-DEVIL=1",
                "VIRTUAL_ENV": "/tmp/evil-venv",
                "CONDA_PREFIX": "/tmp/evil-conda",
                "CARGO_HOME": "/tmp/evil-cargo",
                "PIP_BUILD_TRACKER": "/tmp/evil-pip-build",
            }
            calls: list[tuple[list[object], dict[str, str]]] = []

            def fake_run(
                arguments: object,
                *,
                environment: dict[str, str] | None = None,
                **_: object,
            ) -> subprocess.CompletedProcess[str]:
                command = list(arguments)  # type: ignore[arg-type]
                calls.append((command, dict(environment or {})))
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                mock.patch.dict(os.environ, inherited, clear=True),
                mock.patch.object(sidecar_builder, "_run", side_effect=fake_run),
            ):
                actual_python = sidecar_builder._prepare_environment(
                    layout,
                    skip_dependency_install=False,
                )

            self.assertEqual(build_python, actual_python)
            self.assertEqual(4, len(calls))
            for command, environment in calls:
                self.assertEqual(build_python, command[0])
                for dangerous in (
                    "CC",
                    "CFLAGS",
                    "VIRTUAL_ENV",
                    "CONDA_PREFIX",
                    "CARGO_HOME",
                    "PIP_BUILD_TRACKER",
                ):
                    self.assertNotIn(dangerous, environment)
                self.assertEqual("/controlled/home", environment["HOME"])
                self.assertEqual(sidecar_builder.SANITIZED_PATH, environment["PATH"])
            for _, environment in calls[:2]:
                self.assertEqual(
                    "https://packages.invalid/simple",
                    environment["PIP_INDEX_URL"],
                )
            for _, environment in calls[2:]:
                self.assertNotIn("PIP_INDEX_URL", environment)

    @unittest.skipIf(os.name == "nt", "POSIX wrapper checks do not run on Windows")
    def test_wrapper_python_isolation_blocks_startup_injection(self) -> None:
        with tempfile.TemporaryDirectory(prefix="KORYAO sidecar python injection ") as temporary:
            root = Path(temporary)
            scripts = self.make_posix_wrapper_fixture(root / "repo")
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
                    scripts / "Build-Sidecar.sh",
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

    @unittest.skipUnless(sys.platform == "darwin", "Darwin loader injection test")
    def test_wrapper_ignores_fake_path_and_loader_environment(self) -> None:
        with tempfile.TemporaryDirectory(prefix="KORYAO fake wrapper path ") as temporary:
            fake_bin = Path(temporary) / "fake-bin"
            fake_bin.mkdir()
            trace = Path(temporary) / "fake-command.trace"
            for name in ("dirname", "env", "sed", "python3"):
                executable = fake_bin / name
                executable.write_text(
                    f"#!/bin/sh\nprintf {name!r} >> {os.fspath(trace)!r}\nexit 97\n",
                    encoding="utf-8",
                )
                executable.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": os.fspath(fake_bin),
                    "DYLD_INSERT_LIBRARIES": "/outside/injected.dylib",
                    "DYLD_LIBRARY_PATH": "/outside/dyld",
                    "LD_PRELOAD": "/outside/preload.so",
                    "LD_LIBRARY_PATH": "/outside/ld",
                    "MAGIC": "/outside/magic",
                    "CODESIGN_ALLOCATE": "/outside/codesign_allocate",
                }
            )

            completed = subprocess.run(
                [
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

    @unittest.skipUnless(sys.platform == "darwin", "macOS privileged shell test")
    def test_wrappers_ignore_bash_functions_xtrace_and_secret_expansion(self) -> None:
        with tempfile.TemporaryDirectory(prefix="stage4-bash-startup-") as temporary:
            root = Path(temporary)
            function_trace = root / "function.trace"
            ps4_trace = root / "ps4.trace"
            developer_trace = root / "developer.trace"
            fake_developer = root / "fake-developer"
            fake_xcrun = fake_developer / "usr" / "bin" / "xcrun"
            fake_xcrun.parent.mkdir(parents=True)
            fake_xcrun.write_text(
                f"#!/bin/sh\n/usr/bin/printf executed > {os.fspath(developer_trace)!r}\nexit 97\n",
                encoding="utf-8",
            )
            fake_xcrun.chmod(0o755)
            sentinel = "stage4-pip-credential-sentinel"
            environment = os.environ.copy()
            for name in (
                "cd",
                "pwd",
                "[",
                "set",
                "unset",
                "builtin",
                "command",
                "exec",
            ):
                environment[f"BASH_FUNC_{name}%%"] = (
                    f"() {{ /usr/bin/printf '%s\\n' {name!r} >> {os.fspath(function_trace)!r}; }}"
                )
            environment.update(
                {
                    "SHELLOPTS": "xtrace",
                    "PS4": (f"$(/usr/bin/printf x >> {os.fspath(ps4_trace)!r})stage4-trace "),
                    "PIP_INDEX_URL": (f"https://user:{sentinel}@packages.invalid/simple"),
                    "DEVELOPER_DIR": os.fspath(fake_developer),
                    "SDKROOT": "/tmp/evil-sdk",
                    "TOOLCHAINS": "evil-toolchain",
                    "XCRUN_CACHE_PATH": "/tmp/evil-xcrun-cache",
                }
            )

            for wrapper_name in ("Build-Sidecar.sh", "Test-Sidecar.sh"):
                with self.subTest(wrapper=wrapper_name):
                    completed = subprocess.run(
                        [
                            SCRIPTS_DIR / wrapper_name,
                            "--print-plan",
                            "--target-triple",
                            "aarch64-apple-darwin",
                        ],
                        env=environment,
                        check=False,
                        capture_output=True,
                    )
                    stdout = completed.stdout.decode("utf-8", errors="replace")
                    stderr = completed.stderr.decode("utf-8", errors="replace")
                    self.assertEqual(0, completed.returncode, stderr)
                    self.assertEqual(
                        "aarch64-apple-darwin",
                        json.loads(stdout)["target_triple"],
                    )
                    self.assertNotIn(sentinel, stdout)
                    self.assertNotIn(sentinel, stderr)
                    self.assertFalse(function_trace.exists())
                    self.assertFalse(ps4_trace.exists())
                    self.assertFalse(developer_trace.exists())

    @unittest.skipIf(os.name == "nt", "POSIX exec-chain test")
    def test_wrapper_exec_chain_preserves_pid_and_termination(self) -> None:
        with tempfile.TemporaryDirectory(prefix="stage4-wrapper-exec-") as temporary:
            root = Path(temporary) / "repo"
            scripts = root / "apps" / "starbridge-desktop" / "scripts"
            runner = root / ".venv" / "bin" / "python"
            scripts.mkdir(parents=True)
            runner.parent.mkdir(parents=True)
            runner.symlink_to(Path(sys.executable).resolve())
            (scripts / "sidecar_launcher.py").write_text(
                (SCRIPTS_DIR / "sidecar_launcher.py").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            for entrypoint in ("sidecar_builder.py", "sidecar_tester.py"):
                (scripts / entrypoint).write_text("# fixture\n", encoding="utf-8")

            for wrapper_name in ("Build-Sidecar.sh", "Test-Sidecar.sh"):
                with self.subTest(wrapper=wrapper_name):
                    wrapper = scripts / wrapper_name
                    wrapper.write_text(
                        (SCRIPTS_DIR / wrapper_name).read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
                    wrapper.chmod(0o755)
                    pid_file = Path(temporary) / f"{wrapper_name}.pid"
                    entrypoint = scripts / (
                        "sidecar_builder.py"
                        if wrapper_name == "Build-Sidecar.sh"
                        else "sidecar_tester.py"
                    )
                    entrypoint.write_text(
                        "import os, time\n"
                        f"with open({os.fspath(pid_file)!r}, 'w', encoding='utf-8') as handle:\n"
                        "    handle.write(str(os.getpid()))\n"
                        "time.sleep(30)\n",
                        encoding="utf-8",
                    )

                    process = subprocess.Popen(
                        [wrapper, "argument with spaces"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    runner_pid: int | None = None
                    try:
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline and not pid_file.is_file():
                            if process.poll() is not None:
                                break
                            time.sleep(0.02)
                        self.assertTrue(pid_file.is_file())
                        runner_pid = int(pid_file.read_text(encoding="utf-8"))
                        self.assertEqual(process.pid, runner_pid)
                        process.terminate()
                        process.wait(timeout=5)
                        with self.assertRaises(ProcessLookupError):
                            os.kill(runner_pid, 0)
                    finally:
                        if process.poll() is None:
                            process.terminate()
                            process.wait(timeout=5)
                        if runner_pid is not None and runner_pid != process.pid:
                            try:
                                os.kill(runner_pid, signal.SIGTERM)
                            except ProcessLookupError:
                                pass

    @unittest.skipIf(os.name == "nt", "POSIX wrapper environment checks")
    def test_wrappers_pass_only_the_explicit_environment_allowlist(self) -> None:
        dangerous_names = (
            "CC",
            "cXx",
            "AR",
            "CFLAGS",
            "CPPFLAGS",
            "LDFLAGS",
            "ARCHFLAGS",
            "MACOSX_DEPLOYMENT_TARGET",
            "PKG_CONFIG_PATH",
            "vIrTuAl_EnV",
            "CONDA_PREFIX",
            "CMAKE_GENERATOR",
            "MESON_ARGS",
            "NINJA_STATUS",
            "MAKEFLAGS",
            "RUSTC",
            "CARGO_HOME",
            "PYO3_CONFIG_FILE",
            "MATURIN_PEP517_ARGS",
            "PIP_BUILD_TRACKER",
            "PYTHONPATH",
            "DyLd_LIBRARY_PATH",
            "Ld_PRELOAD",
            "DEVELOPER_DIR",
            "SDKROOT",
            "TOOLCHAINS",
            "XCRUN_CACHE_PATH",
            "xcrun_log",
            "xcrun_nocache",
            "xcrun_verbose",
            "BASH_ENV",
            "ENV",
            "SHELLOPTS",
            "PS4",
            "BASH_FUNC_cd%%",
            "BASH_FUNC_pwd%%",
            "BASH_FUNC_[%%",
            "BASH_FUNC_set%%",
            "BASH_FUNC_unset%%",
            "BASH_FUNC_builtin%%",
            "BASH_FUNC_command%%",
            "BASH_FUNC_exec%%",
        )
        with tempfile.TemporaryDirectory(prefix="KORYAO wrapper environment ") as temporary:
            root = Path(temporary) / "repo"
            scripts = root / "apps" / "starbridge-desktop" / "scripts"
            runner = root / ".venv" / "bin" / "python"
            scripts.mkdir(parents=True)
            runner.parent.mkdir(parents=True)
            runner.symlink_to(Path(sys.executable).resolve())
            (scripts / "sidecar_launcher.py").write_text(
                (SCRIPTS_DIR / "sidecar_launcher.py").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            for entrypoint in ("sidecar_builder.py", "sidecar_tester.py"):
                (scripts / entrypoint).write_text("# fixture\n", encoding="utf-8")
            compiler_trace = Path(temporary) / "fake-compiler.trace"
            fake_compiler = Path(temporary) / "fake-compiler"
            fake_compiler.write_text(
                f'#!/bin/sh\nprintf executed > "{compiler_trace}"\nexit 91\n',
                encoding="utf-8",
            )
            fake_compiler.chmod(0o755)

            for wrapper_name in ("Build-Sidecar.sh", "Test-Sidecar.sh"):
                with self.subTest(wrapper=wrapper_name):
                    wrapper = scripts / wrapper_name
                    wrapper.write_text(
                        (SCRIPTS_DIR / wrapper_name).read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
                    wrapper.chmod(0o755)
                    capture = Path(temporary) / f"{wrapper_name}.environment"
                    expected_entrypoint = scripts / (
                        "sidecar_builder.py"
                        if wrapper_name == "Build-Sidecar.sh"
                        else "sidecar_tester.py"
                    )
                    expected_entrypoint.write_text(
                        "import json, os, sys\n"
                        f"with open({os.fspath(capture)!r}, 'w', encoding='utf-8') as handle:\n"
                        "    json.dump({'environment': dict(os.environ), 'arguments': sys.argv}, handle)\n",
                        encoding="utf-8",
                    )
                    environment = {
                        **os.environ,
                        **{name: os.fspath(fake_compiler) for name in dangerous_names},
                        "HOME": "/controlled/home",
                        "TMPDIR": "/controlled/tmp",
                        "LANG": "C",
                        "LC_CTYPE": "C",
                        "HTTPS_PROXY": "http://proxy.invalid:8443",
                        "SSL_CERT_FILE": "/certs/python.pem",
                        "PIP_INDEX_URL": "https://packages.invalid/simple",
                        "PIP_CERT": "/certs/pip.pem",
                        "CARGO_BUILD_TARGET": "aarch64-apple-darwin",
                    }
                    original_arguments = (
                        "--print-plan",
                        "value with spaces",
                        "",
                        "semi;colon",
                        "$(not-executed)",
                    )
                    completed = subprocess.run(
                        [wrapper, *original_arguments],
                        env=environment,
                        check=False,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                    )

                    self.assertEqual(0, completed.returncode, completed.stderr)
                    snapshot = json.loads(capture.read_text(encoding="utf-8"))
                    captured = snapshot["environment"]
                    self.assertEqual(
                        [
                            os.fspath(expected_entrypoint),
                            *original_arguments,
                        ],
                        snapshot["arguments"],
                    )
                    for name in dangerous_names:
                        self.assertNotIn(name, captured)
                    self.assertEqual(sidecar_builder.SANITIZED_PATH, captured["PATH"])
                    self.assertEqual("/controlled/home", captured["HOME"])
                    self.assertEqual("/controlled/tmp", captured["TMPDIR"])
                    self.assertEqual("C", captured["LANG"])
                    self.assertIn(captured["LC_CTYPE"], {"C", "C.UTF-8"})
                    self.assertEqual(
                        "C",
                        sidecar_launcher.sanitized_launcher_environment(environment)["LC_CTYPE"],
                    )
                    self.assertEqual(
                        "http://proxy.invalid:8443",
                        captured["HTTPS_PROXY"],
                    )
                    self.assertEqual("/certs/python.pem", captured["SSL_CERT_FILE"])
                    self.assertEqual(
                        "https://packages.invalid/simple",
                        captured["PIP_INDEX_URL"],
                    )
                    self.assertEqual("/certs/pip.pem", captured["PIP_CERT"])
                    self.assertEqual(
                        "aarch64-apple-darwin",
                        captured["CARGO_BUILD_TARGET"],
                    )
                    self.assertFalse(compiler_trace.exists())

    @unittest.skipIf(os.name == "nt", "symlink checks require POSIX")
    def test_write_roots_and_input_files_reject_symlink_replacement(self) -> None:
        cases = (
            ".venv-build",
            "apps/starbridge-desktop/build",
            "apps/starbridge-desktop/src-tauri/binaries",
            "apps/starbridge-desktop/scripts/sidecar_launcher.py",
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

        with tempfile.TemporaryDirectory(prefix="KORYAO Mach-O dependency ") as temporary:
            runtime = Path(temporary) / "runtime.so"
            self.write_thin_macho(runtime)
            with (
                mock.patch.object(sidecar_builder, "_run", side_effect=fake_run),
                self.assertRaisesRegex(
                    sidecar_builder.SidecarBuildError,
                    "non-system absolute dependency",
                ),
            ):
                sidecar_builder._verify_macho_file(
                    runtime,
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
        with tempfile.TemporaryDirectory(prefix="KORYAO Mach-O traversal ") as temporary:
            runtime = Path(temporary) / "runtime.so"
            self.write_thin_macho(runtime)
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
                            runtime,
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

        with tempfile.TemporaryDirectory(prefix="KORYAO Mach-O rpath ") as temporary:
            runtime = Path(temporary) / "runtime.so"
            self.write_thin_macho(runtime)
            with (
                mock.patch.object(sidecar_builder, "_run", side_effect=fake_run),
                self.assertRaisesRegex(
                    sidecar_builder.SidecarBuildError,
                    "non-system absolute LC_RPATH",
                ),
            ):
                sidecar_builder._verify_macho_file(
                    runtime,
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
            self.write_thin_macho(native)

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

    def test_extensionless_macho_is_verified_by_content_and_deduplicated(self) -> None:
        for target, architecture in (
            ("aarch64-apple-darwin", "arm64"),
            ("x86_64-apple-darwin", "x86_64"),
        ):
            with (
                self.subTest(target=target),
                tempfile.TemporaryDirectory(prefix="KORYAO extensionless Mach-O ") as temporary,
            ):
                repo_root = Path(temporary) / "repo"
                self.make_layout_fixture(repo_root)
                layout = sidecar_builder.layout_for(repo_root, target)
                layout.staged_support_directory.mkdir()
                runtime = layout.staged_support_directory / "runtime.so"
                helper = layout.staged_support_directory / "extensionless-helper"
                self.write_thin_macho(layout.staged_executable, architecture)
                self.write_thin_macho(runtime, architecture)
                self.write_thin_macho(helper, architecture)
                layout.staged_executable.chmod(0o755)
                (layout.staged_support_directory / "helper-alias-one").symlink_to(helper.name)
                (layout.staged_support_directory / "helper-alias-two").symlink_to(helper.name)
                calls: list[tuple[str, str, Path]] = []

                def fake_run(
                    arguments: object,
                    call_log: list[tuple[str, str, Path]] = calls,
                    **_: object,
                ) -> subprocess.CompletedProcess[str]:
                    command = list(arguments)  # type: ignore[arg-type]
                    tool = str(command[0])
                    option = str(command[1])
                    path = Path(command[-1])
                    call_log.append((tool, option, path))
                    header = path.read_bytes()[:8]
                    cpu_type = int.from_bytes(header[4:8], "little")
                    actual_architecture = {
                        sidecar_builder.MACHO_CPU_TYPES["arm64"]: "arm64",
                        sidecar_builder.MACHO_CPU_TYPES["x86_64"]: "x86_64",
                    }[cpu_type]
                    if tool == "file-tool":
                        stdout = f"Mach-O 64-bit executable {actual_architecture}\n"
                    elif tool == "lipo-tool":
                        stdout = f"{actual_architecture}\n"
                    elif option == "-L":
                        stdout = (
                            f"{path}:\n"
                            "\t/usr/lib/libSystem.B.dylib "
                            "(compatibility version 1.0.0, current version 1.0.0)\n"
                        )
                    else:
                        stdout = f"{path}:\n"
                    return subprocess.CompletedProcess(command, 0, stdout, "")

                with (
                    self.allow_windows_darwin_executable_fixture(layout.staged_executable),
                    mock.patch.object(
                        sidecar_builder,
                        "_required_tool",
                        side_effect=lambda name: f"{name}-tool",
                    ),
                    mock.patch.object(sidecar_builder, "_run", side_effect=fake_run),
                    mock.patch.object(
                        sidecar_builder,
                        "check_vector60_runtime",
                        return_value={"ok": True},
                    ),
                ):
                    result = sidecar_builder.verify_staged_artifact(layout)

                self.assertEqual(4, result["support_file_count"])
                self.assertEqual(2, result["support_symlink_count"])
                self.assertEqual(2, result["support_unique_payload_count"])
                self.assertEqual(4, result["native_macho_logical_entry_count"])
                self.assertEqual(2, result["native_mach_o_verified_count"])
                self.assertEqual(0, result["non_native_logical_entry_count"])
                self.assertEqual(0, result["non_native_file_magic_verified_count"])
                helper_lipo_calls = [
                    call for call in calls if call[0] == "lipo-tool" and call[2] == helper.resolve()
                ]
                helper_otool_calls = [
                    call
                    for call in calls
                    if call[0] == "otool-tool" and call[2] == helper.resolve()
                ]
                self.assertEqual(1, len(helper_lipo_calls))
                self.assertEqual(2, len(helper_otool_calls))

    def test_extensionless_wrong_arch_macho_fails_closed(self) -> None:
        cases = (
            ("aarch64-apple-darwin", "arm64", "x86_64"),
            ("x86_64-apple-darwin", "x86_64", "arm64"),
        )
        for target, expected_architecture, wrong_architecture in cases:
            with (
                self.subTest(target=target),
                tempfile.TemporaryDirectory(prefix="KORYAO wrong-arch Mach-O ") as temporary,
            ):
                repo_root = Path(temporary) / "repo"
                self.make_layout_fixture(repo_root)
                layout = sidecar_builder.layout_for(repo_root, target)
                layout.staged_support_directory.mkdir()
                runtime = layout.staged_support_directory / "runtime.so"
                helper = layout.staged_support_directory / "extensionless-helper"
                self.write_thin_macho(layout.staged_executable, expected_architecture)
                self.write_thin_macho(runtime, expected_architecture)
                self.write_thin_macho(helper, wrong_architecture)
                layout.staged_executable.chmod(0o755)

                def fake_run(
                    arguments: object,
                    **_: object,
                ) -> subprocess.CompletedProcess[str]:
                    command = list(arguments)  # type: ignore[arg-type]
                    tool = str(command[0])
                    option = str(command[1])
                    path = Path(command[-1])
                    header = path.read_bytes()[:8]
                    actual_architecture = {
                        sidecar_builder.MACHO_CPU_TYPES["arm64"]: "arm64",
                        sidecar_builder.MACHO_CPU_TYPES["x86_64"]: "x86_64",
                    }[int.from_bytes(header[4:8], "little")]
                    if tool == "file-tool":
                        stdout = f"Mach-O 64-bit executable {actual_architecture}\n"
                    elif tool == "lipo-tool":
                        stdout = f"{actual_architecture}\n"
                    elif option == "-L":
                        stdout = (
                            f"{path}:\n"
                            "\t/usr/lib/libSystem.B.dylib "
                            "(compatibility version 1.0.0, current version 1.0.0)\n"
                        )
                    else:
                        stdout = f"{path}:\n"
                    return subprocess.CompletedProcess(command, 0, stdout, "")

                with (
                    self.allow_windows_darwin_executable_fixture(layout.staged_executable),
                    mock.patch.object(
                        sidecar_builder,
                        "_required_tool",
                        side_effect=lambda name: f"{name}-tool",
                    ),
                    mock.patch.object(sidecar_builder, "_run", side_effect=fake_run),
                    mock.patch.object(
                        sidecar_builder,
                        "check_vector60_runtime",
                        return_value={"ok": True},
                    ),
                    self.assertRaisesRegex(
                        sidecar_builder.SidecarBuildError,
                        "Mach-O CPU type",
                    ),
                ):
                    sidecar_builder.verify_staged_artifact(layout)

    def test_native_suffix_with_non_macho_payload_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="KORYAO non-Mach-O native suffix ") as temporary:
            layout = self.make_layout_fixture(Path(temporary) / "repo")
            layout.staged_support_directory.mkdir()
            runtime = layout.staged_support_directory / "runtime.so"
            runtime.write_bytes(b"ordinary data")
            self.write_thin_macho(layout.staged_executable)
            layout.staged_executable.chmod(0o755)

            def fake_run(
                arguments: object,
                **_: object,
            ) -> subprocess.CompletedProcess[str]:
                command = list(arguments)  # type: ignore[arg-type]
                tool = str(command[0])
                option = str(command[1])
                path = Path(command[-1])
                if tool == "file-tool":
                    stdout = (
                        "ASCII text\n"
                        if path == runtime.resolve()
                        else "Mach-O 64-bit executable arm64\n"
                    )
                elif tool == "lipo-tool":
                    stdout = "arm64\n"
                elif option == "-L":
                    stdout = (
                        f"{path}:\n"
                        "\t/usr/lib/libSystem.B.dylib "
                        "(compatibility version 1.0.0, current version 1.0.0)\n"
                    )
                else:
                    stdout = f"{path}:\n"
                return subprocess.CompletedProcess(command, 0, stdout, "")

            with (
                self.allow_windows_darwin_executable_fixture(layout.staged_executable),
                mock.patch.object(
                    sidecar_builder,
                    "_required_tool",
                    side_effect=lambda name: f"{name}-tool",
                ),
                mock.patch.object(sidecar_builder, "_run", side_effect=fake_run),
                self.assertRaisesRegex(
                    sidecar_builder.SidecarBuildError,
                    "native-extension entry is not a Mach-O",
                ),
            ):
                sidecar_builder.verify_staged_artifact(layout)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin system tooling required")
    def test_verifier_ignores_fake_path_tools_and_rejects_non_macho(self) -> None:
        with tempfile.TemporaryDirectory(prefix="KORYAO fake Mach-O tools ") as temporary:
            root = Path(temporary)
            layout = self.make_layout_fixture(root / "repo")
            layout.staged_support_directory.mkdir()
            (layout.staged_support_directory / "runtime.so").write_bytes(b"arbitrary bytes")
            layout.staged_executable.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' "
                """'{"ok":true,"versions":{"vtracer":"0.6.15","skia-pathops":"0.9.2","svgpathtools":"1.7.2"}}'"""
                "\n",
                encoding="utf-8",
            )
            layout.staged_executable.chmod(0o755)

            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            trace = root / "fake-tool.trace"
            for name, output in (
                ("file", "Mach-O 64-bit executable arm64"),
                ("lipo", "arm64"),
                ("otool", "/usr/lib/libSystem.B.dylib"),
            ):
                tool = fake_bin / name
                tool.write_text(
                    "#!/bin/sh\n"
                    f"printf {name!r} >> {os.fspath(trace)!r}\n"
                    f"printf '%s\\n' {output!r}\n",
                    encoding="utf-8",
                )
                tool.chmod(0o755)

            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "PATH": os.fspath(fake_bin),
                        "DYLD_INSERT_LIBRARIES": "/outside/injected.dylib",
                        "LD_PRELOAD": "/outside/preload.so",
                    },
                    clear=False,
                ),
                self.assertRaisesRegex(
                    sidecar_builder.SidecarBuildError,
                    "not a thin little-endian 64-bit Mach-O",
                ),
            ):
                sidecar_builder.verify_staged_artifact(layout)
            self.assertFalse(trace.exists())

    @unittest.skipUnless(sys.platform == "darwin", "Darwin system tooling required")
    def test_required_verification_tools_are_fixed_system_paths(self) -> None:
        with mock.patch.dict(os.environ, {"PATH": "/outside/fake-bin"}, clear=False):
            for name in ("file", "lipo", "otool"):
                with self.subTest(name=name):
                    self.assertEqual(
                        f"/usr/bin/{name}",
                        sidecar_builder._required_tool(name),
                    )
        with self.assertRaisesRegex(
            sidecar_builder.SidecarBuildError,
            "Unsupported Darwin artifact verification tool",
        ):
            sidecar_builder._required_tool("future-tool")

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

    @unittest.skipIf(os.name == "nt", "file-backed process capture requires POSIX")
    def test_post_ready_stdout_and_stderr_leaks_fail_closed(self) -> None:
        cases = (
            ("credential-stdout", "STARBRIDGE_SESSION_TOKEN", "1", "literal"),
            ("credential-stderr", "STARBRIDGE_SESSION_TOKEN", "2", "literal"),
            ("path-stdout", "STARBRIDGE_APP_DATA_DIR", "1", "literal"),
            ("path-stderr", "STARBRIDGE_APP_DATA_DIR", "2", "literal"),
            ("json-path-stdout", "STARBRIDGE_APP_DATA_DIR", "1", "json"),
            ("json-path-stderr", "STARBRIDGE_APP_DATA_DIR", "2", "json"),
            ("form-path-stdout", "STARBRIDGE_APP_DATA_DIR", "1", "form"),
            ("form-path-stderr", "STARBRIDGE_APP_DATA_DIR", "2", "form"),
        )
        for stage, variable, descriptor, encoding in cases:
            with (
                self.subTest(stage=stage),
                tempfile.TemporaryDirectory(prefix="KORYAO post-ready leak ") as temporary,
            ):
                root = Path(temporary)
                data_root = root / "private app data"
                data_root.mkdir()
                credential = "post-ready-secret-token"
                fixture = root / f"{stage}.sh"
                leak_command = f"printf '%s\\n' \"${variable}\""
                if encoding == "json":
                    leak_command += " | /usr/bin/sed 's#/#\\\\/#g'"
                elif encoding == "form":
                    leak_command = (
                        "/usr/bin/python3 -I -c "
                        "'import os, urllib.parse; "
                        f'print(urllib.parse.quote_plus(os.environ["{variable}"], safe=""))\''
                    )
                fixture.write_text(
                    "#!/bin/sh\n"
                    "printf 'STARBRIDGE_READY "
                    '{"host":"127.0.0.1","port":4567,"pid":%s,'
                    '"session_required":true}\\n\' "$$"\n'
                    "/bin/sleep 1\n"
                    f"{leak_command} >&{descriptor}\n",
                    encoding="utf-8",
                )
                fixture.chmod(0o755)
                audit = sidecar_tester.OutputAudit()
                audit.register(
                    credentials=(credential,),
                    private_paths=(root, data_root),
                )
                capture = sidecar_tester._spawn_sidecar(
                    fixture,
                    data_root,
                    credential=credential,
                    parent_pid=os.getpid(),
                    capture_root=root,
                    stage=stage,
                )
                try:
                    ready = sidecar_tester._wait_ready(
                        capture,
                        audit=audit,
                        timeout=10,
                        stage=stage,
                    )
                    self.assertEqual(4567, ready["port"])
                    self.assertEqual(0, capture.process.wait(timeout=5))
                    with self.assertRaises(sidecar_tester.SidecarTestError) as raised:
                        sidecar_tester._audit_captured_process(capture, audit)
                    message = str(raised.exception)
                    self.assertNotIn(credential, message)
                    self.assertNotIn(os.fspath(data_root), message)
                finally:
                    sidecar_tester._terminate(capture)

    def test_output_audit_bounded_path_canonicalization_and_near_misses(self) -> None:
        with tempfile.TemporaryDirectory(prefix="KORYAO encoded path audit ") as temporary:
            private_path = Path(temporary) / "private app data"
            raw = os.fspath(private_path.resolve())
            uri = private_path.resolve().as_uri()
            fully_encoded_uri = sidecar_tester.urllib.parse.quote(uri, safe="")
            encoded_raw = sidecar_tester.urllib.parse.quote(raw, safe="")
            double_encoded_raw = sidecar_tester.urllib.parse.quote(encoded_raw, safe="")
            leaks = (
                raw,
                uri,
                sidecar_tester.urllib.parse.quote_plus(raw, safe=""),
                sidecar_tester.urllib.parse.quote_plus(uri, safe=""),
                fully_encoded_uri,
                double_encoded_raw,
                sidecar_tester.PERCENT_ESCAPE_PATTERN.sub(
                    lambda match: match.group(0).lower(),
                    fully_encoded_uri,
                ),
                sidecar_tester.PERCENT_ESCAPE_PATTERN.sub(
                    lambda match: match.group(0).lower(),
                    double_encoded_raw,
                ),
                raw.replace("/", r"\/"),
                sidecar_tester.urllib.parse.quote(
                    raw.replace("/", r"\/"),
                    safe="",
                ),
            )
            for leak in leaks:
                with self.subTest(leak=leak):
                    audit = sidecar_tester.OutputAudit()
                    audit.register(private_paths=(private_path,))
                    with self.assertRaises(sidecar_tester.SidecarTestError) as raised:
                        audit.inspect({"nested": ["safe", leak]}, stage="encoded fixture")
                    self.assertTrue(audit.controlled_path_exposed)
                    self.assertNotIn(leak, str(raised.exception))

            near_miss = raw[:-1] + ("x" if raw[-1] != "x" else "y")
            harmless_values = (
                "progress=100% and malformed=%ZZ%2",
                private_path.name,
                sidecar_tester.urllib.parse.quote(
                    sidecar_tester.urllib.parse.quote(
                        "https://example.invalid/public/resource",
                        safe="",
                    ),
                    safe="",
                ),
                sidecar_tester.urllib.parse.quote(
                    sidecar_tester.urllib.parse.quote(near_miss, safe=""),
                    safe="",
                ),
                raw.replace(" ", "+"),
                "compiler=C%2B%2B+status",
                "ratio%2Funit+unchanged",
                sidecar_tester.urllib.parse.quote_plus(near_miss, safe=""),
            )
            audit = sidecar_tester.OutputAudit()
            audit.register(private_paths=(private_path,))
            for harmless in harmless_values:
                with self.subTest(harmless=harmless):
                    audit.inspect(harmless, stage="near-miss fixture")
            self.assertFalse(audit.controlled_path_exposed)

            deeply_encoded = raw
            for _ in range(sidecar_tester.MAX_PATH_PERCENT_DECODE_ROUNDS + 3):
                deeply_encoded = sidecar_tester.urllib.parse.quote(deeply_encoded, safe="")
            self.assertLessEqual(
                len(sidecar_tester._path_text_views(deeply_encoded)),
                sidecar_tester.MAX_PATH_PERCENT_DECODE_ROUNDS + 1,
            )

    def test_form_decoder_recognizes_windows_absolute_path_tokens_cross_platform(self) -> None:
        raw_paths = (
            r"C:\Users\<USER_HOME>\App Data\private+proof.png",
            r"\\server\share\Private Data\private+proof.png",
            "file:///C:/Users/<USER_HOME>/App Data/private+proof.png",
        )
        for raw in raw_paths:
            encoded = sidecar_tester.urllib.parse.quote_plus(raw, safe="")
            double_encoded = sidecar_tester.urllib.parse.quote(encoded, safe="")
            encoded_drive = "%43" + encoded[1:] if raw.startswith("C:") else encoded
            lowercase_encoded = sidecar_tester.PERCENT_ESCAPE_PATTERN.sub(
                lambda match: match.group(0).lower(),
                encoded,
            )
            for candidate in (
                encoded,
                double_encoded,
                encoded_drive,
                lowercase_encoded,
            ):
                with self.subTest(raw=raw, candidate=candidate):
                    self.assertIn(raw, sidecar_tester._path_text_views(candidate))

        partially_encoded = {
            r"C:\Users\<USER_HOME>\Private Data": (
                r"C:%5CUsers%5C%3CUSER_HOME%3E%5CPrivate+Data",
                r"C%3A\Users\%3CUSER_HOME%3E\Private+Data",
            ),
            "file:///C:/Users/<USER_HOME>/Private Data": (
                "file:%2F%2F/%43%3A/Users/%3CUSER_HOME%3E/Private+Data",
            ),
            r"\\server\share\Private Data": (
                r"%5C\server\share\Private+Data",
                r"\%5Cserver\share\Private+Data",
            ),
        }
        for raw, candidates in partially_encoded.items():
            for candidate in candidates:
                with self.subTest(raw=raw, candidate=candidate):
                    self.assertIn(raw, sidecar_tester._path_text_views(candidate))

        harmless = "compiler=C%2B%2B+status"
        self.assertNotIn(
            "compiler=C++ status",
            sidecar_tester._path_text_views(harmless),
        )

    def test_output_audit_form_urlencoded_paths_preserve_real_plus(self) -> None:
        with tempfile.TemporaryDirectory(prefix="stage4-quote-plus-") as temporary:
            root = Path(temporary).resolve()
            path_with_spaces = root / "private app data" / "source proof.png"
            path_with_pluses = root / "private+app+data" / "source+proof.png"
            mixed_path = root / "private + app data" / "source+ proof.png"

            mixed_raw = os.fspath(mixed_path)
            mixed_uri = mixed_path.as_uri()
            form_raw = sidecar_tester.urllib.parse.quote_plus(mixed_raw, safe="")
            form_uri = sidecar_tester.urllib.parse.quote_plus(mixed_uri, safe="")
            form_unquoted_uri = sidecar_tester.urllib.parse.quote_plus(
                sidecar_tester.urllib.parse.unquote(mixed_uri),
                safe="",
            )
            form_json_slashes = sidecar_tester.urllib.parse.quote_plus(
                mixed_raw.replace("/", r"\/"),
                safe="",
            )
            double_encoded_form = sidecar_tester.urllib.parse.quote(form_raw, safe="")
            leaks = (
                form_raw,
                form_uri,
                form_unquoted_uri,
                form_json_slashes,
                double_encoded_form,
                sidecar_tester.PERCENT_ESCAPE_PATTERN.sub(
                    lambda match: match.group(0).lower(),
                    form_raw,
                ),
            )
            for leak in leaks:
                with self.subTest(leak=leak):
                    audit = sidecar_tester.OutputAudit()
                    audit.register(private_paths=(mixed_path,))
                    with self.assertRaises(sidecar_tester.SidecarTestError):
                        audit.inspect(leak, stage="form-urlencoded fixture")
                    self.assertTrue(audit.controlled_path_exposed)

            encoded_real_pluses = sidecar_tester.urllib.parse.quote_plus(
                os.fspath(path_with_pluses),
                safe="",
            )
            self.assertIn("%2B", encoded_real_pluses)
            plus_audit = sidecar_tester.OutputAudit()
            plus_audit.register(private_paths=(path_with_pluses,))
            with self.assertRaises(sidecar_tester.SidecarTestError):
                plus_audit.inspect(
                    encoded_real_pluses,
                    stage="encoded real-plus fixture",
                )

            space_audit = sidecar_tester.OutputAudit()
            space_audit.register(private_paths=(path_with_spaces,))
            space_near_miss = os.fspath(path_with_spaces)
            space_near_miss = space_near_miss[:-1] + ("x" if space_near_miss[-1] != "x" else "y")
            harmless_values = (
                os.fspath(path_with_spaces).replace(" ", "+"),
                encoded_real_pluses,
                "form=ordinary+words&language=C%2B%2B",
                "public=folder%2Fnot-the-private-path+complete",
                sidecar_tester.urllib.parse.quote_plus(
                    space_near_miss,
                    safe="",
                ),
                (
                    os.fspath(path_with_spaces).replace(" ", "+")
                    + " unrelated=%2Fpublic+encoded+path"
                ),
            )
            for harmless in harmless_values:
                with self.subTest(harmless=harmless):
                    space_audit.inspect(harmless, stage="form-urlencoded near miss")
            self.assertFalse(space_audit.controlled_path_exposed)

            bounded_form = form_raw
            for _ in range(sidecar_tester.MAX_PATH_PERCENT_DECODE_ROUNDS - 1):
                bounded_form = sidecar_tester.urllib.parse.quote(bounded_form, safe="")
            bounded_audit = sidecar_tester.OutputAudit()
            bounded_audit.register(private_paths=(mixed_path,))
            with self.assertRaises(sidecar_tester.SidecarTestError):
                bounded_audit.inspect(bounded_form, stage="bounded form fixture")

            beyond_bound = sidecar_tester.urllib.parse.quote(bounded_form, safe="")
            beyond_audit = sidecar_tester.OutputAudit()
            beyond_audit.register(private_paths=(mixed_path,))
            beyond_audit.inspect(beyond_bound, stage="beyond-bound form fixture")
            self.assertFalse(beyond_audit.controlled_path_exposed)

    def test_every_http_payload_stage_rejects_credentials_and_private_paths(
        self,
    ) -> None:
        stages = (
            "primary health",
            "wrong-credential bootstrap",
            "authenticated bootstrap",
            "initial connections",
            "paired connections",
            "vector selection",
            "vector job start",
            "exact-vector job poll",
            "primary shutdown",
            "released-port health",
            "released-port shutdown",
        )

        class FakeResponse:
            status = 200

            def __init__(self, payload: bytes) -> None:
                self.payload = payload

            def read(self) -> bytes:
                return self.payload

        class FakeConnection:
            def __init__(self, payload: bytes) -> None:
                self.response = FakeResponse(payload)

            def request(self, *_: object, **__: object) -> None:
                return None

            def getresponse(self) -> FakeResponse:
                return self.response

            def close(self) -> None:
                return None

        with tempfile.TemporaryDirectory(prefix="KORYAO HTTP audit ") as temporary:
            private_path = Path(temporary) / "private app data"
            credential = "http-response-secret"
            for stage in stages:
                for leak in (credential, os.fspath(private_path.resolve())):
                    with self.subTest(
                        stage=stage, leak="credential" if leak == credential else "path"
                    ):
                        audit = sidecar_tester.OutputAudit()
                        audit.register(
                            credentials=(credential,),
                            private_paths=(private_path,),
                        )
                        payload = json.dumps({"leak": leak}).encode("utf-8")
                        with (
                            mock.patch.object(
                                sidecar_tester.http.client,
                                "HTTPConnection",
                                return_value=FakeConnection(payload),
                            ),
                            self.assertRaises(sidecar_tester.SidecarTestError) as raised,
                        ):
                            sidecar_tester._expect_status(
                                4567,
                                "GET",
                                "/fixture",
                                200,
                                audit=audit,
                                stage=stage,
                            )
                        self.assertNotIn(leak, str(raised.exception))

            raw_private_path = os.fspath(private_path.resolve())
            private_uri = private_path.resolve().as_uri()
            fully_encoded_uri = sidecar_tester.urllib.parse.quote(private_uri, safe="")
            double_encoded_raw = sidecar_tester.urllib.parse.quote(
                sidecar_tester.urllib.parse.quote(raw_private_path, safe=""),
                safe="",
            )
            encoded_leaks = (
                private_uri,
                sidecar_tester.urllib.parse.quote(
                    raw_private_path,
                    safe="/:",
                ),
                sidecar_tester.urllib.parse.quote(
                    raw_private_path,
                    safe="",
                ),
                sidecar_tester.urllib.parse.quote_plus(
                    raw_private_path,
                    safe="",
                ),
                sidecar_tester.urllib.parse.quote_plus(
                    private_uri,
                    safe="",
                ),
                raw_private_path.replace("/", r"\/"),
                fully_encoded_uri,
                double_encoded_raw,
                sidecar_tester.urllib.parse.quote(
                    raw_private_path.replace("/", r"\/"),
                    safe="",
                ),
                sidecar_tester.PERCENT_ESCAPE_PATTERN.sub(
                    lambda match: match.group(0).lower(),
                    fully_encoded_uri,
                ),
                sidecar_tester.PERCENT_ESCAPE_PATTERN.sub(
                    lambda match: match.group(0).lower(),
                    double_encoded_raw,
                ),
            )
            for leak in encoded_leaks:
                audit = sidecar_tester.OutputAudit()
                audit.register(private_paths=(private_path,))
                payload = json.dumps({"leak": leak}).encode("utf-8")
                with (
                    mock.patch.object(
                        sidecar_tester.http.client,
                        "HTTPConnection",
                        return_value=FakeConnection(payload),
                    ),
                    self.assertRaises(sidecar_tester.SidecarTestError),
                ):
                    sidecar_tester._expect_status(
                        4567,
                        "GET",
                        "/fixture",
                        200,
                        audit=audit,
                        stage="encoded path fixture",
                    )

            audit = sidecar_tester.OutputAudit()
            audit.register(credentials=(credential,))
            with (
                mock.patch.object(
                    sidecar_tester.http.client,
                    "HTTPConnection",
                    return_value=FakeConnection(f"not-json {credential}".encode()),
                ),
                self.assertRaisesRegex(
                    sidecar_tester.SidecarTestError,
                    "session credential",
                ),
            ):
                sidecar_tester._expect_status(
                    4567,
                    "GET",
                    "/fixture",
                    200,
                    audit=audit,
                    stage="non-JSON fixture",
                )

    def test_mcp_pairing_audits_complete_stdout_and_stderr(self) -> None:
        response = '{"jsonrpc":"2.0","id":1,"result":{"structuredContent":{"ok":true}}}\n'
        with tempfile.TemporaryDirectory(prefix="KORYAO MCP audit ") as temporary:
            data_root = Path(temporary) / "private app data"
            credential = "mcp-output-secret"
            cases = (
                (response + credential, "", credential),
                (response, os.fspath(data_root.resolve()), os.fspath(data_root.resolve())),
            )
            for stdout, stderr, leaked in cases:
                with self.subTest(channel="stdout" if stderr == "" else "stderr"):
                    audit = sidecar_tester.OutputAudit()
                    audit.register(
                        credentials=(credential,),
                        private_paths=(data_root,),
                    )
                    completed = subprocess.CompletedProcess(
                        ["/fixture", "--mcp"],
                        0,
                        stdout,
                        stderr,
                    )
                    with (
                        mock.patch.object(
                            sidecar_tester.subprocess,
                            "run",
                            return_value=completed,
                        ),
                        self.assertRaises(sidecar_tester.SidecarTestError) as raised,
                    ):
                        sidecar_tester._pair_desktop_session(
                            Path("/fixture"),
                            data_root,
                            data_root / "codex home",
                            "PAIRCODE",
                            timeout=1,
                            audit=audit,
                        )
                    self.assertNotIn(leaked, str(raised.exception))

    def test_mcp_pairing_replaces_inherited_runtime_environment(self) -> None:
        response = '{"jsonrpc":"2.0","id":1,"result":{"structuredContent":{"ok":true}}}\n'
        with tempfile.TemporaryDirectory(prefix="KORYAO MCP environment ") as temporary:
            data_root = Path(temporary) / "private app data"
            codex_home = Path(temporary) / "controlled codex home"
            audit = sidecar_tester.OutputAudit()
            completed = subprocess.CompletedProcess(
                ["/fixture", "--mcp"],
                0,
                response,
                "",
            )
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "STARBRIDGE_SESSION_TOKEN": "inherited-secret",
                        "STARBRIDGE_APP_DATA_DIR": "/outside/app-data",
                        "CODEX_HOME": "/outside/codex-home",
                    },
                    clear=False,
                ),
                mock.patch.object(
                    sidecar_tester.subprocess,
                    "run",
                    return_value=completed,
                ) as run,
            ):
                sidecar_tester._pair_desktop_session(
                    Path("/fixture"),
                    data_root,
                    codex_home,
                    "PAIRCODE",
                    timeout=1,
                    audit=audit,
                )

            environment = run.call_args.kwargs["env"]
            self.assertNotIn("STARBRIDGE_SESSION_TOKEN", environment)
            self.assertEqual(os.fspath(data_root), environment["STARBRIDGE_APP_DATA_DIR"])
            self.assertEqual(os.fspath(codex_home), environment["CODEX_HOME"])

    def test_mcp_timeout_output_is_audited_without_error_echo(self) -> None:
        with tempfile.TemporaryDirectory(prefix="KORYAO MCP timeout ") as temporary:
            data_root = Path(temporary) / "private app data"
            credential = "mcp-timeout-secret"
            cases: tuple[tuple[str | bytes, str | bytes, str], ...] = (
                (credential.encode(), b"", credential),
                (b"", os.fspath(data_root.resolve()), os.fspath(data_root.resolve())),
            )
            for stdout, stderr, leaked in cases:
                with self.subTest(channel="stdout" if stdout else "stderr"):
                    audit = sidecar_tester.OutputAudit()
                    audit.register(
                        credentials=(credential,),
                        private_paths=(data_root,),
                    )
                    timeout_error = subprocess.TimeoutExpired(
                        cmd=["/fixture", "--mcp"],
                        timeout=1,
                        output=stdout,
                        stderr=stderr,
                    )
                    with (
                        mock.patch.object(
                            sidecar_tester.subprocess,
                            "run",
                            side_effect=timeout_error,
                        ),
                        self.assertRaises(sidecar_tester.SidecarTestError) as raised,
                    ):
                        sidecar_tester._pair_desktop_session(
                            Path("/fixture"),
                            data_root,
                            data_root / "codex home",
                            "PAIRCODE",
                            timeout=1,
                            audit=audit,
                        )
                    self.assertNotIn(leaked, str(raised.exception))
                    self.assertTrue(audit.credential_exposed or audit.controlled_path_exposed)

    def test_parent_exit_audits_child_logs_after_process_exit(self) -> None:
        credential = "parent-child-secret"
        with tempfile.TemporaryDirectory(prefix="KORYAO parent log audit ") as temporary:
            temporary_root = Path(temporary)

            def fake_run(arguments: object, **_: object) -> subprocess.CompletedProcess[str]:
                command = list(arguments)  # type: ignore[arg-type]
                data_root = Path(command[command.index("--data-root") + 1])
                probe_file = Path(command[command.index("--probe-file") + 1])
                data_root.mkdir(parents=True)
                (data_root / "parent child stdout.log").write_text(
                    credential,
                    encoding="utf-8",
                )
                (data_root / "parent child stderr.log").write_text("", encoding="utf-8")
                probe_file.write_text(
                    json.dumps({"pid": 424242, "port": 4567}),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            audit = sidecar_tester.OutputAudit()
            with (
                mock.patch.object(
                    sidecar_tester.secrets,
                    "token_hex",
                    return_value=credential,
                ),
                mock.patch.object(
                    sidecar_tester.subprocess,
                    "run",
                    side_effect=fake_run,
                ),
                mock.patch.object(sidecar_tester, "_process_exists", return_value=False),
                mock.patch.object(sidecar_tester, "_assert_port_released"),
                self.assertRaises(sidecar_tester.SidecarTestError) as raised,
            ):
                sidecar_tester._verify_parent_exit(
                    Path("/fixture"),
                    temporary_root,
                    startup_timeout=1,
                    audit=audit,
                )
            self.assertNotIn(credential, str(raised.exception))

    def test_parent_exit_timeout_output_is_audited_without_command_echo(self) -> None:
        credential = "parent-timeout-secret"
        with tempfile.TemporaryDirectory(prefix="KORYAO parent timeout ") as temporary:
            temporary_root = Path(temporary)
            private_path = temporary_root / "parent exit app data"
            cases: tuple[tuple[str | bytes, str | bytes, str], ...] = (
                (credential, "", credential),
                ("", os.fspath(private_path.resolve()), os.fspath(private_path.resolve())),
            )
            for stdout, stderr, leaked in cases:
                with self.subTest(channel="stdout" if stdout else "stderr"):
                    audit = sidecar_tester.OutputAudit()
                    timeout_error = subprocess.TimeoutExpired(
                        cmd=[
                            "/private/python",
                            "--data-root",
                            os.fspath(private_path),
                        ],
                        timeout=1,
                        output=stdout,
                        stderr=stderr,
                    )
                    with (
                        mock.patch.object(
                            sidecar_tester.secrets,
                            "token_hex",
                            return_value=credential,
                        ),
                        mock.patch.object(
                            sidecar_tester.subprocess,
                            "run",
                            side_effect=timeout_error,
                        ),
                        self.assertRaises(sidecar_tester.SidecarTestError) as raised,
                    ):
                        sidecar_tester._verify_parent_exit(
                            Path("/fixture"),
                            temporary_root,
                            startup_timeout=1,
                            audit=audit,
                        )
                    message = str(raised.exception)
                    self.assertNotIn(leaked, message)
                    self.assertNotIn(os.fspath(temporary_root), message)
                    self.assertTrue(audit.credential_exposed or audit.controlled_path_exposed)

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
                audit = sidecar_tester.OutputAudit()
                audit.register(private_paths=(data_root,))
                sidecar_tester._verify_exact_svg(
                    4567,
                    "credential",
                    data_root,
                    timeout=1,
                    audit=audit,
                )

    @unittest.skipIf(os.name == "nt", "POSIX wrapper checks do not run on Windows")
    def test_arm_and_x86_plan_paths_preserve_argument_boundaries(self) -> None:
        with tempfile.TemporaryDirectory(prefix="KORYAO wrapper plans ") as temporary:
            scripts = self.make_posix_wrapper_fixture(Path(temporary) / "repo")
            build_wrapper = scripts / "Build-Sidecar.sh"
            test_wrapper = scripts / "Test-Sidecar.sh"
            for target in ("aarch64-apple-darwin", "x86_64-apple-darwin"):
                with self.subTest(target=target):
                    build = subprocess.run(
                        [
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
                    [script, *arguments],
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                )
                self.assertNotEqual(0, completed.returncode)


if __name__ == "__main__":
    unittest.main()
