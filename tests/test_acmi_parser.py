import pytest

from dcs_lso.acmi import AcmiParser, Frame, ObjectRemoved, ObjectUpdate


def feed_all(parser: AcmiParser, text: str) -> list:
    records = []
    for line in text.splitlines():
        records.extend(parser.feed(line))
    return records


HEADER = """﻿FileType=text/acmi/tacview
FileVersion=2.2
0,ReferenceLongitude=35
0,ReferenceLatitude=37
"""


def test_nine_field_transform_applies_reference_and_blanks_keep_values():
    p = AcmiParser()
    feed_all(p, HEADER + "#1.5\n101,T=4.8|5.1||||316.6|463016.91|-306619.12|312,Name=CVN_75\n")
    t = p.objects[0x101].transform
    assert t.lon == pytest.approx(39.8)
    assert t.lat == pytest.approx(42.1)
    assert t.alt is None and t.roll is None
    assert t.yaw == 316.6 and t.heading == 312
    assert (t.u, t.v) == (463016.91, -306619.12)

    feed_all(p, "#2\n101,T=|5.2|||||463020||\n")
    t = p.objects[0x101].transform
    assert t.lon == pytest.approx(39.8)  # blank field: unchanged
    assert t.lat == pytest.approx(42.2)
    assert t.u == 463020 and t.v == -306619.12 and t.heading == 312


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1|2|3", {"lon": 36, "lat": 39, "alt": 3}),
        ("1|2|3|10|20", {"alt": 3, "u": 10, "v": 20}),
        ("1|2|3|4|5|6", {"roll": 4, "pitch": 5, "yaw": 6}),
    ],
)
def test_short_transform_layouts(value, expected):
    p = AcmiParser()
    feed_all(p, HEADER + f"#0\n2a,T={value}\n")
    t = p.objects[0x2A].transform
    for key, v in expected.items():
        assert getattr(t, key) == pytest.approx(v)


def test_records_escaped_commas_multiline_and_removal():
    p = AcmiParser()
    records = feed_all(p, HEADER + "0,Comments=a\\, b\\\nsecond line\n#3.25\n201,T=1|2|3,Name=FA-18C_hornet,AOA=8.1\n-201\n")
    assert p.globals["Comments"] == "a, b\nsecond line"
    frames = [r for r in records if isinstance(r, Frame)]
    assert frames == [Frame(3.25)]
    update = next(r for r in records if isinstance(r, ObjectUpdate))
    assert update.moved and update.props["AOA"] == "8.1" and update.time == 3.25
    assert records[-1] == ObjectRemoved(3.25, 0x201)
    assert 0x201 not in p.objects


def test_property_only_update_is_not_a_move():
    p = AcmiParser()
    records = feed_all(p, HEADER + "#0\n201,T=1|2|3\n#1\n201,AGL=5\n")
    last = records[-1]
    assert isinstance(last, ObjectUpdate) and not last.moved and last.changed == {"AGL": "5"}
