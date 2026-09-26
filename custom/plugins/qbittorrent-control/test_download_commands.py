import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock


PLUGIN_PATH = str(Path(__file__).with_name("__init__.py"))
MAGNET = "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=Sintel"


def load_plugin():
    secret_scope = types.ModuleType("agent.secret_scope")
    secret_scope.get_secret = lambda *_args: ""
    agent = types.ModuleType("agent")
    agent.secret_scope = secret_scope
    sys.modules["agent"] = agent
    sys.modules["agent.secret_scope"] = secret_scope
    spec = importlib.util.spec_from_file_location("test_qbittorrent_plugin", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeContext:
    def __init__(self):
        self.commands = {}
        self.tools = {}

    def register_command(self, name, handler, *_args):
        self.commands[name] = handler

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs


class DownloadCommandTests(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin()

    def test_name_request_routes_once_to_smart_tool(self):
        self.plugin._torrent_search_download = Mock(return_value="smart-result")
        result = self.plugin._media_command(
            "Descarga una única copia de Sintel (2010), versión 1080p MKV."
        )
        self.assertEqual(result, "smart-result")
        self.plugin._torrent_search_download.assert_called_once_with({
            "query": "Sintel (2010) 1080p MKV",
            "media_type": "video",
        })

    def test_simple_name_uses_defaults_without_extra_policy(self):
        self.plugin._torrent_search_download = Mock(return_value="smart-result")
        self.plugin._media_command("Sintel 2010")
        self.plugin._torrent_search_download.assert_called_once_with({
            "query": "Sintel 2010",
            "media_type": "video",
        })

    def test_empty_media_command_does_not_invoke_any_download(self):
        self.plugin._torrent_search_download = Mock()
        self.plugin._add = Mock()
        result = self.plugin._media_command("   ")
        self.assertIn("Falta el nombre", result)
        self.plugin._torrent_search_download.assert_not_called()
        self.plugin._add.assert_not_called()

    def test_direct_media_magnet_keeps_original_handler(self):
        self.plugin._torrent_search_download = Mock()
        self.plugin._add = Mock(return_value="direct-result")
        result = self.plugin._media_command(MAGNET)
        self.assertEqual(result, "direct-result")
        self.plugin._add.assert_called_once_with(MAGNET, self.plugin.MEDIA_PATH)
        self.plugin._torrent_search_download.assert_not_called()

    def test_name_request_returns_compact_success_message(self):
        self.plugin._torrent_search_download = Mock(return_value={
            "ok": True,
            "state": "started",
            "selected": {
                "torrent_name": "Sintel 2010 1080p WEB-DL Amazon",
                "file_name": "Sintel/Sintel.2010.1080p.mkv",
                "file_size_gib": 1.092,
                "language": "LAT",
                "quality": "WEB-DL",
                "source": "Amazon",
                "magnet_uri": "magnet:?secret-test-value",
            },
            "preferences": {"resolution": "1080p", "extension": ".mkv"},
            "search_diagnostics": {"queries": ["must not be shown"]},
        })
        result = self.plugin._media_command("Sintel 2010 1080p MKV")
        self.assertIn("✅ Descarga iniciada", result)
        self.assertIn("Archivo: Sintel.2010.1080p.mkv", result)
        self.assertIn("Resolución: 1080p", result)
        self.assertIn("Tipo: MKV", result)
        self.assertIn("Tamaño: 1.09 GiB", result)
        self.assertIn("Idioma: LAT", result)
        self.assertIn("Versión: WEB-DL", result)
        self.assertIn("Origen: Amazon", result)
        self.assertNotIn("magnet", result.casefold())
        self.assertNotIn("search_diagnostics", result)

    def test_name_request_returns_compact_error_message(self):
        self.plugin._torrent_search_download = Mock(return_value={
            "ok": False,
            "state": "failed",
            "error": "NO_VALID_CANDIDATES",
            "preflight_attempts": 3,
            "rejections": [{"infohash": "must not be shown"}],
        })
        result = self.plugin._media_command("Sintel 2010")
        self.assertIn("❌ Descarga no iniciada", result)
        self.assertIn("Ningún candidato cumplió las políticas", result)
        self.assertIn("Candidatos revisados: 3", result)
        self.assertNotIn("infohash", result)

    def test_registration_has_one_handler_per_download_command(self):
        ctx = FakeContext()
        self.plugin.register(ctx)
        self.assertEqual(
            set(("descarga", "descarga-media", "descarga-archivo")),
            set(ctx.commands).intersection({"descarga", "descarga-media", "descarga-archivo"}),
        )
        self.assertIs(ctx.commands["descarga"], self.plugin._generic_add)
        self.assertIs(ctx.commands["descarga-media"], self.plugin._media_command)
        self.assertEqual(len(ctx.tools), 1)
        self.assertIs(ctx.tools["torrent_search_download"]["handler"], self.plugin._torrent_search_download)


if __name__ == "__main__":
    unittest.main(verbosity=2)
