# 操作与计划格式

## 运行环境

在PowerShell中使用软件自带Python，不下载新运行时：

```powershell
$appRoot = '\\desktop-8roqjoa\电脑文件储存\工具箱\自动化工具\卡卡字幕工具\VideoCaptioner\VideoCaptioner'
$pythonExe = Join-Path $appRoot 'runtime\python.exe'
$skillScripts = Join-Path $env:USERPROFILE '.codex\skills\kaka-template-assistant\scripts'
& $pythonExe -X utf8 "$skillScripts/template_bridge.py" --app-root $appRoot list
& $pythonExe -X utf8 "$skillScripts/template_bridge.py" --app-root $appRoot show --id '实际ID' --out current.json
& $pythonExe -X utf8 "$skillScripts/template_bridge.py" --app-root $appRoot capabilities --out capabilities.json
```

共享盘不可达时不创建本地替代库。输出与客户资料放在当前任务的工作目录，下面示例相对路径均指该目录。若 CODEX_HOME 自定义，scripts目录相应取该目录下的skills路径。

## 计划结构

UTF-8 JSON，完整键为：operation(create/update)、base_id、expected_revision、name、source_text、rules、model_requirements、replace_model_requirements、quality_checks、pending、enable、appearance、canvas。

- base_id是list读取的普通模板ID，expected_revision为其实际最新版本。阿拉伯保护模板不可作为基础。
- rules为`[{"field":"字段","value":"实际值","evidence":"source_text中原文"}]`，只用capabilities返回的字段；可以只改部分字段。更新时保留旧的未改规则记录。
- source_text整理需求原文及操作者明确补充，最多45000字符。不能将待解决要求悄悄丢掉。
- model_requirements是明确的模型文字说明数组；默认追加并保留原有模型要求。只有用户明确替换时设置replace_model_requirements=true，不凭新措辞留下相互冲突的新旧要求。
- quality_checks为`[{"requirement":"标准","method":"如何检查/还未做","coverage":"program或model或external"}]`。记录不等于自动执行检查，更不等于验收通过。
- pending是真正未解决的硬缺口/歧义。先提问和解决，不把保存草稿当完成。移除旧待确认项需在source_text逐字引用该条并说明解决依据或用户明确接受的范围变化。
- canvas省略即1080×1920；另有明确尺寸时填`{"width":1920,"height":1080}`，不重复询问默认竖屏尺寸。
- appearance用软件已校正的剪映单位，例：`{"Default":{"size":14,"outline_width":3,"color":"#FFFFFF","shadow_blur":30,"shadow_distance":5,"shadow_angle":-45},"capcut_y":-645}`。Default/Secondary为软件主/副字幕样式，先说明在当前排列中各对应哪一层。可选font、outline_color、shadow_color、shadow_opacity。capcut_y是剪映坐标，不是图片左上角起算的像素Y；必须通过真实预览确认位置，不混用两种坐标。

## 四环节记录

```powershell
& $pythonExe -X utf8 "$skillScripts/workflow.py" init --session session.json --plan plan.json
```

每环节只在用户真实回复确认后记录，不得为了过脚本自造回复：

```powershell
& $pythonExe -X utf8 "$skillScripts/workflow.py" confirm --session session.json --phase goal --reply '用户对目标的实际确认回复'
& $pythonExe -X utf8 "$skillScripts/workflow.py" confirm --session session.json --phase rules --reply '用户对规则的实际确认回复'
```

计划变化先同步：

```powershell
& $pythonExe -X utf8 "$skillScripts/workflow.py" sync --session session.json --plan plan.json
& $pythonExe -X utf8 "$skillScripts/workflow.py" status --session session.json
```

sync按修改影响撤销确认：语言/处理目标从goal开始，普通规则从rules开始，外观从preview开始，名称从publish开始。仅需求原文变化而未明确编译到参数时，不猜测其影响，重新核对目标。跨聊天继续使用同一session；新电脑从共享模板记录、预览资产和最新版本恢复上下文，不能假装收到新的确认。

## 真实预览

使用软件`ASRData.to_json()`格式提供0~10秒代表台词，保留原文、译文、起止时间身份。可由实际任务已有结果提取；人工构造时必须标示范样例，不得把它当ASR或翻译验收。基本结构示例可从现有canonical.json读取，避免猜字段。

```powershell
& $pythonExe -X utf8 "$skillScripts/template_bridge.py" --app-root $appRoot check --plan plan.json --out check.json
& $pythonExe -X utf8 "$skillScripts/render_preview.py" --app-root $appRoot --plan plan.json --subtitles sample.json --output preview-v1 --sample-kind illustrative
```

默认标准背景、1080×1920、24fps。可用`--image 截图路径`或`--video 已提取的代表短片路径 --sample-kind actual`。视频画布不符会拒绝，不擅自拉伸；仅标准背景不能验收真实遮挡或音频时间。输出非空目录会拒绝，修改后使用新预览版本目录。

返回PNG、MP4、receipt.json。必须给用户展示图片与视频；播放不支持时提供可打开链接，主动收取反馈。该脚本使用正式合成线程比对全部解码帧，校验后附加记录：

```powershell
& $pythonExe -X utf8 "$skillScripts/workflow.py" attach-preview --session session.json --receipt preview-v1/receipt.json
& $pythonExe -X utf8 "$skillScripts/workflow.py" confirm --session session.json --phase preview --reply '用户对实际预览的确认回复'
```

收到最终名称和方案的整体确认后记录publish。不要重问此前未改变的全部细项。

## 发布

```powershell
& $pythonExe -X utf8 "$skillScripts/workflow.py" confirm --session session.json --phase publish --reply '用户对最终发布方案的确认回复'
& $pythonExe -X utf8 "$skillScripts/template_bridge.py" --app-root $appRoot check --plan plan.json --out check.json
$checked = Get-Content check.json -Raw -Encoding utf8 | ConvertFrom-Json
& $pythonExe -X utf8 "$skillScripts/template_bridge.py" --app-root $appRoot commit --plan plan.json --session session.json --apply --expect-plan-sha $checked.plan_sha256 --out saved.json
```

commit再次核对方案、用户确认、预览指纹、当前软件/字体环境和基础模板版本；通过软件save_profile保存并回读，确认启用列表中存在。确认后的PNG、MP4、canonical JSON、ASS及样例SRT保存到共享AppData/template-previews，不复制原始文档或客户源视频。

共享库版本冲突、预览失效或硬要求未解决时，明确原因并继续定位/提问，禁止强制覆盖或把“停用草稿”称为正式模板。脚本不测试服务密钥、ASR质量或云端，不把静态样例通过当成实际素材文字链路已验证。

## 安装与更新

在每台电脑运行共享发布目录的install_on_this_pc.ps1，安装到该用户的Codex skills目录。重开Codex聊天后用`$kaka-template-assistant`，或说“用字母模板创建帮我创建/修改共享字幕模板”。不需要退出字幕软件。

更新重跑同一安装器，会核对包和本机受管理文件哈希并保留备份；本机文件被另行修改时停止覆盖。安装Skill不更改字幕软件代码、接口密钥或云端。

## 前置能力检查

在首次预览前使用 preflight 动作（参数与 check 相同）。保存 preflight.json 和需求覆盖表。checks 是离线执行器探针结果，not_verified 必须保留；不是ASR/翻译/云端验收。外观支持 bold 布尔值，必须通过ASS导出保留检查。边界预览必须使用本模板的长度上限并展示实际图片。

保持字号优先拆条：用户明确选择后，使用delivery_keep_font_size=1（integer），仍保留每种语言45字符上限。软件按现有ASS校准宽度与字号计算显示容量；超过显示容量进入原生词时间拆条及有限重译，不缩字号，不均分时间。无原生边界或有限重试未通过时报告失败。该字段只按模板显式启用，不是所有模板的新默认。

## 独立定位 1.2.1
appearance中的Default与Secondary各允许capcut_y数值，例如Default.capcut_y=-528、Secondary.capcut_y=-660。同一项出现时，分层位置优先于顶层capcut_y。软件按原有calibrated_margin换算并记录VideoCaptionerPosition样式标记；导出只对标记的样式固定位置，不改变字数、行数或文字处理规则。字段数值是当前软件校准输入，不得把它当成原视频像素位置。

## 按样式实投影 1.2.2

`appearance.Default.solid_shadow`和`Secondary.solid_shadow`是布尔值，默认关闭；通过显式ASS标记启用完整字形的独立投影层，颜色和透明度取该样式的shadow_color/shadow_opacity，方向、距离、模糊仍复用原软件换算。启用前查capabilities的appearance_extensions.solid_shadow.executor_available；preflight会检查标记、颜色和透明度接通最终ASS。不能把输入距离或模糊值直接称为最终画面像素，仍须真实渲染。

本版本附install_software_patch.ps1用于另一个独立软件副本的显式安装；主技能安装器不自动改软件。补丁仅接受清单中已核对的旧哈希或新哈希，保留原件，不覆盖未知修改。共享软件与本机已安装并不代表其他电脑验收通过。运行时文件变更会使旧预览凭据失效，需重渲；不会自动改变客户模板。
