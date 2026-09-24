"""Summarize staged repairs without conflating candidates with published fixes."""
from pathlib import Path
import argparse
import csv
import html
import json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', type=Path, required=True)
    parser.add_argument('--artifacts', type=Path, required=True)
    args = parser.parse_args()
    output = args.artifacts
    coverage = json.loads((args.audit / 'coverage_final.json').read_text(encoding='utf-8'))
    issues = json.loads((args.audit / 'issues_final.json').read_text(encoding='utf-8'))
    for row in coverage:
        row['发布状态'] = '未上线'
        row['本轮候选修复'] = '无数据修改；原有核查状态保留'
        row['本轮浏览器验证'] = '逐卷真实页面仍待验证；共性样式已在隔离书库验证'
        if row['类型'] == '中文PDF':
            row['本轮候选修复'] = '共用目录组件取消层级显示上限；本卷数据保持原样'
            if row['书库'] == '文集' and row['卷册'] in (4, 5, 10):
                row['本轮候选修复'] += '；首批有证据的页码/位置修复'
                if row['卷册'] == 5:
                    row['本轮候选修复'] += '；第二批准备恢复 12 个七/八级标题'
        if row['书库'] == 'lenin-ru':
            row['本轮候选修复'] = '新增总目录兼容入口；卷内数据保持原样'
        if row['书库'] == 'mew-de' and row['卷册'] == 42:
            row['本轮候选修复'] = '修正标题首字母错链'
        if 'mega2-ii-5/index.html' in row['来源']:
            row['本轮候选修复'] = '第二批试点：按印刷目录建立 50 个正文条目的层级；保留旧页段入口'
            row['新增证据缺口'] = '当前 PDF 未完整收录导论、编校说明、Apparat、Register；整卷不能判定完整'
    for issue in issues:
        issue['本轮状态'] = '未上线；保留原审计结论，待逐批修复验收'
    for name, data in [('逐卷修复覆盖表.csv', coverage), ('审计问题跟踪表.csv', issues)]:
        fields = list(dict.fromkeys(k for row in data for k in row))
        with (output / name).open('w', newline='', encoding='utf-8-sig') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(data)
    columns = ['类型', '书库', '卷册', '范围', '来源', '本轮候选修复', '核查结论', '发布状态']
    cells = ''.join('<tr>' + ''.join('<td>' + html.escape(str(row.get(c, ''))) + '</td>' for c in columns) + '</tr>' for row in coverage)
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>目录修复候选交付</title><style>body{font:16px/1.65 system-ui;margin:28px;color:#222}h1{font-size:25px}.status{padding:15px;background:#fff2d9;border-left:4px solid #b67b19}input{padding:12px;width:min(90%,600px);margin:20px 0}table{border-collapse:collapse;font-size:13px}td,th{border:1px solid #ddd;padding:9px;text-align:left;vertical-align:top}th{position:sticky;top:0;background:#eee}td{max-width:340px;overflow-wrap:anywhere}.table{overflow:auto}tr[hidden]{display:none}</style>
<h1>目录修复候选交付</h1><p class="status">尚未上线。覆盖表保留全部 777 个审计单元的原核查结论，候选修复不等于整卷核实通过。</p>
<p>已准备兼容基础代码、完整基线和首批定向修复。第二批包含《文集》5 卷及 MEGA² II/5 的有界试点。当前任务已被指定为发布协调者；完整原书核对、真实逐卷验收和生产发布演练仍是放行条件。</p>
<input id="filter" placeholder="筛选书名、卷册、来源或候选修复"><div class="table"><table><thead><tr>'''
    page += ''.join('<th>' + html.escape(c) + '</th>' for c in columns) + '</tr></thead><tbody>' + cells + '</tbody></table></div>'
    page += '''<script>document.getElementById('filter').addEventListener('input',function(){const q=this.value.toLowerCase();document.querySelectorAll('tbody tr').forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q));});</script></html>'''
    (output / '逐卷修复状态.html').write_text(page, encoding='utf-8')
    print('Coverage:', len(coverage), 'Issue records:', len(issues), 'Published: 0')


if __name__ == '__main__':
    main()
