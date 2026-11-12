"""エントリーポイント。 python main.py [--minimized]"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication

from tokutai import autostart, db, paths, settings as settings_mod
from tokutai.ui_main import MainWindow

SERVER_NAME = "TokutaiShoribore-single-instance"


def setup_logging() -> None:
    h = RotatingFileHandler(paths.log_path(), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[h])


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # トレイに常駐
    app.setStyleSheet(
        "* { font-size: 13pt; }"
        "QPushButton { min-height: 40px; padding: 4px 14px; }"
        "QHeaderView::section { padding: 6px; }"
        "QTableWidget::item { padding: 4px; }"
    )

    # 二重起動の防止: 既存の画面を前面に出して終了
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(300)
        return 0
    QLocalServer.removeServer(SERVER_NAME)
    server = QLocalServer()
    server.listen(SERVER_NAME)

    setup_logging()
    db.daily_backup()
    db.init()

    st = settings_mod.load()
    if not st.autostart_initialized:  # 初回起動時にスタートアップへ登録
        try:
            autostart.set_enabled(st.autostart)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("自動起動の登録に失敗")
        st.autostart_initialized = True
        settings_mod.save(st)

    win = MainWindow(start_minimized="--minimized" in sys.argv)

    def on_connection():
        c = server.nextPendingConnection()
        if c:
            c.readyRead.connect(win.show_front)

    server.newConnection.connect(on_connection)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
