"""display_version 展示格式测试（防 vv 双前缀）。"""

from app.utils.version_utils import display_version


def test_plain_version_gets_v_prefix():
    assert display_version("4.0.0") == "v4.0.0"
    assert display_version("1.0") == "v1.0"


def test_v_prefixed_not_doubled():
    assert display_version("v4.0.0") == "v4.0.0"


def test_uppercase_v_normalized():
    assert display_version("V4.0.0") == "v4.0.0"


def test_whitespace_stripped():
    assert display_version("  2.0 ") == "v2.0"
    assert display_version("v 2.0") == "v 2.0"  # 只归一前缀字符，不动其余内容


def test_empty_and_none_return_empty():
    assert display_version("") == ""
    assert display_version(None) == ""
    assert display_version("   ") == ""
