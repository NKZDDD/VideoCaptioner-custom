"""Start the VideoCaptioner desktop application."""
import os
import sys
import traceback

from PyQt5.QtCore import Qt, QTranslator
from PyQt5.QtWidgets import QApplication
from qfluentwidgets import FluentTranslator

from app.common.config import cfg
from app.config import RESOURCE_PATH
from app.core.utils import logger
from app.view.main_window import MainWindow

logger = logger.setup_logger("VideoCaptioner")


def exception_hook(exctype, value, tb):
    logger.error("".join(traceback.format_exception(exctype, value, tb)))
    sys.__excepthook__(exctype, value, tb)  # 调用默认的异常处理


def main():
    sys.excepthook = exception_hook

    # Enable DPI Scale
    if cfg.get(cfg.dpiScale) == "Auto":
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    else:
        os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
        os.environ["QT_SCALE_FACTOR"] = str(cfg.get(cfg.dpiScale))
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)

    app = QApplication(sys.argv)
    app.setAttribute(Qt.AA_DontCreateNativeWidgetSiblings)

    # Internationalization (Multi-language)
    locale = cfg.get(cfg.language).value
    translator = FluentTranslator(locale)
    myTranslator = QTranslator()
    translations_path = (
        RESOURCE_PATH / "translations" / f"VideoCaptioner_{locale.name()}.qm"
    )
    myTranslator.load(str(translations_path))
    app.installTranslator(translator)
    app.installTranslator(myTranslator)

    w = MainWindow()
    w.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
