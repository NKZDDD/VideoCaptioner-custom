"""Editable subtitle intervals; drag previews are transactional and undoable."""
import copy
from PyQt5.QtCore import Qt,QRectF
from PyQt5.QtGui import QPainter,QColor,QPen
from PyQt5.QtWidgets import QWidget
from qfluentwidgets import isDarkTheme

MIN_DURATION=10


def interval_bounds(model,row,duration=None):
    items=list(model._data.values())
    lower=max(0,int(items[row-1]['end_time'])) if row else 0
    upper=int(items[row+1]['start_time']) if row+1<len(items) else 24*60*60*1000-1
    if duration and duration>0:upper=min(upper,int(duration))
    return lower,upper


def apply_interval(model,row,start,end,duration=None):
    items=list(model._data.values())
    if not 0<=row<len(items):return False,'请先选择字幕'
    start,end=int(start),int(end)
    lower,upper=interval_bounds(model,row,duration)
    if start<0 or end-start<MIN_DURATION:return False,'结束时间必须晚于开始时间，字幕至少保留10毫秒'
    if start<lower or end>upper:return False,'时间超出视频或相邻字幕边界；不会自动移动其他字幕'
    item=items[row]
    if (int(item['start_time']),int(item['end_time']))==(start,end):return True,''
    item['start_time'],item['end_time']=start,end
    if hasattr(model,'split_history'):model.split_history.clear()
    model.dataChanged.emit(model.index(row,0),model.index(row,1),[Qt.DisplayRole,Qt.EditRole])
    if hasattr(model,'timelineChanged'):model.timelineChanged.emit()
    return True,''


class CaptionTimeline(QWidget):
    def __init__(self,editor,controller):
        super().__init__(editor)
        self.editor,self.controller=editor,controller
        self.position=0;self.zoom=15000;self.drag=None;self.hover=None
        self.setMinimumHeight(138);self.setMouseTracking(True);self.setFocusPolicy(Qt.StrongFocus)
        self.setToolTip('拖左手柄调整入场，拖右手柄调整结束，拖中间整体移动；滚轮缩放；Esc取消本次拖动')

    def window_start(self):
        return self.drag['window'] if self.drag else max(0,self.position-self.zoom//3)

    def blocks(self):
        start=self.window_start();width=max(1,self.width()-30)
        for row,item in enumerate(self.editor.model._data.values()):
            value=item
            if self.drag and row==self.drag['row']:
                value=dict(item,start_time=self.drag['candidate'][0],end_time=self.drag['candidate'][1])
            left=15+(value['start_time']-start)/self.zoom*width
            right=15+(value['end_time']-start)/self.zoom*width
            yield row,QRectF(left,57,max(4,right-left),42),value

    def hit_test(self,pos):
        selected=self.editor.subtitle_table.currentIndex().row();candidates=[]
        for row,rect,item in self.blocks():
            if not rect.adjusted(-9,0,9,0).contains(pos):continue
            for mode,edge in (('start',rect.left()),('end',rect.right())):
                distance=abs(pos.x()-edge)
                if distance<=9:candidates.append((distance,0 if row==selected else 1,row,mode,item))
        if candidates:
            _,_,row,mode,item=min(candidates,key=lambda c:(c[0],c[1]));return row,mode,item
        for row,rect,item in self.blocks():
            if rect.contains(pos):return row,'move',item
        return None

    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        dark=isDarkTheme();p.fillRect(self.rect(),QColor('#23292f' if dark else '#f5f7f9'))
        p.setPen(QColor('#aab6c0' if dark else '#667585'))
        if self.drag:
            a,b=self.drag['candidate'];description={'start':'调整入场','end':'调整结束','move':'整体移动'}[self.drag['mode']]
            title=f'{description}  {self.editor.model.format_time(a)} → {self.editor.model.format_time(b)}  时长 {(b-a)/1000:.3f}秒'
        else:title='左手柄：入场  |  右手柄：结束  |  中间：整体移动'
        p.drawText(15,18,title)
        start=self.window_start()
        for i in range(6):
            x=15+i*(self.width()-30)/5;label=self.editor.model.format_time(start+i*self.zoom/5)
            p.drawText(min(int(x),max(0,self.width()-p.fontMetrics().horizontalAdvance(label)-8)),40,label)
        selected=self.editor.subtitle_table.currentIndex().row()
        for row,rect,item in self.blocks():
            if rect.right()<0 or rect.left()>self.width():continue
            active=row==selected
            p.setPen(QPen(QColor('#168779'),2) if active else Qt.NoPen)
            p.setBrush(QColor('#2a6f66' if dark else '#d5eee8'));p.drawRoundedRect(rect,5,5)
            p.save();p.setClipRect(rect)
            for edge in (rect.left()+4,rect.right()-4):
                p.setPen(QPen(QColor('#f2fffc' if dark else '#168779'),2 if active else 1));p.drawLine(int(edge),67,int(edge),89)
            p.setPen(QColor('#e5f5f1' if dark else '#155f55'))
            p.drawText(rect.adjusted(11,0,-11,0),Qt.AlignVCenter,str(item.get('optimized_subtitle') or item.get('original_subtitle','')))
            p.restore()
        x=15+(self.position-start)/self.zoom*(self.width()-30)
        p.setPen(QPen(QColor('#1eab93'),2));p.drawLine(int(x),46,int(x),116)

    def _duration(self):
        player=getattr(self.controller,'player',None)
        return player.duration() if player else None

    def mousePressEvent(self,event):
        if event.button()!=Qt.LeftButton:return
        hit=self.hit_test(event.pos())
        if hit:
            row,mode,item=hit;window=self.window_start()
            self.drag={'row':row,'mode':mode,'x':event.x(),'item':copy.deepcopy(item),'window':window,
                'candidate':(int(item['start_time']),int(item['end_time'])),'moved':False}
            self.editor.subtitle_table.selectRow(row);self.setFocus();self.setCursor(Qt.SizeHorCursor if mode!='move' else Qt.ClosedHandCursor)
            self.update();return
        self.controller.seek(max(0,self.window_start()+int((event.x()-15)/max(1,self.width()-30)*self.zoom)))

    def _candidate(self,x):
        drag=self.drag;item=drag['item'];start,end=int(item['start_time']),int(item['end_time'])
        delta=round((x-drag['x'])/max(1,self.width()-30)*self.zoom)
        lower,upper=interval_bounds(self.editor.model,drag['row'],self._duration())
        if drag['mode']=='start':start=max(lower,min(end-MIN_DURATION,start+delta))
        elif drag['mode']=='end':end=min(upper,max(start+MIN_DURATION,end+delta))
        else:
            delta=max(lower-start,min(upper-end,delta));start+=delta;end+=delta
        return start,end

    def mouseMoveEvent(self,event):
        if self.drag:
            if abs(event.x()-self.drag['x'])>=2:self.drag['moved']=True
            self.drag['candidate']=self._candidate(event.x());self.update()
        else:
            hit=self.hit_test(event.pos())
            self.setCursor(Qt.SizeHorCursor if hit and hit[1]!='move' else Qt.OpenHandCursor if hit else Qt.ArrowCursor)

    def mouseReleaseEvent(self,event):
        if event.button()!=Qt.LeftButton or not self.drag:return
        drag=self.drag;candidate=self._candidate(event.x());self.drag=None;self.unsetCursor()
        if drag['moved'] or abs(event.x()-drag['x'])>=2:
            ok,message=apply_interval(self.editor.model,drag['row'],*candidate,duration=self._duration())
            if not ok:self.controller.preview_note.setText(message)
            elif hasattr(self.controller,'sync_timing_fields'):self.controller.sync_timing_fields()
            self.controller.seek(candidate[0] if drag['mode']!='end' else max(candidate[0],candidate[1]-1))
        else:self.controller.seek(drag['item']['start_time'])
        self.update()

    def keyPressEvent(self,event):
        if event.key()==Qt.Key_Escape and self.drag:
            self.drag=None;self.unsetCursor();self.update();event.accept();return
        super().keyPressEvent(event)

    def wheelEvent(self,event):
        if self.drag:return
        self.zoom=max(3000,min(120000,int(self.zoom*(.8 if event.angleDelta().y()>0 else 1.25))))
        self.update();event.accept()
