"""Freeze appearance for one task, without overwriting shared customer profiles."""
import copy
import hashlib
from pathlib import Path
from app.config import SUBTITLE_STYLE_PATH


def freeze_task_style(profile, preset=None):
    result=copy.deepcopy(profile)
    style=result.get('style_template','')
    source='客户模板'
    if preset:
        if Path(preset).name != preset:
            raise ValueError('字幕样式名称无效')
        style=(SUBTITLE_STYLE_PATH/(preset+'.txt')).read_text(encoding='utf-8-sig')
        # Appearance selection must not silently switch language processing.
        language=[line for line in result.get('style_template','').splitlines() if line.startswith('; VideoCaptionerOriginalLanguage:')]
        if language:
            style='\n'.join(line for line in style.splitlines() if not line.startswith('; VideoCaptionerOriginalLanguage:'))+'\n'+'\n'.join(language)
        result['settings']['subtitle_style_name']=preset
        source='已保存样式：'+preset
    if not style.strip():raise ValueError('所选样式内容为空，请先保存字幕样式')
    result['style_template']=style
    result['style_source']=source
    result['style_sha256']=hashlib.sha256(style.encode('utf-8')).hexdigest()
    result['apply_customer_style']=True
    return result


def appearance_details(style):
    fields=next((line.split(',') for line in style.splitlines() if line.startswith('Style: Default,')),None)
    if not fields or len(fields)<23:raise ValueError('字幕样式缺少主字幕参数')
    return {'font':fields[1],'ass_font_size':float(fields[2]),'software_font_size':round(float(fields[2])*14/46,2),
        'outline':float(fields[16]),'shadow':float(fields[17]),'alignment':int(fields[18]),
        'margin_left':int(fields[19]),'margin_right':int(fields[20]),'margin_bottom':int(fields[21]),
        'shadow_colour':fields[6],'effects':[line for line in style.splitlines() if line.startswith('; VideoCaptionerEffect:')]}
