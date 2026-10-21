from __future__ import annotations

import hashlib
import logging
import sys

from PySide6.QtCore import QLockFile
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox

from .config import APP_AUTHOR, APP_NAME, SettingsStore, application_state_directory
from .i18n import translate
from .ui import MainWindow

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    if root_logger.handlers:
        return

    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    )
    root_logger.addHandler(handler)


def main() -> int:
    configure_logging()
    logger.info("Starte %s", APP_NAME)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_AUTHOR)

    state_dir = application_state_directory()
    instance_lock = QLockFile(str(state_dir / "scribe.lock"))
    server_name = "scribe-" + hashlib.sha256(str(state_dir).lower().encode("utf-8")).hexdigest()[:24]
    if not instance_lock.tryLock(0):
        client = QLocalSocket()
        client.connectToServer(server_name)
        if client.waitForConnected(1500):
            client.write(b"show")
            client.waitForBytesWritten(500)
            client.disconnectFromServer()
            return 0
        language = SettingsStore().load().ui_language
        QMessageBox.information(None, APP_NAME, translate(language, "scribe läuft bereits. Bitte verwende das offene Fenster."))
        return 1

    server = QLocalServer(app)
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    # The lock proves no other instance owns this user's stale pipe.
    QLocalServer.removeServer(server_name)
    if not server.listen(server_name):
        logger.error("Lokaler Instanz-Server konnte nicht gestartet werden: %s", server.errorString())

    window = MainWindow(SettingsStore())

    def show_existing_window() -> None:
        while server.hasPendingConnections():
            connection = server.nextPendingConnection()
            connection.close()
            connection.deleteLater()
        window.showNormal()
        window.raise_()
        window.activateWindow()

    server.newConnection.connect(show_existing_window)
    app.setWindowIcon(window.windowIcon())
    window.show()
    try:
        return app.exec()
    finally:
        server.close()
        instance_lock.unlock()


if __name__ == "__main__":
    raise SystemExit(main())
