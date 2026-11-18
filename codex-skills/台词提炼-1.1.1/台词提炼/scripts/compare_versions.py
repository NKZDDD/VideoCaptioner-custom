"""Compare versions without modifying the dialogue strings used for output."""
import argparse,difflib,json,re
from pathlib import Path

def compare(left,right):
 if any(not isinstance(t,str) for t in left+right):raise ValueError('版本台词必须为字符串数组')
 # The join spaces are comparison separators only; never write this back to JSON text.
 a=' '.join(left);b=' '.join(right)
 aa=re.sub(r'\s','',a);bb=re.sub(r'\s','',b)
 changes=[]
 for tag,i,j,k,l in difflib.SequenceMatcher(None,aa,bb,autojunk=False).get_opcodes():
  if tag!='equal':changes.append({'kind':tag,'left':aa[i:j],'right':bb[k:l],
    'left_context':aa[max(0,i-24):min(len(aa),j+24)],'right_context':bb[max(0,k-24):min(len(bb),l+24)]})
 lexical=lambda t:re.findall(r"[\w]+(?:['’][\w]+)*",t.casefold(),re.UNICODE)
 return {'fragments_exact':left==right,'joined_comparison_exact':a==b,
   'same_case_punctuation_and_characters_ignoring_whitespace':aa==bb,
   'word_sequence_equal':lexical(a)==lexical(b),
   'non_whitespace_changes':changes,
   'note':'分段数组严格比较；join只为比较增加分隔空格。忽略空白的比较仍区分大小写、标点。规范化仅用于诊断，输出必须保留所选原文。'}

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--input',required=True);p.add_argument('--out',required=True);a=p.parse_args()
 data=json.loads(Path(a.input).read_text(encoding='utf-8-sig'))
 result=compare(data['left'],data['right'])
 Path(a.out).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps(result,ensure_ascii=False,indent=2))
