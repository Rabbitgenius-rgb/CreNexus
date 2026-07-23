from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import re
import secrets
import selectors
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(os.path.abspath(os.fspath(Path(__file__)))).parent
if os.fspath(SCRIPT_DIR) not in sys.path:
    # Python -I intentionally omits the script directory. Add only this trusted,
    # fixed sibling location so the tester can import the builder contract.
    sys.path.insert(0, os.fspath(SCRIPT_DIR))

from sidecar_builder import (
    SidecarBuildError,
    build_sidecar,
    check_vector60_runtime,
    layout_for,
    resolve_target_triple,
    sanitized_environment,
    verify_staged_sidecar,
)

READY_PREFIX = "STARBRIDGE_READY "
SESSION_ENV = "STARBRIDGE_SESSION_TOKEN"
SESSION_HEADER = "X-KORYAO-Session"
APP_DATA_ENV = "STARBRIDGE_APP_DATA_DIR"
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
CSS_URL_PATTERN = re.compile(r"url\(\s*([^)]+?)\s*\)", re.IGNORECASE)


class SidecarTestError(RuntimeError):
    pass


def _positive_timeout(value: str) -> float:
    parsed = float(value)
    if parsed <= 0 or parsed > 120:
        raise argparse.ArgumentTypeError("timeout must be greater than 0 and at most 120")
    return parsed


def _port(value: str) -> int:
    parsed = int(value)
    if parsed < 0 or parsed > 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return parsed


def _request_json(
    port: int,
    method: str,
    path: str,
    *,
    credential: str | None = None,
    body: Mapping[str, Any] | None = None,
    timeout: float = 10,
) -> tuple[int, dict[str, Any]]:
    headers = {"Accept": "application/json"}
    payload: bytes | None = None
    if credential is not None:
        headers[SESSION_HEADER] = credential
    if body is not None:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        status = response.status
    finally:
        connection.close()
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SidecarTestError(
            f"{method} {path} returned a non-JSON response with status {status}."
        ) from exc
    if not isinstance(decoded, dict):
        raise SidecarTestError(f"{method} {path} returned a non-object JSON response.")
    return status, decoded


def _expect_status(
    port: int,
    method: str,
    path: str,
    expected: int,
    *,
    credential: str | None = None,
    body: Mapping[str, Any] | None = None,
    timeout: float = 10,
) -> dict[str, Any]:
    status, payload = _request_json(
        port,
        method,
        path,
        credential=credential,
        body=body,
        timeout=timeout,
    )
    if status != expected:
        raise SidecarTestError(f"{method} {path} returned {status}; expected {expected}.")
    return payload


def _sidecar_environment(
    data_root: Path,
    *,
    credential: str,
    codex_home: Path | None = None,
) -> dict[str, str]:
    environment = sanitized_environment()
    environment[SESSION_ENV] = credential
    environment[APP_DATA_ENV] = os.fspath(data_root)
    if codex_home is not None:
        environment["CODEX_HOME"] = os.fspath(codex_home)
    return environment


def _spawn_sidecar(
    executable: Path,
    data_root: Path,
    *,
    credential: str,
    parent_pid: int,
    port: int = 0,
    codex_home: Path | None = None,
) -> subprocess.Popen[str]:
    arguments = [
        os.fspath(executable),
        "--desktop",
        "--parent-pid",
        str(parent_pid),
    ]
    if port:
        arguments.extend(["--port", str(port)])
    return subprocess.Popen(
        arguments,
        env=_sidecar_environment(
            data_root,
            credential=credential,
            codex_home=codex_home,
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )


def _wait_ready(
    process: subprocess.Popen[str],
    *,
    credential: str,
    timeout: float,
    stage: str,
) -> dict[str, Any]:
    if process.stdout is None:
        raise SidecarTestError("The sidecar stdout pipe is unavailable.")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stderr = process.stderr.read() if process.stderr is not None else ""
                raise SidecarTestError(
                    f"The {stage} sidecar exited before reporting ready"
                    + (f": {stderr.splitlines()[-1]}" if stderr.strip() else ".")
                )
            remaining = max(0.0, deadline - time.monotonic())
            if not selector.select(min(0.2, remaining)):
                continue
            line = process.stdout.readline().strip()
            if not line:
                continue
            if not line.startswith(READY_PREFIX):
                raise SidecarTestError(f"The {stage} sidecar emitted an invalid ready line.")
            if credential in line:
                raise SidecarTestError("The ready line exposed the session credential.")
            try:
                ready = json.loads(line[len(READY_PREFIX) :])
            except json.JSONDecodeError as exc:
                raise SidecarTestError("The sidecar emitted invalid ready JSON.") from exc
            if not isinstance(ready, dict):
                raise SidecarTestError("The sidecar ready payload is not an object.")
            if (
                ready.get("host") != "127.0.0.1"
                or not isinstance(ready.get("port"), int)
                or ready["port"] <= 0
                or ready.get("pid") != process.pid
                or ready.get("session_required") is not True
            ):
                raise SidecarTestError("The sidecar ready PID/port/session contract failed.")
            return ready
    finally:
        selector.close()
    raise SidecarTestError(f"The {stage} sidecar did not report ready before the timeout.")


def _terminate(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _assert_port_released(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise SidecarTestError(f"Loopback port {port} was not released.") from exc


def _pair_desktop_session(
    executable: Path,
    data_root: Path,
    pairing_code: str,
    *,
    timeout: float,
) -> None:
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "starbridge.desktop_pair",
            "arguments": {
                "pairing_code": pairing_code,
                "confirm_pairing": True,
                "confirm_write": True,
                "dry_run": False,
            },
        },
    }
    environment = sanitized_environment()
    environment[APP_DATA_ENV] = os.fspath(data_root)
    completed = subprocess.run(
        [executable, "--mcp"],
        input=json.dumps(request, separators=(",", ":")) + "\n",
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise SidecarTestError("The packaged MCP connector failed to pair the desktop.")
    response: dict[str, Any] | None = None
    for line in completed.stdout.splitlines():
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("id") == 1:
            response = candidate
            break
    structured = (
        response.get("result", {}).get("structuredContent", {}) if response is not None else {}
    )
    if structured.get("ok") is not True:
        raise SidecarTestError("The packaged MCP connector rejected valid pairing.")


def _assert_paths_redacted(
    value: object,
    private_paths: Sequence[Path],
    *,
    stage: str,
) -> None:
    serialized = (
        value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    )
    for private_path in private_paths:
        resolved = private_path.resolve()
        raw = os.fspath(resolved)
        representations = {
            raw,
            resolved.as_uri(),
            urllib.parse.quote(raw, safe="/:"),
            urllib.parse.quote(raw, safe=""),
        }
        if any(
            representation and representation in serialized for representation in representations
        ):
            raise SidecarTestError(f"The {stage} exposed a private absolute path.")


def _require_zero(value: object, *, field: str) -> None:
    if type(value) is not int or value != 0:
        raise SidecarTestError(f"The exact vector report field {field} is not zero.")


def _validate_exact_artifacts(
    output_root: Path,
    data_root: Path,
    source: Path,
) -> dict[str, object]:
    svg_paths = sorted(output_root.rglob("vector.svg"))
    if len(svg_paths) != 1:
        raise SidecarTestError(
            "The exact-vector job did not create exactly one controlled SVG output."
        )
    svg_path = svg_paths[0]
    report_path = svg_path.with_name("vector_report.json")
    report_paths = sorted(output_root.rglob("vector_report.json"))
    if report_paths != [report_path]:
        raise SidecarTestError(
            "The exact-vector output does not contain one adjacent vector report."
        )

    for path, label in ((svg_path, "SVG"), (report_path, "vector report")):
        try:
            metadata = os.lstat(path)
        except FileNotFoundError as exc:
            raise SidecarTestError(f"The exact-vector {label} is missing.") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise SidecarTestError(f"The exact-vector {label} is not a regular non-symlinked file.")
        try:
            path.resolve(strict=True).relative_to(output_root.resolve(strict=True))
        except ValueError as exc:
            raise SidecarTestError(
                f"The exact-vector {label} escaped the controlled output root."
            ) from exc

    svg_text = svg_path.read_text(encoding="utf-8")
    _assert_paths_redacted(
        svg_text,
        (data_root, source),
        stage="exact SVG",
    )
    lowered_svg = svg_text.casefold()
    if "data:image" in lowered_svg:
        raise SidecarTestError("The exact SVG contains an embedded raster data URI.")
    if "<?" in lowered_svg:
        raise SidecarTestError("The exact SVG contains a processing instruction.")
    if "<!doctype" in lowered_svg or "<!entity" in lowered_svg:
        raise SidecarTestError("The exact SVG contains a forbidden document declaration.")
    if "@import" in lowered_svg:
        raise SidecarTestError("The exact SVG contains an external style import.")
    for match in CSS_URL_PATTERN.finditer(svg_text):
        reference = match.group(1).strip().strip("\"'")
        if reference and not reference.startswith("#"):
            raise SidecarTestError("The exact SVG contains an external CSS reference.")
    try:
        root = ET.fromstring(svg_text)
    except ET.ParseError as exc:
        raise SidecarTestError("The exact SVG is not well-formed XML.") from exc
    if root.tag.rsplit("}", 1)[-1].casefold() != "svg":
        raise SidecarTestError("The exact vector artifact root is not SVG.")
    for element in root.iter():
        local_tag = element.tag.rsplit("}", 1)[-1].casefold()
        if local_tag in {"image", "script", "foreignobject"}:
            raise SidecarTestError(f"The exact SVG contains forbidden {local_tag} content.")
        for attribute, raw_value in element.attrib.items():
            local_attribute = attribute.rsplit("}", 1)[-1].casefold()
            value = raw_value.strip()
            if local_attribute.startswith("on"):
                raise SidecarTestError("The exact SVG contains a script event handler.")
            if local_attribute in {"href", "src"} and value and not value.startswith("#"):
                raise SidecarTestError("The exact SVG contains an external reference.")

    report_text = report_path.read_text(encoding="utf-8")
    _assert_paths_redacted(
        report_text,
        (data_root, source),
        stage="exact vector report",
    )
    try:
        report = json.loads(report_text)
    except json.JSONDecodeError as exc:
        raise SidecarTestError("The exact vector report is not valid JSON.") from exc
    if not isinstance(report, dict):
        raise SidecarTestError("The exact vector report is not a JSON object.")
    validation = report.get("validation")
    exact = report.get("exact_validation")
    if not isinstance(validation, dict) or not isinstance(exact, dict):
        raise SidecarTestError("The exact vector report omitted validation evidence.")
    if validation.get("svg_verified") is not True:
        raise SidecarTestError("The exact vector report did not verify the SVG.")
    if validation.get("image_trace_used") is not False:
        raise SidecarTestError("The exact vector report used image trace.")
    _require_zero(
        validation.get("embedded_raster_count"),
        field="validation.embedded_raster_count",
    )
    _require_zero(
        validation.get("external_reference_count"),
        field="validation.external_reference_count",
    )
    if exact.get("pixel_match") is not True:
        raise SidecarTestError("The exact vector report did not prove a pixel match.")
    _require_zero(
        exact.get("different_pixel_count"),
        field="exact_validation.different_pixel_count",
    )
    _require_zero(
        exact.get("maximum_channel_difference"),
        field="exact_validation.maximum_channel_difference",
    )
    return {
        "svg_parsed": True,
        "embedded_raster_count": 0,
        "external_reference_count": 0,
        "image_trace_used": False,
        "pixel_match": True,
        "different_pixel_count": 0,
        "maximum_channel_difference": 0,
        "report_verified": True,
    }


def _verify_exact_svg(
    port: int,
    credential: str,
    data_root: Path,
    *,
    timeout: float,
) -> dict[str, object]:
    source = data_root / "community vector source.png"
    source.write_bytes(PNG_1X1)
    selection = _expect_status(
        port,
        "POST",
        "/api/vectorization/selections",
        200,
        credential=credential,
        body={"input_path": os.fspath(source)},
        timeout=timeout,
    )
    selection_id = selection.get("data", {}).get("selectionId")
    if selection.get("ok") is not True or not selection_id:
        raise SidecarTestError("The sidecar could not select the exact-vector input.")
    _assert_paths_redacted(
        selection,
        (data_root, source),
        stage="vector selection response",
    )

    started = _expect_status(
        port,
        "POST",
        "/api/vectorization/jobs",
        202,
        credential=credential,
        body={
            "selection_id": selection_id,
            "mode": "exact",
            "parameters": {},
            "confirm_run": True,
            "confirm_write": True,
            "confirm_export": True,
        },
        timeout=timeout,
    )
    job_id = started.get("data", {}).get("jobId")
    if started.get("ok") is not True or not job_id:
        raise SidecarTestError("The sidecar did not start the exact-vector job.")
    _assert_paths_redacted(
        started,
        (data_root, source),
        stage="vector job-start response",
    )

    deadline = time.monotonic() + 20
    completed = started
    while time.monotonic() < deadline:
        completed = _expect_status(
            port,
            "GET",
            f"/api/vectorization/jobs/{job_id}",
            200,
            credential=credential,
            timeout=timeout,
        )
        _assert_paths_redacted(
            completed,
            (data_root, source),
            stage="exact-vector poll response",
        )
        if completed.get("data", {}).get("status") in {"completed", "failed"}:
            break
        time.sleep(0.1)
    data = completed.get("data", {})
    if data.get("status") != "completed":
        raise SidecarTestError(f"The exact-vector job did not complete: {data.get('status')}.")
    if data.get("result", {}).get("metrics", {}).get("pixelMatch") is not True:
        raise SidecarTestError("The exact-vector job did not report a pixel match.")
    _assert_paths_redacted(
        completed,
        (data_root, source),
        stage="exact-vector completion response",
    )
    output_root = data_root / "data" / "vectorization"
    return _validate_exact_artifacts(output_root, data_root, source)


def _verify_primary_contract(
    executable: Path,
    temporary_root: Path,
    *,
    startup_timeout: float,
    requested_port: int,
) -> dict[str, Any]:
    data_root = temporary_root / "primary app data"
    codex_home = temporary_root / "codex home"
    data_root.mkdir(parents=True)
    credential = secrets.token_hex(32)
    process = _spawn_sidecar(
        executable,
        data_root,
        credential=credential,
        parent_pid=os.getpid(),
        port=requested_port,
        codex_home=codex_home,
    )
    ready: dict[str, Any] | None = None
    try:
        ready = _wait_ready(
            process,
            credential=credential,
            timeout=startup_timeout,
            stage="primary",
        )
        port = int(ready["port"])
        health = _expect_status(port, "GET", "/api/health", 200)
        if health.get("ok") is not True:
            raise SidecarTestError("The public loopback health check failed.")

        _expect_status(
            port,
            "GET",
            "/api/bootstrap",
            403,
            credential=secrets.token_hex(32),
        )
        bootstrap = _expect_status(
            port,
            "GET",
            "/api/bootstrap",
            200,
            credential=credential,
        )
        if bootstrap.get("ok") is not True:
            raise SidecarTestError("The authenticated bootstrap request failed.")

        connections = _expect_status(
            port,
            "GET",
            "/api/connections",
            200,
            credential=credential,
        )
        connection_data = connections.get("data", {})
        if connection_data.get("drawing_enabled") is not False:
            raise SidecarTestError("The desktop session did not begin locked.")
        if connection_data.get("schema_version") != "starbridge.desktop-connections.v2":
            raise SidecarTestError("The desktop pairing schema version changed.")
        applications = connection_data.get("applications")
        if not isinstance(applications, list) or len(applications) != 6:
            raise SidecarTestError("The sidecar omitted creative application adapters.")
        pairing_code = connection_data.get("codex", {}).get("pairing_code")
        if not isinstance(pairing_code, str) or len(pairing_code) != 8:
            raise SidecarTestError("The sidecar returned an invalid pairing code.")

        _pair_desktop_session(
            executable,
            data_root,
            pairing_code,
            timeout=startup_timeout,
        )
        paired = _expect_status(
            port,
            "GET",
            "/api/connections",
            200,
            credential=credential,
        )
        if paired.get("data", {}).get("drawing_enabled") is not True:
            raise SidecarTestError("Valid Codex pairing did not unlock vectorization.")

        exact_evidence = _verify_exact_svg(
            port,
            credential,
            data_root,
            timeout=startup_timeout,
        )
        _expect_status(
            port,
            "POST",
            "/api/lifecycle/shutdown",
            202,
            credential=credential,
            body={},
        )
        try:
            return_code = process.wait(timeout=10)
        except subprocess.TimeoutExpired as exc:
            raise SidecarTestError(
                "The sidecar did not exit after authenticated shutdown."
            ) from exc
        if return_code != 0:
            raise SidecarTestError(f"The sidecar exited with code {return_code} after shutdown.")
        stderr = process.stderr.read() if process.stderr is not None else ""
        if credential in stderr:
            raise SidecarTestError("The sidecar exposed its credential on stderr.")
        _assert_port_released(port)
        return {**ready, "exact_artifact": exact_evidence}
    finally:
        _terminate(process)
        if ready is not None:
            _assert_port_released(int(ready["port"]))


def _verify_occupied_port_recovery(
    executable: Path,
    temporary_root: Path,
    *,
    startup_timeout: float,
) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        port = int(occupied.getsockname()[1])
        credential = secrets.token_hex(32)
        data_root = temporary_root / "occupied port app data"
        data_root.mkdir(parents=True)
        process = _spawn_sidecar(
            executable,
            data_root,
            credential=credential,
            parent_pid=os.getpid(),
            port=port,
        )
        try:
            try:
                stdout, stderr = process.communicate(timeout=startup_timeout)
            except subprocess.TimeoutExpired as exc:
                raise SidecarTestError(
                    "The sidecar did not fail closed on an occupied port."
                ) from exc
            if process.returncode == 0:
                raise SidecarTestError("The occupied-port sidecar reported success.")
            if READY_PREFIX in stdout:
                raise SidecarTestError("The occupied-port sidecar reported ready.")
            if credential in stdout or credential in stderr:
                raise SidecarTestError("The occupied-port failure exposed the session credential.")
        finally:
            _terminate(process)

    retry_root = temporary_root / "released port retry app data"
    retry_root.mkdir(parents=True)
    credential = secrets.token_hex(32)
    retry = _spawn_sidecar(
        executable,
        retry_root,
        credential=credential,
        parent_pid=os.getpid(),
        port=port,
    )
    try:
        ready = _wait_ready(
            retry,
            credential=credential,
            timeout=startup_timeout,
            stage="released-port retry",
        )
        if ready.get("port") != port:
            raise SidecarTestError("The released-port retry bound the wrong port.")
        _expect_status(port, "GET", "/api/health", 200)
        _expect_status(
            port,
            "POST",
            "/api/lifecycle/shutdown",
            202,
            credential=credential,
            body={},
        )
        if retry.wait(timeout=10) != 0:
            raise SidecarTestError("The released-port retry did not stop cleanly.")
    finally:
        _terminate(retry)
        _assert_port_released(port)
    return port


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _parent_probe(
    executable: Path,
    data_root: Path,
    probe_file: Path,
    *,
    startup_timeout: float,
) -> int:
    data_root.mkdir(parents=True, exist_ok=True)
    stdout_path = data_root / "parent child stdout.log"
    stderr_path = data_root / "parent child stderr.log"
    credential = secrets.token_hex(32)
    process: subprocess.Popen[bytes] | None = None
    try:
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                [
                    executable,
                    "--desktop",
                    "--parent-pid",
                    str(os.getpid()),
                ],
                env=_sidecar_environment(data_root, credential=credential),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
            )
            deadline = time.monotonic() + startup_timeout
            ready: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                text = stdout_path.read_text(encoding="utf-8", errors="replace")
                for line in text.splitlines():
                    if line.startswith(READY_PREFIX):
                        if credential in line:
                            raise SidecarTestError(
                                "The parent-exit ready line exposed its credential."
                            )
                        ready = json.loads(line[len(READY_PREFIX) :])
                        break
                if ready is not None:
                    break
                if process.poll() is not None:
                    raise SidecarTestError("The parent-exit child stopped before reporting ready.")
                time.sleep(0.05)
            if ready is None:
                raise SidecarTestError("The parent-exit child did not report ready before timeout.")
            probe_file.write_text(
                json.dumps(
                    {
                        "pid": process.pid,
                        "port": ready["port"],
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        return 0
    except Exception:
        if process is not None and process.poll() is None:
            process.terminate()
        raise


def _verify_parent_exit(
    executable: Path,
    temporary_root: Path,
    *,
    startup_timeout: float,
) -> tuple[int, int]:
    data_root = temporary_root / "parent exit app data"
    probe_file = temporary_root / "parent exit probe.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            Path(__file__).resolve(),
            "--parent-probe",
            "--executable",
            executable,
            "--data-root",
            data_root,
            "--probe-file",
            probe_file,
            "--startup-timeout",
            str(startup_timeout),
        ],
        env=sanitized_environment(),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=startup_timeout + 5,
    )
    if completed.returncode != 0 or not probe_file.is_file():
        raise SidecarTestError("The parent-exit helper did not produce a probe.")
    probe = json.loads(probe_file.read_text(encoding="utf-8"))
    pid = int(probe["pid"])
    port = int(probe["port"])
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if not _process_exists(pid):
            try:
                _assert_port_released(port)
            except SidecarTestError:
                time.sleep(0.1)
                continue
            return pid, port
        time.sleep(0.1)
    if _process_exists(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    raise SidecarTestError("The sidecar remained alive after its parent exited.")


def verify_sidecar(
    executable: Path,
    target_triple: str,
    *,
    startup_timeout: float,
    requested_port: int,
) -> dict[str, Any]:
    runtime = check_vector60_runtime(executable)
    with tempfile.TemporaryDirectory(prefix="KORYAO Sidecar Test with spaces ") as value:
        temporary_root = Path(value).resolve()
        system_temp = Path(tempfile.gettempdir()).resolve()
        try:
            temporary_root.relative_to(system_temp)
        except ValueError as exc:
            raise SidecarTestError(
                "Refusing to use a test directory outside the system temporary root."
            ) from exc
        ready = _verify_primary_contract(
            executable,
            temporary_root,
            startup_timeout=startup_timeout,
            requested_port=requested_port,
        )
        recovered_port = _verify_occupied_port_recovery(
            executable,
            temporary_root,
            startup_timeout=startup_timeout,
        )
        parent_pid, parent_port = _verify_parent_exit(
            executable,
            temporary_root,
            startup_timeout=startup_timeout,
        )

    return {
        "ok": True,
        "target_triple": target_triple,
        "ready": True,
        "ready_pid": ready["pid"],
        "ready_port": ready["port"],
        "loopback_only": True,
        "session_header": SESSION_HEADER,
        "wrong_credential_rejected": True,
        "authenticated_bootstrap": True,
        "community_exact_vectorization": True,
        "exact_svg_parsed": ready["exact_artifact"]["svg_parsed"],
        "exact_vector_report_verified": ready["exact_artifact"]["report_verified"],
        "exact_embedded_raster_count": ready["exact_artifact"]["embedded_raster_count"],
        "exact_external_reference_count": ready["exact_artifact"]["external_reference_count"],
        "exact_image_trace_used": ready["exact_artifact"]["image_trace_used"],
        "exact_pixel_match": ready["exact_artifact"]["pixel_match"],
        "exact_different_pixel_count": ready["exact_artifact"]["different_pixel_count"],
        "exact_maximum_channel_difference": ready["exact_artifact"]["maximum_channel_difference"],
        "vector60_python_runtime": True,
        "vector60_python_runtime_versions": runtime["versions"],
        "vector60_svgo_runtime_included": False,
        "vector_path_redacted": True,
        "graceful_shutdown": True,
        "port_released": True,
        "occupied_port_fail_closed": True,
        "released_port_restart": True,
        "released_port": recovered_port,
        "parent_exit_cleanup": True,
        "parent_exit_pid": parent_pid,
        "parent_exit_port": parent_port,
        "orphan_process": False,
        "credential_exposed": False,
        "temporary_app_data_cleaned": True,
        "rust_supervisor_recovery_tested": False,
    }


def _main_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify the staged Darwin sidecar contract on a POSIX host."
    )
    parser.add_argument("--target-triple")
    parser.add_argument("--startup-timeout", type=_positive_timeout, default=120.0)
    parser.add_argument("--port", type=_port, default=0)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--skip-dependency-install", action="store_true")
    parser.add_argument(
        "--print-plan",
        action="store_true",
        help="Resolve the staged executable without building or running it.",
    )
    return parser


def _parent_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--parent-probe", action="store_true")
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--probe-file", type=Path, required=True)
    parser.add_argument("--startup-timeout", type=_positive_timeout, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--parent-probe" in arguments:
        args = _parent_parser().parse_args(arguments)
        try:
            return _parent_probe(
                args.executable.resolve(),
                args.data_root.resolve(),
                args.probe_file.resolve(),
                startup_timeout=args.startup_timeout,
            )
        except (SidecarTestError, OSError, subprocess.SubprocessError) as exc:
            print(f"Darwin sidecar parent probe failed: {exc}", file=sys.stderr)
            return 1

    args = _main_parser().parse_args(arguments)
    script_dir = SCRIPT_DIR
    repo_root = script_dir.parents[2]
    try:
        target_triple = resolve_target_triple(args.target_triple)
        layout = layout_for(repo_root, target_triple)
        if args.print_plan:
            result = {
                "ok": True,
                "plan_only": True,
                "target_triple": target_triple,
                "executable": layout.staged_executable.relative_to(layout.desktop_root).as_posix(),
                "support_directory": layout.staged_support_directory.relative_to(
                    layout.desktop_root
                ).as_posix(),
            }
        else:
            if not layout.staged_executable.is_file():
                if args.skip_build:
                    raise SidecarTestError(f"The staged sidecar is missing for {target_triple}.")
                build_sidecar(
                    repo_root,
                    target_triple,
                    skip_dependency_install=args.skip_dependency_install,
                )
            if not layout.staged_support_directory.is_dir():
                raise SidecarTestError(
                    f"The staged support directory is missing for {target_triple}."
                )
            staged_artifact = verify_staged_sidecar(repo_root, target_triple)
            result = verify_sidecar(
                layout.staged_executable,
                target_triple,
                startup_timeout=args.startup_timeout,
                requested_port=args.port,
            )
            result["staged_artifact"] = staged_artifact
    except (
        SidecarBuildError,
        SidecarTestError,
        OSError,
        subprocess.SubprocessError,
        ValueError,
    ) as exc:
        print(f"Darwin sidecar test failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
