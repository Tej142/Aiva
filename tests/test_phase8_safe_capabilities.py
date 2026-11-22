"""Unit tests for Phase 8: safe capabilities (web_fetch, calculate, SSRF defenses)."""

import asyncio
from unittest.mock import MagicMock, patch
import unittest

from core.agent.policy import RISK_READ
from core.agent.tools.builtin import register_builtin_tools
from core.agent.tools.calculator import safe_calculate
from core.agent.tools.registry import ToolContext, ToolRegistry
from core.agent.tools.web_fetch import (
    HTMLTextExtractor,
    extract_content_text,
    fetch_url_safely,
    validate_url_for_ssrf,
)


class TestWebFetchSSRFProtection(unittest.TestCase):
    """Verify comprehensive SSRF protection on web_fetch."""

    def test_forbidden_schemes(self):
        blocked_schemes = [
            "file:///etc/passwd",
            "file:///C:/Windows/win.ini",
            "ftp://ftp.example.com/file",
            "gopher://127.0.0.1:70/",
            "dict://127.0.0.1:2628/",
            "data:text/html,<html>alert(1)</html>",
            "javascript:alert(1)",
        ]
        for url in blocked_schemes:
            valid, reason, _ = validate_url_for_ssrf(url)
            self.assertFalse(valid, f"Expected {url} to be blocked by SSRF check")
            self.assertIn("scheme", reason.lower())

    def test_blocked_ip_ranges(self):
        blocked_ips = [
            # Loopback
            "http://127.0.0.1/admin",
            "http://127.0.1.1:8080/status",
            "http://[::1]/metrics",
            # Cloud metadata
            "http://169.254.169.254/latest/meta-data/",
            "http://169.254.1.1/internal",
            "http://[fe80::1]/link-local",
            # RFC 1918 Private networks
            "http://10.0.0.1/console",
            "http://10.255.255.255/api",
            "http://172.16.0.1/dashboard",
            "http://172.31.255.255/keys",
            "http://192.168.1.1/router",
            "http://192.168.0.254/config",
            # Unspecified & IPv6 Unique Local
            "http://0.0.0.0:8000/",
            "http://[fc00::1]/internal",
            "http://[fd12:3456:789a::1]/data",
            # IPv4 mapped IPv6 loopback / metadata
            "http://[::ffff:127.0.0.1]/",
            "http://[::ffff:169.254.169.254]/",
        ]
        for url in blocked_ips:
            valid, reason, _ = validate_url_for_ssrf(url)
            self.assertFalse(valid, f"Expected {url} to be blocked by SSRF check")

    def test_blocked_hostnames_and_numeric_evasion(self):
        blocked_hosts = [
            "http://localhost/admin",
            "http://localhost:5000/api",
            "http://localhost.localdomain/test",
            "http://myhost.localhost/api",
            "http://internal.local/",
            # Integer decimal notation for 127.0.0.1 (2130706433)
            "http://2130706433/",
            # Hex notation for 127.0.0.1
            "http://0x7f000001/",
        ]
        for url in blocked_hosts:
            valid, reason, _ = validate_url_for_ssrf(url)
            self.assertFalse(valid, f"Expected {url} to be blocked by SSRF check")

    def test_dns_resolution_private_ip_blocked(self):
        with patch("socket.getaddrinfo") as mock_gai:
            # Simulate DNS returning a private 10.0.0.5 IP
            mock_gai.return_value = [
                (2, 1, 6, "", ("10.0.0.5", 80))
            ]
            valid, reason, _ = validate_url_for_ssrf("http://intranet.company-internal.net/portal")
            self.assertFalse(valid)
            self.assertIn("10.0.0.5", reason)

    def test_fetch_url_safely_blocks_ssrf(self):
        res = fetch_url_safely("http://169.254.169.254/latest/meta-data")
        self.assertEqual(res.get("status"), "blocked")
        self.assertEqual(res.get("error"), "ssrf_blocked")

    def test_fetch_url_safely_redirect_interception(self):
        # Test that following a redirect to a blocked SSRF target is caught
        with patch("requests.Session.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 302
            mock_resp.headers = {"Location": "http://127.0.0.1:8000/stolen-token"}
            mock_get.return_value = mock_resp

            res = fetch_url_safely("https://trusted-external.com/redirect-service")
            self.assertEqual(res.get("status"), "blocked")
            self.assertEqual(res.get("error"), "ssrf_blocked")


class TestHTMLTextExtraction(unittest.TestCase):
    """Verify HTML cleanup, tag stripping, and size capping."""

    def test_extract_clean_text_and_title(self):
        html_sample = (
            "<!DOCTYPE html>"
            "<html>"
            "<head>"
            "  <title>Engineering Blog</title>"
            "  <style>body { font-family: sans-serif; }</style>"
            "  <script>window.__SECRET = 'leak';</script>"
            "</head>"
            "<body>"
            "  <h1>SQLite Architecture</h1>"
            "  <p>SQLite supports <b>WAL</b> mode for concurrent readers and writers.</p>"
            "  <noscript>Please enable javascript</noscript>"
            "  <div>Read more below.</div>"
            "</body>"
            "</html>"
        )
        text, title, truncated = extract_content_text(html_sample.encode("utf-8"), "text/html; charset=utf-8")
        self.assertEqual(title, "Engineering Blog")
        self.assertIn("SQLite Architecture", text)
        self.assertIn("SQLite supports WAL mode", text)
        self.assertNotIn("font-family", text)
        self.assertNotIn("window.__SECRET", text)
        self.assertNotIn("Please enable javascript", text)
        self.assertFalse(truncated)

    def test_character_truncation(self):
        long_content = "A" * 10000
        text, title, truncated = extract_content_text(long_content.encode("utf-8"), "text/plain", max_chars=100)
        self.assertTrue(truncated)
        self.assertTrue(text.startswith("A" * 100))
        self.assertIn("[Content truncated", text)


class TestSafeCalculator(unittest.TestCase):
    """Verify restricted AST arithmetic evaluation and unit conversions without eval/exec."""

    def test_arithmetic_operations(self):
        cases = [
            ("(45 * 12) / 3", 180),
            ("10 + 20 * 3 - 5", 65),
            ("2 ** 8", 256),
            ("100 // 6", 16),
            ("100 % 6", 4),
            ("-(-15) + (+5)", 20),
        ]
        for expr, expected in cases:
            res = safe_calculate(expr)
            self.assertEqual(res.get("status"), "success", f"Failed on '{expr}': {res}")
            self.assertEqual(res.get("result"), expected)

    def test_math_functions_and_constants(self):
        res = safe_calculate("sqrt(144) + 10")
        self.assertEqual(res.get("status"), "success")
        self.assertEqual(res.get("result"), 22)

        res2 = safe_calculate("round(pi, 4)")
        self.assertEqual(res2.get("status"), "success")
        self.assertEqual(res2.get("result"), 3.1416)

        res3 = safe_calculate("abs(-42) + floor(4.9)")
        self.assertEqual(res3.get("status"), "success")
        self.assertEqual(res3.get("result"), 46)

    def test_percentage_calculations(self):
        res1 = safe_calculate("15% of 80")
        self.assertEqual(res1.get("status"), "success")
        self.assertEqual(res1.get("result"), 12)

        res2 = safe_calculate("20% * 150")
        self.assertEqual(res2.get("status"), "success")
        self.assertEqual(res2.get("result"), 30)

    def test_unit_conversions(self):
        # Length
        res_len = safe_calculate("100 km to miles")
        self.assertEqual(res_len.get("status"), "success")
        self.assertAlmostEqual(res_len.get("result"), 62.1371, places=3)

        # Mass
        res_mass = safe_calculate("5 kg in lbs")
        self.assertEqual(res_mass.get("status"), "success")
        self.assertAlmostEqual(res_mass.get("result"), 11.0231, places=3)

        # Temperature
        res_temp_f2c = safe_calculate("32 fahrenheit in celsius")
        self.assertEqual(res_temp_f2c.get("status"), "success")
        self.assertEqual(res_temp_f2c.get("result"), 0.0)

        res_temp_c2f = safe_calculate("100 celsius to fahrenheit")
        self.assertEqual(res_temp_c2f.get("status"), "success")
        self.assertEqual(res_temp_c2f.get("result"), 212.0)

        # Data
        res_data = safe_calculate("16 gb in mb")
        self.assertEqual(res_data.get("status"), "success")
        self.assertEqual(res_data.get("result"), 16384)

        # Incompatible units error
        res_bad = safe_calculate("10 km to kg")
        self.assertEqual(res_bad.get("status"), "error")
        self.assertEqual(res_bad.get("error"), "incompatible_units")

    def test_zero_division(self):
        res = safe_calculate("10 / 0")
        self.assertEqual(res.get("status"), "error")
        self.assertEqual(res.get("error"), "zero_division")

    def test_denial_of_service_exponent_guard(self):
        res = safe_calculate("2 ** 9999999")
        self.assertEqual(res.get("status"), "error")
        self.assertIn("Exponent too large", res.get("message", ""))

    def test_security_rejection_of_arbitrary_code(self):
        malicious = [
            "__import__('os').system('dir')",
            "open('C:/Windows/win.ini').read()",
            "eval('2 + 2')",
            "exec('x = 1')",
            "[x for x in range(10)]",
            "(lambda x: x + 1)(5)",
            "globals()",
        ]
        for code in malicious:
            res = safe_calculate(code)
            self.assertEqual(res.get("status"), "error", f"Malicious code was not rejected: '{code}'")


class TestToolsRegistrationAndPolicy(unittest.TestCase):
    """Verify tool registration, schemas, channel allowlists, and untrusted fencing."""

    def setUp(self):
        self.reg = ToolRegistry()
        register_builtin_tools(self.reg)

    def test_web_fetch_registration(self):
        spec = self.reg.get("web_fetch")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.risk, RISK_READ)
        self.assertEqual(spec.allowed_channels, ("hud", "voice", "whatsapp"))
        self.assertTrue(spec.untrusted_result)
        self.assertIn("url", spec.parameters["properties"])
        self.assertIn("url", spec.parameters["required"])

    def test_calculate_registration(self):
        spec = self.reg.get("calculate")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.risk, RISK_READ)
        self.assertEqual(spec.allowed_channels, ("hud", "voice", "whatsapp"))
        self.assertFalse(spec.untrusted_result)
        self.assertIn("expression", spec.parameters["properties"])
        self.assertIn("expression", spec.parameters["required"])

    def test_calculate_execution_via_registry(self):
        ctx = ToolContext(user_id="u_test", channel="hud")
        res = asyncio.run(self.reg.execute("calculate", {"expression": "sqrt(16) * 5"}, ctx))
        self.assertEqual(res.get("status"), "success")
        self.assertEqual(res.get("result"), 20)


if __name__ == "__main__":
    unittest.main()

