from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication

from .config import APP_AUTHOR, APP_NAME, SettingsStore
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

    window = MainWindow(SettingsStore())
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
