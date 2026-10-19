"""更新检查纯逻辑测试：is_newer_version 版本比较与 UpdateStatus.is_success 分类。"""

from app.services.update_service import UpdateStatus, is_newer_version


# ── is_newer_version ────────────────────────────────────────

def test_newer_patch():
    assert is_newer_version("0.0.2", "0.0.1") is True


def test_numeric_comparison_not_lexical():
    # 0.0.10 > 0.0.9：纯字符串比较会判反
    assert is_newer_version("0.0.10", "0.0.9") is True
    assert is_newer_version("0.9.0", "0.10.0") is False


def test_same_version():
    assert is_newer_version("1.2.3", "1.2.3") is False


def test_v_prefix_stripped():
    assert is_newer_version("v1.1.0", "1.0.0") is True
    assert is_newer_version("V1.1.0", "v1.0.0") is True


def test_empty_latest_is_not_newer():
    assert is_newer_version("", "1.0.0") is False
    assert is_newer_version(None, "1.0.0") is False


def test_unparseable_falls_back_to_inequality():
    # 不可解析时保守判定：不同即视为有更新
    assert is_newer_version("dev-build", "1.0.0") is True
    assert is_newer_version("dev-build", "dev-build") is False


def test_real_version_vs_placeholder():
    # _auto_check_update 用它区分占位版本 0.0.0 与真实发布版
    assert is_newer_version("0.0.1", "0.0.0") is True
    assert is_newer_version("0.0.0", "0.0.0") is False


# ── UpdateStatus.is_success ─────────────────────────────────

def test_definitive_answers_count_as_success():
    for status in (
        UpdateStatus.UP_TO_DATE,
        UpdateStatus.UPDATE_AVAILABLE,
        UpdateStatus.PRERELEASE_AVAILABLE,
    ):
        assert status.is_success is True


def test_failures_do_not_count_as_success():
    # 失败状态不写入上次检查时间，保证节流不会挡住下次自动重试
    for status in (
        UpdateStatus.NETWORK_ERROR,
        UpdateStatus.RATE_LIMITED,
        UpdateStatus.PARSE_ERROR,
    ):
        assert status.is_success is False
