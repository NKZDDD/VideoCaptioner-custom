"""Customer-only appearance drafts. No cfg writes or shared preset writes."""
import re
import uuid
from pathlib import Path
from PyQt5.QtCore import Qt, QSignalBlocker, QUrl
from PyQt5.QtGui import QColor, QFontDatabase
from PyQt5.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,
    QScrollArea,QSplitter,QSlider,QDoubleSpinBox,QComboBox,QLineEdit,QPushButton,
    QColorDialog,QDialog,QPlainTextEdit,QDialogButtonBox,QTableWidget,QTableWidgetItem,
    QFileDialog,QTabWidget,QMessageBox)
from qfluentwidgets import PushButton
from app.view.studio_controls import StyledCombo
from app.config import SUBTITLE_STYLE_PATH,RESOURCE_PATH,CACHE_PATH
from app.view.subtitle_style_interface import CAPCUT_FONT_TO_ASS,CAPCUT_OUTLINE_TO_ASS,calibrated_margin


class StyleDraft:
    """Patch only touched fields; opening/saving an untouched draft is byte-identical."""
    def __init__(self,text): self.text=text
    def fields(self,name='Default'):
        for line in self.text.splitlines():
            if re.match(r'^Style:\s*'+re.escape(name)+r',',line):return line.split(',')
        raise ValueError('此外观缺少 '+name+' 样式；请保留原模板并选择有效外观。')
    def get(self,index,name='Default'):return self.fields(name)[index]
    def set(self,index,value,name='Default'):
        lines=self.text.splitlines(keepends=True)
        for i,line in enumerate(lines):
            if re.match(r'^Style:\s*'+re.escape(name)+r',',line):
                ending='\r\n' if line.endswith('\r\n') else '\n' if line.endswith('\n') else ''
                parts=line.rstrip('\r\n').split(',');parts[index]=str(value)
                lines[i]=','.join(parts)+ending;self.text=''.join(lines);return
        raise ValueError('找不到样式 '+name)
    def effect(self,key,name='Default'):
        for line in self.text.splitlines():
            if re.match(r'^;\s*VideoCaptionerEffect:\s*'+name+r',',line):
                match=re.search(r'(?:,|\s)'+key+r'=([-+\d.]+)',line)
                if match:return float(match[1])
        return {'blur':0,'distance':float(self.get(17,name)),'angle':-45}[key]
    def set_effect(self,key,value,name='Default'):
        lines=self.text.splitlines(keepends=True)
        for i,line in enumerate(lines):
            if re.match(r'^;\s*VideoCaptionerEffect:\s*'+name+r',',line):
                lines[i]=re.sub(r'\b'+key+r'=[-+\d.]+',key+'='+str(value),line)
                if lines[i]==line and not re.search(r'\b'+key+'=',line):
                    lines[i]=line.rstrip('\r\n')+','+key+'='+str(value)+'\n'
                self.text=''.join(lines);return
        values={k:self.effect(k,name) for k in ('blur','distance','angle')};values[key]=value
        self.text=self.text.rstrip('\r\n')+'\n; VideoCaptionerEffect: '+name+','+','.join(k+'='+str(v) for k,v in values.items())+'\n'


def number(value):return format(float(value),'.6f').rstrip('0').rstrip('.') or '0'


class NumericRow(QWidget):
    def __init__(self,value,low,high,decimals,callback,parent=None):
        super().__init__(parent);layout=QHBoxLayout(self);layout.setContentsMargins(0,0,0,0)
        self.scale=10**decimals;self.box=QDoubleSpinBox(self)
        self.box.setDecimals(decimals);self.box.setRange(min(low,value),max(high,value));self.box.setValue(value)
        self.box.setFixedWidth(84);self.box.setKeyboardTracking(False);self.box.setButtonSymbols(QDoubleSpinBox.NoButtons)
        self.box.setAlignment(Qt.AlignCenter)
        self.slider=QSlider(Qt.Horizontal,self)
        self.slider.setRange(round(self.box.minimum()*self.scale),round(self.box.maximum()*self.scale))
        self.slider.setValue(round(value*self.scale));layout.addWidget(self.slider,1);layout.addWidget(self.box)
        self.slider.setTracking(False) # Commit when released; no expensive render per pixel.
        def slid(v):self.box.setValue(v/self.scale)
        def changed(v):
            blocker=QSignalBlocker(self.slider);self.slider.setValue(round(v*self.scale));del blocker
            callback(v)
        self.slider.valueChanged.connect(slid);self.box.valueChanged.connect(changed)


class RequirementsDialog(QDialog):
    def __init__(self,appearance):
        super().__init__(appearance);self.appearance=appearance;self.suggestions=[]
        self.setWindowTitle('填写客户需求');self.resize(650,570);layout=QVBoxLayout(self)
        self.setStyleSheet('''QDialog {background:#f4f6f8;} QLabel {color:#304552;font-size:13px;}
QPlainTextEdit,QTableWidget {background:white;color:#304552;border:1px solid #dce5eb;border-radius:7px;padding:8px;}
QHeaderView::section {background:#edf4f2;color:#345b52;border:0;padding:9px;}
QPushButton {background:white;color:#304552;border:1px solid #dce5eb;border-radius:7px;padding:8px 14px;}
QPushButton:disabled {color:#9aa7ad;}''')
        note=QLabel('支持识别明确的字号、距底部、剪映竖屏Y、颜色及不透明度。其他需求请手动核对。\n确认后仅应用到编辑草稿，正式生效仍需保存模板并确认。')
        note.setWordWrap(True);layout.addWidget(note)
        self.input=QPlainTextEdit(self);self.input.setPlaceholderText('例如：剪映字号 14，Y=-645，白字，黑色描边\n或：字号 14，距底部 217，阴影不透明度 80%\n模糊描述或未校准软件的数值会提示人工确认。')
        self.input.setPlainText(appearance.requirement_text);layout.addWidget(self.input)
        parse=PushButton('识别数值并查看差异',self);layout.addWidget(parse);parse.clicked.connect(self.parse)
        self.table=QTableWidget(0,3,self);self.table.setHorizontalHeaderLabels(['参数','当前值','建议值'])
        self.table.horizontalHeader().setStretchLastSection(True);self.table.setEditTriggers(QTableWidget.NoEditTriggers);layout.addWidget(self.table)
        self.notice=QLabel(self);self.notice.setWordWrap(True);layout.addWidget(self.notice)
        buttons=QDialogButtonBox(QDialogButtonBox.Apply|QDialogButtonBox.Cancel,self)
        self.apply=buttons.button(QDialogButtonBox.Apply);self.apply.setText('应用到编辑草稿');self.apply.setEnabled(False)
        self.apply.clicked.connect(self.apply_draft);buttons.rejected.connect(self.reject);layout.addWidget(buttons)
        self.input.textChanged.connect(lambda:self.apply.setEnabled(False))
    def parse(self):
        self.suggestions,warnings=self.appearance.parse_requirements(self.input.toPlainText())
        self.table.setRowCount(len(self.suggestions))
        for row,(label,key,value) in enumerate(self.suggestions):
            old=self.appearance.controls[key]
            previous=old.box.value() if isinstance(old,NumericRow) else old.currentText() if isinstance(old,QComboBox) else old.text()
            for col,text in enumerate((label,str(previous),str(value))):self.table.setItem(row,col,QTableWidgetItem(text))
        self.notice.setText('\n'.join(warnings) if warnings else '仅匹配明确支持的参数；请核对上方差异，未列出的要求请手动调整。')
        self.apply.setEnabled(bool(self.suggestions))
    def apply_draft(self):
        for _,key,value in self.suggestions:
            control=self.appearance.controls[key]
            if isinstance(control,NumericRow):control.box.setValue(float(value))
            elif isinstance(control,QComboBox):control.setCurrentText(str(value))
            else:control.setText(str(value));control.editingFinished.emit()
        self.appearance.requirement_text=self.input.toPlainText()
        self.appearance.requirement_drafts[self.appearance.requirement_key]=self.input.toPlainText();self.accept()


class CustomerAppearance(QWidget):
    def __init__(self,page):
        super().__init__(page);self.page=page;self.loading=False;self.controls={};self.draft=None
        self.video='';self.subtitle='';self.player=None;self.renderer=None;self.requirement_text='';self.preview_files=[]
        self.requirement_drafts={};self.requirement_key=''
        self.setStyleSheet('''QWidget {font-family:"Microsoft YaHei UI";font-size:12px;color:#304552;}
QTabWidget::pane {background:white;border:1px solid #dce5eb;border-radius:9px;}
QTabBar::tab {background:#eef3f5;color:#637783;padding:10px 22px;border:0;}
QTabBar::tab:selected {background:#e3f2ed;color:#147e72;}
QDoubleSpinBox,QLineEdit,QComboBox {background:white;color:#304552;border:1px solid #dce5eb;border-radius:6px;min-height:30px;padding:0 5px;}
QComboBox QAbstractItemView {background:white;color:#304552;selection-background-color:#e4f3ef;}
QDoubleSpinBox:focus,QLineEdit:focus,QComboBox:focus {border-color:#168779;}
QSlider::groove:horizontal {height:4px;background:#e3eae9;border-radius:2px;}
QSlider::sub-page:horizontal {background:#168779;border-radius:2px;}
QSlider::handle:horizontal {background:#168779;border:2px solid white;width:13px;height:13px;margin:-6px 0;border-radius:8px;}
QScrollArea,QScrollArea > QWidget > QWidget {background:white;border:0;}
QPushButton {background:white;color:#304552;border:1px solid #dce5eb;border-radius:7px;padding:7px 9px;}
QPushButton:hover {border-color:#168779;background:#f1f8f5;}
QPushButton:disabled {color:#93a4ad;background:#f0f4f5;}
QSplitter::handle {background:#edf2f4;width:8px;}
''')
        self.font_ids=[]
        for path in (RESOURCE_PATH/'fonts').glob('*'):
            if path.suffix.lower() in ('.ttf','.otf','.ttc'):self.font_ids.append(QFontDatabase.addApplicationFont(str(path)))
        outer=QVBoxLayout(self);outer.setContentsMargins(0,0,0,0);outer.setSpacing(10)
        bar=QHBoxLayout();title=QLabel('字幕外观');title.setStyleSheet('font-size:17px;font-weight:600;');bar.addWidget(title);bar.addStretch()
        requirements=PushButton('填写客户需求',self);requirements.clicked.connect(self.open_requirements);bar.addWidget(requirements);outer.addLayout(bar)
        self.split=QSplitter(Qt.Horizontal,self);outer.addWidget(self.split,1)
        self.tabs=QTabWidget(self);self.tabs.setMinimumWidth(260);self.split.addWidget(self.tabs)
        self.preview=QWidget(self);right=QVBoxLayout(self.preview);right.setContentsMargins(12,0,0,0)
        actions=QHBoxLayout()
        for label,callback in [('选视频',self.choose_video),('选字幕',self.choose_subtitle)]:
            button=PushButton(label,self);button.clicked.connect(callback);actions.addWidget(button)
        right.addLayout(actions)
        self.preview_area=QWidget(self);self.preview_layout=QVBoxLayout(self.preview_area);self.preview_layout.setContentsMargins(0,0,0,0)
        self.empty=QLabel('选择实际视频和字幕\n按原画面比例查看字幕外观',self);self.empty.setAlignment(Qt.AlignCenter);self.empty.setWordWrap(True)
        self.empty.setStyleSheet('background:#18252c;color:#c7d6dc;border-radius:10px;padding:16px;');self.empty.setMinimumHeight(200)
        self.preview_layout.addWidget(self.empty,1);right.addWidget(self.preview_area,1)
        self.preview_button=PushButton('更新成片预览',self);self.preview_button.clicked.connect(self.render);self.preview_button.setEnabled(False);right.addWidget(self.preview_button)
        self.preview_status=QLabel('预览仅生成本机临时文件；不调用云端，不覆盖原视频。',self);self.preview_status.setWordWrap(True);right.addWidget(self.preview_status)
        self.split.addWidget(self.preview);self.split.setSizes([320,400]);self.split.setChildrenCollapsible(False)
        self.note=QLabel('编辑草稿 · 点击顶部保存并确认后，所有电脑的新任务才使用新设置。',self);self.note.setWordWrap(True);outer.addWidget(self.note)
        page.layout_combo.activated.connect(self.invalidate)
        page.style.activated.connect(self.choose_style)
        from PyQt5.QtWidgets import QApplication
        QApplication.instance().aboutToQuit.connect(self.cleanup)
    def open_requirements(self):RequirementsDialog(self).exec_()
    def choose_style(self,*args):
        path=SUBTITLE_STYLE_PATH/(self.page.style.currentText()+'.txt')
        try:
            text=path.read_text(encoding='utf-8');StyleDraft(text).fields()
            self.page.current['style_template']=text
            self.page.current.setdefault('settings',{})['subtitle_style_name']=self.page.style.currentText()
            self.load_draft();self.note.setText('已选择外观作为草稿；保存并确认后生效。')
        except (OSError,ValueError) as exc:QMessageBox.warning(self,'无法载入外观',str(exc))
    def load_draft(self):
        self.loading=True
        self.requirement_key=self.page.current.get('id') or self.page.current.get('name') or '__new'
        self.requirement_text=self.requirement_drafts.get(self.requirement_key,'')
        try:
            text=self.page.current.get('style_template','')
            if not text:text=(SUBTITLE_STYLE_PATH/(self.page.style.currentText()+'.txt')).read_text(encoding='utf-8')
            self.draft=StyleDraft(text);self.draft.fields();self.controls={}
            while self.tabs.count():
                old=self.tabs.widget(0);self.tabs.removeTab(0);old.deleteLater()
            for name,label in [('Default','主字幕'),('Secondary','副字幕')]:
                try:self.draft.fields(name)
                except ValueError:continue
                self.add_style_tab(name,label)
            self.note.setText('已载入客户保存的外观；数值沿用原有校准，修改后需保存并确认。')
        except (OSError,ValueError,IndexError) as exc:
            self.draft=None;self.note.setText('外观暂不可编辑：'+str(exc))
        finally:self.loading=False
        self.invalidate()
    def add_style_tab(self,name,label):
        scroll=QScrollArea(self);scroll.setWidgetResizable(True);content=QWidget();form=QFormLayout(content)
        form.setContentsMargins(12,12,12,12);form.setVerticalSpacing(14)
        font=StyledCombo(content);font.setMinimumWidth(0);font.setMaximumWidth(230)
        font.addItems(sorted(set(QFontDatabase().families()+[self.draft.get(1,name)])))
        font.setCurrentText(self.draft.get(1,name));form.addRow('字体',font);self.controls[name+'.font']=font
        font.currentTextChanged.connect(lambda text:self.change_field(name,1,text) if text and ',' not in text else None)
        def numeric(key,title,index,low,high,decimals=1,factor=1):
            value=float(self.draft.get(index,name))/factor
            row=NumericRow(value,low,high,decimals,lambda v:self.change_field(name,index,number(v*factor)),content)
            form.addRow(title,row);self.controls[name+'.'+key]=row
        numeric('size','字号（剪映校准）',2,1,300,2,CAPCUT_FONT_TO_ASS)
        numeric('bottom','距底部（越大越高）',21,0,10000,0)
        hint=QLabel('沿用原参数：距底部 217 对应竖屏校准 Y=-645。');hint.setWordWrap(True);form.addRow(hint)
        for key,title,index in [('color','文字颜色',3),('outline_color','描边颜色',5),('shadow_color','阴影颜色',6)]:
            hexvalue=self.draft.get(index,name).replace('&H','').zfill(8)
            value='#'+hexvalue[-2:]+hexvalue[-4:-2]+hexvalue[-6:-4]
            box=QLineEdit(value,content);box.setMaximumWidth(120);choose=QPushButton('选色',content)
            wrapper=QWidget(content);row=QHBoxLayout(wrapper);row.setContentsMargins(0,0,0,0);row.addWidget(box);row.addWidget(choose);row.addStretch()
            self.controls[name+'.'+key]=box;form.addRow(title,wrapper)
            box.editingFinished.connect(lambda n=name,i=index,b=box:self.change_color(n,i,b))
            def pick(checked=False,b=box):
                selected=QColorDialog.getColor(QColor(b.text()),self,'选择颜色')
                if selected.isValid():b.setText(selected.name());b.editingFinished.emit()
            choose.clicked.connect(pick)
        numeric('outline','描边（原软件值）',16,0,100,2,CAPCUT_OUTLINE_TO_ASS)
        numeric('spacing','字间距',13,0,50,2)
        numeric('bold','加粗（0关 / -1开）',7,-1,0,0)
        for key,title in [('shadow_opacity','阴影不透明度 %'),('outline_opacity','描边不透明度 %')]:
            index=6 if key.startswith('shadow') else 5
            opacity=(255-int(self.draft.get(index,name).replace('&H','').zfill(8)[:2],16))*100/255
            row=NumericRow(opacity,0,100,1,lambda v,n=name,i=index:self.change_opacity(n,i,v),content)
            self.controls[name+'.'+key]=row;form.addRow(title,row)
        for key,title,low,high in [('blur','阴影柔和度（原软件值）',0,100),('distance','阴影距离（原软件值）',0,100),('angle','阴影角度',-180,180)]:
            row=NumericRow(self.draft.effect(key,name),low,high,2,lambda v,n=name,k=key:self.change_effect(n,k,v),content)
            self.controls[name+'.'+key]=row;form.addRow(title,row)
        scroll.setWidget(content);self.tabs.addTab(scroll,label)
    def change_field(self,name,index,value):
        if self.loading or not self.draft:return
        if self.draft.get(index,name)==str(value):return
        self.draft.set(index,value,name)
        # The renderer consumes the shadow extension, when present.
        if index==16:
            lines=self.draft.text.splitlines(keepends=True)
            for i,line in enumerate(lines):
                if re.match(r'^;\s*VideoCaptionerOutline:\s*'+name+',',line):
                    lines[i]=re.sub(r'width=[-+\d.]+','width='+number(float(value)/CAPCUT_OUTLINE_TO_ASS),line)
            self.draft.text=''.join(lines)
        self.commit_draft()
    def change_color(self,name,index,box):
        color=QColor(box.text())
        if not color.isValid():box.setToolTip('请输入 #RRGGBB 颜色');return
        alpha=self.draft.get(index,name).replace('&H','').zfill(8)[:2]
        self.change_field(name,index,f'&H{alpha}{color.blue():02X}{color.green():02X}{color.red():02X}')
    def change_opacity(self,name,index,value):
        old=self.draft.get(index,name).replace('&H','').zfill(8)
        self.change_field(name,index,f'&H{round(255*(100-value)/100):02X}'+old[2:])
    def change_effect(self,name,key,value):
        if self.loading:return
        self.draft.set_effect(key,number(value),name);self.commit_draft()
    def commit_draft(self):
        self.page.current['style_template']=self.draft.text
        self.page.current.setdefault('settings',{})['subtitle_style_name']=self.page.style.currentText()
        self.note.setText('未保存 · 当前仅为编辑草稿，点击顶部保存并确认后生效。')
        self.invalidate()
    def invalidate(self,*args):
        if self.renderer:self.renderer.cancel()
        if self.player:
            self.player.backend.set_pause(1)
            if self.video:
                self.player.setVideo(QUrl.fromLocalFile(self.video));self.player.seek_paused(0)
            self.player.set_preparing(False)
            self.preview_status.setText('原视频（当前修改尚未生成字幕预览），点击更新后后台生成。')
        self.preview_button.setEnabled(bool(self.video and self.subtitle and self.draft))
    def choose_video(self):
        path,_=QFileDialog.getOpenFileName(self,'选择预览视频','','视频 (*.mp4 *.mov *.mkv *.avi)')
        if path:self.video=path;self.invalidate();self.preview_status.setText('视频：'+Path(path).name+'；请选字幕后更新预览。')
    def choose_subtitle(self):
        path,_=QFileDialog.getOpenFileName(self,'选择已有字幕','','字幕 (*.srt *.ass *.vtt)')
        if path:self.subtitle=path;self.invalidate();self.preview_status.setText('字幕：'+Path(path).name+'；点击更新预览。')
    def render(self):
        if not (self.video and self.subtitle and self.draft):return
        try:
            from app.core.bk_asr.asr_data import ASRData
            from app.view.studio_editor import StudioVideoWidget
            from app.view.studio_preview_renderer import ExactPreviewRenderer
            if self.player is None:
                self.player=StudioVideoWidget(self);self.player.expand_button.hide();self.empty.hide();self.preview_layout.addWidget(self.player,1)
                self.renderer=ExactPreviewRenderer(self);self.renderer.ready.connect(self.rendered);self.renderer.failed.connect(self.render_failed);self.renderer.progress.connect(self.preview_status.setText)
            ass=CACHE_PATH/('customer-appearance-'+uuid.uuid4().hex+'.ass');self.preview_files.append(ass)
            ASRData.from_subtitle_file(self.subtitle).to_ass(self.draft.text,self.page.layout_combo.currentText(),str(ass))
            self.player.setVideo(QUrl.fromLocalFile(self.video));self.player.seek_paused(0)
            self.player.set_preparing(False);self.preview_status.setText('原视频可操作 · 字幕预览后台生成中…')
            self.renderer.start(self.video,str(ass))
        except Exception as exc:self.render_failed(str(exc))
    def rendered(self,path):
        self.player.use_rendered_video(path,max(0,self.player.backend.get_time()),bool(self.player.backend.is_playing()));self.preview_status.setText('成片流程预览 · 完整画面等比例显示。修改后请更新预览。')
    def render_failed(self,message):
        self.preview_status.setText('预览失败：'+message)
        if self.player:self.player.set_preparing(False)
    def hideEvent(self,event):
        if self.player:self.player.backend.set_pause(1)
        super().hideEvent(event)
    def cleanup(self):
        if self.player:self.player.stop();self.player.timer.stop()
        if self.renderer:self.renderer.close()
        for path in self.preview_files:path.unlink(missing_ok=True)
    def parse_requirements(self,text):
        suggestions=[];warnings=[]
        if re.search(r'达芬奇|DaVinci|Premiere|\bPR\b',text,re.I):
            return [],['该软件的数值尚未校准，不能自动换算；请使用当前软件参数手动调试。']
        specs=[('字号','size',r'(?:字号|字体大小)\s*[:：=]?\s*(\d+(?:\.\d+)?)'),
            ('距底部','bottom',r'(?:距底部|底部距离)\s*[:：=]?\s*(\d+(?:\.\d+)?)'),
            ('阴影不透明度 %','shadow_opacity',r'阴影(?:不透明度|透明度)\s*[:：=]?\s*(\d+(?:\.\d+)?)\s*%'),
            ('描边不透明度 %','outline_opacity',r'描边不透明度\s*[:：=]?\s*(\d+(?:\.\d+)?)\s*%')]
        for label,key,pattern in specs:
            values=re.findall(pattern,text)
            if len(set(values))>1:warnings.append(label+'有多个数值，请手动确认。');continue
            if values:
                if key=='shadow_opacity' and '阴影透明度' in text:warnings.append('透明度与不透明度含义相反，请明确写“阴影不透明度”。');continue
                value=float(values[0]);control=self.controls['Default.'+key].box
                if control.minimum()<=value<=control.maximum():suggestions.append((label,'Default.'+key,value))
                else:warnings.append(label+'超出原控件范围，未应用。')
        y=re.findall(r'\bY\s*[:：=]?\s*(-?\d+(?:\.\d+)?)',text,re.I)
        if y:
            if '剪映' in text and not re.search(r'横屏|1920\s*[×x*]\s*1080|方形|4:3|1:1',text) and len(set(y))==1 and not any(s[1]=='Default.bottom' for s in suggestions):
                value=calibrated_margin(float(y[0]));control=self.controls['Default.bottom'].box
                if control.minimum()<=value<=control.maximum():suggestions.append(('距底部（沿用竖屏Y校准）','Default.bottom',value))
                else:warnings.append('Y超出原位置范围，未应用。')
            else:warnings.append('Y仅支持明确的剪映1080×1920竖屏校准；多个位置值请人工确认。')
        for word,key,color in [('白字','color','#ffffff'),('黑字','color','#000000'),('黑色描边','outline_color','#000000'),('白色描边','outline_color','#ffffff')]:
            if word in text:suggestions.append((word,'Default.'+key,color))
        for label,key in [('文字颜色','color'),('描边颜色','outline_color'),('阴影颜色','shadow_color')]:
            values=re.findall(label+r'\s*[:：=]?\s*(#[0-9a-fA-F]{6})\b',text)
            if len(set(values))==1:suggestions.append((label,'Default.'+key,values[0]))
            elif values:warnings.append(label+'有多个值，请手动确认。')
        if re.search(r'大一点|小一点|靠下|靠上|差不多|左右位置|横向|描边\s*\d|模糊\s*\d|距离\s*\d',text):warnings.append('部分描述未提供明确单位或缺少对应校准，未自动调整，请在外观页手动核对。')
        # Never silently choose one of conflicting values for the same parameter.
        conflicts={key for _,key,_ in suggestions if len({str(v) for _,k,v in suggestions if k==key})>1}
        if conflicts:warnings.append('存在互相冲突的要求，对应参数未应用。')
        return [s for s in suggestions if s[1] not in conflicts],warnings


def install_appearance(page):
    page.appearance=CustomerAppearance(page)
    page.template_sections.addWidget(page.appearance)
    page.template_tabs.addItem(routeKey='appearance',text='字幕外观',onClick=lambda:page.template_sections.setCurrentWidget(page.appearance))
    # Keep preview usable at normal laptop sizes. Template metadata stays in
    # the original tabs; selecting another tab restores the same live controls.
    from PyQt5.QtWidgets import QGroupBox
    basic=next((box for box in page.findChildren(QGroupBox) if box.title()=='模板信息'),None)
    if basic:
        page.template_sections.currentChanged.connect(lambda i:basic.setVisible(page.template_sections.widget(i) is not page.appearance))
    # Keep every existing output/text control and handler accessible.
    for label in page.findChildren(QLabel):
        if label.text().startswith('字号、颜色、描边、阴影和位置均保存在外观快照中。'):
            label.setText('在“字幕外观”页调整并预览；修改只进入草稿，保存并确认后更新此客户。旧字幕样式预设保持不变。')
