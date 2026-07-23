"""Phone image uploads: validation, normalization, quotas, cleanup (invariant 54)."""
import json, os, time, subprocess, threading, copy, stat


from . import paths as pathcfg
from .config import (IMAGE_UPLOAD_BYTES, IMAGE_UPLOAD_TTL_SECONDS, IMAGE_UPLOAD_SESSION_COUNT, IMAGE_UPLOAD_SESSION_BYTES, IMAGE_UPLOAD_GLOBAL_COUNT, IMAGE_UPLOAD_GLOBAL_BYTES, IMAGE_UPLOAD_ID_RE, IMAGE_UPLOAD_MIMES, _write_private_json)




class UploadOps:

    @staticmethod
    def _image_kind(data):
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in {
                b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
            return "image/heic"
        return None

    @staticmethod
    def _strip_jpeg_metadata(data):
        """Drop JPEG APP/COM segments (EXIF GPS, XMP, camera data, comments)."""
        if not isinstance(data, bytes) or not data.startswith(b"\xff\xd8"):
            return None
        out = bytearray(data[:2])
        pos = 2
        while pos < len(data):
            marker_start = pos
            if data[pos] != 0xff:
                return None
            while pos < len(data) and data[pos] == 0xff:
                pos += 1
            if pos >= len(data):
                return None
            marker = data[pos]
            pos += 1
            if marker == 0xda:  # scan data has byte-stuffing, so preserve the rest verbatim
                out.extend(data[marker_start:])
                return bytes(out)
            if marker in tuple(range(0xd0, 0xda)) + (0x01,):
                out.extend(data[marker_start:pos])
                continue
            if pos + 2 > len(data):
                return None
            length = int.from_bytes(data[pos:pos + 2], "big")
            end = pos + length
            if length < 2 or end > len(data):
                return None
            if not (0xe0 <= marker <= 0xef or marker == 0xfe):
                out.extend(data[marker_start:end])
            pos = end
        return bytes(out) if data.endswith(b"\xff\xd9") else None

    @staticmethod
    def _image_upload_paths(upload_id):
        root = os.path.join(pathcfg.BASE, "uploads")
        return root, os.path.join(root, upload_id + ".jpg"), os.path.join(root, upload_id + ".json")

    def _cleanup_image_uploads(self, now=None):
        with self._image_upload_lock:
            now = float(now or time.time())
            root = os.path.join(pathcfg.BASE, "uploads")
            try:
                entries = os.scandir(root)
            except FileNotFoundError:
                return
            visited = processed = 0
            exhausted = True
            try:
                for entry in entries:
                    visited += 1
                    if visited <= self._image_cleanup_skip:
                        continue
                    if processed >= 2000:
                        exhausted = False
                        break
                    processed += 1
                    name = entry.name
                    if not name.endswith(".json") or not IMAGE_UPLOAD_ID_RE.fullmatch(name[:-5]):
                        continue
                    upload_id = name[:-5]
                    _, image_path, meta_path = self._image_upload_paths(upload_id)
                    expired = False
                    try:
                        info = os.lstat(meta_path)
                        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                            expired = True
                        else:
                            with open(meta_path) as handle:
                                meta = json.load(handle)
                            expired = float(meta.get("expires_at") or 0) <= now
                    except Exception:
                        expired = True
                    if expired:
                        for path in (image_path, meta_path):
                            try:
                                if stat.S_ISREG(os.lstat(path).st_mode):
                                    os.unlink(path)
                            except FileNotFoundError:
                                pass
                            except OSError:
                                pass
            finally:
                entries.close()
            # Rotate through a polluted/legacy directory instead of inspecting
            # the same first 2,000 names forever. Deletions may shift order, but
            # reaching the end resets the cursor and catches anything skipped.
            self._image_cleanup_skip = 0 if exhausted else self._image_cleanup_skip + processed

    def _schedule_image_cleanup(self):
        now = time.time()
        with self._image_upload_lock:
            if self._image_cleanup_running or now < self._image_cleanup_due:
                return
            self._image_cleanup_running = True
            self._image_cleanup_due = now + 3600

        def clean():
            try:
                self._cleanup_image_uploads()
            finally:
                with self._image_upload_lock:
                    self._image_cleanup_running = False
        threading.Thread(target=clean, name="fleet-image-cleanup", daemon=True).start()

    def _image_upload_usage(self, root, sid):
        """Return bounded live-upload usage without trusting client filenames or sizes."""
        session_count = session_bytes = global_count = global_bytes = 0
        try:
            entries = os.scandir(root)
        except FileNotFoundError:
            return session_count, session_bytes, global_count, global_bytes
        visited = 0
        try:
            for entry in entries:
                visited += 1
                # Fail closed on a directory polluted outside Fleet. API-created
                # storage cannot legitimately exceed this after the hard quota.
                if visited > 4000:
                    global_count = IMAGE_UPLOAD_GLOBAL_COUNT
                    break
                name = entry.name
                if not name.endswith(".json") or not IMAGE_UPLOAD_ID_RE.fullmatch(name[:-5]):
                    continue
                upload_id = name[:-5]
                _, image_path, meta_path = self._image_upload_paths(upload_id)
                try:
                    image_info, meta_info = os.lstat(image_path), os.lstat(meta_path)
                    if not stat.S_ISREG(image_info.st_mode) or not stat.S_ISREG(meta_info.st_mode):
                        continue
                    with open(meta_path) as handle:
                        meta = json.load(handle)
                    size = image_info.st_size
                    if size < 1 or size > IMAGE_UPLOAD_BYTES or size != int(meta.get("size") or -1):
                        continue
                except Exception:
                    continue
                global_count += 1
                global_bytes += size
                if meta.get("session_id") == sid:
                    session_count += 1
                    session_bytes += size
                if (global_count >= IMAGE_UPLOAD_GLOBAL_COUNT or
                        session_count >= IMAGE_UPLOAD_SESSION_COUNT or
                        global_bytes >= IMAGE_UPLOAD_GLOBAL_BYTES or
                        session_bytes >= IMAGE_UPLOAD_SESSION_BYTES):
                    break
        finally:
            entries.close()
        return session_count, session_bytes, global_count, global_bytes

    def _known_message_provider(self, sid, session=None):
        """Resolve only a server-observed live or ledger-backed message target."""
        provider = str((session or {}).get("provider") or "")
        if provider in ("claude", "codex"):
            return provider
        db = None
        try:
            db = self.ledger_reader()
            row = db.execute(
                "SELECT provider FROM session_runs WHERE session_id=? LIMIT 1", (sid,)).fetchone()
            provider = str((row or [""])[0] or "")
            return provider if provider in ("claude", "codex") else None
        except Exception:
            return None
        finally:
            if db is not None:
                db.close()

    def store_image_upload(self, sid, upload_id, display_name, content_type, data):
        """Validate and normalize one private image for an interactive live session."""
        sid = str(sid or "")
        upload_id = str(upload_id or "")
        content_type = str(content_type or "").split(";", 1)[0].strip().lower()
        if not IMAGE_UPLOAD_ID_RE.fullmatch(upload_id):
            return {"ok": False, "error": "invalid image ID"}
        if not isinstance(data, bytes) or not 1 <= len(data) <= IMAGE_UPLOAD_BYTES:
            return {"ok": False, "error": "image must be between 1 byte and 10 MB"}
        detected = self._image_kind(data)
        if content_type not in IMAGE_UPLOAD_MIMES or detected != content_type and not (
                content_type == "image/heif" and detected == "image/heic"):
            return {"ok": False, "error": "image type does not match its contents"}
        with self.lock:
            session = next((copy.deepcopy(item) for item in
                self.snapshot_cache.get("sessions") or [] if item.get("session_id") == sid), None)
        if not self._known_message_provider(sid, session):
            return {"ok": False, "error": "session is not available for image messages"}
        root, image_path, meta_path = self._image_upload_paths(upload_id)
        os.makedirs(root, mode=0o700, exist_ok=True)
        os.chmod(root, 0o700, follow_symlinks=False)
        source_path = os.path.join(root, "." + upload_id + IMAGE_UPLOAD_MIMES[content_type])
        output_path = os.path.join(root, "." + upload_id + "-normalized.jpg")
        safe_name = os.path.basename(str(display_name or "image"))[:120]
        with self._image_upload_lock:
            self._cleanup_image_uploads()
            # IDs are immutable ownership handles. Reusing one must never replace
            # another session's image or mutate a draft that already references it.
            if os.path.lexists(image_path) or os.path.lexists(meta_path):
                return {"ok": False, "error": "image ID already exists; choose a new ID"}
            session_count, session_bytes, global_count, global_bytes = \
                self._image_upload_usage(root, sid)
            if (session_count >= IMAGE_UPLOAD_SESSION_COUNT or
                    session_bytes + len(data) > IMAGE_UPLOAD_SESSION_BYTES):
                return {"ok": False, "error": "this session's pending image limit is full"}
            if (global_count >= IMAGE_UPLOAD_GLOBAL_COUNT or
                    global_bytes + len(data) > IMAGE_UPLOAD_GLOBAL_BYTES):
                return {"ok": False, "error": "Fleet's pending image storage is full"}
            published_image = False
            try:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(source_path, flags, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                converted = subprocess.run([
                    "/usr/bin/sips", "-s", "format", "jpeg", "-s", "formatOptions", "85",
                    source_path, "--out", output_path], capture_output=True, text=True, timeout=30)
                if converted.returncode != 0:
                    return {"ok": False, "error": "image could not be normalized"}
                with open(output_path, "rb") as stream:
                    scrubbed = self._strip_jpeg_metadata(stream.read(IMAGE_UPLOAD_BYTES + 1))
                if not scrubbed or len(scrubbed) > IMAGE_UPLOAD_BYTES:
                    return {"ok": False, "error": "image metadata could not be removed"}
                flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(output_path, flags)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(scrubbed)
                    stream.flush()
                    os.fsync(stream.fileno())
                info = os.lstat(output_path)
                if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= IMAGE_UPLOAD_BYTES:
                    return {"ok": False, "error": "normalized image exceeds 10 MB"}
                # Conversion can make a compact HEIC/PNG larger. Quotas apply to
                # the bytes Fleet actually retains, so repeat the serialized
                # check with the normalized size before publishing either file.
                if (session_count >= IMAGE_UPLOAD_SESSION_COUNT or
                        session_bytes + info.st_size > IMAGE_UPLOAD_SESSION_BYTES):
                    return {"ok": False, "error": "this session's pending image limit is full"}
                if (global_count >= IMAGE_UPLOAD_GLOBAL_COUNT or
                        global_bytes + info.st_size > IMAGE_UPLOAD_GLOBAL_BYTES):
                    return {"ok": False, "error": "Fleet's pending image storage is full"}
                os.chmod(output_path, 0o600, follow_symlinks=False)
                os.replace(output_path, image_path)
                published_image = True
                created = time.time()
                _write_private_json(meta_path, {"version": 1, "upload_id": upload_id,
                    "session_id": sid, "display_name": safe_name, "content_type": "image/jpeg",
                    "size": info.st_size, "created_at": created,
                    "expires_at": created + IMAGE_UPLOAD_TTL_SECONDS})
                return {"ok": True, "upload_id": upload_id, "name": safe_name,
                        "content_type": "image/jpeg", "size": info.st_size,
                        "expires_at": created + IMAGE_UPLOAD_TTL_SECONDS}
            except (OSError, subprocess.SubprocessError):
                if published_image:
                    try:
                        os.unlink(image_path)
                    except OSError:
                        pass
                return {"ok": False, "error": "image upload failed"}
            finally:
                for path in (source_path, output_path):
                    try:
                        os.unlink(path)
                    except FileNotFoundError:
                        pass

    def _resolve_image_uploads(self, sid, upload_ids):
        if not isinstance(upload_ids, list) or not 1 <= len(upload_ids) <= 4:
            return None, "attach between 1 and 4 images"
        paths = []
        now = time.time()
        with self._image_upload_lock:
            self._cleanup_image_uploads(now)
            for upload_id in upload_ids:
                upload_id = str(upload_id or "")
                if not IMAGE_UPLOAD_ID_RE.fullmatch(upload_id):
                    return None, "invalid image ID"
                _, image_path, meta_path = self._image_upload_paths(upload_id)
                try:
                    image_info, meta_info = os.lstat(image_path), os.lstat(meta_path)
                    if not stat.S_ISREG(image_info.st_mode) or not stat.S_ISREG(meta_info.st_mode):
                        raise ValueError
                    with open(meta_path) as handle:
                        meta = json.load(handle)
                    if meta.get("session_id") != sid or float(meta.get("expires_at") or 0) <= now:
                        raise ValueError
                    if image_info.st_size != int(meta.get("size") or -1):
                        raise ValueError
                except Exception:
                    return None, "image upload is missing, expired, or belongs to another session"
                paths.append(image_path)
        return paths, None
