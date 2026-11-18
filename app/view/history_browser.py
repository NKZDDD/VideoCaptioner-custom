"""Search saved projects by date and folder, without reading media on the GUI thread."""
import datetime
import ntpath
from PyQt5.QtCore import Qt,QAbstractTableModel,QModelIndex,QDate,QTimer
from PyQt5.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLabel,QLineEdit,QComboBox,
    QDateEdit,QTableView,QHeaderView,QAbstractItemView,QPushButton,QDialogButtonBox)


def folder_key(project):
    value=str(project.get('source_video') or '').replace('/','\\')
    return ntpath.normcase(ntpath.dirname(ntpath.normpath(value))) if value else ''


def folder_label(project):
    source=str(project.get('source_video') or '').replace('/','\\')
    value=ntpath.dirname(ntpath.normpath(source)) if source else ''
    parts=[part for part in value.split('\\') if part]
    return ' / '.join(parts[-3:]) or '未记录素材文件夹'


def customer_name(project):
    return str((project.get('customer_template_snapshot') or {}).get('name') or project.get('style_name') or '未记录客户')


def history_label(project):
    return ' · '.join((folder_label(project),str(project.get('title') or ntpath.basename(str(project.get('source_video','')))),customer_name(project),str(project.get('updated_at','')).replace('T',' ')))


def modified_date(project):
    try:return datetime.date.fromisoformat(str(project.get('updated_at',''))[:10])
    except (ValueError,TypeError):return None


def filter_projects(projects,query='',folder='',period='7',today=None,start=None,end=None):
    today=today or datetime.date.today();tokens=query.casefold().split()
    if period=='today':start=end=today
    elif period=='yesterday':start=end=today-datetime.timedelta(days=1)
    elif period in ('7','30'):start=today-datetime.timedelta(days=int(period)-1);end=today
    elif period=='all':start=end=None
    result=[]
    for project in projects:
        if folder and folder_key(project)!=folder:continue
        if start or end:
            date=modified_date(project)
            if date is None or (start and date<start) or (end and date>end):continue
        haystack=' '.join((str(project.get('title','')),str(project.get('source_video','')),customer_name(project),str(project.get('operator','')))).casefold()
        if any(token not in haystack for token in tokens):continue
        result.append(project)
    return sorted(result,key=lambda p:str(p.get('updated_at','')),reverse=True)


def recent_projects(projects,current_source=None,limit=12):
    if current_source:
        folder=folder_key({'source_video':current_source})
        return filter_projects(projects,folder=folder,period='all')[:limit]
    return filter_projects(projects)[:limit]


class HistoryModel(QAbstractTableModel):
    headers=('素材名称','所属项目 / 文件夹','客户模板','最近修改时间','制作人','状态')
    def __init__(self,parent=None):super().__init__(parent);self.projects=[]
    def rowCount(self,parent=QModelIndex()):return 0 if parent.isValid() else len(self.projects)
    def columnCount(self,parent=QModelIndex()):return len(self.headers)
    def headerData(self,section,orientation,role=Qt.DisplayRole):
        if role==Qt.DisplayRole and orientation==Qt.Horizontal:return self.headers[section]
        return super().headerData(section,orientation,role)
    def data(self,index,role=Qt.DisplayRole):
        if not index.isValid() or not 0<=index.row()<len(self.projects):return None
        project=self.projects[index.row()]
        if role==Qt.ToolTipRole:return str(project.get('source_video',''))+'\n'+history_label(project)
        if role!=Qt.DisplayRole:return None
        states={'completed':'已完成','edited':'字幕已修改','subtitle_ready':'字幕已生成','ready_to_render':'待合成','render_failed':'合成失败'}
        return (project.get('title') or ntpath.basename(str(project.get('source_video',''))),folder_label(project),customer_name(project),str(project.get('updated_at','')).replace('T',' '),project.get('operator',''),states.get(project.get('status'),project.get('status','')))[index.column()]
    def replace(self,projects):
        self.beginResetModel();self.projects=list(projects);self.endResetModel()


class HistoryDialog(QDialog):
    def __init__(self,projects,parent=None,multiple=False,current_source=None):
        super().__init__(parent);self.setWindowTitle('查找历史任务');self.resize(1180,700)
        self.projects=list(projects);self.multiple=multiple
        layout=QVBoxLayout(self)
        top=QHBoxLayout();top.addWidget(QLabel('修改日期',self))
        self.period=QComboBox(self)
        for label,value in [('最近7天','7'),('今天','today'),('昨天','yesterday'),('最近30天','30'),('全部日期','all'),('自定义日期','custom')]:self.period.addItem(label,value)
        top.addWidget(self.period)
        self.start=QDateEdit(QDate.currentDate().addDays(-6),self);self.end=QDateEdit(QDate.currentDate(),self)
        for control in (self.start,self.end):control.setCalendarPopup(True);control.setDisplayFormat('yyyy-MM-dd');top.addWidget(control)
        top.addWidget(QLabel('项目文件夹',self));self.folder=QComboBox(self);self.folder.setMinimumWidth(280);self.folder.addItem('全部项目','')
        folders={folder_key(p):p for p in projects if folder_key(p)}
        for key,project in sorted(folders.items(),key=lambda kv:folder_label(kv[1])):
            self.folder.addItem(folder_label(project),key);self.folder.setItemData(self.folder.count()-1,key,Qt.ToolTipRole)
        top.addWidget(self.folder,1);layout.addLayout(top)
        self.search=QLineEdit(self);self.search.setPlaceholderText('搜索剧名、文件夹、集数、客户模板或制作人；空格可组合条件');self.search.setClearButtonEnabled(True);layout.addWidget(self.search)
        self.model=HistoryModel(self);self.table=QTableView(self);self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows);self.table.setSelectionMode(QAbstractItemView.ExtendedSelection if multiple else QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers);self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide();self.table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeToContents)
        for column in (1,2):self.table.horizontalHeader().setSectionResizeMode(column,QHeaderView.Stretch)
        self.table.setColumnWidth(3,170);self.table.setColumnWidth(4,100);self.table.setColumnWidth(5,105)
        layout.addWidget(self.table,1)
        footer=QHBoxLayout();self.count=QLabel(self);footer.addWidget(self.count,1)
        clear=QPushButton('重置筛选',self);clear.clicked.connect(self.reset_filters);footer.addWidget(clear)
        if multiple:
            all_button=QPushButton('全选当前结果',self);all_button.clicked.connect(self.table.selectAll);footer.addWidget(all_button)
        layout.addLayout(footer)
        note=QLabel('日期按记录的最近修改时间筛选；鼠标停在记录上可查看完整素材路径。',self);note.setWordWrap(True);layout.addWidget(note)
        buttons=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel,self)
        self.accept_button=buttons.button(QDialogButtonBox.Ok);self.accept_button.setText('加入选中任务' if multiple else '打开选中字幕')
        buttons.accepted.connect(self.accept_selected);buttons.rejected.connect(self.reject);layout.addWidget(buttons)
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.setInterval(120);self.timer.timeout.connect(self.apply_filter)
        self.search.textChanged.connect(lambda:self.timer.start())
        for control in (self.period,self.folder):control.currentIndexChanged.connect(self.apply_filter)
        for control in (self.start,self.end):control.dateChanged.connect(self.apply_filter)
        self.table.selectionModel().selectionChanged.connect(self.update_selection)
        self.table.doubleClicked.connect(self.accept_selected)
        if current_source:
            index=self.folder.findData(folder_key({'source_video':current_source}))
            if index>=0:self.folder.setCurrentIndex(index)
        self.apply_filter()

    def apply_filter(self,*args):
        period=self.period.currentData();custom=period=='custom'
        self.start.setVisible(custom);self.end.setVisible(custom)
        start=self.start.date().toPyDate();end=self.end.date().toPyDate()
        if custom and start>end:
            self.model.replace([]);self.count.setText('开始日期不能晚于结束日期');self.accept_button.setEnabled(False);return
        self.model.replace(filter_projects(self.projects,self.search.text(),self.folder.currentData(),period,start=start,end=end))
        self.update_selection()

    def update_selection(self,*args):
        count=len(self.table.selectionModel().selectedRows())
        self.count.setText(f'找到 {len(self.model.projects)} 条 / 共 {len(self.projects)} 条，已选 {count} 条')
        self.accept_button.setEnabled(count>0)

    def reset_filters(self):
        self.search.clear();self.folder.setCurrentIndex(0);self.period.setCurrentIndex(0);self.apply_filter()

    def selected_projects(self):
        return [self.model.projects[index.row()] for index in sorted(self.table.selectionModel().selectedRows(),key=lambda i:i.row())]

    def accept_selected(self,*args):
        if self.selected_projects():self.accept()


def choose_history(projects,parent=None,multiple=False,current_source=None):
    dialog=HistoryDialog(projects,parent,multiple,current_source)
    return dialog.selected_projects() if dialog.exec_()==QDialog.Accepted else []
