"""更新检查服务

通过 GitHub Releases API 检查 KK Mod Manager 是否有新版本：
  - 端点：https://api.github.com/repos/{owner}/{repo}/releases/latest
  - 异步发起请求，UI 线程通过 checked 信号接收结果（无需 QThread）
  - 比较 packaging.version.Version 与当前 __version__
  - 默认忽略 prerelease

异常与边界处理：
  - 网络异常 / 超时 → UpdateStatus.NETWORK_ERROR（不弹错误对话框）
  - API 限速（403 或剩余次数 < 10） → UpdateStatus.RATE_LIMITED
  - tag_name 解析失败 → UpdateStatus.PARSE_ERROR
  - 已是最新 → UpdateStatus.UP_TO_DATE
"""

from dataclasses import dataclass
from enum import Enum

from loguru import logger
from packaging.version import Version, InvalidVersion
from PyQt6.QtCore import QObject, QTimer, pyqtSignal, QUrl
from PyQt6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)

from app import __version__ as CURRENT_VERSION


GITHUB_REPO = "fcl0613/kk_mod_manager"
# 无法拿到具体 release 的 html_url 时的兜底下载页
RELEASES_PAGE_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"


def is_newer_version(latest: str, current: str) -> bool:
    """latest 是否比 current 新

    版本号不可解析时回退为“字符串不同即视为有更新”的保守判断。
    """
    latest = (latest or "").lstrip("v").strip()
    current = (current or "").lstrip("v").strip()
    if not latest:
        return False
    try:
        return Version(latest) > Version(current)
    except InvalidVersion:
        return latest != current


# ═══════════════════════════════════════════════════════════
# 数据类
# ═══════════════════════════════════════════════════════════

class UpdateStatus(Enum):
    """更新检查结果分类"""
    UP_TO_DATE = "up_to_date"               # 已是最新
    UPDATE_AVAILABLE = "update_available"   # 有新版本
    PRERELEASE_AVAILABLE = "prerelease_available"  # 仅 prerelease（V1 忽略）
    NETWORK_ERROR = "network_error"         # 网络异常或超时
    RATE_LIMITED = "rate_limited"           # API 限速
    PARSE_ERROR = "parse_error"             # tag_name 等解析失败

    @property
    def is_success(self) -> bool:
        """是否拿到 GitHub 的确定答案（有新版 / 已最新 / 仅预览版）。

        用于节流计时：只有成功才写入上次检查时间，失败不计时，
        避免一次网络故障把之后的自动重试全部挡在节流之外。
        """
        return self in (
            UpdateStatus.UP_TO_DATE,
            UpdateStatus.UPDATE_AVAILABLE,
            UpdateStatus.PRERELEASE_AVAILABLE,
        )


@dataclass
class UpdateResult:
    """更新检查结果"""
    status: UpdateStatus
    current: str
    latest: str = ""
    release_url: str = ""
    changelog: str = ""
    error: str = ""


# ═══════════════════════════════════════════════════════════
# 服务
# ═══════════════════════════════════════════════════════════

class UpdateService(QObject):
    """GitHub Releases 更新检查服务

    使用方法：
        service = UpdateService(parent)
        service.checked.connect(on_result)
        service.check()
    """

    GITHUB_API_URL = (
        f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    )
    TIMEOUT_MS = 10_000
    USER_AGENT = b"KK-Mod-Manager"

    checked = pyqtSignal(object)  # UpdateResult

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._nam = QNetworkAccessManager(self)
        self._reply: QNetworkReply | None = None
        self._timeout_timer: QTimer | None = None

    # ── 公开属性 ──

    @property
    def current_version(self) -> str:
        """当前应用版本（运行时）"""
        return CURRENT_VERSION

    def is_busy(self) -> bool:
        """是否有请求正在飞行中"""
        return self._reply is not None and self._reply.isRunning()

    # ── 公开方法 ──

    def check(self) -> None:
        """异步触发一次检查。结果通过 checked 信号回调。"""
        logger.info("[update-service] check() 入口：current={}", self.current_version)
        # 避免并发请求
        if self._reply is not None and self._reply.isRunning():
            logger.info("[update-service] 已有请求在飞行中，跳过本次 check() 调用")
            return

        request = QNetworkRequest(QUrl(self.GITHUB_API_URL))
        request.setRawHeader(b"Accept", b"application/vnd.github+json")
        request.setRawHeader(
            b"User-Agent",
            f"{self.USER_AGENT.decode()}/{CURRENT_VERSION}".encode(),
        )

        try:
            reply = self._nam.get(request)
        except Exception as e:
            logger.warning("[update-service] 启动 HTTP 请求失败: {}", e)
            self._emit_result(UpdateResult(
                status=UpdateStatus.NETWORK_ERROR,
                current=self.current_version,
                error=str(e),
            ))
            return

        self._reply = reply
        reply.finished.connect(self._on_reply_finished)

        # 超时控制
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(self._on_timeout)
        timer.start(self.TIMEOUT_MS)
        self._timeout_timer = timer

        logger.info(
            "[update-service] 已发起 GET {}（超时 {}ms）",
            self.GITHUB_API_URL, self.TIMEOUT_MS,
        )

    def cancel(self) -> None:
        """取消正在进行的请求（窗口关闭时调用）"""
        logger.info("[update-service] cancel() 被调用")
        if self._timeout_timer is not None:
            self._timeout_timer.stop()
            self._timeout_timer = None
        if self._reply is not None:
            try:
                self._reply.abort()
            except Exception:
                pass
            self._reply = None

    # ── 内部回调 ──

    def _on_timeout(self) -> None:
        """超时回调"""
        reply = self._reply
        if reply is not None and reply.isRunning():
            logger.warning(
                "[update-service] 请求超时（{}ms），调用 reply.abort()",
                self.TIMEOUT_MS,
            )
            reply.abort()

    def _on_reply_finished(self) -> None:
        """QNetworkReply 完成回调"""
        logger.info("[update-service] _on_reply_finished 被触发")
        reply = self._reply
        if reply is None:
            logger.debug("[update-service] reply 已为 None，忽略回调")
            return

        # 清理 timer（必须先 stop 再清引用，避免 reply GC 后 timer 触发出错）
        if self._timeout_timer is not None:
            self._timeout_timer.stop()
            self._timeout_timer = None

        # 断开信号，防止多次回调
        try:
            reply.finished.disconnect(self._on_reply_finished)
        except Exception:
            pass

        try:
            result = self._parse_reply(reply)
        except Exception as e:
            logger.exception("[update-service] 解析 reply 时异常: {}", e)
            result = UpdateResult(
                status=UpdateStatus.PARSE_ERROR,
                current=self.current_version,
                error=str(e),
            )
        finally:
            reply.deleteLater()
            self._reply = None

        logger.info(
            "[update-service] 解析完成：status={} latest={} 即将 emit checked",
            result.status.value, result.latest or "（空）",
        )
        self._emit_result(result)

    def _parse_reply(self, reply: QNetworkReply) -> UpdateResult:
        """解析 QNetworkReply 为 UpdateResult"""
        status_code = reply.attribute(
            QNetworkRequest.Attribute.HttpStatusCodeAttribute
        )
        # PyQt6 的 QNetworkReply.NetworkError 不是 IntEnum，不能直接 int()，
        # 需要用 .value 拿底层 int
        err = reply.error()
        err_int = err.value if hasattr(err, "value") else int(err)
        logger.info(
            "[update-service] HTTP 响应：status_code={} error_code={} error_str={}",
            status_code, err_int, reply.errorString(),
        )

        # 限速检测
        if status_code == 403:
            logger.warning("[update-service] GitHub API 限速（HTTP 403）")
            return UpdateResult(
                status=UpdateStatus.RATE_LIMITED,
                current=self.current_version,
                error="GitHub API rate limit reached",
            )

        if reply.error() != QNetworkReply.NetworkError.NoError:
            logger.warning(
                "[update-service] 网络错误：{} ({})",
                reply.errorString(), reply.error(),
            )
            return UpdateResult(
                status=UpdateStatus.NETWORK_ERROR,
                current=self.current_version,
                error=reply.errorString(),
            )

        if status_code != 200:
            logger.warning(
                "[update-service] 非 200 响应：status_code={}", status_code,
            )
            return UpdateResult(
                status=UpdateStatus.PARSE_ERROR,
                current=self.current_version,
                error=f"HTTP {status_code}",
            )

        # 检查限速响应头（避免下次请求被拒）
        remaining_header = reply.rawHeader(b"X-RateLimit-Remaining")
        if remaining_header:
            try:
                remaining = int(remaining_header.data().decode())
                if remaining < 10:
                    logger.warning(
                        "[update-service] GitHub API 剩余限速次数低: {}",
                        remaining,
                    )
            except (ValueError, AttributeError):
                pass

        raw = bytes(reply.readAll()).decode("utf-8", errors="replace")
        logger.debug(
            "[update-service] 响应体长度 {} bytes，前 120 字：{}",
            len(raw), raw[:120],
        )
        return self._parse_payload(raw)

    def _parse_payload(self, raw: str) -> UpdateResult:
        """解析 GitHub Releases API 响应 JSON"""
        import json

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("更新检查响应不是合法 JSON: {}", e)
            return UpdateResult(
                status=UpdateStatus.PARSE_ERROR,
                current=self.current_version,
                error=f"Invalid JSON: {e}",
            )

        # 404 / 私有 repo：GitHub 返回 {"message": "Not Found", ...}
        if isinstance(data, dict) and "message" in data and "tag_name" not in data:
            logger.warning("更新检查 API 消息: {}", data.get("message"))
            return UpdateResult(
                status=UpdateStatus.PARSE_ERROR,
                current=self.current_version,
                error=data.get("message", "Unknown API error"),
            )

        tag_name = data.get("tag_name", "")
        if not tag_name:
            logger.warning("更新检查响应缺少 tag_name")
            return UpdateResult(
                status=UpdateStatus.PARSE_ERROR,
                current=self.current_version,
                error="Missing tag_name",
            )

        # 解析版本号（去前导 v）
        latest_str = tag_name.lstrip("v").strip()
        try:
            current_v = Version(self.current_version)
            latest_v = Version(latest_str)
        except InvalidVersion as e:
            logger.warning(
                "更新检查版本解析失败: current={} latest={} ({})",
                self.current_version, latest_str, e,
            )
            return UpdateResult(
                status=UpdateStatus.PARSE_ERROR,
                current=self.current_version,
                latest=latest_str,
                error=f"Invalid version: {e}",
            )

        is_prerelease = bool(data.get("prerelease", False))
        release_url = data.get("html_url", "")
        changelog = data.get("body", "") or ""

        # 已是最新
        if latest_v <= current_v:
            logger.debug(
                "更新检查：已是最新（当前 {}，最新 {}）",
                self.current_version, latest_str,
            )
            return UpdateResult(
                status=UpdateStatus.UP_TO_DATE,
                current=self.current_version,
                latest=latest_str,
                release_url=release_url,
                changelog=changelog,
            )

        # prerelease 默认忽略
        if is_prerelease:
            logger.info(
                "更新检查：发现 prerelease（{}），默认忽略",
                latest_str,
            )
            return UpdateResult(
                status=UpdateStatus.PRERELEASE_AVAILABLE,
                current=self.current_version,
                latest=latest_str,
                release_url=release_url,
                changelog=changelog,
            )

        # 有可用的稳定版更新
        logger.info(
            "更新检查：发现新版本 {} → {}",
            self.current_version, latest_str,
        )
        return UpdateResult(
            status=UpdateStatus.UPDATE_AVAILABLE,
            current=self.current_version,
            latest=latest_str,
            release_url=release_url,
            changelog=changelog,
        )

    def _emit_result(self, result: UpdateResult) -> None:
        """发出结果信号（统一入口，方便未来加埋点）"""
        self.checked.emit(result)
