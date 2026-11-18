"""Presentation only: original binding/store/processing methods remain authoritative."""
from pathlib import Path
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QStackedWidget,QScrollArea,
    QHeaderView,QAbstractItemView,QListWidgetItem,QTableWidgetItem)
from qfluentwidgets import (BodyLabel,StrongBodyLabel,CardWidget,PushButton,PrimaryPushButton,
    TextEdit,LineEdit,ListWidget,TableWidget,SegmentedWidget,FluentIcon as FIF)


def label(text,parent,muted=False,title=False):
    value=BodyLabel(text,parent)
    value.setStyleSheet('color:%s;font-size:%dpx;%s'%('#788795' if muted else '#253846',26 if title else 12 if muted else 13,'font-weight:600;' if title else ''))
    return value


def build_binding_ui(page):
    page.setWindowTitle('剧本绑定')
    page.setStyleSheet('''QWidget#ScriptReferenceInterface {background:#f4f6f8;}
        CardWidget {background:white;border:1px solid #e0e7ed;border-radius:10px;}
        QListWidget {background:#f8fafb;border:1px solid #e5ebf0;border-radius:7px;color:#344a59;}
        QListWidget::item {padding:6px;border:0;}
        QListWidget::item:selected {background:#e4f3ef;color:#147f73;border-radius:4px;}
        QTextEdit,QLineEdit {background:white;color:#304552;border:1px solid #dce5eb;border-radius:7px;}
        QTableWidget {background:white;alternate-background-color:#f8fafb;gridline-color:transparent;color:#304552;border:0;}
        QHeaderView::section {background:#f8fafb;color:#72828f;border:0;border-bottom:1px solid #e5ebf0;padding:10px;}
    ''')
    root=QVBoxLayout(page);root.setContentsMargins(24,22,24,22);root.setSpacing(16)
    heading=QHBoxLayout();text=QVBoxLayout();text.setSpacing(4)
    text.addWidget(label('剧本绑定',page,title=True))
    text.addWidget(label('让每集视频，使用对应的剧本台词。',page,muted=True))
    heading.addLayout(text,1)
    page.reference_count_label=label('',page)
    page.reference_count_label.setStyleSheet('color:#148478;background:#e6f3ef;border-radius:12px;padding:7px 12px;font-size:12px;')
    heading.addWidget(page.reference_count_label);root.addLayout(heading)
    page.binding_tabs=SegmentedWidget(page);page.binding_tabs.setFixedWidth(260)
    page.binding_stack=QStackedWidget(page)
    page.batch_binding_page=QWidget(page);page.single_binding_page=QWidget(page)
    page.binding_stack.addWidget(page.batch_binding_page);page.binding_stack.addWidget(page.single_binding_page)
    for key,text,widget in [('batch','批量绑定',page.batch_binding_page),('single','单集绑定',page.single_binding_page)]:
        page.binding_tabs.addItem(routeKey=key,text=text,onClick=lambda checked=False,w=widget:page.binding_stack.setCurrentWidget(w))
    page.binding_tabs.setCurrentItem('batch');page.binding_stack.setCurrentWidget(page.batch_binding_page)
    root.addWidget(page.binding_tabs);root.addWidget(page.binding_stack,1)
    build_batch(page);build_single(page)
    page.import_button.clicked.connect(page.import_script)
    page.use_selection_button.clicked.connect(page.use_selected_script)
    page.video_button.clicked.connect(page.choose_video)
    page.save_button.clicked.connect(page.save_reference)
    for name,fn in [('script_files_button',page.choose_script_files),('script_folder_button',page.choose_script_folder),
        ('video_files_button',page.choose_video_files),('video_folder_button',page.choose_video_folder),
        ('replace_scripts_button',page.replace_script_files),('replace_videos_button',page.replace_video_files),
        ('clear_batch_inputs_button',page.clear_batch_inputs),('batch_bind_button',page.batch_bind_folders),
        ('batch_start_button',page.batch_bind_and_start)]:getattr(page,name).clicked.connect(fn)
    page.single_start_button.clicked.connect(lambda:page.batch_bind_and_start(single=True))
    page._update_count();refresh_inputs(page)


def build_batch(page):
    outer=QVBoxLayout(page.batch_binding_page);outer.setContentsMargins(0,0,0,0);outer.setSpacing(12)
    scroll=QScrollArea(page);scroll.setWidgetResizable(True);scroll.setFrameShape(QScrollArea.NoFrame)
    scroll.setStyleSheet('QScrollArea,QScrollArea > QWidget > QWidget {background:transparent;}')
    scroll.setAcceptDrops(False);scroll.viewport().setAcceptDrops(False)
    content=QWidget();content.setMinimumHeight(560)
    layout=QVBoxLayout(content);layout.setContentsMargins(0,0,6,0);layout.setSpacing(14)
    row=QHBoxLayout();row.setSpacing(14)
    for kind,title,subtitle in [('script','1  添加剧本','支持 JSON、TXT、DOCX、SRT'),('video','2  添加原视频','支持多个视频、文件夹或混合拖入')]:
        card=CardWidget(page);card.setMinimumHeight(292)
        box=QVBoxLayout(card);box.setContentsMargins(16,15,16,15);box.setSpacing(9)
        title_row=QHBoxLayout();title_row.addWidget(StrongBodyLabel(title,card));title_row.addStretch()
        remove=PushButton('移除所选',card)
        remove.setFixedHeight(28);remove.clicked.connect(lambda checked=False,k=kind:remove_selected(page,k))
        title_row.addWidget(remove);box.addLayout(title_row);box.addWidget(label(subtitle,card,muted=True))
        choices=ListWidget(card);choices.setFixedHeight(138);choices.setSelectionMode(QAbstractItemView.ExtendedSelection)
        choices.setAcceptDrops(False);choices.viewport().setAcceptDrops(False)
        setattr(page,kind+'_input_list',choices);box.addWidget(choices)
        summary=label('尚未添加',card,muted=True);setattr(page,'script_folder_input' if kind=='script' else 'video_folder_input',summary);box.addWidget(summary)
        buttons=QHBoxLayout();buttons.setSpacing(7)
        add=PushButton('添加剧本' if kind=='script' else '添加视频',card,FIF.ADD)
        folder=PushButton('文件夹',card,FIF.FOLDER);replace=PushButton('重新选择',card)
        setattr(page,kind+'_files_button',add);setattr(page,kind+'_folder_button',folder)
        setattr(page,'replace_scripts_button' if kind=='script' else 'replace_videos_button',replace)
        for button in (add,folder,replace):buttons.addWidget(button)
        buttons.addStretch();box.addLayout(buttons);row.addWidget(card,1)
    layout.addLayout(row)
    card=CardWidget(page);card.setMinimumHeight(246)
    box=QVBoxLayout(card);box.setContentsMargins(16,14,16,12);box.setSpacing(10)
    head=QHBoxLayout();head.addWidget(StrongBodyLabel('3  查看绑定结果',card));head.addStretch()
    head.addWidget(label('分集按集号 · 单篇一对一',card,muted=True));box.addLayout(head)
    page.binding_results_stack=QStackedWidget(card)
    empty=QWidget(card);empty_box=QVBoxLayout(empty);empty_box.addStretch()
    prompt=label('添加剧本和视频后，点击「匹配并保存」',empty);prompt.setAlignment(Qt.AlignCenter);empty_box.addWidget(prompt)
    hint=label('分集：EP01.json → 第一集.mp4；单篇：一个剧本＋一个视频可直接对应',empty,muted=True);hint.setAlignment(Qt.AlignCenter);empty_box.addWidget(hint);empty_box.addStretch()
    page.binding_empty=empty;page.binding_results_stack.addWidget(empty)
    table=TableWidget(card);table.setColumnCount(4);table.setHorizontalHeaderLabels(['集数','原视频','剧本文件','结果'])
    table.setEditTriggers(QAbstractItemView.NoEditTriggers);table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setShowGrid(False);table.setWordWrap(False);table.verticalHeader().hide();table.verticalHeader().setDefaultSectionSize(42)
    table.horizontalHeader().setSectionResizeMode(0,QHeaderView.Fixed);table.setColumnWidth(0,90)
    table.horizontalHeader().setSectionResizeMode(1,QHeaderView.Stretch);table.horizontalHeader().setSectionResizeMode(2,QHeaderView.Stretch)
    table.horizontalHeader().setSectionResizeMode(3,QHeaderView.Fixed);table.setColumnWidth(3,148)
    page.binding_results_table=table;page.binding_results_stack.addWidget(table);box.addWidget(page.binding_results_stack,1)
    page.batch_result_label=label('绑定后，单集处理和批量任务都会自动读取。',card,muted=True)
    page.batch_result_label.setWordWrap(True);box.addWidget(page.batch_result_label);layout.addWidget(card,1)
    scroll.setWidget(content);outer.addWidget(scroll,1);page.batch_content_scroll=scroll
    footer=QHBoxLayout();page.clear_batch_inputs_button=PushButton('清空本次选择',page,FIF.DELETE);footer.addWidget(page.clear_batch_inputs_button)
    footer.addStretch();page.batch_bind_button=PushButton('匹配并保存',page,FIF.SAVE)
    page.batch_start_button=PrimaryPushButton('绑定并开始处理',page,FIF.PLAY)
    footer.addWidget(page.batch_bind_button);footer.addWidget(page.batch_start_button);outer.addLayout(footer)


def build_single(page):
    outer=QVBoxLayout(page.single_binding_page);outer.setContentsMargins(0,0,0,0)
    scroll=QScrollArea(page);scroll.setWidgetResizable(True);scroll.setFrameShape(QScrollArea.NoFrame)
    scroll.setStyleSheet('QScrollArea,QScrollArea > QWidget > QWidget {background:transparent;}')
    content=QWidget();layout=QVBoxLayout(content);layout.setContentsMargins(0,0,8,0);layout.setSpacing(14)
    card=CardWidget(content);box=QVBoxLayout(card);box.setContentsMargins(18,16,18,16);box.setSpacing(12)
    row=QHBoxLayout();row.addWidget(StrongBodyLabel('1  导入并确认台词',card));row.addStretch()
    page.import_button=PushButton('导入剧本',card,FIF.DOCUMENT);row.addWidget(page.import_button);box.addLayout(row)
    page.script_source_label=label('导入分集文件后，台词会自动填入下方。',card,muted=True)
    page.script_source_label.setWordWrap(True);box.addWidget(page.script_source_label)
    toggle=PushButton('展开原始剧本 / 选择部分台词',card);toggle.setCheckable(True);box.addWidget(toggle,0,Qt.AlignLeft)
    page.script_source_panel=QWidget(card);source_box=QVBoxLayout(page.script_source_panel);source_box.setContentsMargins(0,0,0,0)
    page.script_editor=TextEdit(card);page.script_editor.setMinimumHeight(150);page.script_editor.setPlaceholderText('导入剧本后，可选中部分台词作为本集内容。')
    page.use_selection_button=PushButton('使用选中台词（未选中则使用全部）',card,FIF.ACCEPT)
    source_box.addWidget(page.script_editor);source_box.addWidget(page.use_selection_button,0,Qt.AlignRight)
    page.script_source_panel.hide();toggle.toggled.connect(page.script_source_panel.setVisible);box.addWidget(page.script_source_panel)
    box.addWidget(label('本集实际绑定台词 · 可直接修改',card,muted=True))
    page.excerpt_editor=TextEdit(card);page.excerpt_editor.setMinimumHeight(190)
    page.excerpt_editor.setPlaceholderText('导入本集 JSON 后自动填充；也可在此粘贴需要绑定的台词。')
    box.addWidget(page.excerpt_editor);layout.addWidget(card,1)
    card=CardWidget(content);box=QVBoxLayout(card);box.setContentsMargins(18,16,18,16);box.setSpacing(12)
    box.addWidget(StrongBodyLabel('2  选择对应视频',card));row=QHBoxLayout()
    page.video_input=LineEdit(card);page.video_input.setPlaceholderText('选择对应集数的原视频')
    page.video_button=PushButton('选择视频',card,FIF.VIDEO);row.addWidget(page.video_input,1);row.addWidget(page.video_button);box.addLayout(row)
    box.addWidget(label('支持 01、第一集、第1集、EP01 等命名；集号冲突会提示。',card,muted=True));layout.addWidget(card)
    scroll.setWidget(content);outer.addWidget(scroll,1)
    row=QHBoxLayout();row.addWidget(label('保存后会自动用于该视频的后续任务。',page,muted=True));row.addStretch()
    page.save_button=PushButton('保存绑定',page,FIF.SAVE);page.single_start_button=PrimaryPushButton('绑定并开始处理',page,FIF.PLAY)
    row.addWidget(page.save_button);row.addWidget(page.single_start_button);outer.addLayout(row)
    for edit in (page.script_editor,page.excerpt_editor):edit.setAcceptDrops(False);edit.viewport().setAcceptDrops(False)
    page.video_input.setAcceptDrops(False)


def refresh_inputs(page):
    for kind,paths in [('script',page.script_batch_inputs),('video',page.video_batch_inputs)]:
        widget=getattr(page,kind+'_input_list');widget.clear()
        for path in paths:
            p=Path(path);item=QListWidgetItem(('文件夹  ' if p.is_dir() else '')+p.name)
            item.setData(Qt.UserRole,path);item.setToolTip(path);widget.addItem(item)
    page.batch_bind_button.setEnabled(bool(page.script_batch_inputs and page.video_batch_inputs))
    page.batch_start_button.setEnabled(bool(page.script_batch_inputs and page.video_batch_inputs))
    page.binding_results_table.setRowCount(0);page.binding_results_stack.setCurrentWidget(page.binding_empty)
    page.batch_result_label.setText('选择已更新，点击「匹配并保存」查看结果。已有绑定不会因清空选择而删除。' if page.script_batch_inputs or page.video_batch_inputs else '绑定后，单集处理和批量任务都会自动读取。')


def remove_selected(page,kind):
    selected={item.data(Qt.UserRole) for item in getattr(page,kind+'_input_list').selectedItems()}
    target=page.script_batch_inputs if kind=='script' else page.video_batch_inputs
    target[:]=[path for path in target if path not in selected]
    page._update_batch_input_summary()


def show_binding_result(page,result):
    rows=[]
    ambiguous=set(result['ambiguous_script_episodes'])|set(result['ambiguous_video_episodes'])
    missing_video=[ep for ep in result['unmatched_script_episodes'] if ep not in ambiguous]
    missing_script=[ep for ep in result['unmatched_video_episodes'] if ep not in ambiguous]
    for item in result['matched']:rows.append((item['episode'],Path(item['video_path']).name,Path(item['script_path']).name,'已绑定','#168779',item['video_path']+'\n'+item['script_path']))
    for item in result['failed']:rows.append((item['episode'],Path(item['video_path']).name,Path(item['script_path']).name,'读取失败','#c24f42',item['error']))
    for ep in missing_video:rows.append((ep,'—','第 %s 集剧本'%ep,'缺少视频','#ae7b27','没有找到该集的视频'))
    for ep in missing_script:rows.append((ep,'第 %s 集视频'%ep,'—','缺少剧本','#ae7b27','没有找到该集的剧本'))
    for ep in sorted(ambiguous):rows.append((ep,'候选重复' if ep in result['ambiguous_video_episodes'] else '—','候选重复' if ep in result['ambiguous_script_episodes'] else '—','重复集号 · 跳过','#ae7b27','请在上方移除错误候选，或重新选择文件后匹配。'))
    unknown_scripts=result.get('unrecognized_script_files',[])
    unknown_videos=result.get('unrecognized_video_files',[])
    for path in unknown_scripts:rows.append((None,'—',Path(path).name,'未识别集号','#ae7b27',path+'\n单篇请只选一个剧本和一个视频；多集请使用分集JSON或明确集号。'))
    for path in unknown_videos:rows.append((None,Path(path).name,'—','未识别集号','#ae7b27',path+'\n单篇请只选一个剧本和一个视频；多集视频可使用第1集、EP01等命名。'))
    table=page.binding_results_table;table.setRowCount(len(rows))
    for row,values in enumerate(sorted(rows,key=lambda item:(item[0] is None,item[0] or 0))):
        for col,value in enumerate([('单篇' if values[3]=='已绑定' else '未识别') if values[0] is None else '第 %s 集'%values[0],*values[1:4]]):
            cell=QTableWidgetItem(value);cell.setToolTip(values[5]);table.setItem(row,col,cell)
            if col==3:cell.setForeground(QColor(values[4]))
    page.binding_results_stack.setCurrentWidget(table if rows else page.binding_empty)
    page.batch_result_label.setText('已绑定 %s 集 · 缺少视频 %s 集 · 缺少剧本 %s 集 · 重复 %s 集 · 读取失败 %s 集'%(len(result['matched']),len(missing_video),len(missing_script),len(ambiguous),len(result['failed'])) + (' · 未识别集号 %s 个文件'%(len(unknown_scripts)+len(unknown_videos)) if unknown_scripts or unknown_videos else ''))


def refresh_script_source(page):
    path=Path(page.script_source_path) if page.script_source_path else None
    page.script_source_label.setText(('已导入：'+path.name) if path else '导入分集文件后，台词会自动填入下方。')
    page.script_source_label.setToolTip(str(path) if path else '')
