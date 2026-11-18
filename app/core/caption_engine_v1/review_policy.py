"""Conservative review classification; never equate no flags with acceptance."""
import copy,unicodedata

CORRECTION_KINDS={'剧本文字校正待听检','剧本校正待听检'}

def format_key(text):
    return ''.join(c for c in unicodedata.normalize('NFC',text)
                   if not c.isspace() and not unicodedata.category(c).startswith('P'))

def is_automatic_format(item,mode,normalize_numbers):
    if mode!='script_correct' or item.get('kind') not in CORRECTION_KINDS:return False
    before,after=item.get('before'),item.get('after')
    if not isinstance(before,str) or not isinstance(after,str):return False
    # Only classify Chinese correction evidence here. Locked/other language
    # reports remain governed by their existing review policies.
    if not any('\u3400'<=c<='\u9fff' for c in before+after):return False
    a,b=format_key(before),format_key(after)
    if not a or not b:return False
    if a==b:return True
    # A number display conversion is automatic; changing a spoken Chinese
    # quantity (e.g. 六零零零 -> 六千) stays a manual correction.
    return any(c.isdecimal() for c in before) and format_key(normalize_numbers(before))==b

def classify_report(report,normalize_numbers):
    manual=[];automatic=[]
    for item in report.get('automatic_format_changes',[]):
        if isinstance(item,dict) and is_automatic_format(item,report.get('mode'),normalize_numbers):
            automatic.append(copy.deepcopy(item))
    for item in report.get('review_items',[]):
        if not isinstance(item,dict):continue
        if is_automatic_format(item,report.get('mode'),normalize_numbers):
            entry=copy.deepcopy(item);entry['classification']='automatic_format'
            if entry not in automatic:automatic.append(entry)
        else:manual.append(copy.deepcopy(item))
    return {'manual':manual,'automatic':automatic,
            'manual_count':len(manual),'automatic_count':len(automatic)}
