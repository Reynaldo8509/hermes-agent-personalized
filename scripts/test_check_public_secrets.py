import importlib.util
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "check_public_secrets", Path(__file__).with_name("check-public-secrets.py")
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PublicSecretScanTests(unittest.TestCase):
    def test_detects_hex_fallback_without_disclosing_value(self):
        line = 'const SECRET = process.env.INTERNAL_SECRET || "' + "a" * 64 + '";'
        result = MODULE.findings([("src/api/route.ts", 8, line)])
        self.assertEqual(result, [("src/api/route.ts", 8, "long hexadecimal credential assignment")])

    def test_detects_hardcoded_mailbox_setting(self):
        mailbox = "person@" + "private.invalid"
        result = MODULE.findings([("src/auth.ts", 4, f'const MILO_EMAIL = "{mailbox}";')])
        self.assertEqual(result, [("src/auth.ts", 4, "hardcoded email identity")])

    def test_ignores_env_configuration_and_example_placeholder(self):
        lines = [
            ("src/auth.ts", 1, "const token = process.env.API_TOKEN;"),
            (".env.example", 2, "API_TOKEN=your-token-here"),
            ("docs/setup.md", 3, 'Use EMAIL="user@example.com" as a placeholder.'),
        ]
        self.assertEqual(MODULE.findings(lines), [])

    def test_detects_private_key_marker(self):
        result = MODULE.findings([("keys.txt", 1, "-----BEGIN " + "PRIVATE KEY-----")])
        self.assertEqual(result[0][2], "private-key material")

    def test_detects_provider_token_without_disclosing_value(self):
        token = "ghp_" + "x" * 24
        result = MODULE.findings([("src/config.ts", 9, f'const API_TOKEN = "{token}";')])
        self.assertEqual(result, [("src/config.ts", 9, "provider token pattern")])


if __name__ == "__main__":
    unittest.main()
