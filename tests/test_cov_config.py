"""Coverage for fleetdash.config: validators, private-json/log helpers,
load_config env overrides, and small pure functions."""
import importlib
import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from fleetdash import paths as engine_paths
from fleetdash import config


class ValidatorTests(unittest.TestCase):
    def test_delivery_uncertain_rejects_non_dict_and_bad_entries(self):
        self.assertEqual(config._validated_claude_delivery_uncertain("nope"), {})
        raw = {"sid-ok": "nonce-ok", 5: "x", "sid": "\x01bad",
               "bad": 7, "": "n", "n": ""}
        out = config._validated_claude_delivery_uncertain(raw)
        self.assertEqual(out, {"sid-ok": "nonce-ok"})

    def test_control_overrides_rejects_non_dict(self):
        self.assertEqual(config._validated_claude_control_overrides([]), {})

    def test_control_overrides_skips_bad_sids_and_values(self):
        raw = {
            "": {"model": {"value": "opus", "accepted_at": 1.0, "baseline": 0}},
            "ctrl\x01": {"model": {"value": "opus", "accepted_at": 1.0}},
            "notdict": "x",
            "bad-model": {"model": {"value": "nope", "accepted_at": 1.0,
                                    "baseline": 0}},
            "bad-accept": {"model": {"value": "opus", "accepted_at": "nan-str",
                                     "baseline": 0}},
            "nonfinite": {"model": {"value": "opus", "accepted_at": float("inf"),
                                    "baseline": 0}},
            "good": {"model": {"value": "opus", "accepted_at": 5.0, "baseline": 2},
                     "effort": {"value": "high", "accepted_at": 6.0, "baseline": 1}},
        }
        out = config._validated_claude_control_overrides(raw)
        self.assertEqual(set(out), {"good"})
        self.assertEqual(out["good"]["model"]["value"], "opus")

    def test_control_uncertain_variants(self):
        self.assertEqual(config._validated_claude_control_uncertain(0), {})
        raw = {
            "sid\x01": {"attempted_at": 1.0, "fields": {"model": {"baseline": 0}}},
            "bad": "x",
            "bad-at": {"attempted_at": "no", "fields": {}},
            "neg-at": {"attempted_at": -1.0, "fields": {"model": {"baseline": 0}}},
            "bad-baseline": {"attempted_at": 1.0,
                             "fields": {"model": {"baseline": "x"}}},
            "empty-fields": {"attempted_at": 1.0, "fields": {}},
            "good": {"attempted_at": 2.0,
                     "fields": {"effort": {"baseline": 3}}},
        }
        out = config._validated_claude_control_uncertain(raw)
        self.assertEqual(set(out), {"good"})
        self.assertEqual(out["good"]["fields"]["effort"]["baseline"], 3)


class PrivateJsonTests(unittest.TestCase):
    def test_write_private_json_atomic_and_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "secret.json")
            config._write_private_json(path, {"a": 1})
            with open(path) as handle:
                self.assertEqual(json.load(handle), {"a": 1})
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_write_private_json_closes_fd_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "secret.json")
            with mock.patch.object(os, "fchmod", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    config._write_private_json(path, {"a": 1})
            # The tmp file must have been unlinked in the finally block.
            self.assertEqual([n for n in os.listdir(tmp) if n.startswith("secret")],
                             [])


class ScrubLogTests(unittest.TestCase):
    def test_scrub_replaces_secret_bytes_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "log.txt")
            with open(path, "w") as handle:
                handle.write("token=SUPERSECRETVALUE rest\n")
            config._scrub_private_log(path, ["SUPERSECRETVALUE"])
            with open(path) as handle:
                body = handle.read()
            self.assertNotIn("SUPERSECRETVALUE", body)
            self.assertIn("*", body)

    def test_scrub_noop_without_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "log.txt")
            with open(path, "w") as handle:
                handle.write("body\n")
            # Values shorter than 8 chars are ignored -> early return.
            config._scrub_private_log(path, ["short", None, 123])
            with open(path) as handle:
                self.assertEqual(handle.read(), "body\n")

    def test_scrub_rejects_non_regular_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A directory is not a regular file -> S_ISREG guard returns.
            config._scrub_private_log(tmp, ["SUPERSECRETVALUE"])

    def test_scrub_swallows_errors(self):
        # A missing file makes os.lstat raise OSError which is swallowed.
        config._scrub_private_log("/no/such/scrub/path", ["SUPERSECRETVALUE"])


class RuntimeLogSecretsTests(unittest.TestCase):
    def test_reads_push_secrets_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(engine_paths, "BASE", tmp):
                with open(os.path.join(tmp, "push-secrets.json"), "w") as handle:
                    json.dump({"vapid_private_key": "vapid-priv",
                               "action_secret": "act-secret"}, handle)
                values = config._runtime_log_secrets(
                    {"act_token": "tok", "ntfy_topic": "top"})
        self.assertIn("vapid-priv", values)
        self.assertIn("act-secret", values)

    def test_oversized_secret_file_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(engine_paths, "BASE", tmp):
                with open(os.path.join(tmp, "push-secrets.json"), "w") as handle:
                    handle.write("x" * 40000)
                values = config._runtime_log_secrets({"act_token": "tok"})
        self.assertNotIn("x" * 40000, values)

    def test_missing_secret_file_returns_base_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(engine_paths, "BASE", tmp):
                values = config._runtime_log_secrets({"act_token": "tok"})
        self.assertIn("tok", values)


class LoadConfigTests(unittest.TestCase):
    def test_missing_config_creates_base_and_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "state")
            with mock.patch.object(engine_paths, "BASE", base):
                cfg = config.load_config()
            self.assertTrue(cfg["act_token"])
            self.assertTrue(os.path.isdir(base))

    def test_unreadable_config_returns_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w") as handle:
                handle.write("{ not json")
            with mock.patch.object(engine_paths, "BASE", tmp):
                cfg = config.load_config()
            # Defaults returned; no act_token minted on the error path.
            self.assertNotIn("act_token", cfg)
            self.assertEqual(cfg["port"], config.DEFAULT_CONFIG["port"])

    def test_port_and_bind_env_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "config.json"), "w") as handle:
                json.dump({"act_token": "keep", "_stall_default_v2": True}, handle)
            with mock.patch.object(engine_paths, "BASE", tmp):
                with mock.patch.dict(os.environ, {"FLEET_DASH_PORT": "9999",
                                                  "FLEET_DASH_BIND": "0.0.0.0"}):
                    cfg = config.load_config()
        self.assertEqual(cfg["port"], 9999)
        self.assertEqual(cfg["bind"], "0.0.0.0")

    def test_invalid_port_env_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "config.json"), "w") as handle:
                json.dump({"act_token": "keep", "_stall_default_v2": True,
                           "port": 8377}, handle)
            with mock.patch.object(engine_paths, "BASE", tmp):
                for bad in ("notint", "70000"):
                    with mock.patch.dict(os.environ, {"FLEET_DASH_PORT": bad}):
                        cfg = config.load_config()
                    self.assertEqual(cfg["port"], 8377)


class SmallHelperTests(unittest.TestCase):
    def test_model_family(self):
        self.assertEqual(config.model_family("claude-haiku"), "haiku")
        self.assertEqual(config.model_family("claude-sonnet"), "sonnet")
        self.assertEqual(config.model_family(None), "opus")

    def test_usd_uses_opus_fallback(self):
        cfg = {"rates": config.DEFAULT_CONFIG["rates"]}
        self.assertGreater(config.usd(cfg, "unknown", 1_000_000, 0, 0, 0), 0)

    def test_ktok_formats(self):
        self.assertEqual(config.ktok(None), "0")
        self.assertEqual(config.ktok(500), "500")
        self.assertEqual(config.ktok(1500), "2k")

    def test_iso_epoch(self):
        self.assertIsNone(config.iso_epoch("garbage"))
        self.assertIsInstance(config.iso_epoch("2026-07-15T00:00:00Z"), float)

    def test_cwd_to_project_dir(self):
        with mock.patch.object(engine_paths, "PROJECTS", "/proj"):
            self.assertEqual(config.cwd_to_project_dir("/work/repo.x"),
                             os.path.join("/proj", "-work-repo-x"))

    def test_capture_base_reads_at_call_time(self):
        with mock.patch.object(engine_paths, "CAPTURE_BASE", "/cap/base"):
            self.assertEqual(engine_paths.capture_base(), "/cap/base")


class PathsReloadTests(unittest.TestCase):
    def test_invalid_instance_mode_falls_back_to_production(self):
        with mock.patch.dict(os.environ, {"FLEET_DASH_INSTANCE": "garbage"}):
            reloaded = importlib.reload(engine_paths)
            try:
                self.assertEqual(reloaded.INSTANCE_MODE, "production")
            finally:
                # Restore the module to the ambient environment for other tests.
                importlib.reload(engine_paths)


if __name__ == "__main__":
    unittest.main()
