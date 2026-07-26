from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import sys
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA = "starbridge.sidecar-artifact.v1"
TARGET_PATTERN = re.compile(r"(?:aarch64|x86_64)-apple-darwin\Z")
DIGEST_PATTERN = re.compile(r"([0-9a-f]{64})  ([^/\r\n]+)\n\Z")


class ArtifactError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _names(target: str) -> tuple[str, str]:
    if TARGET_PATTERN.fullmatch(target) is None:
        raise ArtifactError("unsupported target triple")
    return f"starbridge-sidecar-{target}", f"_internal-{target}"


def _safe_relative(name: str, allowed: set[str]) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or name != path.as_posix()
        or "\\" in name
        or ".." in path.parts
        or not path.parts
        or path.parts[0] not in allowed
    ):
        raise ArtifactError("unsafe archive entry")
    return path


def _normalized_link(path: PurePosixPath, target: str, support: str) -> str:
    link = PurePosixPath(target)
    if link.is_absolute() or "\\" in target:
        raise ArtifactError("unsafe archive symlink")
    parts: list[str] = []
    for part in (*path.parent.parts, *link.parts):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise ArtifactError("archive symlink escapes support directory")
            parts.pop()
        else:
            parts.append(part)
    if not parts or parts[0] != support:
        raise ArtifactError("archive symlink escapes support directory")
    return target


def _check_collisions(paths: list[str]) -> None:
    if len(paths) != len(set(paths)):
        raise ArtifactError("duplicate archive entry")
    folded = [path.casefold() for path in paths]
    if len(folded) != len(set(folded)):
        raise ArtifactError("case-insensitive archive entry collision")


def _filesystem_inventory(root: Path, target: str) -> list[dict[str, Any]]:
    executable, support = _names(target)
    entries: list[dict[str, Any]] = []

    def visit(path: Path, relative: PurePosixPath) -> None:
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if mode & ~0o777:
            raise ArtifactError("special permission bits are not allowed")
        entry: dict[str, Any] = {"path": relative.as_posix(), "mode": mode}
        if stat.S_ISREG(metadata.st_mode):
            entry.update(type="file", sha256=_sha256_file(path))
        elif stat.S_ISDIR(metadata.st_mode):
            entry["type"] = "directory"
        elif stat.S_ISLNK(metadata.st_mode):
            entry.update(
                type="symlink",
                target=_normalized_link(relative, os.readlink(path), support),
            )
        else:
            raise ArtifactError("unsupported filesystem entry")
        entries.append(entry)
        if entry["type"] == "directory":
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                visit(child, relative / child.name)

    executable_path = root / executable
    support_path = root / support
    if not executable_path.is_file() or executable_path.is_symlink():
        raise ArtifactError("sidecar executable is not a regular file")
    if not support_path.is_dir() or support_path.is_symlink():
        raise ArtifactError("sidecar support root is not a directory")
    if stat.S_IMODE(executable_path.stat().st_mode) & 0o111 != 0o111:
        raise ArtifactError("sidecar executable mode is not executable")
    visit(executable_path, PurePosixPath(executable))
    visit(support_path, PurePosixPath(support))
    entries.sort(key=lambda entry: entry["path"])
    _check_collisions([entry["path"] for entry in entries])
    return entries


def _manifest(target: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schema": SCHEMA, "target_triple": target, "entries": entries}


def pack(
    binaries_root: Path,
    target: str,
    archive: Path,
    digest_path: Path,
    manifest_path: Path,
) -> None:
    entries = _filesystem_inventory(binaries_root, target)
    payload = _manifest(target, entries)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT, dereference=False) as bundle:
        for entry in entries:
            source = binaries_root.joinpath(*PurePosixPath(entry["path"]).parts)
            bundle.add(source, arcname=entry["path"], recursive=False)
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    digest_path.write_text(f"{_sha256_file(archive)}  {archive.name}\n", encoding="ascii")


def _load_digest(archive: Path, digest_path: Path) -> None:
    try:
        text = digest_path.read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise ArtifactError("invalid archive digest file") from exc
    match = DIGEST_PATTERN.fullmatch(text)
    if match is None or match.group(2) != archive.name:
        raise ArtifactError("invalid archive digest file")
    if not hmac.compare_digest(match.group(1), _sha256_file(archive)):
        raise ArtifactError("archive digest mismatch")


def _archive_inventory(
    bundle: tarfile.TarFile, target: str
) -> tuple[list[dict[str, Any]], list[tarfile.TarInfo]]:
    executable, support = _names(target)
    allowed = {executable, support}
    entries: list[dict[str, Any]] = []
    members = bundle.getmembers()
    paths: list[PurePosixPath] = []
    symlinks: list[PurePosixPath] = []
    for member in members:
        path = _safe_relative(member.name, allowed)
        paths.append(path)
        if member.mode & ~0o777:
            raise ArtifactError("special permission bits are not allowed")
        entry: dict[str, Any] = {
            "path": path.as_posix(),
            "mode": member.mode & 0o777,
        }
        if member.isreg():
            extracted = bundle.extractfile(member)
            if extracted is None:
                raise ArtifactError("archive file payload is unavailable")
            entry.update(type="file", sha256=hashlib.sha256(extracted.read()).hexdigest())
        elif member.isdir():
            entry["type"] = "directory"
        elif member.issym():
            entry.update(
                type="symlink",
                target=_normalized_link(path, member.linkname, support),
            )
            symlinks.append(path)
        elif member.islnk():
            raise ArtifactError("hard links are not allowed")
        else:
            raise ArtifactError("special archive entries are not allowed")
        entries.append(entry)

    _check_collisions([path.as_posix() for path in paths])
    by_path = {entry["path"]: entry for entry in entries}
    if by_path.get(executable, {}).get("type") != "file":
        raise ArtifactError("archive executable is missing or invalid")
    if by_path.get(support, {}).get("type") != "directory":
        raise ArtifactError("archive support root is missing or invalid")
    for symlink in symlinks:
        if any(
            path != symlink and path.parts[: len(symlink.parts)] == symlink.parts for path in paths
        ):
            raise ArtifactError("archive contains an entry beneath a symlink")
    entries.sort(key=lambda entry: entry["path"])
    return entries, members


def _load_manifest(path: Path, target: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactError("invalid artifact manifest") from exc
    if not isinstance(payload, dict) or set(payload) != {"schema", "target_triple", "entries"}:
        raise ArtifactError("invalid artifact manifest")
    if payload["schema"] != SCHEMA or payload["target_triple"] != target:
        raise ArtifactError("artifact manifest contract mismatch")
    if not isinstance(payload["entries"], list):
        raise ArtifactError("invalid artifact manifest entries")
    return payload


def _safe_extract(
    bundle: tarfile.TarFile,
    members: list[tarfile.TarInfo],
    destination: Path,
    target: str,
) -> None:
    executable, support = _names(target)
    destination.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or not destination.is_dir():
        raise ArtifactError("invalid extraction destination")
    if (destination / executable).exists() or (destination / support).exists():
        raise ArtifactError("artifact destination already contains sidecar payload")

    directories = [member for member in members if member.isdir()]
    files = [member for member in members if member.isreg()]
    symlinks = [member for member in members if member.issym()]
    for member in sorted(directories, key=lambda item: len(PurePosixPath(item.name).parts)):
        destination.joinpath(*PurePosixPath(member.name).parts).mkdir(mode=0o700)
    for member in files:
        target_path = destination.joinpath(*PurePosixPath(member.name).parts)
        source = bundle.extractfile(member)
        if source is None:
            raise ArtifactError("archive file payload is unavailable")
        with target_path.open("xb") as output:
            shutil.copyfileobj(source, output)
        target_path.chmod(member.mode & 0o777)
    for member in symlinks:
        target_path = destination.joinpath(*PurePosixPath(member.name).parts)
        os.symlink(member.linkname, target_path)
    for member in sorted(
        directories,
        key=lambda item: len(PurePosixPath(item.name).parts),
        reverse=True,
    ):
        destination.joinpath(*PurePosixPath(member.name).parts).chmod(member.mode & 0o777)


def verify_extract(
    archive: Path,
    digest_path: Path,
    manifest_path: Path,
    destination: Path,
    target: str,
) -> None:
    _load_digest(archive, digest_path)
    expected = _load_manifest(manifest_path, target)
    try:
        with tarfile.open(archive, "r:") as bundle:
            entries, members = _archive_inventory(bundle, target)
            if _manifest(target, entries) != expected:
                raise ArtifactError("archive inventory does not match artifact manifest")
            _safe_extract(bundle, members, destination, target)
    except (OSError, tarfile.TarError) as exc:
        raise ArtifactError("invalid sidecar archive") from exc
    if _manifest(target, _filesystem_inventory(destination, target)) != expected:
        raise ArtifactError("extracted inventory does not match artifact manifest")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pack or verify a Darwin sidecar CI artifact.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("pack", "verify-extract"):
        child = subparsers.add_parser(command)
        child.add_argument("--target-triple", required=True)
        child.add_argument("--archive", type=Path, required=True)
        child.add_argument("--digest", type=Path, required=True)
        child.add_argument("--manifest", type=Path, required=True)
        if command == "pack":
            child.add_argument("--binaries-root", type=Path, required=True)
        else:
            child.add_argument("--destination", type=Path, required=True)
    return parser


def main(arguments: list[str] | None = None) -> int:
    args = _parser().parse_args(arguments)
    try:
        if args.command == "pack":
            pack(args.binaries_root, args.target_triple, args.archive, args.digest, args.manifest)
        else:
            verify_extract(
                args.archive,
                args.digest,
                args.manifest,
                args.destination,
                args.target_triple,
            )
    except ArtifactError as exc:
        print(f"sidecar artifact failed: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print("sidecar artifact failed: filesystem operation failed", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "command": args.command}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
