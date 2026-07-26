from __future__ import annotations

import time
import unittest

from starbridge_mcp.core.security import (
    contains_sensitive_text,
    redact_path,
    sanitize_details,
    sanitize_path,
    sanitize_text,
)

BANNED_OUTPUT_FRAGMENTS = ("C:\\Users\\", "/Users/", "/home/", "AppData", "Desktop", "Documents")


class SecuritySanitizerTests(unittest.TestCase):
    def assert_clean(self, value: object) -> None:
        text = str(value)
        for fragment in BANNED_OUTPUT_FRAGMENTS:
            self.assertNotIn(fragment, text)
        self.assertFalse(contains_sensitive_text(value))

    def test_sanitize_path_redacts_common_private_paths(self) -> None:
        windows_home = "C:" + "\\Users\\SomeName"
        samples = [
            windows_home + "\\Desktop\\file" + ".psd",
            windows_home + "\\AppData\\Local\\Adobe",
            "/Users/somename/Documents/file" + ".ai",
            "/home/somename/models/model" + ".safetensors",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                sanitized = sanitize_path(sample)
                self.assert_clean(sanitized)

    def test_redact_path_public_alias(self) -> None:
        sanitized = redact_path("C:" + "\\Users\\SomeName\\Desktop\\source.png")
        self.assertIn("<REDACTED_PATH>", sanitized)
        self.assert_clean(sanitized)

    def test_sanitize_text_preserves_normal_bridge_text(self) -> None:
        text = "Photoshop 修图桥 当前未完全就绪，详见 details.notes。"
        self.assertEqual(sanitize_text(text), text)
        self.assert_clean(sanitize_text(text))

    def test_long_png_data_url_is_unchanged_and_sanitizes_under_five_seconds(self) -> None:
        data_url = "data:image/png;base64," + ("A" * 36_114)

        started = time.perf_counter()
        sanitized = sanitize_text(data_url)
        elapsed = time.perf_counter() - started

        self.assertEqual(data_url, sanitized)
        self.assertLess(elapsed, 5.0)

    def test_long_png_data_url_sensitive_check_completes_under_five_seconds(self) -> None:
        data_url = "data:image/png;base64," + ("A" * 36_114)

        started = time.perf_counter()
        sensitive = contains_sensitive_text(data_url)
        elapsed = time.perf_counter() - started

        self.assertFalse(sensitive)
        self.assertLess(elapsed, 5.0)

    def test_sensitive_extension_matching_preserves_existing_boundaries(self) -> None:
        sensitive_samples = (
            "exports/CLIENT.PSD",
            "models/portrait.SaFeTeNsOrS",
            r"relative\scene.AEPX",
        )
        for sample in sensitive_samples:
            with self.subTest(sample=sample):
                self.assertEqual("<SENSITIVE_FILE>", sanitize_path(sample))
                self.assertTrue(contains_sensitive_text(sample))

        ordinary_samples = (
            "notes.psdraft",
            "vector.ai2",
            "checkpoint.safetensors_backup",
            "普通 bridge 文本与 image/png data URL",
        )
        for sample in ordinary_samples:
            with self.subTest(sample=sample):
                self.assertEqual(sample, sanitize_path(sample))
                self.assertFalse(contains_sensitive_text(sample))

    def test_malformed_uri_like_text_does_not_raise(self) -> None:
        samples = (
            "https://exa／mple.com/path",
            "file://local／host/tmp/path",
            "file:////tmp/secret",
            "file://localhost//tmp/secret",
            "https://e.test/?local=%252Ftmp%252Fsecret",
            "file:///tmp%252Fsecret",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertIsInstance(sanitize_path(sample), str)
                self.assertIsInstance(contains_sensitive_text(sample), bool)

    def test_sanitize_details_recurses_dicts_and_lists(self) -> None:
        payload = {
            "bridge": "illustrator",
            "details": [
                {"path": "C:" + "\\Users\\SomeName\\Documents\\client" + ".ai"},
                {"model": "/home/somename/models/model" + ".safetensors"},
            ],
        }
        sanitized = sanitize_details(payload)
        self.assertEqual(sanitized["bridge"], "illustrator")
        self.assert_clean(sanitized)


if __name__ == "__main__":
    unittest.main()
