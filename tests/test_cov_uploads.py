"""Coverage for fleetdash.engine_uploads: image sniffing, JPEG metadata
stripping, quota/cleanup helpers, and store/resolve error paths."""
import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from fleetdash.engine import Engine
from fleetdash import engine_uploads as uploads_module
from fleetdash.config import IMAGE_UPLOAD_SESSION_COUNT
from tests.test_cov_common_ops import EngineFixture

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def valid_jpeg(payload=b"body"):
    # SOI, an APPn segment (to be stripped), SOS scan marker, EOI.
    return (b"\xff\xd8" + b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big")
            + payload + b"\xff\xda\x00\x02\xff\xd9")


class ImageKindTests(unittest.TestCase):
    def test_all_kinds(self):
        self.assertEqual(Engine._image_kind(b"\xff\xd8\xff rest"), "image/jpeg")
        self.assertEqual(Engine._image_kind(b"\x89PNG\r\n\x1a\n"), "image/png")
        self.assertEqual(Engine._image_kind(b"GIF89a....."), "image/gif")
        self.assertEqual(
            Engine._image_kind(b"RIFF\x00\x00\x00\x00WEBPxxxx"), "image/webp")
        self.assertEqual(
            Engine._image_kind(b"\x00\x00\x00\x18ftypheic...."), "image/heic")
        self.assertIsNone(Engine._image_kind(b"not an image at all"))


class StripJpegTests(unittest.TestCase):
    def test_rejects_non_jpeg(self):
        self.assertIsNone(Engine._strip_jpeg_metadata(b"not jpeg"))
        self.assertIsNone(Engine._strip_jpeg_metadata("string"))

    def test_strips_appn_segment(self):
        out = Engine._strip_jpeg_metadata(valid_jpeg(b"secret exif"))
        self.assertNotIn(b"secret exif", out)
        self.assertTrue(out.endswith(b"\xff\xd9"))

    def test_preserves_restart_markers(self):
        data = b"\xff\xd8\xff\xd0\xff\xda\x00\x02\xff\xd9"
        out = Engine._strip_jpeg_metadata(data)
        self.assertIsNotNone(out)

    def test_malformed_segment_returns_none(self):
        # Non-0xff where a marker is expected.
        self.assertIsNone(Engine._strip_jpeg_metadata(b"\xff\xd8\x00\x01"))
        # A segment claiming a length past the buffer.
        self.assertIsNone(Engine._strip_jpeg_metadata(b"\xff\xd8\xff\xe1\xff\xff"))
        # Length under the minimum of 2.
        self.assertIsNone(Engine._strip_jpeg_metadata(
            b"\xff\xd8\xff\xe1\x00\x01"))
        # No trailing EOI marker.
        self.assertIsNone(Engine._strip_jpeg_metadata(
            b"\xff\xd8\xff\xe0\x00\x04ab"))

    def test_truncated_marker_tail(self):
        # Ends right after 0xff run with nothing after.
        self.assertIsNone(Engine._strip_jpeg_metadata(b"\xff\xd8\xff"))

    def test_length_header_truncated(self):
        # A marker whose 2-byte length header runs off the end.
        self.assertIsNone(Engine._strip_jpeg_metadata(b"\xff\xd8\xff\xe0\x00"))

    def test_non_app_segment_is_preserved(self):
        # A SOF0 (0xC0) segment is not APP/COM, so it is copied verbatim.
        seg = b"\xff\xc0\x00\x04ab"
        data = b"\xff\xd8" + seg + b"\xff\xda\x00\x02\xff\xd9"
        out = Engine._strip_jpeg_metadata(data)
        self.assertIn(seg, out)


class CleanupTests(EngineFixture):
    def _root(self):
        root = os.path.join(self.base, "uploads")
        os.makedirs(root, exist_ok=True)
        return root

    def test_cleanup_missing_dir_is_noop(self):
        # No uploads directory yet -> early return.
        self.engine._cleanup_image_uploads(now=100)

    def test_cleanup_removes_expired_and_bad_meta(self):
        root = self._root()
        _, img, meta = self.engine._image_upload_paths("expired1")
        with open(img, "wb") as handle:
            handle.write(b"x")
        with open(meta, "w") as handle:
            json.dump({"expires_at": 1}, handle)
        # A meta file that is unreadable JSON -> treated as expired.
        _, img2, meta2 = self.engine._image_upload_paths("bad2")
        with open(img2, "wb") as handle:
            handle.write(b"x")
        with open(meta2, "w") as handle:
            handle.write("{ not json")
        self.engine._cleanup_image_uploads(now=100)
        self.assertFalse(os.path.exists(meta))
        self.assertFalse(os.path.exists(meta2))

    def test_cleanup_honors_skip_cursor(self):
        root = self._root()
        _, img, meta = self.engine._image_upload_paths("skipme")
        with open(img, "wb") as handle:
            handle.write(b"x")
        with open(meta, "w") as handle:
            json.dump({"expires_at": 1}, handle)
        # A non-zero cursor makes the first visited entry skip.
        self.engine._image_cleanup_skip = 1
        self.engine._cleanup_image_uploads(now=100)

    def test_schedule_skips_when_running(self):
        self.engine._image_cleanup_running = True
        # Early return -> no thread launched, no crash.
        self.engine._schedule_image_cleanup()
        self.engine._image_cleanup_running = False

    def test_cleanup_oversized_meta_is_expired(self):
        root = self._root()
        _, img, meta = self.engine._image_upload_paths("big3")
        with open(img, "wb") as handle:
            handle.write(b"x")
        with open(meta, "w") as handle:
            handle.write(" " * 5000)
        self.engine._cleanup_image_uploads(now=100)
        self.assertFalse(os.path.exists(meta))


class UsageTests(EngineFixture):
    def test_usage_missing_dir(self):
        counts = self.engine._image_upload_usage(
            os.path.join(self.base, "no-uploads"), "same")
        self.assertEqual(counts, (0, 0, 0, 0))

    def test_usage_skips_size_mismatch(self):
        root = os.path.join(self.base, "uploads")
        os.makedirs(root, exist_ok=True)
        _, img, meta = self.engine._image_upload_paths("mismatch")
        with open(img, "wb") as handle:
            handle.write(b"abcd")
        with open(meta, "w") as handle:
            json.dump({"session_id": "same", "size": 999}, handle)
        counts = self.engine._image_upload_usage(root, "same")
        self.assertEqual(counts, (0, 0, 0, 0))

    def test_usage_skips_non_regular_and_bad_meta(self):
        root = os.path.join(self.base, "uploads")
        os.makedirs(root, exist_ok=True)
        # image path is a directory -> not a regular file -> skipped.
        _, img, meta = self.engine._image_upload_paths("dir1")
        os.makedirs(img)
        with open(meta, "w") as handle:
            json.dump({"session_id": "same", "size": 4}, handle)
        # regular image but unreadable-json meta -> exception -> skipped.
        _, img2, meta2 = self.engine._image_upload_paths("badmeta")
        with open(img2, "wb") as handle:
            handle.write(b"abcd")
        with open(meta2, "w") as handle:
            handle.write("{ not json")
        self.assertEqual(self.engine._image_upload_usage(root, "same"),
                         (0, 0, 0, 0))

    def test_usage_counts_session_and_global(self):
        root = os.path.join(self.base, "uploads")
        os.makedirs(root, exist_ok=True)
        _, img, meta = self.engine._image_upload_paths("ok1")
        with open(img, "wb") as handle:
            handle.write(b"abcd")
        with open(meta, "w") as handle:
            json.dump({"session_id": "same", "size": 4}, handle)
        session_count, session_bytes, global_count, global_bytes = \
            self.engine._image_upload_usage(root, "same")
        self.assertEqual((session_count, global_count), (1, 1))
        self.assertEqual((session_bytes, global_bytes), (4, 4))


class KnownProviderTests(EngineFixture):
    def test_from_session_provider(self):
        self.assertEqual(self.engine._known_message_provider(
            "x", {"provider": "codex"}), "codex")

    def test_from_ledger(self):
        self.engine.scan()  # writes session_runs for 'same'
        self.assertEqual(self.engine._known_message_provider("same"), "claude")

    def test_unknown_session(self):
        self.assertIsNone(self.engine._known_message_provider("never-seen"))

    def test_ledger_exception_returns_none(self):
        with mock.patch.object(self.engine, "ledger_reader",
                               side_effect=RuntimeError("db down")):
            self.assertIsNone(self.engine._known_message_provider("x"))


class StoreImageTests(EngineFixture):
    def test_invalid_id(self):
        out = self.engine.store_image_upload("same", "bad id!", "n.png",
                                             "image/png", PNG)
        self.assertIn("invalid image ID", out["error"])

    def test_bad_size(self):
        out = self.engine.store_image_upload("same", "u1", "n.png",
                                             "image/png", b"")
        self.assertIn("1 byte", out["error"])

    def test_type_mismatch(self):
        out = self.engine.store_image_upload("same", "u2", "n.png",
                                             "image/png", b"not a png")
        self.assertIn("does not match", out["error"])

    def test_unknown_session_rejected(self):
        out = self.engine.store_image_upload("never", "u3", "n.png",
                                             "image/png", PNG)
        self.assertIn("not available", out["error"])

    def _convert(self, output_bytes=None):
        def convert(argv, **kwargs):
            with open(argv[-1], "wb") as out:
                out.write(output_bytes if output_bytes is not None
                          else valid_jpeg(b"clean"))
            return SimpleNamespace(returncode=0)
        return convert

    def test_conversion_failure(self):
        self.engine.scan()
        with mock.patch("fleetdash.engine_uploads.subprocess.run",
                        return_value=SimpleNamespace(returncode=1)):
            out = self.engine.store_image_upload("same", "conv-fail", "n.png",
                                                 "image/png", PNG)
        self.assertIn("could not be normalized", out["error"])

    def test_metadata_strip_failure(self):
        self.engine.scan()
        with mock.patch("fleetdash.engine_uploads.subprocess.run",
                        side_effect=self._convert(b"junk-not-jpeg")):
            out = self.engine.store_image_upload("same", "strip-fail", "n.png",
                                                 "image/png", PNG)
        self.assertIn("metadata could not be removed", out["error"])

    def test_successful_store_and_collision(self):
        self.engine.scan()
        with mock.patch("fleetdash.engine_uploads.subprocess.run",
                        side_effect=self._convert()):
            ok = self.engine.store_image_upload("same", "good-1", "photo.png",
                                                "image/png", PNG)
            self.assertTrue(ok["ok"], ok)
            collision = self.engine.store_image_upload("same", "good-1",
                                                       "other.png", "image/png", PNG)
        self.assertIn("already exists", collision["error"])

    def test_global_storage_full(self):
        self.engine.scan()
        with mock.patch.object(uploads_module, "IMAGE_UPLOAD_GLOBAL_COUNT", 0):
            out = self.engine.store_image_upload("same", "global-full", "n.png",
                                                 "image/png", PNG)
        self.assertIn("storage is full", out["error"])

    def test_session_limit_full(self):
        self.engine.scan()
        root = os.path.join(self.base, "uploads")
        os.makedirs(root, exist_ok=True)
        for i in range(IMAGE_UPLOAD_SESSION_COUNT):
            _, img, meta = self.engine._image_upload_paths(f"pre{i}")
            with open(img, "wb") as h:
                h.write(b"x")
            with open(meta, "w") as h:
                json.dump({"session_id": "same", "size": 1,
                           "expires_at": time.time() + 300}, h)
        out = self.engine.store_image_upload("same", "over", "n.png",
                                             "image/png", PNG)
        self.assertIn("session's pending image limit", out["error"])

    def test_subprocess_exception_cleans_up(self):
        self.engine.scan()
        with mock.patch("fleetdash.engine_uploads.subprocess.run",
                        side_effect=OSError("sips blew up")):
            out = self.engine.store_image_upload("same", "boom", "n.png",
                                                 "image/png", PNG)
        self.assertIn("upload failed", out["error"])


class ResolveTests(EngineFixture):
    def test_bad_list(self):
        self.assertEqual(self.engine._resolve_image_uploads("same", "notalist"),
                         (None, "attach between 1 and 4 images"))
        self.assertEqual(self.engine._resolve_image_uploads("same", []),
                         (None, "attach between 1 and 4 images"))

    def test_invalid_id(self):
        paths, err = self.engine._resolve_image_uploads("same", ["bad id!"])
        self.assertIn("invalid image ID", err)

    def test_non_regular_image_rejected(self):
        root = os.path.join(self.base, "uploads")
        os.makedirs(root, exist_ok=True)
        _, img, meta = self.engine._image_upload_paths("dirimg")
        os.makedirs(img)
        with open(meta, "w") as handle:
            json.dump({"session_id": "same", "size": 4,
                       "expires_at": time.time() + 300}, handle)
        paths, err = self.engine._resolve_image_uploads("same", ["dirimg"])
        self.assertIsNone(paths)
        self.assertIn("missing, expired", err)

    def test_size_mismatch_and_missing(self):
        root = os.path.join(self.base, "uploads")
        os.makedirs(root, exist_ok=True)
        _, img, meta = self.engine._image_upload_paths("mm")
        with open(img, "wb") as handle:
            handle.write(b"abcd")
        with open(meta, "w") as handle:
            json.dump({"session_id": "same", "size": 2,
                       "expires_at": time.time() + 300}, handle)
        paths, err = self.engine._resolve_image_uploads("same", ["mm"])
        self.assertIsNone(paths)
        self.assertIn("missing, expired", err)


if __name__ == "__main__":
    unittest.main()
