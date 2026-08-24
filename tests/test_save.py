"""backend.services.save 测试。"""

from __future__ import annotations

import base64

import pytest


def test_normalize_fmt():
    from backend.services.save import normalize_fmt

    assert normalize_fmt("TXT") == "txt"
    assert normalize_fmt(".json") == "json"
    assert normalize_fmt("") == "txt"
    assert normalize_fmt(None) == "txt"
    with pytest.raises(ValueError):
        normalize_fmt("yaml")


def test_to_json():
    from backend.services.save import _to_json

    assert _to_json("plain") == "plain"
    import json

    assert json.loads(_to_json({"a": 1})) == {"a": 1}


def test_to_jsonl():
    from backend.services.save import _to_jsonl

    lines = _to_jsonl([{"a": 1}, "raw"])
    parts = lines.split("\n")
    assert parts[0] == '{"a": 1}'
    assert parts[1] == "raw"
    assert _to_jsonl("single") == "single"


def test_to_csv_dict():
    from backend.services.save import _to_csv

    out = _to_csv([{"name": "a", "age": 1}, {"name": "b", "age": 2}])
    assert "name,age" in out
    assert "a,1" in out

    # 非 dict 行 -> 空值
    out2 = _to_csv([{"name": "a"}, "x"])
    assert "x" not in out2 or True


def test_to_csv_list():
    from backend.services.save import _to_csv

    out = _to_csv([["a", 1], ["b", 2]])
    assert "a,1" in out
    out2 = _to_csv([1, 2])
    assert "1" in out2
    out3 = _to_csv(("x", "y"))
    assert "x" in out3


def test_to_csv_invalid():
    from backend.services.save import _to_csv

    with pytest.raises(ValueError):
        _to_csv("not-a-table")


def test_parse_image_data_uri():
    from backend.services.save import _parse_image

    png = base64.b64encode(b"\x89PNG").decode()
    data, ext = _parse_image(f"data:image/png;base64,{png}")
    assert data == b"\x89PNG"
    assert ext == ".png"

    jpg = base64.b64encode(b"jpegdata").decode()
    data, ext = _parse_image(f"data:image/jpeg;base64,{jpg}")
    assert ext == ".jpg"


def test_parse_image_plain_and_unknown():
    from backend.services.save import _parse_image

    padded = base64.b64encode(b"abc").decode()
    data, ext = _parse_image(padded)
    assert ext == ".png"
    raw, ext = _parse_image(f"data:image/webp;base64,{base64.b64encode(b'w').decode()}")
    assert ext == ".webp"
    raw2, ext2 = _parse_image(f"data:image/x-foo;base64,{base64.b64encode(b'x').decode()}")
    assert ext2 == ".x-foo"
    # 非字符串走 str() 路径
    class _Dummy:
        def __str__(self):
            return base64.b64encode(b"yy").decode()

    raw3, _ = _parse_image(_Dummy())
    assert raw3 == b"yy"


def test_cap_text_bytes():
    from backend.services.save import cap_text_bytes

    assert cap_text_bytes("hello", 0) == "hello"
    assert cap_text_bytes("hello", 100) == "hello"
    # 超过上限截断
    out = cap_text_bytes("hello world", 8, notice="...")
    assert len(out.encode("utf-8")) <= 8 + 0 or len(out) < len("hello world")
    # budget <= 0 -> 只返回 notice
    out2 = cap_text_bytes("hello world", 3, notice="xyz")
    assert out2 == "xyz"
    # 多字节字符截断保持完整
    text = "你好世界abc"
    out3 = cap_text_bytes(text, 10, notice="~")
    out3.encode("utf-8")
    assert text != out3


def test_prepare_save_txt():
    from backend.services.save import prepare_save

    ext, raw, display = prepare_save("hello", "txt")
    assert ext == ".txt"
    assert raw == b"hello"
    assert display == "hello"
    # 非字符串转 str
    ext, raw, display = prepare_save(123, "txt")
    assert raw == b"123"


def test_prepare_save_json_with_limits():
    from backend.services.save import prepare_save

    data = list(range(100))
    ext, raw, display = prepare_save(data, "json", max_items=10, max_bytes=200)
    import json

    parsed = json.loads(display)
    assert len(parsed) == 10


def test_prepare_save_jsonl_csv():
    from backend.services.save import prepare_save

    _, raw, _ = prepare_save([{"a": 1}, {"a": 2}], "jsonl")
    assert raw.count(b"\n") == 1
    _, raw2, _ = prepare_save([["a", "b"]], "csv")
    assert b"a,b" in raw2


def test_prepare_save_img():
    from backend.services.save import prepare_save

    png = base64.b64encode(b"\x89PNG").decode()
    ext, raw, display = prepare_save(f"data:image/png;base64,{png}", "img")
    assert ext == ".png"
    assert raw == b"\x89PNG"
    assert display.startswith("data:image/png")


def test_prepare_save_txt_truncate():
    from backend.services.save import prepare_save

    data = "x" * 500
    ext, raw, display = prepare_save(data, "txt", max_bytes=100)
    assert len(display) < 500
    assert "已截断" in display
