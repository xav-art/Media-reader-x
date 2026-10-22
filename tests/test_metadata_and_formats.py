from fractions import Fraction

from mediareaderx.formats import ALL_EXTS, ASSOCIATION_EXTS, MediaKind, dialog_filter, kind_of
from mediareaderx.livephoto import find_motion_companion
from mediareaderx.metadata import gps_items, human_size


def test_kind_of_is_case_insensitive():
    assert kind_of("IMG_1.HEIC") is MediaKind.IMAGE
    assert kind_of("a.CR3") is MediaKind.RAW
    assert kind_of("clip.MOV") is MediaKind.VIDEO
    assert kind_of("logo.svg") is MediaKind.SVG
    assert kind_of("notes.txt") is None


def test_association_list_only_contains_supported_types():
    assert ASSOCIATION_EXTS <= ALL_EXTS
    assert ".heic" in ASSOCIATION_EXTS and ".jpg" not in ASSOCIATION_EXTS


def test_dialog_filter_lists_heic():
    assert "*.heic" in dialog_filter()


def test_gps_decimal_and_hemisphere():
    gps = {1: "N", 2: (Fraction(55), Fraction(40), Fraction(3006, 100)),
           3: "W", 4: (Fraction(12), Fraction(34), Fraction(0)),
           5: b"\x00", 6: Fraction(125, 10)}
    rows = {r.key: r.value for r in gps_items(gps)}
    assert rows["Coordinates"] == "55.675017, -12.566667"
    assert rows["Altitude"] == "12.5 m"


def test_human_size():
    assert human_size(512) == "512 bytes"
    assert human_size(2048) == "2.0 KB"


def test_live_photo_pairing(samples):
    assert find_motion_companion(samples / "IMG_0001.HEIC").name == "IMG_0001.MOV"
    assert find_motion_companion(samples / "sample.heic") is None
    assert find_motion_companion(samples / "IMG_0001.MOV") is None


def test_self_test_passes(qapp):
    from mediareaderx.selftest import run_self_test

    assert run_self_test() == 0
