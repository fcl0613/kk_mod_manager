"""卡片解析后台线程（F17-11 / NF-P05）

场景卡内嵌角色多、解析耗时可能超过同步阈值，参照
BatchInstallDialog / SyncWorker 的 worker 模式在 QThread 中执行：
  - 解析复用主窗口传入的 CardService（kc 状态供后续转卡/重解析使用；
    同一时刻仅允许一次解析，由 MainWindow 忙标志串行化）
  - 可用性检查在 worker 线程内自建 session，不与主窗口共享
"""

from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from app.database import get_session
from app.services.card_service import CardService, CardParseResult


class CardParseWorker(QObject):
    """在后台线程中解析卡片并完成可用性检查"""

    progress = pyqtSignal(int, int)   # (已处理内嵌角色数, 总角色数)，仅场景卡触发
    finished = pyqtSignal(object)     # CardParseResult（is_valid=False 的失败结果也走这里）
    error = pyqtSignal(str)           # 未预期异常（解析失败本身不算，见 finished）

    def __init__(self, card_service: CardService, card_path: Path) -> None:
        super().__init__()
        self._card_service = card_service
        self._card_path = card_path

    def run(self) -> None:
        try:
            result = self._card_service.parse_card(
                self._card_path,
                progress=lambda done, total: self.progress.emit(done, total),
            )
            if result.is_valid:
                session = get_session()
                try:
                    self._card_service.check_mod_availability(result, session)
                finally:
                    session.close()
            self.finished.emit(result)
        except Exception as e:  # noqa: BLE001 - 后台线程兜底，任何异常都要回传 UI
            self.error.emit(str(e))
