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

    def test_service_worker_caches_only_shell_and_the_last_exact_fleet_snapshot(self):
        worker = self.read("static/sw.js")
        self.assertIn('url.pathname.startsWith(\'/api/\')', worker)
        self.assertIn("if (url.pathname === '/api/fleet')", worker)
        self.assertIn("cache.put('/api/fleet', response.clone())", worker)
        self.assertIn("if (url.pathname.startsWith('/api/')) return;", worker)
        self.assertIn("request.mode === 'navigate'", worker)
        self.assertIn("catch(async () => (await caches.match('/')) || caches.match('/static/offline.html'))", worker)
        self.assertIn("const SHELL_CACHE = 'fleet-shell-n14-v1'", worker)
        shell_assets = worker.split("const SHELL_ASSETS", 1)[1].split("];", 1)[0]
        for module in ("main.js", "state-store.js", "cards.js", "workspace.js"):
            self.assertIn(f"'/static/js/{module}'", shell_assets)
        for font in ("SpaceGrotesk-var.woff2", "IBMPlexMono-Regular.woff2",
                     "IBMPlexMono-Medium.woff2", "IBMPlexMono-SemiBold.woff2"):
            self.assertIn(f"'/static/fonts/{font}'", shell_assets)
        self.assertNotIn("'/static/app.js'", shell_assets)
        self.assertIn("const RUNTIME_CACHE = 'fleet-runtime-n6-v1'", worker)
        self.assertIn("fetch(request).then(response =>", worker)
        self.assertIn("catch(() => caches.match(url.pathname))", worker)
        self.assertNotIn("'/api/", worker.split("const SHELL_ASSETS", 1)[1].split("];", 1)[0])
        self.assertNotIn("'/#", worker.split("const SHELL_ASSETS", 1)[1].split("];", 1)[0])
        self.assertEqual(STATIC_FILES["/sw.js"][0], "static/sw.js")
        self.assertEqual(STATIC_FILES["/sw.js"][3]["Service-Worker-Allowed"], "/")

    def test_dashboard_declares_manifest_theme_and_touch_icon(self):
        dashboard = self.read("dashboard.html")
        self.assertIn('rel="manifest" href="/static/manifest.webmanifest"', dashboard)
        self.assertIn('name="theme-color" content="#0E0D0B"', dashboard)
        self.assertIn('rel="apple-touch-icon" href="/static/icons/fleet-192.png"', dashboard)


if __name__ == "__main__":
    unittest.main()
