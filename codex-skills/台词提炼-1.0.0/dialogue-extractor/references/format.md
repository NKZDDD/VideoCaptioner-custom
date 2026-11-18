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

start/end为Python字符串索引，end不含该位置；示例位置必须按实际原文重新计算，不能照搬。多行台词可有多个refs，按顺序拼接必须等于text；不改写原文，不任意添加换行或空格。字符跨度重复会被拒绝，但不同原文位置真正重复的台词允许。coverage覆盖所有非空单元，kind为dialogue/non_spoken/duplicate/review；后两种需明确原因，duplicate_of引用实际保留来源；review会阻止正式发布。

scan中的unsupported列出的文件需要逐一判断：无关素材可明确记录为不适用；存在台词的PDF/图片等必须用对应工具读取并生成同格式、可追溯原件的清单。不得为绕过检查伪造source_hash或把有台词文件标成无关。

实际导入验证（不读取密钥、不改绑定）：

```python
from app.core.script_reference_store import load_script_file,extract_episode_number
reference=load_script_file(str(episode_json))
assert reference.strip()
assert extract_episode_number(str(episode_json))==episode_number
```

mode仅为保留发声类型，不承诺软件按mode处理声音。导入器主要读取顶层dialogues中的speaker/text，故所有发声内容必须放在这里。
