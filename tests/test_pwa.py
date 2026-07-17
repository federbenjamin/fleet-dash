import json
import os
import struct
import unittest

from server import STATIC_FILES


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class PwaContractTest(unittest.TestCase):
    def read(self, path, mode="r"):
        with open(os.path.join(ROOT, path), mode) as handle:
            return handle.read()

    def test_manifest_has_stable_identity_scope_and_install_icons(self):
        manifest = json.loads(self.read("static/manifest.webmanifest"))
        self.assertEqual(manifest["id"], "/")
        self.assertEqual(manifest["start_url"], "/#now")
        self.assertEqual(manifest["scope"], "/")
        self.assertEqual(manifest["display"], "standalone")
        icons = {(item["sizes"], item["purpose"]): item["src"]
                 for item in manifest["icons"]}
        self.assertIn(("192x192", "any"), icons)
        self.assertIn(("512x512", "any"), icons)
        self.assertIn(("512x512", "maskable"), icons)
        for (size, _purpose), path in icons.items():
            data = self.read(path.lstrip("/"), "rb")
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
            width, height = struct.unpack(">II", data[16:24])
            expected = int(size.split("x", 1)[0])
            self.assertEqual((width, height), (expected, expected))

    def test_service_worker_is_root_scoped_and_private_data_is_network_only(self):
        worker = self.read("static/sw.js")
        self.assertIn('url.pathname.startsWith(\'/api/\')', worker)
        self.assertIn("request.mode === 'navigate'", worker)
        self.assertIn("fetch(request).catch(() => caches.match('/static/offline.html'))", worker)
        self.assertIn("const SHELL_CACHE = 'fleet-shell-n4-v1'", worker)
        self.assertIn("fetch(request).then(response =>", worker)
        self.assertIn("catch(() => caches.match(request))", worker)
        self.assertNotIn("'/api/", worker.split("const SHELL_ASSETS", 1)[1].split("];", 1)[0])
        self.assertNotIn("'/#", worker.split("const SHELL_ASSETS", 1)[1].split("];", 1)[0])
        self.assertEqual(STATIC_FILES["/sw.js"][0], "static/sw.js")
        self.assertEqual(STATIC_FILES["/sw.js"][3]["Service-Worker-Allowed"], "/")

    def test_dashboard_declares_manifest_theme_and_touch_icon(self):
        dashboard = self.read("dashboard.html")
        self.assertIn('rel="manifest" href="/static/manifest.webmanifest"', dashboard)
        self.assertIn('name="theme-color" content="#0a0e14"', dashboard)
        self.assertIn('rel="apple-touch-icon" href="/static/icons/fleet-192.png"', dashboard)


if __name__ == "__main__":
    unittest.main()
