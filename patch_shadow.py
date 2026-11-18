# -*- coding: utf-8 -*-
from pathlib import Path
import shutil, sys

ROOT = Path(__file__).resolve().parent
candidates = [
    ROOT / 'app' / 'view' / 'subtitle_style_interface.py',
    ROOT / 'VideoCaptioner' / 'app' / 'view' / 'subtitle_style_interface.py',
]
ui = next((p for p in candidates if p.exists()), None)
if ui is None:
    hits = list(ROOT.rglob('subtitle_style_interface.py'))
    ui = next((p for p in hits if p.as_posix().endswith('/app/view/subtitle_style_interface.py')), None)
if ui is None:
    print('ERROR: Cannot find app/view/subtitle_style_interface.py')
    print('Please put this patch folder beside VideoCaptioner.exe.')
    input('Press Enter to exit...')
    sys.exit(1)

text = ui.read_text(encoding='utf-8-sig')
backup = ui.with_suffix(ui.suffix + '.shadow_backup')

if 'self.mainShadowColorCard' in text and 'self.subShadowColorCard' in text:
    print('Shadow controls are already installed.')
    print(str(ui))
    input('Press Enter to exit...')
    sys.exit(0)

original = text

def once(old, new, label):
    global text
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f'Anchor failed: {label} (found {count})')
    text = text.replace(old, new, 1)

# 1) Add main shadow controls after main outline size.
once('''        self.mainOutlineSizeCard = DoubleSpinBoxSettingCard(\n            FIF.ZOOM,\n            self.tr("主字幕边框大小"),\n            self.tr("设置主字幕的边框粗细"),\n            minimum=0.0,\n            maximum=10.0,\n            decimals=1,\n        )\n''', '''        self.mainOutlineSizeCard = DoubleSpinBoxSettingCard(\n            FIF.ZOOM,\n            self.tr("主字幕边框大小"),\n            self.tr("设置主字幕的边框粗细"),\n            minimum=0.0,\n            maximum=10.0,\n            decimals=1,\n        )\n\n        self.mainShadowColorCard = ColorSettingCard(\n            QColor(0, 0, 0),\n            FIF.PALETTE,\n            self.tr("主字幕阴影颜色"),\n            self.tr("设置主字幕的阴影颜色"),\n        )\n\n        self.mainShadowSizeCard = DoubleSpinBoxSettingCard(\n            FIF.ZOOM,\n            self.tr("主字幕阴影大小"),\n            self.tr("设置主字幕阴影的偏移距离，0 为关闭"),\n            minimum=0.0,\n            maximum=20.0,\n            decimals=1,\n        )\n''', 'main cards')

# 2) Add sub shadow controls.
once('''        self.subOutlineSizeCard = DoubleSpinBoxSettingCard(\n            FIF.ZOOM,\n            self.tr("副字幕边框大小"),\n            self.tr("设置副字幕的边框粗细"),\n            minimum=0.0,\n            maximum=50.0,\n            decimals=1,\n        )\n''', '''        self.subOutlineSizeCard = DoubleSpinBoxSettingCard(\n            FIF.ZOOM,\n            self.tr("副字幕边框大小"),\n            self.tr("设置副字幕的边框粗细"),\n            minimum=0.0,\n            maximum=50.0,\n            decimals=1,\n        )\n\n        self.subShadowColorCard = ColorSettingCard(\n            QColor(0, 0, 0),\n            FIF.PALETTE,\n            self.tr("副字幕阴影颜色"),\n            self.tr("设置副字幕的阴影颜色"),\n        )\n\n        self.subShadowSizeCard = DoubleSpinBoxSettingCard(\n            FIF.ZOOM,\n            self.tr("副字幕阴影大小"),\n            self.tr("设置副字幕阴影的偏移距离，0 为关闭"),\n            minimum=0.0,\n            maximum=20.0,\n            decimals=1,\n        )\n''', 'sub cards')

# 3) Add to layouts.
once('''        self.mainGroup.addSettingCard(self.mainOutlineSizeCard)\n\n        self.subGroup.addSettingCard(self.subFontCard)\n''', '''        self.mainGroup.addSettingCard(self.mainOutlineSizeCard)\n        self.mainGroup.addSettingCard(self.mainShadowColorCard)\n        self.mainGroup.addSettingCard(self.mainShadowSizeCard)\n\n        self.subGroup.addSettingCard(self.subFontCard)\n''', 'main layout')
once('''        self.subGroup.addSettingCard(self.subOutlineSizeCard)\n\n        self.previewGroup.addSettingCard(self.previewTextCard)\n''', '''        self.subGroup.addSettingCard(self.subOutlineSizeCard)\n        self.subGroup.addSettingCard(self.subShadowColorCard)\n        self.subGroup.addSettingCard(self.subShadowSizeCard)\n\n        self.previewGroup.addSettingCard(self.previewTextCard)\n''', 'sub layout')

# 4) Connect signals.
once('''        self.mainOutlineSizeCard.spinBox.valueChanged.connect(self.onSettingChanged)\n\n        # 副字幕样式\n''', '''        self.mainOutlineSizeCard.spinBox.valueChanged.connect(self.onSettingChanged)\n        self.mainShadowColorCard.colorChanged.connect(self.onSettingChanged)\n        self.mainShadowSizeCard.spinBox.valueChanged.connect(self.onSettingChanged)\n\n        # 副字幕样式\n''', 'main signals')
once('''        self.subOutlineSizeCard.spinBox.valueChanged.connect(self.onSettingChanged)\n\n        # 预览设置\n''', '''        self.subOutlineSizeCard.spinBox.valueChanged.connect(self.onSettingChanged)\n        self.subShadowColorCard.colorChanged.connect(self.onSettingChanged)\n        self.subShadowSizeCard.spinBox.valueChanged.connect(self.onSettingChanged)\n\n        # 预览设置\n''', 'sub signals')

# 5) Generate shadow colors and sizes.
once('''        main_spacing = self.mainSpacingCard.spinBox.value()\n        main_outline_size = self.mainOutlineSizeCard.spinBox.value()\n\n        # 提取副字幕样式元素\n''', '''        main_spacing = self.mainSpacingCard.spinBox.value()\n        main_outline_size = self.mainOutlineSizeCard.spinBox.value()\n        main_shadow_hex = self.mainShadowColorCard.colorPicker.color.name()\n        main_shadow_color = f"&H00{main_shadow_hex[5:7]}{main_shadow_hex[3:5]}{main_shadow_hex[1:3]}"\n        main_shadow_size = self.mainShadowSizeCard.spinBox.value()\n\n        # 提取副字幕样式元素\n''', 'main generation')
once('''        sub_spacing = self.subSpacingCard.spinBox.value()\n        sub_outline_size = self.subOutlineSizeCard.spinBox.value()\n\n        # 生成样式字符串\n''', '''        sub_spacing = self.subSpacingCard.spinBox.value()\n        sub_outline_size = self.subOutlineSizeCard.spinBox.value()\n        sub_shadow_hex = self.subShadowColorCard.colorPicker.color.name()\n        sub_shadow_color = f"&H00{sub_shadow_hex[5:7]}{sub_shadow_hex[3:5]}{sub_shadow_hex[1:3]}"\n        sub_shadow_size = self.subShadowSizeCard.spinBox.value()\n\n        # 生成样式字符串\n''', 'sub generation')

# In ASS, BackColour is used by libass as shadow color and Shadow is the offset amount.
once('''        main_style = f"Style: Default,{main_font},{main_size},{main_color},&H000000FF,{main_outline_color},&H00000000,-1,0,0,0,100,100,{main_spacing},0,1,{main_outline_size},0,2,10,10,{vertical_spacing},1,\\q1"\n        sub_style = f"Style: Secondary,{sub_font},{sub_size},{sub_color},&H000000FF,{sub_outline_color},&H00000000,-1,0,0,0,100,100,{sub_spacing},0,1,{sub_outline_size},0,2,10,10,{vertical_spacing},1,\\q1"\n''', '''        main_style = f"Style: Default,{main_font},{main_size},{main_color},&H000000FF,{main_outline_color},{main_shadow_color},-1,0,0,0,100,100,{main_spacing},0,1,{main_outline_size},{main_shadow_size},2,10,10,{vertical_spacing},1,\\q1"\n        sub_style = f"Style: Secondary,{sub_font},{sub_size},{sub_color},&H000000FF,{sub_outline_color},{sub_shadow_color},-1,0,0,0,100,100,{sub_spacing},0,1,{sub_outline_size},{sub_shadow_size},2,10,10,{vertical_spacing},1,\\q1"\n''', 'ASS lines')

# 6) Load saved ASS shadow color/size. BackColour is index 6; Shadow index 17.
needle = '''                self.mainSpacingCard.spinBox.setValue(float(parts[13]))\n                self.mainOutlineSizeCard.spinBox.setValue(float(parts[16]))\n'''
replacement = '''                self.mainSpacingCard.spinBox.setValue(float(parts[13]))\n                self.mainOutlineSizeCard.spinBox.setValue(float(parts[16]))\n                self.mainShadowSizeCard.spinBox.setValue(float(parts[17]))\n                shadow_color = parts[6].strip()\n                if shadow_color.startswith("&H") and len(shadow_color) >= 10:\n                    color_hex = shadow_color[2:]\n                    alpha = int(color_hex[0:2], 16)\n                    blue = int(color_hex[2:4], 16)\n                    green = int(color_hex[4:6], 16)\n                    red = int(color_hex[6:8], 16)\n                    self.mainShadowColorCard.setColor(QColor(red, green, blue, alpha))\n'''
once(needle, replacement, 'main load')
needle2 = '''                self.subSpacingCard.spinBox.setValue(float(parts[13]))\n                self.subOutlineSizeCard.spinBox.setValue(float(parts[16]))\n'''
replacement2 = '''                self.subSpacingCard.spinBox.setValue(float(parts[13]))\n                self.subOutlineSizeCard.spinBox.setValue(float(parts[16]))\n                self.subShadowSizeCard.spinBox.setValue(float(parts[17]))\n                shadow_color = parts[6].strip()\n                if shadow_color.startswith("&H") and len(shadow_color) >= 10:\n                    color_hex = shadow_color[2:]\n                    alpha = int(color_hex[0:2], 16)\n                    blue = int(color_hex[2:4], 16)\n                    green = int(color_hex[4:6], 16)\n                    red = int(color_hex[6:8], 16)\n                    self.subShadowColorCard.setColor(QColor(red, green, blue, alpha))\n'''
once(needle2, replacement2, 'sub load')

if text == original:
    raise RuntimeError('No changes made')

if not backup.exists():
    shutil.copy2(ui, backup)
ui.write_text(text, encoding='utf-8')

# Validate syntax using the current Python interpreter.
compile(text, str(ui), 'exec')

# Remove local bytecode cache for this module if present.
pycache = ui.parent / '__pycache__'
if pycache.exists():
    for p in pycache.glob('subtitle_style_interface*.pyc'):
        try: p.unlink()
        except OSError: pass

print('OK: subtitle shadow controls installed.')
print(f'Patched: {ui}')
print('Restart VideoCaptioner completely to see the new controls.')
input('Press Enter to exit...')
