"""Native desktop presentation for the approved Studio workspace.



Only widget composition and presentation live here. Existing batch tasks,

customer profiles, editor models and synthesis workers remain authoritative.

"""

from pathlib import Path

from PyQt5.QtCore import Qt, QSize, QTimer, QObject, QEvent

from PyQt5.QtGui import QColor, QFont, QIcon, QPainter

from PyQt5.QtWidgets import (QWidget,QFrame,QDialog,QLabel,QPushButton,QHBoxLayout,QVBoxLayout,

    QGridLayout,QButtonGroup,QHeaderView,QAbstractItemView,QStyledItemDelegate)

from qfluentwidgets import FluentIcon as FIF,Theme

from app.core.entities import BatchTaskStatus



STYLE = '''

QWidget#studioSidebar {background:#ffffff;border-right:1px solid #e4e9ee;}

QWidget#batchProcessInterface,QWidget#HomeInterface {background:#f4f6f8;color:#263440;}

QLabel {color:#263440;font-family:"Microsoft YaHei UI";font-size:13px;}

QLabel[role="title"] {font-size:26px;font-weight:600;letter-spacing:-1px;}

QLabel[role="muted"] {color:#758493;font-size:12px;}

QLabel[role="eyebrow"] {color:#8a98a5;font-size:11px;letter-spacing:1px;}

QLabel[role="service"] {color:#148478;font-size:12px;}

QLabel[role="brand"] {font-weight:600;font-size:15px;}

QFrame[role="card"] {background:#ffffff;border:1px solid #e3e9ee;border-radius:10px;}

QPushButton {font-family:"Microsoft YaHei UI";font-size:13px;color:#344554;background:#ffffff;border:1px solid #dfe6eb;border-radius:7px;padding:8px 13px;}

QPushButton:hover {background:#eef6f4;border-color:#c4dcd5;}

QPushButton:disabled {color:#a9b4be;background:#f0f3f6;}

QPushButton[role="primary"] {color:#ffffff;background:#168779;border-color:#168779;font-weight:500;}

QPushButton[role="primary"]:hover {background:#107769;}

QPushButton[role="subtle"] {background:transparent;border-color:transparent;color:#6b7b88;}

QPushButton[role="navigation"] {text-align:left;background:transparent;border:0;color:#647583;padding:12px 14px;}

QPushButton[role="navigation"]:checked {color:#137b6e;background:#e8f4ef;font-weight:600;}

QPushButton[role="navigation"]:hover {background:#eff5f3;}

QPushButton[role="tab"] {background:transparent;border:0;border-radius:0;padding:11px 0;color:#84929e;}

QPushButton[role="tab"]:checked {color:#263c47;border-bottom:2px solid #168779;font-weight:600;}

QPushButton#studioDropZone {border:1px dashed #d9e2e8;border-radius:9px;background:transparent;color:#81909d;padding:17px;}

QPushButton#studioDropZone:hover {background:#eaf5f0;border-color:#168779;}

QComboBox {background:#fff;color:#30434e;border:1px solid #dce4e9;border-radius:6px;padding:8px 10px;}

QComboBox QAbstractItemView {background:#fff;color:#30434e;selection-background-color:#e8f4ef;selection-color:#126e61;border:1px solid #dce4e9;}

QSpinBox {background:#fff;color:#30434e;border:1px solid #dce4e9;border-radius:6px;padding:7px;}

QTableView#studioTaskTable,QTableView#studioCaptionTable {background:#ffffff;alternate-background-color:#ffffff;color:#2f414e;border:0;border-radius:0;gridline-color:transparent;selection-background-color:#edf7f3;selection-color:#23443f;}

QTableView#studioTaskTable::item {border:0;border-bottom:1px solid #edf0f3;padding:10px 12px;}

QTableView#studioCaptionTable::item {border:0;border-bottom:1px solid #edf0f3;padding:10px 5px;}

QHeaderView::section {background:#f8fafb;color:#82909e;border:0;border-bottom:1px solid #e8edf0;padding:12px 9px;font-size:12px;}

QScrollBar:vertical {background:transparent;width:8px;}

QScrollBar::handle:vertical {background:#d9e1e7;border-radius:4px;min-height:28px;}

QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {height:0;}

QLabel#studioKLogo {background:#168779;color:white;border-radius:8px;font-size:18px;font-weight:600;}

QWidget#studioEmpty {background:#ffffff;}

QLabel#studioEmptyIcon {color:#91b6ad;font-size:30px;}

'''





def label(text,parent,role='muted'):

    obj=QLabel(text,parent);obj.setProperty('role',role);obj.setWordWrap(True)

    return obj





def button(text,parent,callback=None,role='subtle',icon=None):

    b=QPushButton(text,parent);b.setProperty('role',role)

    b.setMinimumHeight(35)

    if icon:b.setIcon(icon.icon(Theme.LIGHT));b.setIconSize(QSize(16,16))

    if callback:b.clicked.connect(callback)

    return b





def style_primary(widget,icon=None):

    widget.setStyleSheet('QPushButton {background:#168779;color:#ffffff;border:1px solid #168779;border-radius:7px;padding:9px 16px;font-size:13px;font-weight:500;} QPushButton:hover {background:#107769;} QPushButton:disabled {background:#a3c9c2;border-color:#a3c9c2;}')

    # Fluent PushButton paints its icon separately; native padding overrides

    # would otherwise paint the icon over the label.

    widget.setIcon(None)





def detach_layout(layout):

    """Remove presentation containers without deleting live functional widgets."""

    while layout.count():

        item=layout.takeAt(0)

        if item.widget():item.widget().hide()

        if item.layout():detach_layout(item.layout());item.layout().deleteLater()





class DropZone(QPushButton):

    def __init__(self,page):

        super().__init__('将视频、音频或已有字幕拖到这里，继续添加任务',page)

        self.page=page;self.setObjectName('studioDropZone');self.setAcceptDrops(True)

        self.clicked.connect(page.on_add_file_clicked)

    def dragEnterEvent(self,event):

        if event.mimeData().hasUrls():event.acceptProposedAction()

    def dropEvent(self,event):

        self.page.add_files([u.toLocalFile() for u in event.mimeData().urls()]);event.acceptProposedAction()





class FileDelegate(QStyledItemDelegate):

    def paint(self,painter,option,index):

        super().paint(painter,option,index) if index.column()!=0 else self.draw_file(painter,option,index)

    def draw_file(self,painter,option,index):

        from PyQt5.QtWidgets import QStyle

        painter.save();rect=option.rect

        selected=bool(option.state & QStyle.State_Selected)

        painter.fillRect(rect,QColor('#edf7f3' if selected else '#ffffff'))

        painter.setPen(QColor('#edf0f3'));painter.drawLine(rect.bottomLeft(),rect.bottomRight())

        thumb=rect.adjusted(14,16,-rect.width()+48,-16)

        painter.setPen(Qt.NoPen);painter.setBrush(QColor('#e5efed'));painter.drawRoundedRect(thumb,5,5)

        FIF.VIDEO.icon(Theme.LIGHT).paint(painter,thumb.adjusted(8,9,-8,-9))

        painter.setPen(QColor('#2d414c'));font=QFont('Microsoft YaHei UI',10);font.setWeight(QFont.Medium);painter.setFont(font)

        name=Path(str(index.data() or '')).stem

        area=rect.adjusted(62,11,-12,-30)

        painter.drawText(area,Qt.AlignVCenter,painter.fontMetrics().elidedText(name,Qt.ElideMiddle,area.width()))

        painter.setPen(QColor('#8b99a4'));font.setPointSize(8);font.setWeight(QFont.Normal);painter.setFont(font)

        painter.drawText(rect.adjusted(62,38,-12,-7),Qt.AlignVCenter,Path(str(index.data() or '')).suffix.lstrip('.').upper()+'  ·  原始素材')

        painter.restore()





class StatusDelegate(QStyledItemDelegate):

    def paint(self,painter,option,index):

        text=str(index.data() or '')

        if index.column()==3:

            text='待绑定' if text.startswith('未绑定') else '已绑定' if text.startswith('已绑定') else text

        done=text==str(BatchTaskStatus.COMPLETED)

        quality=index.data(Qt.UserRole+61) if index.column()==2 and done else None
        if quality:
            text=str(quality).replace(' · ','\n').replace('待核对 ','待核对').replace(' 项','项')
            if '报告无待核对项' in text:text='已生成\n无待核对项'
            if '读取失败' in text:text='已生成\n报告读取失败'
        color='#c34848' if any(x in text for x in ('失败','错误','未通过')) else '#168779' if done or text=='已绑定' else '#627c8b' if any(x in text for x in ('转录','替换','翻译','合成中','上传')) else '#8a97a2'

        if quality and ('待核对' in text or '未检查' in text or '读取失败' in text):color='#ac741c'
        painter.save();painter.setPen(QColor(color))

        font=QFont('Microsoft YaHei UI',9);painter.setFont(font)

        painter.drawText(option.rect.adjusted(6,0,-6,0),Qt.AlignCenter|Qt.TextWordWrap,('✓ '+text) if done else text)

        painter.restore()





class BatchPresentation:

    def __init__(self,page):

        self.page=page;self.filter='all'

        self.refresh_timer=QTimer(page);self.refresh_timer.setSingleShot(True);self.refresh_timer.setInterval(120)

        self.refresh_timer.timeout.connect(self.refresh)

        old=page.layout();detach_layout(old)

        old.setContentsMargins(30,26,30,22);old.setSpacing(14)

        self.layout=old

        heading=QHBoxLayout();text=QVBoxLayout();text.setSpacing(4)

        text.addWidget(label('制作中心',page,'title'))

        text.addWidget(label('让每一集，从素材到成品。',page))

        heading.addLayout(text,1)

        page.add_file_btn.setText('添加素材');page.add_file_btn.setProperty('role','primary')

        page.add_file_btn.setMinimumHeight(39);heading.addWidget(page.add_file_btn)

        style_primary(page.add_file_btn,FIF.ADD)

        old.addLayout(heading)



        self.template_card=QFrame(page);self.template_card.setProperty('role','card')

        box=QHBoxLayout(self.template_card);box.setContentsMargins(18,16,18,16);box.setSpacing(15)

        icon=QLabel(self.template_card);icon.setPixmap(FIF.DOCUMENT.icon(Theme.LIGHT).pixmap(23,23));box.addWidget(icon)

        desc=QVBoxLayout();top=QHBoxLayout()

        top.addWidget(label('共享客户模板',page,'eyebrow'))

        page.customer_selector.combo.setMinimumWidth(260);page.customer_selector.combo.setMaximumWidth(340)

        top.addWidget(page.customer_selector.combo);top.addStretch()

        desc.addLayout(top);page.customer_selector.hint.setMaximumHeight(22);desc.addWidget(page.customer_selector.hint)

        self.flow=label('',page);desc.addWidget(self.flow)

        box.addLayout(desc,1)

        page.settings_btn.setText('任务选项');page.settings_btn.setProperty('role','subtle')

        try:page.settings_btn.toggled.disconnect()

        except TypeError:pass

        box.addWidget(page.settings_btn)

        old.addWidget(self.template_card)



        self.advanced=QDialog(page);self.advanced.setWindowTitle('任务选项（仅用于本次任务）')

        self.advanced.setMinimumWidth(610)

        self.advanced.setStyleSheet(STYLE+'QDialog {background:#f4f6f8;}')

        grid=QGridLayout(self.advanced);grid.setContentsMargins(18,16,18,16);grid.setHorizontalSpacing(22);grid.setVerticalSpacing(12)

        for i,(name,control) in enumerate([('转录方式',page.backend_combo),('任务类型',page.task_type_combo),('文字处理方式',page.customer_selector.mode_combo),('输出质量',page.quality_combo)]):

            grid.addWidget(label(name,page),i//2*2,i%2)

            grid.addWidget(control,i//2*2+1,i%2);control.setMinimumWidth(0);control.show()

        # Studio reparents the route controls into a dialog. Device selection

        # must move too; leaving it on the underlying page makes it invisible

        # behind this modal settings window.

        if hasattr(page,'device_panel'):

            grid.addWidget(page.device_panel,4,0,1,2)

            page.device_combo.setMinimumWidth(0)

        if hasattr(page, 'execution_combo'):

            grid.addWidget(label('全流程执行位置', self.advanced),5,0)

            grid.addWidget(page.execution_combo,5,1)

            page.execution_combo.setMinimumWidth(0)

            page.execution_combo.show()

        self.advanced.hide();page.route_panel.hide();page.route_hint.hide()

        page.settings_btn.toggled.connect(self.advanced.setVisible)

        page.settings_btn.toggled.connect(page.route_panel.setVisible)

        if hasattr(page,'device_panel'):

            page.settings_btn.toggled.connect(page.update_backend_hint)

        # Keep route_panel as the compatibility visibility flag; its original

        # controls now live in the actual settings card, not a second form.

        page.route_panel.setMaximumHeight(0)

        done=button('完成设置',self.advanced,self.advanced.accept,role='primary')

        grid.addWidget(label('接口和文字方式可临时切换；外观等规则沿用模板。',self.advanced),6,0)

        grid.addWidget(done,6,1,Qt.AlignRight)
        page.retry_settings_btn=button('自动重试：最多'+str(page.batch_thread.auto_retry_limit)+'次' if page.batch_thread.auto_retry_limit else '自动重试：关闭',self.advanced,page.configure_auto_retry)
        grid.addWidget(label('临时失败自动恢复（不改变客户模板）',self.advanced),7,0)
        grid.addWidget(page.retry_settings_btn,7,1)

        self.advanced.finished.connect(lambda *args:page.settings_btn.setChecked(False))



        tools=QHBoxLayout();tools.setSpacing(23);self.tabs={}

        self.group=QButtonGroup(page);self.group.setExclusive(True)

        for key,text in [('all','全部任务'),('active','进行中'),('done','已完成'),('terminal','需处理 / 已取消')]:

            b=button(text,page,role='tab');b.setCheckable(True);b.setChecked(key=='all')

            b.clicked.connect(lambda checked,k=key:self.set_filter(k));self.group.addButton(b);self.tabs[key]=b;tools.addWidget(b)

        tools.addStretch()

        page.preflight_btn=button('检查结果',page,page.show_preflight_results);tools.addWidget(page.preflight_btn)
        bind=button('批量绑定剧本',page,self.open_binding,icon=FIF.LINK);tools.addWidget(bind)

        page.history_btn.setText('历史任务');page.history_btn.setProperty('role','subtle');tools.addWidget(page.history_btn)

        page.clear_btn.setProperty('role','subtle');tools.addWidget(page.clear_btn)

        page.cancel_all_btn.setProperty('role','subtle');tools.addWidget(page.cancel_all_btn);page.cancel_all_btn.show()

        old.addLayout(tools)



        self.task_card=QFrame(page);self.task_card.setProperty('role','card')

        task_layout=QVBoxLayout(self.task_card);task_layout.setContentsMargins(0,0,0,0);task_layout.setSpacing(0)

        table=page.task_table;table.setObjectName('studioTaskTable');table.setShowGrid(False)

        table.setAlternatingRowColors(False);table.verticalHeader().hide()

        table.setSelectionBehavior(QAbstractItemView.SelectRows)

        table.setColumnCount(5)

        table.setHorizontalHeaderLabels(['素材名称','处理进度','状态','剧本文件','操作'])

        table.horizontalHeader().setSectionResizeMode(0,QHeaderView.Stretch)

        table.horizontalHeader().setSectionResizeMode(3,QHeaderView.Fixed)

        table.horizontalHeader().setSectionResizeMode(4,QHeaderView.Fixed)

        table.setColumnWidth(1,130);table.setColumnWidth(2,250);table.setColumnWidth(3,90);table.setColumnWidth(4,252)

        table.setMinimumHeight(150)

        table.verticalHeader().setDefaultSectionSize(112)

        table.setItemDelegateForColumn(0,FileDelegate(table));table.setBorderVisible(False)

        table.setItemDelegateForColumn(2,StatusDelegate(table));table.setItemDelegateForColumn(3,StatusDelegate(table))

        table.horizontalHeader().setStyleSheet('QHeaderView::section {background:#f8fafb;color:#82909e;border:0;border-bottom:1px solid #e8edf0;padding:10px 9px;font-size:12px;}')

        task_layout.addWidget(table,1)

        self.empty=QWidget(page);self.empty.setObjectName('studioEmpty')

        empty=QVBoxLayout(self.empty);empty.setContentsMargins(30,50,30,50);empty.setSpacing(10);empty.addStretch()

        image=QLabel(self.empty);image.setPixmap(FIF.FOLDER_ADD.icon(Theme.LIGHT).pixmap(36,36));image.setAlignment(Qt.AlignCenter)

        empty.addWidget(image)

        message=label('把素材放进来，从这里开始',page,'brand');message.setAlignment(Qt.AlignCenter);empty.addWidget(message)

        detail=label('视频、音频、已有字幕均可导入，支持一次添加多个文件。',page);detail.setAlignment(Qt.AlignCenter);empty.addWidget(detail)

        self.empty_action=button('选择素材',page,page.on_add_file_clicked,role='primary');empty.addWidget(self.empty_action,0,Qt.AlignHCenter)

        empty.addStretch();task_layout.addWidget(self.empty,1)

        old.addWidget(self.task_card,1)

        self.drop=DropZone(page);old.addWidget(self.drop)



        footer=QHBoxLayout();where=QVBoxLayout();where.setSpacing(2)

        where.addWidget(label('成品保存至原视频目录',page))

        folder=button('打开输出文件夹',page,self.open_folder,icon=FIF.FOLDER);where.addWidget(folder,0,Qt.AlignLeft)

        footer.addLayout(where,1)

        footer.addWidget(label('同时处理',page));footer.addWidget(page.concurrency_spin)

        footer.addWidget(label('条',page));page.concurrency_spin.setFixedSize(118,38)

        page.start_all_btn.setProperty('role','primary');page.start_all_btn.setMinimumHeight(40)

        style_primary(page.start_all_btn,FIF.PLAY)

        footer.addWidget(page.start_all_btn)

        old.addLayout(footer)

        page.route_summary.setProperty('role','service');old.addWidget(page.route_summary)

        self.old_summary=page.route_summary

        page.editor_btn.hide();page.output_hint.hide();page.customer_selector.note.hide()

        # Explicitly show widgets moved out of the old hidden containers.

        for w in (page.add_file_btn,page.customer_selector.combo,page.customer_selector.hint,page.settings_btn,

                  page.history_btn,page.clear_btn,page.concurrency_spin,page.start_all_btn,page.route_summary):w.show()

        table.model().rowsInserted.connect(self.schedule_refresh);table.model().rowsRemoved.connect(self.schedule_refresh)

        table.model().modelReset.connect(self.schedule_refresh);table.model().dataChanged.connect(self.schedule_refresh)

        page.backend_combo.currentIndexChanged.connect(self.refresh_flow)

        page.customer_selector.combo.currentIndexChanged.connect(self.refresh_flow)

        page.task_type_combo.currentIndexChanged.connect(self.refresh_flow)

        previous_open = page.customer_selector.combo.before_open

        def refresh_shared_picker():

            if previous_open: previous_open()

            page.sync_shared_defaults()

            self.refresh_flow()

        page.customer_selector.combo.before_open = refresh_shared_picker

        page.settings_btn.clicked.connect(page.sync_shared_defaults)

        self.refresh();self.refresh_flow()



    def refresh_flow(self,*args):

        from PyQt5 import sip
        if sip.isdeleted(self.page):return
        mode=self.page.customer_selector.mode_combo.currentText()

        task=self.page.task_type_combo.currentText()

        self.flow.setText('处理流程  ·  '+task+'   /   '+mode)



    def schedule_refresh(self,*args):

        from PyQt5 import sip
        if sip.isdeleted(self.page) or sip.isdeleted(self.refresh_timer):return
        if not self.refresh_timer.isActive():self.refresh_timer.start()



    def set_filter(self,key):self.filter=key;self.refresh()



    def refresh(self,*args):

        from PyQt5 import sip

        table=self.page.task_table

        if sip.isdeleted(table):return

        counts={'all':table.rowCount(),'active':0,'done':0,'terminal':0}

        for row in range(table.rowCount()):

            item=table.item(row,2)
            if item:
                item.setFont(QFont('Microsoft YaHei UI',10))
                item.setForeground(QColor('#263c47'))
                item.setToolTip(item.text())
            done=bool(item and item.text()==str(BatchTaskStatus.COMPLETED))

            terminal=bool(item and item.text() in (str(BatchTaskStatus.FAILED),'已取消','检查未通过'))
            key='done' if done else ('terminal' if terminal else 'active')
            if key in counts:counts[key]+=1

            hidden=self.filter not in ('all',key)

            if table.isRowHidden(row)!=hidden:table.setRowHidden(row,hidden)

            bar=table.cellWidget(row,1)

            if bar and not bar.property('studioReady'):

                bar.setProperty('studioReady',True)

                bar.setCustomBarColor('#168779','#168779');bar.setCustomBackgroundColor('#e7eeed','#e7eeed')

                bar.ani.stop();bar.setUseAni(False);bar.setVal(bar.value());bar.setFixedHeight(5)

            source=table.item(row,0)

            if source:

                action=table.cellWidget(row,4)

                if action is None:

                    action=QWidget(table);actions=QVBoxLayout(action)

                    actions.setContentsMargins(6,4,6,4);actions.setSpacing(4)

                    action.open_button=button('打开目录',action,role='subtle');action.open_button.setFixedHeight(32)

                    action.open_button.clicked.connect(lambda checked=False,b=action.open_button:self.row_action(b))

                    actions.addWidget(action.open_button)

                    action.cancel_button=button('取消任务',action,role='subtle');action.cancel_button.setFixedHeight(30)

                    action.cancel_button.setStyleSheet('QPushButton {color:#c24a48;background:transparent;border:0;padding:3px 6px;border-radius:5px;} QPushButton:hover {background:#fceeee;}')

                    action.cancel_button.setToolTip('取消后等待后台停止并释放占用；清理列表不会删除原视频和成片')

                    action.cancel_button.clicked.connect(lambda checked=False,b=action.cancel_button:self.page.cancel_task(b.property('video')))

                    action_row=QGridLayout();action_row.setContentsMargins(0,0,0,0);action_row.setHorizontalSpacing(8);action_row.setVerticalSpacing(4)

                    action.retry_button=button('重试任务',action,role='primary');action.retry_button.setFixedHeight(30)

                    action.retry_button.clicked.connect(lambda checked=False,b=action.retry_button:self.page.retry_task(b.property('video')))

                    action.recompose_button=button('重新合成',action,role='subtle');action.recompose_button.setFixedHeight(30)
                    action.recompose_button.setToolTip('使用现有ASS在云端重新合成；不转录、不校正文字')
                    action.recompose_button.clicked.connect(lambda checked=False,b=action.recompose_button:self.page.recompose_task(b.property('video')))
                    action.error_button=button('查看原因',action,role='subtle')
                    action.error_button.clicked.connect(lambda checked=False,b=action.error_button:self.page.show_task_error(b.property('video')))
                    action_row.addWidget(action.error_button,0,1)
                    action_row.addWidget(action.recompose_button,1,0)
                    action_row.addWidget(action.retry_button,0,0);action_row.addWidget(action.cancel_button,1,1)
                    for control in (action.retry_button,action.error_button,action.recompose_button,action.cancel_button):
                        control.setMinimumWidth(108);control.setFixedHeight(30)
                    action.error_button.setText("查看失败原因")

                    actions.addLayout(action_row);table.setCellWidget(row,4,action)

                manifest=source.data(Qt.UserRole)

                quality=getattr(self.page,'_quality_cache',{}).get(source.toolTip(),{})
                signature=(source.toolTip(),manifest,item.text() if item else '',self.page._ass_available.get(source.toolTip(),False),quality.get('count',0))

                if getattr(action,'_row_state',None)==signature:continue

                action._row_state=signature

                action.open_button.setProperty('video',source.toolTip());action.open_button.setProperty('manifest',manifest)

                action.open_button.setText(('核对字幕（'+str(quality['count'])+'）' if quality.get('count') else '预览与编辑') if manifest else '打开目录')

                action.cancel_button.setProperty('video',source.toolTip())

                failed=bool(item and item.text() in (str(BatchTaskStatus.FAILED),'已取消','检查未通过'))

                terminal=bool(item and item.text() in (str(BatchTaskStatus.COMPLETED),str(BatchTaskStatus.FAILED),'已取消','检查未通过'))

                action.cancel_button.setText('移除任务' if terminal else '取消任务')
                action.cancel_button.setEnabled(not (item and item.text()=='正在取消'))
                action.error_button.setProperty('video',source.toolTip())
                action.error_button.setVisible(bool(item and item.text() in (str(BatchTaskStatus.FAILED),'检查未通过')))

                action.cancel_button.setVisible(True)

                action.retry_button.setProperty('video',source.toolTip())

                action.retry_button.setVisible(failed)
                action.retry_button.setText('重新检查' if item and item.text()=='检查未通过' else '重试任务')
                task=self.page.batch_thread.current_tasks.get(source.toolTip())
                if task:
                    from app.core.task_retry import recovery
                    action.retry_button.setToolTip(recovery(task)+'；手动恢复会重新计算自动重试次数')
                action.recompose_button.setProperty('video',source.toolTip())
                action.recompose_button.setVisible(terminal and self.page._ass_available.get(source.toolTip(),False))

        for key,name in [('all','全部任务'),('active','进行中'),('done','已完成'),('terminal','需处理 / 已取消')]:

            text=name+'  '+str(counts[key])

            if self.tabs[key].text()!=text:self.tabs[key].setText(text)

        table.setVisible(table.rowCount()>0);self.empty.setVisible(table.rowCount()==0)

        self.drop.setVisible(table.rowCount()>0)



    def open_binding(self):

        main=self.page.window()

        if hasattr(main,'scriptReferenceInterface'):

            main.switchTo(main.scriptReferenceInterface)



    def row_action(self,action):

        source=action.property('video');manifest=action.property('manifest')

        if manifest:self.page.open_project_editor(source,manifest,review=bool(getattr(self.page,'_quality_cache',{}).get(source,{}).get('count')))

        elif source:self.page.open_output_folder(source)



    def open_folder(self):

        table=self.page.task_table;row=table.currentRow()

        if row<0 and table.rowCount():row=0

        if row>=0:self.page.open_output_folder(table.item(row,0).toolTip())

        else:self.page.load_saved_projects()





def apply_shell(window):

    window.setMicaEffectEnabled(False)

    window.setCustomBackgroundColor('#f4f6f8','#f4f6f8')

    window.setStyleSheet(STYLE)

    window.navigationInterface.hide()

    sidebar=QWidget(window);sidebar.setObjectName('studioSidebar');sidebar.setFixedWidth(174)

    layout=QVBoxLayout(sidebar);layout.setContentsMargins(13,67,13,19);layout.setSpacing(7)

    layout.addWidget(label('工作空间',sidebar,'eyebrow'))

    entries=[('制作中心',FIF.VIDEO,lambda:window.switchTo(window.batchProcessInterface)),

             ('字幕编辑器',FIF.EDIT,window.openStudioEditor),

             ('客户模板',FIF.PEOPLE,lambda:window.switchTo(window.customerTemplateInterface))]

    buttons=[]

    group=QButtonGroup(window);group.setExclusive(True)

    for name,icon,fn in entries:

        b=button(name,sidebar,fn,role='navigation',icon=icon);b.setCheckable(True);group.addButton(b);layout.addWidget(b);buttons.append(b)

    buttons[0].setChecked(True)

    layout.addSpacing(20)

    more=button('更多工具',sidebar,role='subtle',icon=FIF.MORE);more.setCheckable(True);layout.addWidget(more)

    extra=QWidget(sidebar);extra_layout=QVBoxLayout(extra);extra_layout.setContentsMargins(0,0,0,0)

    for name,icon,fn in [('单条处理',FIF.HOME,lambda:show_single(window)),('剧本绑定',FIF.DOCUMENT,lambda:window.switchTo(window.scriptReferenceInterface)),('字幕样式',FIF.FONT,lambda:window.switchTo(window.subtitleStyleInterface)),('视频合成',FIF.VIDEO,lambda:show_synthesis(window))]:

        extra_layout.addWidget(button(name,extra,fn,role='navigation',icon=icon))

    extra.hide();more.toggled.connect(extra.setVisible);layout.addWidget(extra)

    layout.addStretch()

    layout.addWidget(label('● 团队配置',sidebar,'service'))

    layout.addWidget(label('模板共享 · 任务独立',sidebar))

    layout.addWidget(label('Studio 2026.10.27.3',sidebar,'eyebrow'))

    layout.addSpacing(10)

    layout.addWidget(button('服务设置',sidebar,lambda:window.switchTo(window.settingInterface),role='navigation',icon=FIF.SETTING))

    layout.addWidget(button('项目主页',sidebar,window.onGithubDialog,role='subtle',icon=FIF.GITHUB))

    window.hBoxLayout.insertWidget(0,sidebar)

    window.studio_sidebar=sidebar;window.studio_nav_buttons=buttons

    window.titleBar.titleLabel.setText('岩中花树字幕工具')

    window.windowTitleChanged.connect(lambda *args:window.titleBar.titleLabel.setText('岩中花树字幕工具'))

    window.titleBar.titleLabel.setStyleSheet('font-size:13px;font-weight:600;color:#344b57;')

    window.titleBar.setStyleSheet('background:#ffffff;border-bottom:1px solid #e4e9ee;')

    window.titleBar.iconLabel.setPixmap(FIF.VIDEO.icon(Theme.LIGHT).pixmap(18,18))

    window.titleBar.hBoxLayout.setContentsMargins(18,0,0,0)

    window.titleBar.move(0,0);window.titleBar.resize(window.width(),window.titleBar.height())

    window.batchProcessInterface.presentation=BatchPresentation(window.batchProcessInterface)

    window.stackedWidget.currentChanged.connect(lambda *args:sync_navigation(window))

    window.titleBar.raise_()





def style_loaded_page(window, key, page):

    if key == 'home':

        page.stackedWidget.currentChanged.connect(lambda *args:sync_navigation(window))

        page.pivot.hide()

        page.vBoxLayout.setContentsMargins(24,20,24,22)

        style_editor(page.subtitle_optimization_interface)

    elif key == 'customers':

        from app.view.studio_form_theme import apply_form_theme

        apply_form_theme(page)







def sync_navigation(window):

    current=window.stackedWidget.currentWidget()

    index=0 if current is window.batchProcessInterface else 2 if current is window._lazy_pages.get("customers") else 1 if current is window._lazy_pages.get("home") else None

    if index is not None:window.studio_nav_buttons[index].setChecked(True)

    window.titleBar.titleLabel.setText('岩中花树字幕工具')





def show_single(window):

    window.switchTo(window.homeInterface)

    home=window.homeInterface;home.show_step(home.task_creation_interface,'TaskCreationInterface')





def show_synthesis(window):

    window.switchTo(window.homeInterface)

    home=window.homeInterface;home.show_step(home.video_synthesis_interface,'VideoSynthesisInterface')





def style_editor(editor):

    layout=editor.main_layout;layout.setContentsMargins(0,0,0,0);layout.setSpacing(9)

    head=QHBoxLayout();head.setSpacing(9)

    head.addWidget(label('字幕编辑器',editor,'title'))

    history=layout.takeAt(0).layout()

    first=True

    while history.count():

        item=history.takeAt(0)

        if item.widget():

            widget=item.widget()

            if first:widget.hide();first=False;continue

            head.addWidget(widget,1 if widget is editor.studio.history else 0)

    editor.studio.history.setMinimumWidth(200)

    editor.studio.timeline.setFixedHeight(86)

    render=button('保存并合成',editor,editor.prepare_batch_project_rerender,role='primary',icon=FIF.VIDEO)

    head.addWidget(render);layout.insertLayout(0,head)

    editor.subtitle_table.setObjectName('studioCaptionTable')

    editor.subtitle_table.setShowGrid(False);editor.subtitle_table.setBorderVisible(False)

    editor.subtitle_table.verticalHeader().setDefaultSectionSize(64)

    editor.subtitle_table.setColumnWidth(0,103);editor.subtitle_table.setColumnWidth(1,103)

    editor.studio.preview_box.setStyleSheet('QWidget {background:#182027;}')

    editor.studio.preview_box.setObjectName('studioPreview')

    editor.preview_skin=PreviewSkin(editor.studio.preview_box)

    editor.studio.preview_box.installEventFilter(editor.preview_skin)

    editor.preview_skin.apply()

    editor.studio.preview_box.setMinimumWidth(360)

    editor.studio.splitter.setStretchFactor(0,3);editor.studio.splitter.setStretchFactor(1,2)

    editor.studio.splitter.setSizes([550,450])

    editor.studio.splitter.setChildrenCollapsible(False)

    editor.start_button.setText('处理字幕')

    editor.command_bar.removeAction(editor.rerender_action)

    render.setEnabled(editor.rerender_action.isEnabled())

    editor.rerender_action.changed.connect(lambda:render.setEnabled(editor.rerender_action.isEnabled()))





class PreviewSkin(QObject):

    def __init__(self,box):super().__init__(box);self.box=box

    def eventFilter(self,obj,event):

        if event.type()==QEvent.ChildAdded:QTimer.singleShot(0,self.apply)

        return False

    def apply(self):

        for text in self.box.findChildren(QLabel):text.setStyleSheet('color:#c1d1da;font-size:12px;padding:6px;')

        for b in self.box.findChildren(QPushButton):b.setStyleSheet('QPushButton {background:#24343f;color:#e3edf2;border:1px solid #3a4d5a;border-radius:6px;padding:8px 12px;} QPushButton:hover {background:#304754;}')

