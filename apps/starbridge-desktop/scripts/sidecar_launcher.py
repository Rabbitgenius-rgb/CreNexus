from __future__ import annotations

import os
import stat
import sys
from collections.abc import Mapping
from pathlib import Path

SANITIZED_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
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
ENTRYPOINTS = {
    "build": "sidecar_builder.py",
    "test": "sidecar_tester.py",
}


class SidecarLaunchError(RuntimeError):
    pass


def sanitized_launcher_environment(
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if environment is None else environment
    sanitized: dict[str, str] = {}
    for key, value in source.items():
        normalized = key.upper()
        if key in ENVIRONMENT_PASSTHROUGH_ALLOWLIST:
            sanitized[key] = value
        elif key == normalized and normalized in PIP_NETWORK_ENVIRONMENT_ALLOWLIST:
            sanitized[normalized] = value
    if "CARGO_BUILD_TARGET" in source:
        sanitized["CARGO_BUILD_TARGET"] = source["CARGO_BUILD_TARGET"]
    sanitized["PATH"] = SANITIZED_PATH
    sanitized["PIP_CONFIG_FILE"] = os.devnull
    sanitized["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    sanitized["PIP_NO_INPUT"] = "1"
    return sanitized


def _require_regular_file(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SidecarLaunchError(f"The {label} is unavailable.") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SidecarLaunchError(f"The {label} must be a regular non-symlink file.")


def _launcher_command(arguments: list[str]) -> tuple[Path, list[str]]:
    if len(arguments) < 2 or arguments[1] not in ENTRYPOINTS:
        raise SidecarLaunchError("Expected a build or test sidecar launcher mode.")

    launcher_path = Path(os.path.abspath(__file__))
    _require_regular_file(launcher_path, label="Darwin sidecar launcher")
    scripts_directory = launcher_path.parent
    repository_root = scripts_directory.parent.parent.parent
    entrypoint = scripts_directory / ENTRYPOINTS[arguments[1]]
    _require_regular_file(entrypoint, label="Darwin sidecar Python entry point")

    repository_python = repository_root / ".venv" / "bin" / "python"
    if not repository_python.is_file() or not os.access(repository_python, os.X_OK):
        raise SidecarLaunchError("The repository Python environment is unavailable.")
    try:
        resolved_runner = repository_python.resolve(strict=True)
    except OSError as exc:
        raise SidecarLaunchError("The repository Python environment is unavailable.") from exc
    if not resolved_runner.is_file() or not os.access(resolved_runner, os.X_OK):
        raise SidecarLaunchError("The repository Python environment is unavailable.")
    if resolved_runner == Path("/usr/bin/python3"):
        raise SidecarLaunchError("The repository Python environment uses an unsafe tool shim.")
    runner = repository_python

    command = [
        os.fspath(runner),
        "-I",
        os.fspath(entrypoint),
        *arguments[2:],
    ]
    return runner, command


def main(arguments: list[str] | None = None) -> int:
    argv = sys.argv if arguments is None else arguments
    try:
        runner, command = _launcher_command(argv)
        os.execve(
            os.fspath(runner),
            command,
            sanitized_launcher_environment(),
        )
    except SidecarLaunchError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OSError:
        print("The trusted Darwin sidecar Python runner could not be started.", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
