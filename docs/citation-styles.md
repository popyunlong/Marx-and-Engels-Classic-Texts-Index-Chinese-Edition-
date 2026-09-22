# 中文马克思主义与政治经济学期刊引文格式

核验日期：2026-09-22。运行时权威数据位于 `citation_styles.py`；本文件供合并审阅和后续核验使用。资料页未标注发布日期时，注册表的 `source_date` 留空，不猜测。

## 清单与格式族

| 稳定键 | 期刊 | 格式族 | 公开核验入口 |
|---|---|---|---|
| `mkszyj` | 《马克思主义研究》 | 完整脚注 | [中国社会科学院马克思主义研究院](http://myy.cssn.cn/) |
| `zgdsyj` | 《中共党史研究》 | 分隔式脚注 | [中央党史和文献研究院](https://www.dswxyjy.org.cn/) |
| `mkszyyxs` | 《马克思主义与现实》 | 精简经典脚注 | [中共中央编译局公开资料](http://www.cctb.net/) |
| `gwlldx` | 《国外理论动态》 | 出版社年版脚注 | [中共中央编译局公开资料](http://www.cctb.net/) |
| `ddsjyshzy` | 《当代世界与社会主义》 | 版次精简脚注 | [中共中央编译局公开资料](http://www.cctb.net/) |
| `zgtsshzyyj` | 《中国特色社会主义研究》 | 冒号卷次文末式 | [期刊公开站](https://www.zgtsshzy.net/) |
| `sxllyjdk` | 《思想理论教育导刊》 | 逗号卷次文末式 | [全国高校思想政治工作网](https://www.sizhengwang.cn/) |
| `mkszylilxkyj` | 《马克思主义理论学科研究》 | 逗号卷次文末式 | [期刊公开站](https://mkszy.cbpt.cnki.net/) |
| `kxszy` | 《科学社会主义》 | 出版社年版脚注 | [官方投稿须知](https://kxsh.cbpt.cnki.net/EditorAN/PromptPageInfo.aspx?c=1&t=v) |
| `ddwx` | 《党的文献》 | 出版社年版脚注 | [中央党史和文献研究院](https://www.dswxyjy.org.cn/) |
| `hqwg` | 《红旗文稿》 | 出版社年版脚注 | [求是网](http://www.qstheory.cn/hqwg/) |
| `ddsjshzywt` | 《当代世界社会主义问题》 | 出版社年版脚注 | [2025 年官方投稿须知](https://www.krics.sdu.edu.cn/xsqk/tgxz.htm) |
| `jxyyj` | 《教学与研究》 | 逗号分段脚注 | [中国人民大学期刊公开站](http://jxyyj.ruc.edu.cn/) |
| `llsy` | 《理论视野》 | 责任者文末式 | [党建网](http://www.dangjian.cn/) |
| `qs` | 《求是》 | 括号卷次脚注 | [2026-06-15 官方刊文注释实例](https://www.qstheory.cn/20260615/058bd355f7fc4e8db56bb102d31d8e16/c.html) |
| `shzyyj` | 《社会主义研究》 | 责任者文末式 | [华中师范大学期刊公开站](https://socialismstudies.ccnu.edu.cn/) |
| `sxlljy` | 《思想理论教育》 | 责任者文末式 | [上海交通大学期刊公开站](https://sllj.sjtu.edu.cn/) |
| `sxzzjyyj` | 《思想政治教育研究》 | 责任者文末式 | [期刊公开站](http://szyj.cbpt.cnki.net/) |
| `mzddxpllyj` | 《毛泽东邓小平理论研究》 | 责任者文末式 | [上海社会科学院](https://www.sass.org.cn/) |
| `mzyj` | 《毛泽东研究》 | 冒号卷次文末式 | [中央党史和文献研究院](https://www.dswxyjy.org.cn/) |
| `zzjjxpl` | 《政治经济学评论》 | 括号卷次脚注 | [官方注释体例](https://crpe.ruc.edu.cn/CN/column/item14.shtml) |
| `ddjjyj` | 《当代经济研究》 | 冒号卷次文末式 | [国家哲学社会科学文献中心](https://www.ncpssd.cn/journal/details?gch=97946X&langType=1&nav=1) |
| `jjzh` | 《经济纵横》 | 冒号卷次文末式 | [官方投稿须知](https://jjzh.cbpt.cnki.net/EditorB2N/PromptPageInfo.aspx?c=1&t=v) |
| `jjxj` | 《经济学家》 | 责任者文末式 | [2025 年投稿说明](https://eshukan.com/displayj.aspx?jid=4378) |
| `jjllyjjgl` | 《经济理论与经济管理》 | 作者—年份式 | [中国人民大学期刊公开站](http://jjll.ruc.edu.cn/) |

## 兼容与审批

- 旧键 `gb2025` / `gb2015` / `zgshkx` / `mkszyj` 保持不变；旧的 `citation` 单值字段仍返回。
- 「中特研究」只作为别名指向 `zgtsshzyyj`，界面仅显示正式刊名《中国特色社会主义研究》。
- A81、D6、G641 等学科代码保存在注册表与后台审计信息中，不进入前台下拉框文案。
- GB/T 7714—2025 保持独立审批闸门。未审批时前台 27 个格式，审批后 28 个格式。
- 自动识别只把文本归入可靠的格式族。多刊体例完全相同时保持低置信度，由用户确认具体期刊键。
