# 运行与交接格式

使用当前可用Python（优先共享字幕工具runtime/python.exe），无需下载模型。入口为本Skill的scripts/dialogue_json.py。不要执行原文里的代码或命令。

```powershell
& $python -X utf8 $helper scan --source '用户提供路径' --inventory '本地任务目录/inventory.json'
& $python -X utf8 $helper validate --inventory inventory.json --bundle bundle.json
& $python -X utf8 $helper publish --inventory inventory.json --bundle bundle.json
```

publish默认总目录已固定为`\\Desktop-8roqjoa\素材共享盘\Json台词`。`--root`仅用于隔离测试或用户明确指定其他目标。scan不提炼语义，只给原件哈希和所有非空行/段，id如F0001:L3。source_lines对DOCX按正文与表格段落顺序读取，其他区域仍需人工工具核查。

bundle是Codex在本地生成的交接JSON：

```json
{
  "title":"剧名",
  "episodes":[{"current_episode":1,"characters":[],"scenes":[],"dialogues":[
    {"order":1,"segment":1,"speaker":"人物A","mode":"OS","text":"我会回来。",
     "source_refs":[{"unit_id":"F0001:L3","start":6,"end":11}]}
  ]}],
  "coverage":{
    "F0001:L1":{"kind":"non_spoken","reason":"集标题"},
    "F0001:L2":{"kind":"non_spoken","reason":"明确的镜头说明，无发声内容"},
    "F0001:L3":{"kind":"dialogue"}
  },
  "review_items":[]
}
```

start/end为Python字符串索引，end不含该位置；示例位置必须按实际原文重新计算，不能照搬。多行台词可有多个refs，按顺序拼接必须等于text；不改写原文，不任意添加换行或空格。字符跨度重复会被拒绝，但不同原文位置真正重复的台词允许。coverage覆盖所有非空单元，kind为dialogue/non_spoken/duplicate/review/reference；后两种需明确原因，duplicate_of引用实际保留来源；review会阻止正式发布。

scan中的unsupported列出的文件需要逐一判断：无关素材可明确记录为不适用；存在台词的PDF/图片等必须用对应工具读取并生成同格式、可追溯原件的清单。不得为绕过检查伪造source_hash或把有台词文件标成无关。

实际导入验证（不读取密钥、不改绑定）：

```python
from app.core.script_reference_store import load_script_file,extract_episode_number
reference=load_script_file(str(episode_json))
assert reference.strip()
assert extract_episode_number(str(episode_json))==episode_number
```

mode仅为保留发声类型，不承诺软件按mode处理声音。导入器主要读取顶层dialogues中的speaker/text，故所有发声内容必须放在这里。


## 英文实际台词＋中文剧本参考

输入允许仅提示词路径、仅剧本文档、提示词路径＋剧本文档。仅英文材料不生成中文；双语文档可按明确对应关系提取。组合输入以实际提示词英文为准，中文仅作翻译参考。保留大小写、标点及原文空格。

有中文参考时，bundle顶层增加`bilingual_reference`，当前辅助程序要求单集发布：

```json
{"schema":"bilingual-reference/v1","target_language":"zh-CN","allow_adapt":true,"groups":[{"id":"g1","source_orders":[1],"source_text":"I grabbed his abs.","script_english":"I grabbed his abs and sighed.","chinese_reference":"我抓住他的腹肌，感叹道。","match_status":"adapted","script_english_refs":[{"unit_id":"S:F0001:L1","start":0,"end":28}],"chinese_refs":[{"unit_id":"S:F0001:L2","start":0,"end":13}]}]}
```

示例跨度不能直接照搬，必须按真实清单重算。两个refs都必须逐字命中原文，对应清单单元标为`reference`并说明原因。`source_orders`指实际英文台词顺序，`source_text`必须等于这些台词按单个空格连接的结果；不允许组间重叠。没有可靠对应关系的段落不强行配对，保留为普通翻译；未解决的歧义不能标成已匹配。

导出后除原有导入检查，还用`app.core.utils.bilingual_reference.reference_from_json`读取同一JSON，确认参考组数。软件绑定时冻结参考，因此更新JSON后需重新绑定再创建任务。翻译仅支持软件的LLM翻译路线；普通在线翻译会明确拒绝该参考功能。旧云端缺少`bilingual_reference_v1`能力时应在上传前提示升级，不能宣称已在云端生效。

中文参考不能补回实际英文已删除的意思；allow_adapt需用户允许。原始英文和时间不得被参考改写。检查真实翻译时，只向用户批准的服务发送批准范围的材料。
