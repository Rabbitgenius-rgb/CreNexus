from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "apps" / "starbridge-desktop" / "scripts" / "sidecar_artifact.py"
TARGET = "aarch64-apple-darwin"
EXECUTABLE = f"starbridge-sidecar-{TARGET}"
SUPPORT = f"_internal-{TARGET}"


class SidecarArtifactTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="KORYAO artifact ")
        self.root = Path(self.temporary.name)
        self.binaries = self.root / "binaries"
        self.destination = self.root / "destination"
        self.archive = self.root / "sidecar.tar"
        self.digest = self.root / "sidecar.tar.sha256"
        self.manifest = self.root / "sidecar.manifest.json"
        self.binaries.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_helper(self, command: str, *, expect_success: bool) -> subprocess.CompletedProcess[str]:
        arguments = [
            sys.executable,
            "-I",
            os.fspath(HELPER),
            command,
            "--target-triple",
            TARGET,
            "--archive",
            os.fspath(self.archive),
            "--digest",
            os.fspath(self.digest),
            "--manifest",
            os.fspath(self.manifest),
        ]
        if command == "pack":
            arguments.extend(("--binaries-root", os.fspath(self.binaries)))
        else:
            arguments.extend(("--destination", os.fspath(self.destination)))
        completed = subprocess.run(arguments, capture_output=True, text=True, check=False)
        self.assertEqual(expect_success, completed.returncode == 0, completed.stderr)
        if not expect_success:
            self.assertNotIn(os.fspath(self.root), completed.stderr)
        return completed

    def make_valid_source(self) -> None:
        executable = self.binaries / EXECUTABLE
        executable.write_bytes(b"Mach-O fixture")
        executable.chmod(0o755)
        dylibs = self.binaries / SUPPORT / "cv2" / ".dylibs"
        dylibs.mkdir(parents=True)
        (dylibs / "libfixture.dylib").write_bytes(b"dylib")
        os.symlink("cv2/.dylibs/libfixture.dylib", self.binaries / SUPPORT / "libfixture.dylib")

    def pack_valid(self) -> None:
        self.make_valid_source()
        self.run_helper("pack", expect_success=True)

    def write_digest(self) -> None:
        value = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.digest.write_text(f"{value}  {self.archive.name}\n", encoding="ascii")

    def write_dummy_manifest(self) -> None:
        self.manifest.write_text(
            json.dumps(
                {
                    "schema": "starbridge.sidecar-artifact.v1",
                    "target_triple": TARGET,
                    "entries": [],
                }
            ),
            encoding="utf-8",
        )

    @staticmethod
    def add_file(bundle: tarfile.TarFile, name: str, mode: int = 0o644) -> None:
        payload = b"fixture"
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        info.mode = mode
        bundle.addfile(info, io.BytesIO(payload))

    def write_malicious(self, mutation: str) -> None:
        with tarfile.open(self.archive, "w") as bundle:
            self.add_file(bundle, EXECUTABLE, 0o755)
            support = tarfile.TarInfo(SUPPORT)
            support.type = tarfile.DIRTYPE
            support.mode = 0o755
            bundle.addfile(support)
            if mutation in {"absolute", "dotdot", "casefold", "backslash_member"}:
                names = {
                    "absolute": "/absolute",
                    "dotdot": f"{SUPPORT}/../escape",
                    "casefold": f"{SUPPORT}/File",
                    "backslash_member": f"{SUPPORT}/bad\\name",
                }
                self.add_file(bundle, names[mutation])
                if mutation == "casefold":
                    self.add_file(bundle, f"{SUPPORT}/file")
            elif mutation == "duplicate":
                self.add_file(bundle, f"{SUPPORT}/duplicate")
                self.add_file(bundle, f"{SUPPORT}/duplicate")
            elif mutation == "hardlink":
                info = tarfile.TarInfo(f"{SUPPORT}/hardlink")
                info.type = tarfile.LNKTYPE
                info.linkname = EXECUTABLE
                bundle.addfile(info)
            elif mutation in {"escaping_symlink", "beneath_symlink", "backslash_symlink"}:
                info = tarfile.TarInfo(f"{SUPPORT}/link")
                info.type = tarfile.SYMTYPE
                if mutation == "escaping_symlink":
                    info.linkname = "../../outside"
                elif mutation == "backslash_symlink":
                    info.linkname = "cv2\\.dylibs\\libfixture.dylib"
                else:
                    info.linkname = "cv2"
                bundle.addfile(info)
                if mutation == "beneath_symlink":
                    self.add_file(bundle, f"{SUPPORT}/link/file")
            elif mutation == "special":
                info = tarfile.TarInfo(f"{SUPPORT}/fifo")
                info.type = tarfile.FIFOTYPE
                bundle.addfile(info)
        self.write_digest()
        self.write_dummy_manifest()

    def test_valid_roundtrip_preserves_hidden_dylib_symlink_and_0755(self) -> None:
        self.pack_valid()
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertTrue(any(entry["path"].endswith("cv2/.dylibs") for entry in payload["entries"]))
        self.run_helper("verify-extract", expect_success=True)
        executable = self.destination / EXECUTABLE
        link = self.destination / SUPPORT / "libfixture.dylib"
        self.assertEqual(0o755, stat.S_IMODE(executable.stat().st_mode))
        self.assertTrue(link.is_symlink())
        self.assertEqual("cv2/.dylibs/libfixture.dylib", os.readlink(link))
        self.assertEqual(b"dylib", link.read_bytes())

    def test_unsafe_tar_members_fail_closed(self) -> None:
        expected_errors = {
            "absolute": "unsafe archive entry",
            "dotdot": "unsafe archive entry",
            "backslash_member": "unsafe archive entry",
            "hardlink": "hard links are not allowed",
            "escaping_symlink": "archive symlink escapes support directory",
            "backslash_symlink": "unsafe archive symlink",
            "special": "special archive entries are not allowed",
            "duplicate": "duplicate archive entry",
            "casefold": "case-insensitive archive entry collision",
            "beneath_symlink": "entry beneath a symlink",
        }
        for mutation, expected_error in expected_errors.items():
            with self.subTest(mutation=mutation):
                self.archive.unlink(missing_ok=True)
                self.digest.unlink(missing_ok=True)
                self.manifest.unlink(missing_ok=True)
                self.write_malicious(mutation)
                completed = self.run_helper("verify-extract", expect_success=False)
                self.assertIn(expected_error, completed.stderr)

    def test_digest_tamper_fails_closed(self) -> None:
        self.pack_valid()
        self.archive.write_bytes(self.archive.read_bytes() + b"tamper")
        completed = self.run_helper("verify-extract", expect_success=False)
        self.assertIn("archive digest mismatch", completed.stderr)

    def test_manifest_tamper_fails_closed(self) -> None:
        self.pack_valid()
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        payload["entries"][0]["mode"] ^= 1
        self.manifest.write_text(json.dumps(payload), encoding="utf-8")
        completed = self.run_helper("verify-extract", expect_success=False)
        self.assertIn("archive inventory does not match artifact manifest", completed.stderr)

    def test_archive_mode_tamper_fails_closed(self) -> None:
        self.pack_valid()
        rewritten = self.root / "rewritten.tar"
        with tarfile.open(self.archive, "r:") as source, tarfile.open(rewritten, "w") as output:
            for member in source.getmembers():
                if member.name == EXECUTABLE:
                    member.mode = 0o644
                stream = source.extractfile(member) if member.isreg() else None
                output.addfile(member, stream)
        rewritten.replace(self.archive)
        self.write_digest()
        completed = self.run_helper("verify-extract", expect_success=False)
        self.assertIn("archive inventory does not match artifact manifest", completed.stderr)


if __name__ == "__main__":
    unittest.main()
