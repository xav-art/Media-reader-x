"""1.6.0 hardening: findings from the 2026-11-05 security review (see docs/TASKS.md)."""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import imagecodecs
import numpy as np
import pytest
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

from mediareaderx import content, decoders, recycle, ui_text
from mediareaderx.decoders import DecodeError, decode, jxl_size
from mediareaderx.logging_setup import RedactingFormatter, describe_file, redact

SRC = Path(__file__).resolve().parents[1] / "src" / "mediareaderx"


# -- JPEG XL: the size is read even when the codestream is split, and never decoded blind ----------------
def _box(kind: bytes, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


SIG = b"\x00\x00\x00\x0cJXL \x0d\x0a\x87\x0a" + _box(b"ftyp", b"jxl \x00\x00\x00\x00jxl ")


def test_jxl_size_from_split_codestream():
    raw = imagecodecs.jpegxl_encode(np.zeros((90, 160, 3), np.uint8), level=50)
    split = SIG + _box(b"jxlp", (0).to_bytes(4, "big") + raw[:2]) + _box(b"jxlp", (1 << 31 | 1).to_bytes(4, "big")
                                                                        + raw[2:])
    assert jxl_size(split) == (160, 90)  # 1.5 read only the first part and reported 1 x 1


def test_jxl_without_a_readable_size_is_refused(tmp_path):
    path = tmp_path / "short.jxl"
    path.write_bytes(SIG + _box(b"jxlp", (1 << 31).to_bytes(4, "big") + b"\xff\x0a"))
    with pytest.raises(DecodeError):
        decode(path)


def test_jxl_decodes_the_first_frame_only(tmp_path, qapp):
    frames = np.zeros((4, 40, 60, 3), np.uint8)
    path = tmp_path / "anim.jxl"
    path.write_bytes(imagecodecs.jpegxl_encode(frames, photometric="RGB"))
    image = decode(path)
    assert len(image.frames) == 1 and image.size.width() == 60


def test_every_tiff_page_is_size_checked(tmp_path, monkeypatch, qapp):
    path = tmp_path / "pages.tif"
    Image.new("RGB", (10, 10)).save(path, save_all=True, append_images=[Image.new("RGB", (400, 400))])
    monkeypatch.setattr(decoders.config, "MAX_IMAGE_PIXELS", 10_000)
    with pytest.raises(DecodeError, match="too large"):
        decode(path)


def test_unreadable_video_is_refused(tmp_path):
    with pytest.raises(content.Refused):
        content.check_video(tmp_path / "missing.mp4")  # 1.5 let the player open it unchecked


# -- logs hold no paths or names ------------------------------------------------------------------------------
@pytest.mark.parametrize("line", [
    r"[Errno 13] Permission denied: 'C:\\Users\\someone\\Pictures\\Salary review Jane Doe.jpg'",
    "qt.svg: cannot open C:/Users/someone/Documents/Termination letter Dr. Hansen.svg",
    r"Cannot open \\fileserver\hr\private folder\x.png",
    "url file:///C:/Users/someone/x.mp4 failed",
])
def test_redaction_removes_paths(line):
    out = redact(line)
    for word in ("Jane", "Hansen", "fileserver", "someone", "private"):
        assert word not in out


def test_redaction_keeps_ordinary_text_and_source_names():
    assert redact("Opened .jpg (3 KB) at 12:30:45, ratio 4:3") == "Opened .jpg (3 KB) at 12:30:45, ratio 4:3"
    assert redact('  File "C:\\x\\site-packages\\PIL\\Image.py", line 3') == '  File "Image.py", line 3'


def test_exception_text_is_redacted_too():
    formatter = RedactingFormatter("%(message)s")
    try:
        open(r"C:\no such folder\Letter to Dr. Hansen.jpg")
    except OSError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "Save failed", None, __import__("sys").exc_info())
    assert "Hansen" not in formatter.format(record)


def test_describe_file_logs_only_known_extensions(tmp_path):
    odd = tmp_path / "Letter for Dr. Hansen"
    odd.write_bytes(b"x")
    assert "hansen" not in describe_file(odd).lower()
    assert describe_file(tmp_path / "x.HEIC").startswith(".heic")


# -- delete never destroys a file ------------------------------------------------------------------------------
def test_network_folder_has_no_recycle_bin():
    assert recycle.refusal(Path(r"\\server\share\photo.jpg")) is not None


def test_local_file_can_be_recycled(tmp_path):
    path = tmp_path / "x.jpg"
    path.write_bytes(b"x")
    assert recycle.refusal(path) is None


def test_drive_set_to_delete_immediately_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "x.jpg"
    path.write_bytes(b"x" * 2048)
    monkeypatch.setattr(recycle, "_dword", lambda key, name: 1 if name == "NukeOnDelete" else None)
    assert recycle.refusal(path) is not None
    monkeypatch.setattr(recycle, "_dword", lambda key, name: 0 if name == "MaxCapacity" else None)
    assert "larger than the Recycle Bin" in recycle.refusal(path)


def test_delete_refuses_and_keeps_the_file(qapp, tmp_path, monkeypatch, _no_blocking_message_boxes):
    from mediareaderx.main_window import MainWindow

    path = tmp_path / "keep.png"
    Image.new("RGB", (4, 4)).save(path)
    window = MainWindow()
    window.current = path
    monkeypatch.setattr(recycle, "refusal", lambda p: "no recycle bin")
    window.delete_current()
    assert path.exists()
    assert [box[1] for box in _no_blocking_message_boxes] == ["Move to Recycle Bin"]
    _no_blocking_message_boxes.clear()
    window.close()


# -- text from files is never Qt rich text ----------------------------------------------------------------------
def test_tooltips_go_through_ui_text():
    """Every setToolTip gets a literal or ui_text.tooltip(...): metadata and names must not become HTML."""
    bad = []
    for path in SRC.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "setToolTip":
                arg = node.args[-1]
                literal = isinstance(arg, ast.Constant)
                wrapped = isinstance(arg, ast.Call) and getattr(arg.func, "attr", "") == "tooltip"
                static = "# static text:" in lines[node.lineno - 1]  # built only from constants in the code
                if not (literal or wrapped or static):
                    bad.append(f"{path.name}:{node.lineno}")
    assert not bad, bad


def test_message_boxes_with_variable_text_are_escaped():
    bad = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and getattr(node.func, "attr", "") in ("warning", "information", "critical")
                    and getattr(getattr(node.func, "value", None), "id", "") == "QMessageBox" and len(node.args) >= 3):
                text = node.args[2]
                plain = isinstance(text, ast.Constant) or (isinstance(text, ast.JoinedStr) and all(
                    isinstance(v, ast.Constant) or getattr(getattr(v, "value", None), "id", "") == "APP_NAME"
                    for v in text.values))
                wrapped = isinstance(text, ast.Call) and getattr(text.func, "attr", "") == "box_text"
                if not (plain or wrapped):
                    bad.append(f"{path.name}:{node.lineno}")
    assert not bad, bad


def test_every_label_is_plain_text(qapp):
    from mediareaderx.main_window import MainWindow

    window = MainWindow()
    labels = window.findChildren(QLabel)
    assert labels and all(label.textFormat() == Qt.TextFormat.PlainText for label in labels), [
        label.objectName() or label.text()[:20] for label in labels if label.textFormat() != Qt.TextFormat.PlainText]
    window.close()


def test_escaped_text_shows_literally():
    text = '<img src="file://server/share/x.png">'
    assert "<img" not in ui_text.tooltip(text) and "&lt;img" in ui_text.tooltip(text)


# -- privacy by default ---------------------------------------------------------------------------------------
def test_saved_copies_drop_gps_by_default(qapp):
    from mediareaderx.main_window import MainWindow

    window = MainWindow()
    assert window.act_strip_gps.isChecked()
    window.close()


def test_recent_files_can_be_turned_off(qapp, tmp_path):
    from mediareaderx.main_window import MainWindow

    path = tmp_path / "a.png"
    Image.new("RGB", (4, 4)).save(path)
    window = MainWindow()
    window._add_recent(path)
    window.settings.setValue("last_dir", str(tmp_path))
    assert window._recent()
    window._set_remember_recent(False)
    window._add_recent(path)
    assert window._recent() == [] and window.settings.value("last_dir") is None
    window.close()


@pytest.mark.parametrize("line, secret", [
    (r"path:D:\Photos\Jane.jpg", "Jane"),
    ("source:file:///D:/Holiday/Jane.mp4", "Jane"),
    ("Error:" + chr(92) * 2 + "server" + chr(92) + "share" + chr(92) + "Jane.png", "server"),  # a UNC path
    ("dst=/D:/Private/Jane.jpg", "Jane"),
    ("open //fileserver/hr/Jane.png failed", "fileserver"),
])
def test_redaction_after_colons_and_slashes(line, secret):
    assert secret not in redact(line)


def test_redaction_keeps_urls_and_times():
    assert redact("see http://example.com/a at 12:30:45") == "see http://example.com/a at 12:30:45"
