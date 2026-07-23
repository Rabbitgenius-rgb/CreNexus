from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - imported by cross-platform unit tests
    fcntl = None  # type: ignore[assignment]

SUPPORTED_DARWIN_TARGETS = {
    "aarch64-apple-darwin",
    "x86_64-apple-darwin",
}
TARGET_TRIPLE_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
COLLISION_COPY_PATTERN = re.compile(r" \d+(?=(?:\.[^.]+)?$)")
VECTOR60_RUNTIME_VERSIONS = {
    "vtracer": "0.6.15",
    "skia-pathops": "0.9.2",
    "svgpathtools": "1.7.2",
}
PIP_NETWORK_ENVIRONMENT_ALLOWLIST = frozenset(
    {
        "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL",
        "PIP_NO_INDEX",
        "PIP_FIND_LINKS",
        "PIP_TRUSTED_HOST",
        "PIP_PROXY",
        "PIP_CERT",
        "PIP_CLIENT_CERT",
        "PIP_TIMEOUT",
        "PIP_DEFAULT_TIMEOUT",
        "PIP_RETRIES",
        "PIP_RESUME_RETRIES",
        "PIP_DISABLE_PIP_VERSION_CHECK",
        "PIP_NO_INPUT",
        "PIP_KEYRING_PROVIDER",
        "PIP_NETRC",
        "PIP_REQUIRE_VIRTUALENV",
        "PIP_REQUIRE_VENV",
    }
)
EXPECTED_DARWIN_ARCHITECTURE = {
    "aarch64-apple-darwin": "arm64",
    "x86_64-apple-darwin": "x86_64",
}
DARWIN_NATIVE_SUFFIXES = (".so", ".dylib")
SANITIZED_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
TRUSTED_DARWIN_TOOLS = {
    "file": Path("/usr/bin/file"),
    "lipo": Path("/usr/bin/lipo"),
    "otool": Path("/usr/bin/otool"),
}
ENVIRONMENT_PASSTHROUGH_ALLOWLIST = frozenset(
    {
        "ALL_PROXY",
        "CURL_CA_BUNDLE",
        "FTP_PROXY",
        "HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LANGUAGE",
        "LC_ADDRESS",
        "LC_ALL",
        "LC_COLLATE",
        "LC_CTYPE",
        "LC_IDENTIFICATION",
        "LC_MEASUREMENT",
        "LC_MESSAGES",
        "LC_MONETARY",
        "LC_NAME",
        "LC_NUMERIC",
        "LC_PAPER",
        "LC_TELEPHONE",
        "LC_TIME",
        "LOGNAME",
        "NO_PROXY",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
        "USER",
        "all_proxy",
        "ftp_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
MACHO_CPU_TYPES = {
    "arm64": 0x0100000C,
    "x86_64": 0x01000007,
}
MACHO_MAGICS = frozenset(
    {
        b"\xfe\xed\xfa\xce",
        b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xcf\xfa\xed\xfe",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
        b"\xca\xfe\xba\xbf",
        b"\xbf\xba\xfe\xca",
    }
)


class SidecarBuildError(RuntimeError):
    pass


@dataclass(frozen=True)
class BuildLayout:
    repo_root: Path
    desktop_root: Path
    target_triple: str
    build_environment: Path
    build_root: Path
    dist_root: Path
    work_root: Path
    pyinstaller_config_root: Path
    source_folder: Path
    source_executable: Path
    contents_directory_name: str
    source_support_directory: Path
    binaries_root: Path
    staged_executable: Path
    staged_support_directory: Path


def validate_target_triple(value: str) -> str:
    target_triple = value.strip()
    if not target_triple or not TARGET_TRIPLE_PATTERN.fullmatch(target_triple):
        raise SidecarBuildError("Target triple contains unsupported characters.")
    if target_triple not in SUPPORTED_DARWIN_TARGETS:
        supported = ", ".join(sorted(SUPPORTED_DARWIN_TARGETS))
        raise SidecarBuildError(
            f"Unsupported Darwin target triple: {target_triple}. Supported values: {supported}."
        )
    return target_triple


def _platform_target(system: str, machine: str) -> str:
    if system.lower() != "darwin":
        raise SidecarBuildError(
            "The POSIX sidecar builder only supports Darwin hosts. "
            "Use Build-Sidecar.ps1 on Windows."
        )
    normalized = machine.lower()
    if normalized in {"arm64", "aarch64"}:
        return "aarch64-apple-darwin"
    if normalized in {"x86_64", "amd64"}:
        return "x86_64-apple-darwin"
    raise SidecarBuildError(f"Unsupported Darwin host architecture: {machine}.")


def detect_host_target() -> str:
    return _platform_target(platform.system(), platform.machine())


def resolve_target_triple(
    explicit: str | None,
    environment: Mapping[str, str] | None = None,
) -> str:
    env = os.environ if environment is None else environment
    candidate = explicit or env.get("CARGO_BUILD_TARGET")
    if candidate:
        return validate_target_triple(candidate)
    return detect_host_target()


def sanitized_environment(
    environment: Mapping[str, str] | None = None,
    *,
    for_pip: bool = False,
) -> dict[str, str]:
    source = os.environ if environment is None else environment
    sanitized: dict[str, str] = {}
    for key, value in source.items():
        normalized = key.upper()
        if key in ENVIRONMENT_PASSTHROUGH_ALLOWLIST:
            sanitized[key] = value
        elif for_pip and key == normalized and normalized in PIP_NETWORK_ENVIRONMENT_ALLOWLIST:
            sanitized[normalized] = value
    sanitized["PATH"] = SANITIZED_PATH
    if for_pip:
        # Match bootstrap.sh: disable every pip config file while preserving only
        # network/authentication settings needed by legitimate managed networks.
        # pip --isolated is deliberately not used because it would discard those
        # narrowly allowed proxy, certificate, and index settings.
        sanitized["PIP_CONFIG_FILE"] = os.devnull
        sanitized["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
        sanitized["PIP_NO_INPUT"] = "1"
    return sanitized


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def layout_for(repo_root: Path, target_triple: str) -> BuildLayout:
    target = validate_target_triple(target_triple)
    # Do not resolve here: resolving would erase evidence that an authorized
    # repository or one of its write roots was replaced with a symlink.
    root = _absolute(repo_root)
    desktop_root = root / "apps" / "starbridge-desktop"
    build_root = desktop_root / "build" / "sidecar" / target
    dist_root = build_root / "dist"
    contents_name = f"_internal-{target}"
    source_folder = dist_root / "starbridge-sidecar"
    binaries_root = desktop_root / "src-tauri" / "binaries"
    return BuildLayout(
        repo_root=root,
        desktop_root=desktop_root,
        target_triple=target,
        build_environment=root / ".venv-build" / "sidecar" / target,
        build_root=build_root,
        dist_root=dist_root,
        work_root=build_root / "work",
        pyinstaller_config_root=build_root / "pyinstaller-config",
        source_folder=source_folder,
        source_executable=source_folder / "starbridge-sidecar",
        contents_directory_name=contents_name,
        source_support_directory=source_folder / contents_name,
        binaries_root=binaries_root,
        staged_executable=binaries_root / f"starbridge-sidecar-{target}",
        staged_support_directory=binaries_root / contents_name,
    )


def _relative(path: Path, root: Path) -> str:
    try:
        return _absolute(path).relative_to(_absolute(root)).as_posix()
    except ValueError as exc:
        raise SidecarBuildError(
            "Refusing to describe a path outside the authorized repository."
        ) from exc


def _assert_lexically_within(path: Path, root: Path) -> None:
    try:
        _absolute(path).relative_to(_absolute(root))
    except ValueError as exc:
        raise SidecarBuildError(
            "Refusing a filesystem operation outside the authorized build directory."
        ) from exc


def _physical_repo_root(repo_root: Path) -> Path:
    root = _absolute(repo_root)
    try:
        metadata = os.lstat(root)
    except FileNotFoundError as exc:
        raise SidecarBuildError("The repository root does not exist.") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise SidecarBuildError("Refusing to use a symlinked repository root.")
    if not stat.S_ISDIR(metadata.st_mode):
        raise SidecarBuildError("The repository root is not a directory.")
    return root.resolve(strict=True)


def _validate_path_chain(
    path: Path,
    repo_root: Path,
    *,
    kind: str,
    allow_missing: bool,
    label: str,
) -> bool:
    root = _absolute(repo_root)
    target = _absolute(path)
    _assert_lexically_within(target, root)
    physical_root = _physical_repo_root(root)
    relative = target.relative_to(root)
    current = root
    target_exists = True
    for index, component in enumerate(relative.parts):
        current /= component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError as exc:
            target_exists = False
            if not allow_missing:
                raise SidecarBuildError(f"{label} does not exist.") from exc
            break
        if stat.S_ISLNK(metadata.st_mode):
            raise SidecarBuildError(f"Refusing a symlinked {label}.")
        final = index == len(relative.parts) - 1
        if not final and not stat.S_ISDIR(metadata.st_mode):
            raise SidecarBuildError(f"{label} has a non-directory parent component.")
        if final:
            if kind == "directory" and not stat.S_ISDIR(metadata.st_mode):
                raise SidecarBuildError(f"{label} is not a directory.")
            if kind == "file" and not stat.S_ISREG(metadata.st_mode):
                raise SidecarBuildError(f"{label} is not a regular file.")
        try:
            current.resolve(strict=True).relative_to(physical_root)
        except ValueError as exc:
            raise SidecarBuildError(f"{label} is physically outside the repository.") from exc
    return target_exists


def _looks_like_numbered_copy(name: str, canonical_name: str) -> bool:
    if not name.startswith(canonical_name):
        return False
    suffix = name[len(canonical_name) :]
    return re.fullmatch(r" \d+(?:\.[^/]+)?", suffix) is not None


def _reject_target_collision_copies(layout: BuildLayout) -> None:
    families = (
        (
            layout.binaries_root,
            (layout.staged_executable.name, layout.staged_support_directory.name),
        ),
        (layout.build_root.parent, (layout.target_triple,)),
        (layout.build_environment.parent, (layout.target_triple,)),
    )
    for parent, canonical_names in families:
        if not parent.exists():
            continue
        _validate_path_chain(
            parent,
            layout.repo_root,
            kind="directory",
            allow_missing=False,
            label="target artifact family directory",
        )
        for child in parent.iterdir():
            if any(
                _looks_like_numbered_copy(child.name, canonical_name)
                for canonical_name in canonical_names
            ):
                raise SidecarBuildError(
                    "A numbered collision-copy exists beside a target-specific "
                    "sidecar artifact; refusing to build or verify."
                )


def _validate_layout_roots(layout: BuildLayout) -> None:
    required_directories = (
        (layout.repo_root / "apps", "apps directory"),
        (layout.desktop_root, "desktop root"),
        (layout.desktop_root / "scripts", "desktop scripts directory"),
        (layout.desktop_root / "src-tauri", "Tauri source directory"),
        (layout.binaries_root, "Tauri binaries directory"),
    )
    for path, label in required_directories:
        _validate_path_chain(
            path,
            layout.repo_root,
            kind="directory",
            allow_missing=False,
            label=label,
        )

    optional_directories = (
        (layout.repo_root / ".venv-build", "sidecar environment root"),
        (layout.repo_root / ".venv-build" / "sidecar", "sidecar environment family"),
        (layout.build_environment, "target sidecar environment"),
        (layout.desktop_root / "build", "desktop build root"),
        (layout.desktop_root / "build" / "sidecar", "sidecar build family"),
        (layout.build_root, "target sidecar build root"),
        (layout.dist_root, "target sidecar dist root"),
        (layout.work_root, "target sidecar work root"),
        (layout.pyinstaller_config_root, "target PyInstaller config root"),
        (layout.source_folder, "PyInstaller output directory"),
        (layout.source_support_directory, "PyInstaller support directory"),
        (layout.staged_support_directory, "staged support directory"),
    )
    for path, label in optional_directories:
        _validate_path_chain(
            path,
            layout.repo_root,
            kind="directory",
            allow_missing=True,
            label=label,
        )

    required_files = (
        (layout.repo_root / "pyproject.toml", "project metadata"),
        (
            layout.desktop_root / "scripts" / "requirements-sidecar-build.txt",
            "sidecar build requirements",
        ),
        (
            layout.desktop_root / "scripts" / "sidecar_entry.py",
            "sidecar entry point",
        ),
        (
            layout.desktop_root / "scripts" / "sidecar_builder.py",
            "Darwin sidecar builder",
        ),
        (
            layout.desktop_root / "scripts" / "sidecar_tester.py",
            "Darwin sidecar tester",
        ),
        (
            layout.desktop_root / "scripts" / "sidecar_environment.sh",
            "Darwin sidecar environment wrapper",
        ),
        (
            layout.desktop_root / "scripts" / "starbridge-sidecar.spec",
            "PyInstaller specification",
        ),
    )
    for path, label in required_files:
        _validate_path_chain(
            path,
            layout.repo_root,
            kind="file",
            allow_missing=False,
            label=label,
        )

    for path, label in (
        (layout.source_executable, "PyInstaller sidecar executable"),
        (layout.staged_executable, "staged sidecar executable"),
    ):
        _validate_path_chain(
            path,
            layout.repo_root,
            kind="file",
            allow_missing=True,
            label=label,
        )
    _reject_target_collision_copies(layout)


@contextlib.contextmanager
def _target_build_lock(layout: BuildLayout):
    if fcntl is None:
        raise SidecarBuildError("Darwin sidecar locking requires POSIX fcntl support.")
    _validate_layout_roots(layout)
    _validate_path_chain(
        layout.build_root,
        layout.repo_root,
        kind="directory",
        allow_missing=False,
        label="target sidecar build root",
    )
    lock_path = layout.build_root / ".build-and-stage.lock"
    _validate_path_chain(
        lock_path,
        layout.repo_root,
        kind="file",
        allow_missing=True,
        label="target sidecar build lock",
    )
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SidecarBuildError("The target sidecar build lock is not a regular file.")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        _validate_path_chain(
            lock_path,
            layout.repo_root,
            kind="file",
            allow_missing=False,
            label="target sidecar build lock",
        )
        path_metadata = os.stat(lock_path, follow_symlinks=False)
        if (
            not stat.S_ISREG(path_metadata.st_mode)
            or path_metadata.st_dev != metadata.st_dev
            or path_metadata.st_ino != metadata.st_ino
        ):
            raise SidecarBuildError(
                "The target sidecar build lock changed while it was being acquired."
            )
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _safe_mkdir(path: Path, layout: BuildLayout) -> None:
    _validate_layout_roots(layout)
    _validate_path_chain(
        path,
        layout.repo_root,
        kind="directory",
        allow_missing=True,
        label="sidecar build directory",
    )
    path.mkdir(parents=True, exist_ok=True)
    _validate_path_chain(
        path,
        layout.repo_root,
        kind="directory",
        allow_missing=False,
        label="sidecar build directory",
    )


def _run(
    arguments: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path | None = None,
    environment: Mapping[str, str] | None = None,
    capture_output: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            [os.fspath(argument) for argument in arguments],
            cwd=cwd,
            env=None if environment is None else dict(environment),
            check=False,
            capture_output=capture_output,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise SidecarBuildError("Command timed out.") from None
    except OSError:
        raise SidecarBuildError("Command could not be started.") from None
    if completed.returncode != 0:
        detail = ""
        if capture_output:
            detail = (completed.stderr or completed.stdout).strip()
            if detail:
                detail = f" ({detail.splitlines()[-1]})"
        raise SidecarBuildError(f"Command failed with exit code {completed.returncode}{detail}.")
    return completed


def _build_python(layout: BuildLayout) -> Path:
    return layout.build_environment / "bin" / "python"


def _ensure_supported_python() -> None:
    if not ((3, 10) <= sys.version_info[:2] <= (3, 13)):
        raise SidecarBuildError(
            "Sidecar builds require Python 3.10 through 3.13. "
            "Run bootstrap.sh first or install a supported python3 interpreter."
        )


def _prepare_environment(
    layout: BuildLayout,
    *,
    skip_dependency_install: bool,
) -> Path:
    _ensure_supported_python()
    build_python = _build_python(layout)
    _validate_layout_roots(layout)
    if not build_python.is_file():
        if skip_dependency_install:
            raise SidecarBuildError(
                "The target-isolated build environment is missing. "
                "Run again without --skip-dependency-install."
            )
        _safe_mkdir(layout.build_environment.parent, layout)
        _validate_layout_roots(layout)
        _run(
            [sys.executable, "-I", "-m", "venv", layout.build_environment],
            environment=sanitized_environment(),
        )
        _validate_layout_roots(layout)

    requirements = layout.desktop_root / "scripts" / "requirements-sidecar-build.txt"
    if not skip_dependency_install:
        pip_environment = sanitized_environment(for_pip=True)
        _run(
            [
                build_python,
                "-I",
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "-r",
                requirements,
            ],
            environment=pip_environment,
        )
        _run(
            [
                build_python,
                "-I",
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--no-deps",
                "-e",
                layout.repo_root,
            ],
            environment=pip_environment,
        )

    python_environment = sanitized_environment()
    _run(
        [build_python, "-I", "-c", "import PyInstaller"],
        environment=python_environment,
        capture_output=True,
    )
    runtime_probe = (
        "import importlib.metadata as m; "
        "import pathops, svgpathtools, vtracer; "
        f"expected={VECTOR60_RUNTIME_VERSIONS!r}; "
        "actual={name:m.version(name) for name in expected}; "
        "assert actual == expected, (expected, actual)"
    )
    _run(
        [build_python, "-I", "-c", runtime_probe],
        environment=python_environment,
        capture_output=True,
    )
    return build_python


def check_vector60_runtime(executable: Path) -> dict[str, object]:
    completed = _run(
        [executable, "--vector60-runtime-check"],
        environment=sanitized_environment(),
        capture_output=True,
        timeout=60,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SidecarBuildError(
            "The packaged sidecar returned an invalid Vector60 runtime result."
        ) from exc
    if payload.get("ok") is not True or payload.get("versions") != VECTOR60_RUNTIME_VERSIONS:
        raise SidecarBuildError(
            "The packaged sidecar contains unexpected Vector60 Python runtime versions."
        )
    return payload


def _scan_support_tree(
    directory: Path,
    *,
    allow_internal_file_symlinks: bool = False,
) -> dict[str, int]:
    try:
        root_metadata = os.lstat(directory)
    except FileNotFoundError as exc:
        raise SidecarBuildError("The sidecar support directory is missing.") from exc
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise SidecarBuildError("The sidecar support path must be a real, non-symlinked directory.")

    support_file_count = 0
    native_extension_count = 0
    support_symlink_count = 0
    physical_root = directory.resolve(strict=True)
    for current_root, directory_names, file_names in os.walk(
        directory, topdown=True, followlinks=False
    ):
        current = Path(current_root)
        for name in directory_names:
            if COLLISION_COPY_PATTERN.search(name):
                raise SidecarBuildError(
                    "The sidecar support tree contains a collision-copy directory."
                )
            candidate = current / name
            metadata = os.lstat(candidate)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise SidecarBuildError(
                    "The sidecar support tree contains a directory symlink or non-directory node."
                )
        for name in file_names:
            if COLLISION_COPY_PATTERN.search(name):
                raise SidecarBuildError("The sidecar support tree contains a collision-copy file.")
            candidate = current / name
            metadata = os.lstat(candidate)
            payload_path = candidate
            if stat.S_ISLNK(metadata.st_mode):
                if not allow_internal_file_symlinks:
                    raise SidecarBuildError("The staged sidecar support tree contains a symlink.")
                try:
                    resolved = candidate.resolve(strict=True)
                    resolved.relative_to(physical_root)
                except (FileNotFoundError, ValueError) as exc:
                    raise SidecarBuildError(
                        "The PyInstaller support tree contains an escaping or broken symlink."
                    ) from exc
                if not resolved.is_file():
                    raise SidecarBuildError(
                        "The PyInstaller support symlink does not target a regular file."
                    )
                payload_path = resolved
                support_symlink_count += 1
            elif not stat.S_ISREG(metadata.st_mode):
                raise SidecarBuildError("The sidecar support tree contains a non-regular file.")
            with payload_path.open("rb") as handle:
                if handle.read(2) == b"MZ":
                    raise SidecarBuildError(
                        "The Darwin sidecar support tree contains a Windows executable."
                    )
            support_file_count += 1
            lowered = name.casefold()
            if lowered.endswith((".exe", ".dll")):
                raise SidecarBuildError(
                    "The Darwin sidecar support tree contains a Windows executable."
                )
            if lowered.endswith(DARWIN_NATIVE_SUFFIXES):
                native_extension_count += 1

    if support_file_count == 0:
        raise SidecarBuildError("The sidecar support directory is empty.")
    if native_extension_count == 0:
        raise SidecarBuildError(
            "The sidecar support directory contains no Darwin native extension."
        )
    return {
        "support_file_count": support_file_count,
        "native_extension_count": native_extension_count,
        "support_symlink_count": support_symlink_count,
    }


def _remove_transaction_node(path: Path) -> None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or stat.S_ISREG(metadata.st_mode):
        path.unlink()
        return
    if stat.S_ISDIR(metadata.st_mode):
        shutil.rmtree(path)
        return
    raise SidecarBuildError("A sidecar staging transaction contains an unsafe node.")


def _replace_staged_pair(
    source_executable: Path,
    source_support_directory: Path,
    layout: BuildLayout,
) -> dict[str, object]:
    _validate_layout_roots(layout)
    _validate_path_chain(
        source_executable,
        layout.repo_root,
        kind="file",
        allow_missing=False,
        label="PyInstaller sidecar executable",
    )
    _validate_path_chain(
        source_support_directory,
        layout.repo_root,
        kind="directory",
        allow_missing=False,
        label="PyInstaller support directory",
    )
    _scan_support_tree(
        source_support_directory,
        allow_internal_file_symlinks=True,
    )
    _reject_target_collision_copies(layout)

    transaction_root = Path(
        tempfile.mkdtemp(
            prefix=f".sidecar-stage-{layout.target_triple}-",
            dir=layout.binaries_root,
        )
    )
    candidate_root = transaction_root / "candidate"
    backup_root = transaction_root / "backup"
    candidate_root.mkdir()
    backup_root.mkdir()
    candidate_executable = candidate_root / layout.staged_executable.name
    candidate_support = candidate_root / layout.staged_support_directory.name
    backup_executable = backup_root / layout.staged_executable.name
    backup_support = backup_root / layout.staged_support_directory.name

    candidate_layout = replace(
        layout,
        staged_executable=candidate_executable,
        staged_support_directory=candidate_support,
    )
    moved_previous_executable = False
    moved_previous_support = False
    installed_candidate_executable = False
    installed_candidate_support = False
    cleanup_transaction = True
    try:
        _validate_path_chain(
            transaction_root,
            layout.repo_root,
            kind="directory",
            allow_missing=False,
            label="sidecar staging transaction",
        )
        shutil.copy2(source_executable, candidate_executable)
        candidate_executable.chmod(
            candidate_executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )
        shutil.copytree(source_support_directory, candidate_support, symlinks=True)
        _scan_support_tree(
            candidate_support,
            allow_internal_file_symlinks=True,
        )
        _validate_path_chain(
            candidate_executable,
            layout.repo_root,
            kind="file",
            allow_missing=False,
            label="candidate staged sidecar executable",
        )
        _validate_path_chain(
            candidate_support,
            layout.repo_root,
            kind="directory",
            allow_missing=False,
            label="candidate staged support directory",
        )
        # Validate the complete pair before any canonical artifact is moved.
        verify_staged_artifact(candidate_layout)

        if layout.staged_executable.exists():
            layout.staged_executable.rename(backup_executable)
            moved_previous_executable = True
        if layout.staged_support_directory.exists():
            layout.staged_support_directory.rename(backup_support)
            moved_previous_support = True
        candidate_executable.rename(layout.staged_executable)
        installed_candidate_executable = True
        candidate_support.rename(layout.staged_support_directory)
        installed_candidate_support = True
        staged_verification = verify_staged_artifact(layout)
    except Exception:
        try:
            if installed_candidate_executable:
                _remove_transaction_node(layout.staged_executable)
            if installed_candidate_support:
                _remove_transaction_node(layout.staged_support_directory)
            if moved_previous_executable:
                backup_executable.rename(layout.staged_executable)
            if moved_previous_support:
                backup_support.rename(layout.staged_support_directory)
        except Exception as rollback_exc:
            cleanup_transaction = False
            raise SidecarBuildError(
                "The sidecar pair transaction failed and automatic rollback "
                "could not complete; preserved the hidden transaction directory."
            ) from rollback_exc
        raise
    finally:
        if cleanup_transaction and transaction_root.exists():
            _validate_path_chain(
                transaction_root,
                layout.repo_root,
                kind="directory",
                allow_missing=False,
                label="sidecar staging transaction",
            )
            shutil.rmtree(transaction_root)

    _validate_path_chain(
        layout.staged_executable,
        layout.repo_root,
        kind="file",
        allow_missing=False,
        label="staged sidecar executable",
    )
    _validate_path_chain(
        layout.staged_support_directory,
        layout.repo_root,
        kind="directory",
        allow_missing=False,
        label="staged support directory",
    )
    return staged_verification


def _required_tool(name: str) -> str:
    executable = TRUSTED_DARWIN_TOOLS.get(name)
    if executable is None:
        raise SidecarBuildError(f"Unsupported Darwin artifact verification tool: {name}.")
    for component in (Path("/"), Path("/usr"), Path("/usr/bin"), executable):
        try:
            metadata = os.lstat(component)
        except FileNotFoundError as exc:
            raise SidecarBuildError(
                f"Required Darwin artifact verification tool is unavailable: {name}."
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise SidecarBuildError(f"Trusted Darwin verification path is a symlink: {component}.")
        if metadata.st_uid != 0 or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise SidecarBuildError(
                f"Trusted Darwin verification path has unsafe ownership or permissions: {component}."
            )
    metadata = os.lstat(executable)
    if not stat.S_ISREG(metadata.st_mode) or not os.access(executable, os.X_OK):
        raise SidecarBuildError(
            f"Required Darwin artifact verification tool is not executable: {name}."
        )
    return os.fspath(executable)


def _verify_thin_macho_header(
    path: Path,
    *,
    label: str,
    expected_architecture: str,
) -> None:
    try:
        with path.open("rb") as handle:
            header = handle.read(8)
    except OSError as exc:
        raise SidecarBuildError(f"Could not read the Mach-O header for {label}.") from exc
    if len(header) != 8 or header[:4] != b"\xcf\xfa\xed\xfe":
        raise SidecarBuildError(f"{label} is not a thin little-endian 64-bit Mach-O file.")
    expected_cpu_type = MACHO_CPU_TYPES[expected_architecture]
    actual_cpu_type = int.from_bytes(header[4:8], byteorder="little", signed=False)
    if actual_cpu_type != expected_cpu_type:
        raise SidecarBuildError(
            f"{label} has Mach-O CPU type {actual_cpu_type:#x}; "
            f"expected {expected_cpu_type:#x} ({expected_architecture})."
        )


def _support_file_paths(directory: Path) -> list[Path]:
    paths: list[Path] = []
    for current_root, _, file_names in os.walk(directory, topdown=True, followlinks=False):
        current = Path(current_root)
        paths.extend(current / name for name in file_names)
    return sorted(paths)


def _support_payload_groups(directory: Path) -> list[tuple[Path, tuple[Path, ...]]]:
    physical_root = directory.resolve(strict=True)
    grouped: dict[Path, list[Path]] = {}
    for logical_path in _support_file_paths(directory):
        try:
            payload_path = logical_path.resolve(strict=True)
            payload_path.relative_to(physical_root)
            metadata = os.stat(payload_path)
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise SidecarBuildError(
                "The staged support inventory contains an unsafe payload."
            ) from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise SidecarBuildError("The staged support inventory contains a non-regular payload.")
        grouped.setdefault(payload_path, []).append(logical_path)
    return [
        (payload_path, tuple(sorted(logical_paths)))
        for payload_path, logical_paths in sorted(
            grouped.items(),
            key=lambda item: os.fspath(item[0]),
        )
    ]


def _read_payload_header(path: Path, *, label: str) -> bytes:
    try:
        with path.open("rb") as handle:
            return handle.read(8)
    except OSError as exc:
        raise SidecarBuildError(f"Could not read the payload header for {label}.") from exc


def _validate_macho_reference(
    reference: str,
    *,
    label: str,
    reference_kind: str,
    allow_locator_parent_segments: bool,
    bundle_root: Path | None = None,
    loader_directory: Path | None = None,
    executable_directory: Path | None = None,
) -> None:
    if not reference or "\\" in reference or "\x00" in reference:
        raise SidecarBuildError(f"{label} contains an unsafe {reference_kind}.")
    lowered = reference.casefold()
    if any(marker in lowered for marker in (".dll", ".exe", "\\windows\\")):
        raise SidecarBuildError(f"{label} links a Windows payload.")

    if reference.startswith("/"):
        normalized = posixpath.normpath(reference)
        if normalized != reference:
            raise SidecarBuildError(f"{label} contains a non-normalized absolute {reference_kind}.")
        if reference.startswith(("/usr/lib/", "/System/Library/")):
            return
        raise SidecarBuildError(f"{label} contains a non-system absolute {reference_kind}.")

    locator, separator, tail = reference.partition("/")
    allowed_locators = {"@loader_path", "@executable_path"}
    if reference_kind == "dependency":
        allowed_locators.add("@rpath")
    if locator not in allowed_locators or not separator or not tail:
        raise SidecarBuildError(f"{label} contains an unsafe relative {reference_kind}.")
    components = tail.split("/")
    if (
        any(component in {"", "."} for component in components)
        or (not allow_locator_parent_segments and ".." in components)
        or posixpath.normpath(tail).startswith("/")
    ):
        raise SidecarBuildError(
            f"{label} contains a traversing or non-normalized {reference_kind}."
        )
    if allow_locator_parent_segments:
        if bundle_root is None:
            raise SidecarBuildError(
                f"{label} has no bundle boundary for relative {reference_kind}."
            )
        locator_base = loader_directory if locator == "@loader_path" else executable_directory
        if locator_base is None:
            raise SidecarBuildError(f"{label} has no locator base for relative {reference_kind}.")
        resolved_reference = (locator_base / Path(tail)).resolve(strict=False)
        try:
            resolved_reference.relative_to(bundle_root.resolve(strict=True))
        except ValueError as exc:
            raise SidecarBuildError(
                f"{label} contains a bundle-escaping {reference_kind}."
            ) from exc


def _lc_rpaths(otool_output: str, *, label: str) -> list[str]:
    lines = otool_output.splitlines()
    rpaths: list[str] = []
    for index, line in enumerate(lines):
        if line.strip() != "cmd LC_RPATH":
            continue
        for detail in lines[index + 1 : index + 6]:
            stripped = detail.strip()
            if stripped.startswith("path "):
                rpaths.append(stripped.removeprefix("path ").split(" (offset", 1)[0])
                break
        else:
            raise SidecarBuildError(f"otool returned a malformed LC_RPATH for {label}.")
    return rpaths


def _verify_macho_file(
    path: Path,
    *,
    label: str,
    expected_architecture: str,
    file_tool: str,
    lipo_tool: str,
    otool_tool: str,
    environment: Mapping[str, str],
    require_linked_library: bool,
    bundle_root: Path | None = None,
    executable_directory: Path | None = None,
    file_description: str | None = None,
) -> tuple[list[str], int, int]:
    _verify_thin_macho_header(
        path,
        label=label,
        expected_architecture=expected_architecture,
    )
    file_result = (
        file_description.strip()
        if file_description is not None
        else _run(
            [file_tool, "-d", "-L", "-b", path],
            environment=environment,
            capture_output=True,
            timeout=30,
        ).stdout.strip()
    )
    lowered_file_result = file_result.casefold()
    if (
        "mach-o" not in lowered_file_result
        or expected_architecture.casefold() not in lowered_file_result
        or any(marker in lowered_file_result for marker in ("pe32", "ms-dos", "windows"))
    ):
        raise SidecarBuildError(f"{label} is not the expected target-specific Mach-O file.")

    lipo_result = _run(
        [lipo_tool, "-archs", path],
        environment=environment,
        capture_output=True,
        timeout=30,
    ).stdout.strip()
    architectures = lipo_result.split()
    if architectures != [expected_architecture]:
        raise SidecarBuildError(
            f"{label} does not contain exactly the requested Darwin architecture."
        )

    otool_result = _run(
        [otool_tool, "-L", path],
        environment=environment,
        capture_output=True,
        timeout=30,
    ).stdout
    linked_libraries = [
        line.strip().split(" (", 1)[0] for line in otool_result.splitlines()[1:] if line.strip()
    ]
    if require_linked_library and not linked_libraries:
        raise SidecarBuildError(f"otool reported no linked libraries for {label}.")
    for library in linked_libraries:
        _validate_macho_reference(
            library,
            label=label,
            reference_kind="dependency",
            allow_locator_parent_segments=False,
        )

    load_commands = _run(
        [otool_tool, "-l", path],
        environment=environment,
        capture_output=True,
        timeout=30,
    ).stdout
    rpaths = _lc_rpaths(load_commands, label=label)
    for rpath in rpaths:
        loader_directory = path.resolve(strict=True).parent if rpath.startswith("@") else None
        _validate_macho_reference(
            rpath,
            label=label,
            reference_kind="LC_RPATH",
            # Wheel-provided Darwin libraries legitimately use values such as
            # @loader_path/../..; unlike an install-name dependency this stays
            # relative after relocation. Absolute rpaths remain system-only.
            allow_locator_parent_segments=True,
            bundle_root=bundle_root,
            loader_directory=loader_directory,
            executable_directory=executable_directory,
        )
    return architectures, len(linked_libraries), len(rpaths)


def verify_staged_artifact(layout: BuildLayout) -> dict[str, object]:
    _validate_layout_roots(layout)
    _validate_path_chain(
        layout.staged_executable,
        layout.repo_root,
        kind="file",
        allow_missing=False,
        label="staged sidecar executable",
    )
    _validate_path_chain(
        layout.staged_support_directory,
        layout.repo_root,
        kind="directory",
        allow_missing=False,
        label="staged support directory",
    )

    metadata = os.lstat(layout.staged_executable)
    if metadata.st_mode & 0o111 != 0o111 or not os.access(layout.staged_executable, os.X_OK):
        raise SidecarBuildError("The staged Darwin sidecar is not executable.")
    with layout.staged_executable.open("rb") as handle:
        if handle.read(2) == b"MZ":
            raise SidecarBuildError("The staged Darwin sidecar is a PE executable.")

    support = _scan_support_tree(
        layout.staged_support_directory,
        allow_internal_file_symlinks=True,
    )
    expected_architecture = EXPECTED_DARWIN_ARCHITECTURE[layout.target_triple]
    file_tool = _required_tool("file")
    lipo_tool = _required_tool("lipo")
    otool_tool = _required_tool("otool")
    tool_environment = sanitized_environment()
    tool_environment["LC_ALL"] = "C"
    architectures, linked_library_count, executable_rpath_count = _verify_macho_file(
        layout.staged_executable,
        label="staged sidecar executable",
        expected_architecture=expected_architecture,
        file_tool=file_tool,
        lipo_tool=lipo_tool,
        otool_tool=otool_tool,
        environment=tool_environment,
        require_linked_library=True,
        bundle_root=layout.staged_support_directory,
        executable_directory=layout.staged_executable.parent,
    )

    payload_groups = _support_payload_groups(layout.staged_support_directory)
    logical_support_count = sum(len(logical_paths) for _, logical_paths in payload_groups)
    if logical_support_count != support["support_file_count"]:
        raise SidecarBuildError("The staged support entry inventory changed during verification.")
    if len(payload_groups) + support["support_symlink_count"] != logical_support_count:
        raise SidecarBuildError("The staged support payload and symlink counts are inconsistent.")
    native_suffix_count = sum(
        logical_path.name.casefold().endswith(DARWIN_NATIVE_SUFFIXES)
        for _, logical_paths in payload_groups
        for logical_path in logical_paths
    )
    if native_suffix_count != support["native_extension_count"]:
        raise SidecarBuildError(
            "The staged native extension inventory changed during verification."
        )

    native_macho_logical_entry_count = 0
    native_macho_payload_count = 0
    native_linked_library_count = 0
    native_rpath_count = 0
    non_native_logical_entry_count = 0
    non_native_payload_count = 0
    for payload_path, logical_paths in payload_groups:
        relative_label = logical_paths[0].relative_to(layout.staged_support_directory).as_posix()
        label = f"staged support payload {relative_label}"
        header = _read_payload_header(payload_path, label=label)
        file_description = _run(
            [file_tool, "-d", "-L", "-b", payload_path],
            environment=tool_environment,
            capture_output=True,
            timeout=30,
        ).stdout.strip()
        lowered_file_description = file_description.casefold()
        raw_macho = header[:4] in MACHO_MAGICS
        file_reports_macho = "mach-o" in lowered_file_description
        has_native_suffix = any(
            logical_path.name.casefold().endswith(DARWIN_NATIVE_SUFFIXES)
            for logical_path in logical_paths
        )
        if raw_macho or file_reports_macho:
            _, dependency_count, rpath_count = _verify_macho_file(
                payload_path,
                label=label,
                expected_architecture=expected_architecture,
                file_tool=file_tool,
                lipo_tool=lipo_tool,
                otool_tool=otool_tool,
                environment=tool_environment,
                require_linked_library=False,
                bundle_root=layout.staged_support_directory,
                executable_directory=layout.staged_executable.parent,
                file_description=file_description,
            )
            native_macho_logical_entry_count += len(logical_paths)
            native_macho_payload_count += 1
            native_linked_library_count += dependency_count
            native_rpath_count += rpath_count
        else:
            if has_native_suffix:
                raise SidecarBuildError(
                    "A staged Darwin native-extension entry is not a Mach-O payload."
                )
            if header[:2] == b"MZ":
                raise SidecarBuildError(
                    "The staged sidecar support tree contains a renamed PE payload."
                )
            if any(
                marker in lowered_file_description
                for marker in ("pe32", "ms-dos", "windows executable")
            ):
                raise SidecarBuildError(
                    "The staged sidecar support tree contains a renamed PE payload."
                )
            non_native_logical_entry_count += len(logical_paths)
            non_native_payload_count += 1

    if (
        native_macho_logical_entry_count + non_native_logical_entry_count != logical_support_count
        or native_macho_payload_count + non_native_payload_count != len(payload_groups)
    ):
        raise SidecarBuildError("The staged support verification counts are inconsistent.")

    check_vector60_runtime(layout.staged_executable)
    with tempfile.TemporaryDirectory(prefix="KORYAO Sidecar Relocation with spaces ") as temporary:
        relocation_root = Path(temporary) / "relocated bundle with spaces"
        relocation_root.mkdir()
        relocated_executable = relocation_root / layout.staged_executable.name
        relocated_support = relocation_root / layout.staged_support_directory.name
        shutil.copy2(layout.staged_executable, relocated_executable)
        shutil.copytree(
            layout.staged_support_directory,
            relocated_support,
            symlinks=True,
        )
        if " " not in os.fspath(relocated_executable):
            raise SidecarBuildError("The relocation verification path contains no spaces.")
        check_vector60_runtime(relocated_executable)

    return {
        "staged_executable_mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
        "staged_executable_bits_verified": True,
        "file_mach_o_verified": True,
        "lipo_architectures": architectures,
        "otool_linked_library_count": linked_library_count,
        "otool_rpath_count": executable_rpath_count,
        "support_unique_payload_count": len(payload_groups),
        "native_macho_logical_entry_count": native_macho_logical_entry_count,
        "native_mach_o_verified_count": native_macho_payload_count,
        "native_architectures": [expected_architecture],
        "native_otool_dependency_count": native_linked_library_count,
        "native_otool_rpath_count": native_rpath_count,
        "non_native_logical_entry_count": non_native_logical_entry_count,
        "non_native_file_magic_verified_count": non_native_payload_count,
        "forbidden_absolute_dependency_count": 0,
        "windows_payload_count": 0,
        **support,
        "relocation_with_spaces_verified": True,
    }


def _build_sidecar_locked(
    layout: BuildLayout,
    host_target: str,
    *,
    skip_dependency_install: bool = False,
) -> dict[str, object]:
    _validate_layout_roots(layout)
    build_python = _prepare_environment(
        layout,
        skip_dependency_install=skip_dependency_install,
    )
    _safe_mkdir(layout.build_root, layout)
    _safe_mkdir(layout.pyinstaller_config_root, layout)
    _validate_layout_roots(layout)
    spec_file = layout.desktop_root / "scripts" / "starbridge-sidecar.spec"
    with tempfile.TemporaryDirectory(
        prefix=".generation-",
        dir=layout.build_root,
    ) as generation_value:
        generation_root = Path(generation_value)
        generation_dist = generation_root / "dist"
        generation_source = generation_dist / "starbridge-sidecar"
        generation_layout = replace(
            layout,
            dist_root=generation_dist,
            work_root=generation_root / "work",
            source_folder=generation_source,
            source_executable=generation_source / "starbridge-sidecar",
            source_support_directory=(generation_source / layout.contents_directory_name),
        )
        _validate_path_chain(
            generation_root,
            layout.repo_root,
            kind="directory",
            allow_missing=False,
            label="target-isolated build generation",
        )
        _safe_mkdir(generation_layout.dist_root, generation_layout)
        _safe_mkdir(generation_layout.work_root, generation_layout)
        _validate_layout_roots(generation_layout)

        build_environment = sanitized_environment()
        build_environment["STARBRIDGE_SIDECAR_TARGET_TRIPLE"] = layout.target_triple
        build_environment["PYINSTALLER_CONFIG_DIR"] = os.fspath(layout.pyinstaller_config_root)
        _run(
            [
                build_python,
                "-I",
                "-m",
                "PyInstaller",
                "--noconfirm",
                "--clean",
                "--distpath",
                generation_layout.dist_root,
                "--workpath",
                generation_layout.work_root,
                spec_file,
            ],
            environment=build_environment,
        )

        _validate_layout_roots(generation_layout)
        _validate_path_chain(
            generation_layout.source_executable,
            layout.repo_root,
            kind="file",
            allow_missing=False,
            label="PyInstaller sidecar executable",
        )
        _validate_path_chain(
            generation_layout.source_support_directory,
            layout.repo_root,
            kind="directory",
            allow_missing=False,
            label="PyInstaller support directory",
        )
        _scan_support_tree(
            generation_layout.source_support_directory,
            allow_internal_file_symlinks=True,
        )
        check_vector60_runtime(generation_layout.source_executable)

        _validate_layout_roots(generation_layout)
        staged_verification = _replace_staged_pair(
            generation_layout.source_executable,
            generation_layout.source_support_directory,
            generation_layout,
        )

    return {
        "ok": True,
        "packaging_mode": "one-folder",
        "target_triple": layout.target_triple,
        "host_triple": host_target,
        "executable": _relative(layout.staged_executable, layout.desktop_root),
        "support_directory": _relative(layout.staged_support_directory, layout.desktop_root),
        "pyinstaller_environment": _relative(layout.build_environment, layout.repo_root),
        "pyinstaller_config_root": _relative(layout.pyinstaller_config_root, layout.desktop_root),
        "build_root": _relative(layout.build_root, layout.desktop_root),
        "community_vectorization_included": True,
        "vector60_python_runtime_included": True,
        "vector60_python_runtime_versions": VECTOR60_RUNTIME_VERSIONS,
        "vector60_svgo_runtime_included": False,
        "vector60_svgo_runtime_blocker": (
            "SVGO requires a distributable Node runtime; this Darwin "
            "PyInstaller/Tauri staging layout does not bundle one."
        ),
        "vectorflow_gui_included": False,
        "universal_binary_verified": False,
        **staged_verification,
    }


def build_sidecar(
    repo_root: Path,
    target_triple: str,
    *,
    skip_dependency_install: bool = False,
) -> dict[str, object]:
    layout = layout_for(repo_root, target_triple)
    host_target = detect_host_target()
    if layout.target_triple != host_target:
        raise SidecarBuildError(
            "PyInstaller cannot cross-build this sidecar. "
            f"Current host is {host_target}; requested target is {layout.target_triple}."
        )

    _validate_layout_roots(layout)
    _safe_mkdir(layout.build_root, layout)
    with _target_build_lock(layout):
        return _build_sidecar_locked(
            layout,
            host_target,
            skip_dependency_install=skip_dependency_install,
        )


def verify_staged_sidecar(
    repo_root: Path,
    target_triple: str,
) -> dict[str, object]:
    layout = layout_for(repo_root, target_triple)
    host_target = detect_host_target()
    _validate_layout_roots(layout)
    _safe_mkdir(layout.build_root, layout)
    with _target_build_lock(layout):
        verification = verify_staged_artifact(layout)
    return {
        "ok": True,
        "verification_only": True,
        "packaging_mode": "one-folder",
        "target_triple": layout.target_triple,
        "host_triple": host_target,
        "executable": _relative(layout.staged_executable, layout.desktop_root),
        "support_directory": _relative(layout.staged_support_directory, layout.desktop_root),
        "vector60_python_runtime_included": True,
        "vector60_python_runtime_versions": VECTOR60_RUNTIME_VERSIONS,
        "vector60_svgo_runtime_included": False,
        "universal_binary_verified": False,
        **verification,
    }


def build_plan(repo_root: Path, target_triple: str) -> dict[str, object]:
    layout = layout_for(repo_root, target_triple)
    try:
        host_target: str | None = detect_host_target()
    except SidecarBuildError:
        host_target = None
    return {
        "ok": True,
        "plan_only": True,
        "target_triple": layout.target_triple,
        "host_triple": host_target,
        "buildable_on_current_host": layout.target_triple == host_target,
        "executable": _relative(layout.staged_executable, layout.desktop_root),
        "support_directory": _relative(layout.staged_support_directory, layout.desktop_root),
        "pyinstaller_environment": _relative(layout.build_environment, layout.repo_root),
        "pyinstaller_config_root": _relative(layout.pyinstaller_config_root, layout.desktop_root),
        "build_root": _relative(layout.build_root, layout.desktop_root),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build and stage the target-isolated Darwin sidecar."
    )
    parser.add_argument("--target-triple")
    parser.add_argument("--skip-dependency-install", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--print-plan",
        action="store_true",
        help="Resolve and print target-specific paths without writing files.",
    )
    mode.add_argument(
        "--verify-staged",
        action="store_true",
        help="Verify the already staged target without rebuilding it.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    script_dir = _absolute(Path(__file__)).parent
    repo_root = script_dir.parents[2]
    try:
        target_triple = resolve_target_triple(args.target_triple)
        if args.print_plan:
            result = build_plan(repo_root, target_triple)
        elif args.verify_staged:
            result = verify_staged_sidecar(repo_root, target_triple)
        else:
            result = build_sidecar(
                repo_root,
                target_triple,
                skip_dependency_install=args.skip_dependency_install,
            )
    except (SidecarBuildError, OSError, subprocess.SubprocessError) as exc:
        print(f"Darwin sidecar build failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
