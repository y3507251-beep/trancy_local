from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.dont_write_bytecode = True


def main():
    from PyQt6.QtWidgets import QApplication, QMessageBox
    from trancy_desktop.settings import DATA
    (DATA / "logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(DATA / "logs" / "desktop.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    app = QApplication(sys.argv)
    app.setApplicationName("Trancy 桌面字幕")
    app.setOrganizationName("TrancyLocal")
    app.setQuitOnLastWindowClosed(True)
    instance = None
    try:
        from trancy_desktop.instance import DesktopInstance
        instance = DesktopInstance(parent=app)
        if not instance.acquire():
            return 0
        from trancy_desktop.window import MainWindow
        window = MainWindow()

        def activate():
            if window.isMinimized():
                window.showNormal()
            else:
                window.show()
            window.raise_()
            window.activateWindow()

        instance.activated.connect(activate)
        window.show()
        return app.exec()
    except Exception as error:
        logging.exception("Desktop initialization failed")
        QMessageBox.critical(None, "Trancy 桌面字幕", f"启动失败：{error}\n诊断日志：{DATA / 'logs' / 'desktop.log'}")
        return 1
    finally:
        if instance is not None:
            instance.close()


if __name__ == "__main__":
    raise SystemExit(main())
