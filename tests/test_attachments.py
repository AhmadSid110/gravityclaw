"""Tests for the Phase 7A attachment system."""

from __future__ import annotations

import io
import struct
import tempfile
import unittest
from pathlib import Path

from gravityclaw.attachments import (
    MAX_UPLOAD_SIZE,
    AttachmentRecord,
    AttachmentResolver,
    AttachmentService,
    AttachmentStorage,
    AttachmentStore,
    ModelMediaCapabilities,
    classify_kind,
    detect_mime_type,
    extract_image_dimensions,
    sanitize_filename,
    validate_upload,
)
from gravityclaw.store import Store


def _make_png(width: int = 100, height: int = 50) -> bytes:
    """Construct a minimal valid PNG header."""
    header = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    # Simplified: just the signature + IHDR enough for dimension extraction
    return header + b"\x00\x00\x00\x0d" + b"IHDR" + ihdr_data


def _make_jpeg(width: int = 320, height: int = 240) -> bytes:
    """Construct a minimal JPEG with SOF0 marker."""
    # FFD8 (start) + FFC0 (SOF0)
    sof = struct.pack(">BBHBHH", 0xFF, 0xC0, 11, 8, height, width)
    return b"\xff\xd8" + sof + b"\x00" * 20


class SanitizeFilenameTests(unittest.TestCase):
    def test_basic_pass_through(self) -> None:
        self.assertEqual(sanitize_filename("photo.png"), "photo.png")

    def test_strips_path_separators(self) -> None:
        self.assertEqual(sanitize_filename("/etc/passwd"), "passwd")
        self.assertEqual(sanitize_filename("C:\\Users\\file.txt"), "file.txt")
        self.assertEqual(sanitize_filename("../../secret.txt"), "secret.txt")

    def test_replaces_special_characters(self) -> None:
        result = sanitize_filename("file<name>.txt")
        self.assertNotIn("<", result)
        self.assertNotIn(">", result)

    def test_empty_filename_becomes_attachment(self) -> None:
        self.assertEqual(sanitize_filename(""), "attachment")
        self.assertEqual(sanitize_filename("..."), "attachment")

    def test_long_filename_truncated(self) -> None:
        long_name = "a" * 300 + ".png"
        result = sanitize_filename(long_name)
        self.assertLessEqual(len(result), 255)
        self.assertTrue(result.endswith(".png"))


class DetectMimeTypeTests(unittest.TestCase):
    def test_png_magic(self) -> None:
        data = _make_png()
        self.assertEqual(detect_mime_type("x.bin", data), "image/png")

    def test_jpeg_magic(self) -> None:
        data = _make_jpeg()
        self.assertEqual(detect_mime_type("x.bin", data), "image/jpeg")

    def test_pdf_magic(self) -> None:
        data = b"%PDF-1.5 fake pdf"
        self.assertEqual(detect_mime_type("doc.bin", data), "application/pdf")

    def test_extension_fallback(self) -> None:
        self.assertEqual(detect_mime_type("image.png", None), "image/png")
        self.assertEqual(detect_mime_type("doc.pdf", None), "application/pdf")

    def test_unknown(self) -> None:
        self.assertEqual(detect_mime_type("unknown.qzx", b"\x00\x00\x00\x00"), "application/octet-stream")


class ClassifyKindTests(unittest.TestCase):
    def test_image_types(self) -> None:
        self.assertEqual(classify_kind("image/png"), "image")
        self.assertEqual(classify_kind("image/jpeg"), "image")

    def test_document_types(self) -> None:
        self.assertEqual(classify_kind("application/pdf"), "document")
        self.assertEqual(classify_kind("text/plain"), "document")

    def test_archive_types(self) -> None:
        self.assertEqual(classify_kind("application/zip"), "archive")

    def test_audio_video(self) -> None:
        self.assertEqual(classify_kind("audio/mpeg"), "audio")
        self.assertEqual(classify_kind("video/mp4"), "video")

    def test_other(self) -> None:
        self.assertEqual(classify_kind("application/octet-stream"), "other")


class ValidateUploadTests(unittest.TestCase):
    def test_valid_file(self) -> None:
        self.assertEqual(validate_upload("photo.png", 1024, "image/png"), [])

    def test_too_large(self) -> None:
        errors = validate_upload("big.bin", MAX_UPLOAD_SIZE + 1, "application/octet-stream")
        self.assertEqual(len(errors), 1)
        self.assertIn("maximum size", errors[0])

    def test_empty_file(self) -> None:
        errors = validate_upload("empty.txt", 0, "text/plain")
        self.assertTrue(any("empty" in e.lower() for e in errors))

    def test_forbidden_extension(self) -> None:
        errors = validate_upload("virus.exe", 100, "application/octet-stream")
        self.assertTrue(any("not allowed" in e for e in errors))


class ExtractImageDimensionsTests(unittest.TestCase):
    def test_png_dimensions(self) -> None:
        data = _make_png(800, 600)
        width, height = extract_image_dimensions(data, "image/png")
        self.assertEqual(width, 800)
        self.assertEqual(height, 600)

    def test_jpeg_dimensions(self) -> None:
        data = _make_jpeg(1920, 1080)
        width, height = extract_image_dimensions(data, "image/jpeg")
        self.assertEqual(width, 1920)
        self.assertEqual(height, 1080)

    def test_gif_dimensions(self) -> None:
        data = b"GIF89a" + struct.pack("<HH", 320, 240) + b"\x00" * 10
        width, height = extract_image_dimensions(data, "image/gif")
        self.assertEqual(width, 320)
        self.assertEqual(height, 240)

    def test_unknown_returns_none(self) -> None:
        width, height = extract_image_dimensions(b"unknown data", "image/unknown")
        self.assertIsNone(width)
        self.assertIsNone(height)


class AttachmentStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gravityclaw-att-")
        self.storage = AttachmentStorage(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_store_and_read(self) -> None:
        data = io.BytesIO(b"hello world")
        path, sha256, size = self.storage.store("att-1", "ws-1", "test.txt", data)
        self.assertEqual(size, 11)
        self.assertIn("att-1", path)
        self.assertIn("test.txt", path)
        # Read back
        absolute = self.storage.read(path)
        self.assertTrue(absolute.is_file())
        self.assertEqual(absolute.read_bytes(), b"hello world")

    def test_delete(self) -> None:
        data = io.BytesIO(b"delete me")
        path, _, _ = self.storage.store("att-2", "ws-1", "del.txt", data)
        self.assertTrue(self.storage.read(path).is_file())
        self.storage.delete(path)
        self.assertFalse(self.storage.read(path).exists())

    def test_path_traversal_blocked(self) -> None:
        with self.assertRaises(ValueError):
            self.storage.read("../../etc/passwd")

    def test_oversize_rejected(self) -> None:
        # Create data that exceeds MAX_UPLOAD_SIZE
        data = io.BytesIO(b"\x00" * (MAX_UPLOAD_SIZE + 1))
        with self.assertRaises(ValueError):
            self.storage.store("att-big", "ws-1", "huge.bin", data)


class AttachmentStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gravityclaw-store-")
        db_path = Path(self.tmp.name) / "test.db"
        self.store = Store(db_path)
        self.store.initialize()
        self.att_store = AttachmentStore(self.store)
        # Create a workspace and conversation for FK constraints
        ws = self.store.create_workspace("Test", Path(self.tmp.name) / "ws")
        self.ws_id = ws.id
        conv = self.store.create_conversation(ws.id)
        self.conv_id = conv.id

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_create_and_get(self) -> None:
        record = self.att_store.create_attachment(
            id="att-1",
            workspace_id=self.ws_id,
            conversation_id=self.conv_id,
            message_id=None,
            filename="photo.png",
            mime_type="image/png",
            kind="image",
            size_bytes=1024,
            storage_path="workspaces/ws/attachments/2026/08/att-1/photo.png",
            sha256="abc123",
            source="web",
            width=800,
            height=600,
        )
        self.assertEqual(record.id, "att-1")
        self.assertEqual(record.kind, "image")
        self.assertEqual(record.width, 800)

        retrieved = self.att_store.get_attachment("att-1")
        self.assertEqual(retrieved.filename, "photo.png")
        self.assertEqual(retrieved.width, 800)

    def test_link_to_message(self) -> None:
        self.att_store.create_attachment(
            id="att-2",
            workspace_id=self.ws_id,
            conversation_id=self.conv_id,
            message_id=None,
            filename="doc.pdf",
            mime_type="application/pdf",
            kind="document",
            size_bytes=2048,
            storage_path="workspaces/ws/attachments/2026/08/att-2/doc.pdf",
            sha256="def456",
            source="api",
        )
        # Create a message
        msg = self.store.append_message(self.conv_id, "user", "Check this document")
        self.att_store.link_attachment("att-2", msg.id)
        retrieved = self.att_store.get_attachment("att-2")
        self.assertEqual(retrieved.message_id, msg.id)

    def test_list_by_message(self) -> None:
        msg = self.store.append_message(self.conv_id, "user", "Files attached")
        self.att_store.create_attachment(
            id="att-3", workspace_id=self.ws_id, conversation_id=self.conv_id,
            message_id=msg.id, filename="a.png", mime_type="image/png",
            kind="image", size_bytes=100, storage_path="test/a.png",
            sha256="aaa", source="web",
        )
        self.att_store.create_attachment(
            id="att-4", workspace_id=self.ws_id, conversation_id=self.conv_id,
            message_id=msg.id, filename="b.pdf", mime_type="application/pdf",
            kind="document", size_bytes=200, storage_path="test/b.pdf",
            sha256="bbb", source="web",
        )
        results = self.att_store.list_attachments(message_id=msg.id)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].filename, "a.png")
        self.assertEqual(results[1].filename, "b.pdf")

    def test_list_by_conversation(self) -> None:
        self.att_store.create_attachment(
            id="att-5", workspace_id=self.ws_id, conversation_id=self.conv_id,
            message_id=None, filename="c.txt", mime_type="text/plain",
            kind="document", size_bytes=50, storage_path="test/c.txt",
            sha256="ccc", source="telegram",
        )
        results = self.att_store.list_attachments(conversation_id=self.conv_id)
        self.assertTrue(any(r.id == "att-5" for r in results))

    def test_not_found_raises(self) -> None:
        with self.assertRaises(KeyError):
            self.att_store.get_attachment("nonexistent")


class AttachmentServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gravityclaw-svc-")
        db_path = Path(self.tmp.name) / "test.db"
        self.store = Store(db_path)
        self.store.initialize()
        self.att_store = AttachmentStore(self.store)
        self.storage = AttachmentStorage(Path(self.tmp.name))
        self.service = AttachmentService(self.att_store, self.storage)
        ws = self.store.create_workspace("Test", Path(self.tmp.name) / "ws")
        self.ws_id = ws.id
        conv = self.store.create_conversation(ws.id)
        self.conv_id = conv.id

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_ingest_png(self) -> None:
        data = io.BytesIO(_make_png(640, 480))
        record = self.service.ingest(
            workspace_id=self.ws_id,
            conversation_id=self.conv_id,
            filename="screenshot.png",
            data=data,
            source="web",
        )
        self.assertEqual(record.kind, "image")
        self.assertEqual(record.mime_type, "image/png")
        self.assertEqual(record.width, 640)
        self.assertEqual(record.height, 480)
        self.assertEqual(record.state, "ready")
        # File should exist on disk
        path = self.storage.read(record.storage_path)
        self.assertTrue(path.is_file())

    def test_ingest_pdf(self) -> None:
        data = io.BytesIO(b"%PDF-1.5 fake content" + b"\x00" * 100)
        record = self.service.ingest(
            workspace_id=self.ws_id,
            conversation_id=self.conv_id,
            filename="report.pdf",
            data=data,
            source="api",
        )
        self.assertEqual(record.kind, "document")
        self.assertEqual(record.mime_type, "application/pdf")

    def test_ingest_rejects_executable(self) -> None:
        data = io.BytesIO(b"MZ fake exe" + b"\x00" * 100)
        with self.assertRaises(ValueError) as ctx:
            self.service.ingest(
                workspace_id=self.ws_id,
                conversation_id=self.conv_id,
                filename="malware.exe",
                data=data,
            )
        self.assertIn("not allowed", str(ctx.exception))

    def test_link_to_message(self) -> None:
        data = io.BytesIO(b"plain text content")
        record = self.service.ingest(
            workspace_id=self.ws_id,
            conversation_id=self.conv_id,
            filename="note.txt",
            data=data,
        )
        msg = self.store.append_message(self.conv_id, "user", "See attached")
        self.service.link_to_message(record.id, msg.id)
        updated = self.service.get(record.id)
        self.assertEqual(updated.message_id, msg.id)


class AttachmentResolverTests(unittest.TestCase):
    def _make_record(self, kind: str = "image", mime_type: str = "image/png", size: int = 1024) -> AttachmentRecord:
        return AttachmentRecord(
            id="att-test",
            workspace_id="ws-1",
            conversation_id="conv-1",
            message_id=None,
            filename="test.png",
            mime_type=mime_type,
            kind=kind,
            size_bytes=size,
            storage_path="test/path",
            sha256="abc",
            source="web",
            state="ready",
        )

    def test_image_with_vision_model(self) -> None:
        resolver = AttachmentResolver()
        record = self._make_record("image", "image/png", 1024)
        results = resolver.resolve([record], "gpt-4o-2024-08-06")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].strategy, "native_image")
        self.assertIsNone(results[0].mount_path)

    def test_image_without_vision(self) -> None:
        resolver = AttachmentResolver()
        record = self._make_record("image", "image/png", 1024)
        results = resolver.resolve([record], "unknown-model")
        self.assertEqual(results[0].strategy, "file_mount")
        self.assertIsNotNone(results[0].mount_path)

    def test_pdf_with_native_support(self) -> None:
        resolver = AttachmentResolver()
        record = self._make_record("document", "application/pdf", 5 * 1024 * 1024)
        results = resolver.resolve([record], "claude-3.5-sonnet")
        self.assertEqual(results[0].strategy, "native_pdf")

    def test_large_pdf_falls_back_to_mount(self) -> None:
        resolver = AttachmentResolver()
        record = self._make_record("document", "application/pdf", 15 * 1024 * 1024)
        results = resolver.resolve([record], "gpt-4o")
        self.assertEqual(results[0].strategy, "file_mount")

    def test_archive_always_mounted(self) -> None:
        resolver = AttachmentResolver()
        record = self._make_record("archive", "application/zip", 1024)
        results = resolver.resolve([record], "gemini-2.5-pro")
        self.assertEqual(results[0].strategy, "file_mount")


class ModelMediaCapabilitiesTests(unittest.TestCase):
    def test_gpt4o_has_vision(self) -> None:
        caps = ModelMediaCapabilities.for_model("gpt-4o-2024-08-06")
        self.assertTrue(caps.vision)
        self.assertTrue(caps.pdf_native)
        self.assertFalse(caps.audio_input)

    def test_claude_has_vision(self) -> None:
        caps = ModelMediaCapabilities.for_model("claude-3-opus-20240229")
        self.assertTrue(caps.vision)
        self.assertTrue(caps.pdf_native)

    def test_gemini_has_all(self) -> None:
        caps = ModelMediaCapabilities.for_model("gemini-2.5-pro-preview")
        self.assertTrue(caps.vision)
        self.assertTrue(caps.audio_input)
        self.assertTrue(caps.video_input)
        self.assertTrue(caps.pdf_native)

    def test_unknown_model_no_media(self) -> None:
        caps = ModelMediaCapabilities.for_model("local-llama-7b")
        self.assertFalse(caps.vision)
        self.assertFalse(caps.audio_input)
        self.assertFalse(caps.pdf_native)


class SchemaUpgradeTests(unittest.TestCase):
    def test_fresh_install_has_attachments_table(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="gravityclaw-schema-")
        try:
            db_path = Path(tmp.name) / "fresh.db"
            store = Store(db_path)
            store.initialize()
            with store._connect() as conn:
                tables = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
            self.assertIn("attachments", tables)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
