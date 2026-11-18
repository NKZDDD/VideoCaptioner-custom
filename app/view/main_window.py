import os

import psutil
from PyQt5.QtCore import QSize, QThread, QUrl
from PyQt5.QtGui import QDesktopServices, QIcon
from PyQt5.QtWidgets import QApplication
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import (
    FluentWindow,
    MessageBox,
    NavigationAvatarWidget,
    NavigationItemPosition,
    SplashScreen,
)

from app.common.config import cfg
from app.common.signal_bus import signalBus
from app.components.DonateDialog import DonateDialog
from app.config import ASSETS_PATH, GITHUB_REPO_URL
from app.thread.version_manager_thread import VersionManager
from app.view.batch_process_interface import BatchProcessInterface

LOGO_PATH = ASSETS_PATH / "logo.png"


class MainWindow(FluentWindow):

    def __init__(self):
        super().__init__()
        from qfluentwidgets import setTheme, setThemeColor, Theme
        setTheme(Theme.LIGHT, save=False)
        setThemeColor("#168779", save=False)
        self.initWindow()

        # Only the immediately usable workspace is constructed at startup.
        self._lazy_pages = {}
        self.batchProcessInterface = BatchProcessInterface(self)
        signalBus.customer_template_applied.connect(self.onCustomerTemplateApplied)

        # 初始化版本管理器
        self.versionManager = VersionManager()
        self.versionManager.newVersionAvailable.connect(self.onNewVersion)
        self.versionManager.announcementAvailable.connect(self.onAnnouncement)

        # 创建版本检查线程
        self.versionThread = QThread()
        self.versionManager.moveToThread(self.versionThread)
        self.versionThread.started.connect(self.versionManager.performCheck)
        self.versionThread.start()

        # 初始化导航界面
        self.initNavigation()
        from app.view.studio_visual import apply_shell
        apply_shell(self)
        from app.view.studio_window_controls import WindowWorkspace
        self.workspace_size_controls = WindowWorkspace(self)
        self.splashScreen.finish()

        # 注册退出处理， 清理进程
        import atexit

        atexit.register(self.stop)

    def initNavigation(self):
        """初始化导航栏"""
        self.addSubInterface(self.batchProcessInterface, FIF.VIDEO, self.tr("制作中心"))
        self.navigationInterface.addItem(
            routeKey="studioEditor", text="字幕编辑器", icon=FIF.EDIT,
            onClick=self.openStudioEditor)
        self.navigationInterface.addSeparator()

        # 在底部添加自定义小部件
        self.navigationInterface.addItem(
            routeKey="avatar",
            text="GitHub",
            icon=FIF.GITHUB,
            onClick=self.onGithubDialog,
            position=NavigationItemPosition.BOTTOM,
        )
        # Keep navigation labels visible for colleagues who do not know the icons.
        self.navigationInterface.setExpandWidth(180)
        self.navigationInterface.expand(useAni=False)
        # 设置默认界面
        self.switchTo(self.batchProcessInterface)

    def _page(self, key, module, class_name, icon, title):
        if key not in self._lazy_pages:
            from importlib import import_module
            page = getattr(import_module(module), class_name)(self)
            self._lazy_pages[key] = page
            self.addSubInterface(page, icon, title)
            from app.view.studio_visual import style_loaded_page
            style_loaded_page(self, key, page)
        return self._lazy_pages[key]

    @property
    def homeInterface(self):
        return self._page('home', 'app.view.home_interface', 'HomeInterface', FIF.HOME, '编辑与单条处理')

    @property
    def settingInterface(self):
        return self._page('settings', 'app.view.setting_interface', 'SettingInterface', FIF.SETTING, '服务与设置')

    @property
    def subtitleStyleInterface(self):
        return self._page('styles', 'app.view.subtitle_style_interface', 'SubtitleStyleInterface', FIF.FONT, '字幕样式')

    @property
    def scriptReferenceInterface(self):
        return self._page('scripts', 'app.view.script_reference_interface', 'ScriptReferenceInterface', FIF.DOCUMENT, '剧本绑定')

    @property
    def customerTemplateInterface(self):
        return self._page('customers', 'app.view.customer_template_interface', 'CustomerTemplateInterface', FIF.PEOPLE, '客户模板')

    def openStudioEditor(self):
        self.switchTo(self.homeInterface)
        editor = self.homeInterface.subtitle_optimization_interface
        self.homeInterface.show_step(editor, "SubtitleInterface")
        editor.studio.refresh()

    def switchTo(self, interface):
        if interface.windowTitle():
            self.setWindowTitle(interface.windowTitle())
        else:
            self.setWindowTitle(self.tr("岩中花树字幕工具"))
        self.setWindowTitle("岩中花树字幕工具 · " + self.windowTitle() + " · Studio UI 2026.10.27.3")
        self.stackedWidget.setCurrentWidget(interface, popOut=False)

    def onCustomerTemplateApplied(self):
        if 'home' not in self._lazy_pages:
            return  # Home reads the current shared template when first opened.
        self.homeInterface.task_creation_interface.setup_values()
        subtitle = self.homeInterface.subtitle_optimization_interface
        subtitle.set_values()
        subtitle.custom_prompt_text = cfg.custom_prompt_text.value
        subtitle._update_prompt_button_style()
        signalBus.subtitle_layout_changed.emit(cfg.subtitle_layout.value)
        signalBus.subtitle_optimization_changed.emit(cfg.need_optimize.value)
        signalBus.subtitle_translation_changed.emit(cfg.need_translate.value)
        signalBus.target_language_changed.emit(cfg.target_language.value.value)
        signalBus.transcription_model_changed.emit(cfg.transcribe_model.value.value)
        signalBus.soft_subtitle_changed.emit(cfg.soft_subtitle.value)
        signalBus.need_video_changed.emit(cfg.need_video.value)
        signalBus.video_quality_changed.emit(cfg.video_quality.value)

    def initWindow(self):
        """初始化窗口"""
        area = QApplication.desktop().availableGeometry()
        self.resize(min(1180, area.width()), min(860, max(640, area.height() - 60)))
        self.setMinimumWidth(1000)
        self.setWindowIcon(QIcon(str(LOGO_PATH)))
        self.setWindowTitle(self.tr("岩中花树字幕工具"))

        self.setMicaEffectEnabled(cfg.get(cfg.micaEnabled))

        # 创建启动画面
        self.splashScreen = SplashScreen(self.windowIcon(), self)
        self.splashScreen.setIconSize(QSize(106, 106))
        self.splashScreen.raise_()

        # 设置窗口位置, 居中
        desktop = QApplication.desktop().availableGeometry()
        w, h = desktop.width(), desktop.height()
        self.move(w // 2 - self.width() // 2, h // 2 - self.height() // 2)

        self.showMaximized()
        QApplication.processEvents()

    def onGithubDialog(self):
        """打开GitHub"""
        w = MessageBox(
            self.tr("GitHub信息"),
            self.tr(
                "VideoCaptioner 由本人在课余时间独立开发完成，目前托管在GitHub上，欢迎Star和Fork。项目诚然还有很多地方需要完善，遇到软件的问题或者BUG欢迎提交Issue。\n\n https://github.com/WEIFENG2333/VideoCaptioner"
            ),
            self,
        )
        w.yesButton.setText(self.tr("打开 GitHub"))
        w.cancelButton.setText(self.tr("支持作者"))
        if w.exec():
            QDesktopServices.openUrl(QUrl(GITHUB_REPO_URL))
        else:
            # 点击"支持作者"按钮时打开捐赠对话框
            donate_dialog = DonateDialog(self)
            donate_dialog.exec_()

    def onNewVersion(self, version, force_update, update_info, download_url):
        """新版本提示"""
        title = "发现新版本" if not force_update else "当前版本已停用"
        content = f"发现新版本 {version}\n\n{update_info}"
        w = MessageBox(title, content, self)
        w.yesButton.setText("立即更新")
        w.cancelButton.setText("稍后再说" if not force_update else "退出程序")
        if w.exec():
            QDesktopServices.openUrl(QUrl(download_url))
        if force_update:
            QApplication.quit()

    def onAnnouncement(self, content):
        """显示公告"""
        w = MessageBox("公告", content, self)
        w.yesButton.setText("我知道了")
        w.cancelButton.hide()
        w.exec()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "studio_sidebar"):
            self.titleBar.move(0, 0)
            self.titleBar.resize(self.width(), self.titleBar.height())
        if hasattr(self, "splashScreen"):
            self.splashScreen.resize(self.size())

    def closeEvent(self, event):
        home = self._lazy_pages.get('home')
        if home is not None:
            editor = home.subtitle_optimization_interface
            if not editor.studio.confirm_switch():
                event.ignore()
                return
            editor.studio.close()
        # 关闭所有子界面
        # self.homeInterface.close()
        # self.batchProcessInterface.close()
        # self.subtitleStyleInterface.close()
        # self.settingInterface.close()
        super().closeEvent(event)

        # 强制退出应用程序
        QApplication.quit()

        # 确保所有线程和进程都被终止 要是一些错误退出就不会处理了。
        # import os
        # os._exit(0)

    def stop(self):
        # 找到 FFmpeg 进程并关闭
        process = psutil.Process(os.getpid())
        for child in process.children(recursive=True):
            child.kill()
