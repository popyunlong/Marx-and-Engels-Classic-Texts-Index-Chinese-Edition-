"""Consistent Chinese display names; citations retain original journal names."""
import re
import unicodedata

JOURNAL_NAMES_ZH = {
    'Historical Materialism: Research in Critical Marxist Theory': '历史唯物主义：批判马克思主义理论研究',
    'Rethinking Marxism': '重新思考马克思主义',
    'Science & Society: A Journal of Marxist Thought and Analysis': '科学与社会：马克思主义思想与分析',
    'Capital & Class': '资本与阶级',
    'Monthly Review': '每月评论',
    'New Left Review': '新左派评论',
    'Critique: Journal of Socialist Theory': '批判：社会主义理论期刊',
    'Socialist Register': '社会主义年鉴',
    'International Critical Thought': '国际批判思想',
    'Capitalism Nature Socialism': '资本主义、自然与社会主义',
    'Cambridge Journal of Economics': '剑桥经济学杂志',
    'Review of Radical Political Economics': '激进政治经济学评论',
    'Review of Political Economy': '政治经济学评论',
    'Journal of Economic Issues': '经济问题杂志',
    'Structural Change and Economic Dynamics': '结构变迁与经济动态',
    'Economic Geography': '经济地理',
    'Journal of Institutional Economics': '制度经济学杂志',
    'International Journal of Political Economy': '国际政治经济学杂志',
    'New Political Economy': '新政治经济学',
    'Review of Development Economics': '发展经济学评论',
    'Economy and Society': '经济与社会',
    'Radical Philosophy': '激进哲学',
    'Philosophy & Social Criticism': '哲学与社会批判',
    'Constellations': '星丛',
    'Critical Horizons': '批判视野',
    'Theory, Culture & Society': '理论、文化与社会',
    'Thesis Eleven': '第十一条论纲',
    'European Journal of Philosophy': '欧洲哲学杂志',
    'Hegel Bulletin': '黑格尔学刊',
    'Continental Philosophy Review': '欧陆哲学评论',
    'Inquiry': '探究',
    'Mind': '心灵',
    'The Philosophical Review': '哲学评论',
    'The Journal of Philosophy': '哲学杂志',
    'Noûs': '努斯',
    'Philosophy and Phenomenological Research': '哲学与现象学研究',
    'Ethics': '伦理学',
    'Philosophy & Public Affairs': '哲学与公共事务',
    'Journal of Political Philosophy': '政治哲学杂志',
    'The Philosophical Quarterly': '哲学季刊',
    'Analysis': '分析',
    'Australasian Journal of Philosophy': '澳大拉西亚哲学杂志',
    'Philosophical Studies': '哲学研究',
    'British Journal for the History of Philosophy': '英国哲学史杂志',
    'Journal of the History of Philosophy': '哲学史杂志',
}


def _key(name: str) -> str:
    name = unicodedata.normalize('NFKD', name.casefold()).replace('&', 'and')
    return re.sub(r'[^a-z0-9]', '', name)


_NAMES = {_key(name): zh for name, zh in JOURNAL_NAMES_ZH.items()}
for name, zh in JOURNAL_NAMES_ZH.items():
    if ':' in name:
        _NAMES[_key(name.split(':')[0])] = zh.split('：')[0]


def chinese_name(name: str) -> str:
    return _NAMES.get(_key(name), '')


def display_name(name: str) -> str:
    zh = chinese_name(name)
    return f'{zh} · {name}' if zh else name
