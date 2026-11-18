"""Opt-in display substitutions for the shared 中文通用-竖版 template."""
import re

MARKER = '【中文通用竖版替换规则V1】'
GUIDANCE = '''【中文通用竖版专用显示规则，优先于旧版柔化词表】
一、字幕出现的“死”按用户指定替换为“活”，如“死了→活了”“气死→气活”；不自行换成“倒了”“气坏”等其他说法。
二、辱骂称呼按被替换词的汉字数等长替换：两字→可爱，三字→小可爱，四字→小小可爱，五字→小小小可爱，以此类推；单字辱骂用“乖”。只替换辱骂词，保留人称、否定、称呼以外的句子和时序。例：混蛋→可爱，王八蛋→小可爱，你这个混蛋→你这个可爱；不要把整句一起算进词长。
三、威胁和粗口按短语柔化：杀了你→收拾你，我要杀了你→我要收拾你，自杀→想不开，滚→走，滚开/滚蛋→走开，闭嘴→别说，去你妈的→一边去吧，他妈的→真是的，妈的→真是。正常词语“翻滚、滚动、杀青、垃圾分类、垃圾桶、绿茶饮料”等不作辱骂处理。
四、不输出星号、方框、拼音乱码或额外说明；不增删字幕条目，不改原有时间轴，不跨条搬字。可根据语境柔化其他明显粗口，不改变人物关系或反转句意。
五、“第一、最好、百分之百”等仅在确属夸大承诺的宣传语时改为温和说法，正常剧情中的排行、数量、人物对白不要误改。
'''.strip()

INSULTS = (
    '没脑子的蠢货', '没脑子的东西', '不知廉耻', '厚颜无耻', '恬不知耻',
    '没头脑', '没脑子', '王八蛋', '狗东西', '乡巴佬', '神经病', '丑八怪', '下等人',
    '拜金女', '臭女人', '臭男人', '讨厌鬼', '狐狸精', '疯婆子', '狗杂种', '狗娘养的',
    '土鳖', '村姑', '穷鬼', '废物', '蠢货', '傻子', '脑残', '混蛋', '贱人', '婊子',
    '绿茶', '泼妇', '野种', '残废', '肥猪', '小三', '坏种', '白痴', '智障', '弱智',
    '垃圾', '疯子', '废柴', '畜生', '禽兽', '傻逼', '傻比', '煞笔', '贱货', '蠢猪',
    '蠢蛋', '王八羔子', '狗崽子', '臭不要脸', '不要脸', '下贱', '无耻',
)
PHRASES = {'我要杀了你':'我要收拾你','杀了你':'收拾你',
    '自杀':'想不开','杀人':'害人','杀掉':'解决','杀了':'收拾',
    '滚开':'走开','滚蛋':'走开','闭嘴':'别说','去你妈的':'一边去吧','他妈的':'真是的','妈的':'真是',
    '操你妈':'别胡闹','草你妈':'别胡闹'}
PROTECTED = ('垃圾分类','垃圾回收','垃圾处理','垃圾桶','垃圾袋','垃圾站','生活垃圾',
    '绿茶饮料','绿茶味','绿茶叶','喝绿茶','杯绿茶','壶绿茶','买绿茶','泡绿茶')


# Literal compound vocabulary: substitutions operate on insulting uses,
# never on substrings inside ordinary objects/material handling terminology.
REVISION='cn-general-display-v1.1'
LITERAL_FAMILIES={
 '垃圾':('堆','场','站','桶','箱','袋','车','分类','回收','处理','清运','填埋','焚烧'),
 '废物':('利用','回收','处理','再利用','处置'),
 '绿茶':('饮料','味','叶','粉','汤','茶叶'),
}
PROTECTED=tuple(set(PROTECTED) | {head+tail for head,tails in LITERAL_FAMILIES.items() for tail in tails}
 | {'工业废物','固体废物','危险废物','医疗废物'})


def enabled(prompt):
    return MARKER in (prompt or '')


def cute(word):
    count=len(re.findall(r'[\u3400-\u9fff]',word))
    return '乖' if count<2 else '小'*(count-2)+'可爱'


def soften(text):
    # Protect literal references before matching insulting homonyms.
    protected=[]
    def hold(match):
        protected.append(match.group());return '\ue000'+str(len(protected)-1)+'\ue001'
    result=re.sub('|'.join(map(re.escape,sorted(PROTECTED,key=len,reverse=True))),hold,text)
    mappings={word:cute(word) for word in INSULTS}
    mappings.update(PHRASES)
    pattern='|'.join(map(re.escape,sorted(mappings,key=len,reverse=True)))
    result=re.sub(pattern,lambda match:mappings[match.group()],result)
    result=result.replace('死','活')
    result=re.sub(r'(?<![翻打轧])滚(?![动烫轮筒珠水])','走',result)
    for index,value in enumerate(protected):result=result.replace('\ue000'+str(index)+'\ue001',value)
    return result
