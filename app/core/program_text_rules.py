"""Explicit customer display rules; no inference from prose and no executable regex."""
import copy,re
NUMBER_POLICIES=('preserve','chinese_only','arabic_allowed','contextual')
def validate(value):
 if value is None:return {}
 if not isinstance(value,dict) or set(value)-{'literal_replacements','remove_spaces','number_policy'}:raise ValueError('程序文字规则字段无效')
 result=copy.deepcopy(value)
 if 'remove_spaces' in result and type(result['remove_spaces']) is not bool:raise ValueError('remove_spaces必须是布尔值')
 if result.get('number_policy','preserve') not in NUMBER_POLICIES:raise ValueError('未知数字显示规则')
 rules=result.get('literal_replacements',[])
 if not isinstance(rules,list) or len(rules)>100:raise ValueError('指定替换最多100条')
 seen={}
 for row in rules:
  if not isinstance(row,dict) or set(row)!={'from','to'} or any(not isinstance(row[k],str) or not 1<=len(row[k])<=100 or any(ord(c)<32 for c in row[k]) for k in ('from','to')):raise ValueError('指定替换需要非空from/to纯文字')
  if re.search(r'\{\\|\\[Nnh]|</?(?:font|span|style|script|div|br|p)\b',row['to'],re.I):raise ValueError('指定替换不能包含样式或换行指令')
  if row['from'] in seen:raise ValueError('指定替换原词重复，需先解决冲突')
  seen[row['from']]=row['to']
 for source,target in seen.items():
  if source!=target and any(term in target for term,replacement in seen.items() if term!=replacement):raise ValueError('替换结果再次命中待替换原词，需先解决级联冲突')
 return result

def apply(text,rules,normalize_numbers):
 rules=validate(rules);mapping={r['from']:r['to'] for r in rules.get('literal_replacements',[]) if r['from']!=r['to']}
 # One pass, longest literal first. No cascading replacement or user regex code.
 if mapping:
  pattern='|'.join(re.escape(k) for k in sorted(mapping,key=len,reverse=True));text=re.sub(pattern,lambda m:mapping[m[0]],text)
 if rules.get('number_policy')=='chinese_only':text=normalize_numbers(text)
 if rules.get('remove_spaces'):text=re.sub(r'\s+','',text)
 return text
