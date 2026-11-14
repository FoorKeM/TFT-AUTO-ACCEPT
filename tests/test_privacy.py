"""Los diagnósticos no persisten datos incluidos en fallos inesperados."""

import ast
import json
from pathlib import Path
import re
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from runtime_check import run_check as runtime_run_check
from ui_check import run_check as ui_run_check


class DiagnosticPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.report = Path(self.folder.name, "report.json")
        self.secret = "sentinel-private-authorization-token"
        self.private_path = r"C:\Users\private-test-account\secrets\token.txt"
        self.root = Mock()
        self.app_factory = Mock(side_effect=RuntimeError(f"{self.secret}: {self.private_path}"))
        self.app_factory.__module__ = __name__

    def assert_private_report(self, status):
        self.assertEqual(status, 1)
        self.app_factory.assert_called_once()
        self.root.destroy.assert_called_once()
        contents = self.report.read_text(encoding="utf-8")
        report = json.loads(contents)
        self.assertFalse(report["ok"])
        self.assertEqual(report["error"], "RuntimeError")
        self.assertNotIn(self.secret, contents)
        self.assertNotIn("private-test-account", contents)
        self.assertNotIn(self.private_path, contents)

    def test_ui_report_redacts_unexpected_constructor_error(self):
        tray = ModuleType("pystray")
        tray.__path__ = []
        tray._win32 = ModuleType("pystray._win32")
        with patch.dict(sys.modules, {"pystray": tray, "pystray._win32": tray._win32}):
            with patch("ui_check.ctypes.WinDLL"), patch("ui_check.tk.Tk", return_value=self.root):
                with patch("requests.Session.request") as request:
                    status = ui_run_check(self.report, self.app_factory)
        request.assert_not_called()
        self.assert_private_report(status)

    def test_runtime_report_redacts_unexpected_constructor_error(self):
        with patch("runtime_check.tk.Tk", return_value=self.root):
            with patch("requests.Session.request") as request:
                status = runtime_run_check(self.report, self.app_factory)
        request.assert_not_called()
        self.assert_private_report(status)

    def test_production_literals_do_not_contain_credentials_or_personal_paths(self):
        patterns = {
            "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
            "private_key": re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
            "windows_user": re.compile(r"(?i)\b[a-z]:[\\/]Users[\\/][^\\/\s<>${}%]+"),
            "unix_user": re.compile(r"(?<![\w.:/])/(?:home|Users)/[^/\s<>${}%]+"),
            "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"),
        }
        # Ejemplos ficticios dentro de tests, nunca credenciales de una cuenta.
        examples = {
            "jwt": "eyJ0ZXN0Ijoib25seSJ9.eyJzdWIiOiJmaWN0aWNpbyJ9.fake-signature",
            "private_key": "-----BEGIN PRIVATE KEY-----",
            "windows_user": r"C:\Users\privacy-fixture\file.txt",
            "unix_user": "/home/privacy-fixture/file.txt",
            "github_token": "ghp_" + "A" * 36,
        }
        for category, example in examples.items():
            with self.subTest(detector=category):
                self.assertTrue(bool(patterns[category].search(example)))
        self.assertTrue(bool(patterns["unix_user"].search("/Users/privacy-fixture/file.txt")))
        for generic in (r"C:\Windows\System32", r"D:\Riot Games\lockfile",
                        "https://example.invalid/home/documentation", "¡Partida encontrada!"):
            with self.subTest(generic=generic):
                self.assertFalse(any(pattern.search(generic) for pattern in patterns.values()))

        violations = []
        for source in sorted(Path(__file__).resolve().parents[1].glob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=source.name)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                for category, pattern in patterns.items():
                    if pattern.search(node.value):
                        # Mostrar solo ubicación/categoría: nunca la cadena detectada.
                        violations.append(f"{source.name}:{node.lineno} ({category})")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
