# -*- coding: utf-8 -*-
"""把扫描卷（邓小平文选第1、2卷 / 胡锦涛文选全三卷）的 OCR 正文就地注入 corpus.sqlite。

正文取自本地 OCR sidecar（scripts/_ocr_scan_volume.py 生成的 data/*_ocr.jsonl，
服务器无 OCR 依赖，上传 sidecar 后在服务器读取注入——同毛文集第6卷模式）。

印刷页码：从 OCR 文本的页眉/页脚独立数字行检测，再取「pdf页−印刷页」偏移量的
众数推出全卷常量偏移（扫描件一页一面、偏移恒定，对 OCR 误读免疫）；若众数
覆盖率不足则回退为逐页检测＋fill_missing_printed_pages 顺序补全。

目录：
- 邓1、邓2 无 PDF 书签，由 OCR 正文「篇名（一九××年…日）」识别篇首页推导
  （best-effort，限正文页、去重相邻同名）——同 build_maowenji_vol6 思路。
- 胡选三卷书签目录已由 build_toc 写入，此处仅回填 toc_entries.printed_page。

只 DELETE/INSERT 指定 book+volume 的行，其余书库与卷不动；完成重算 sha256。
须在 build_textbook_index + build_toc 之后运行（覆盖扫描卷的空行）。

用法：python scripts/build_scan_volumes.py [--only dengxuan_vol1 ...]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from build_index import BUILD_DB_PATH, fill_missing_printed_pages, normalize  # noqa: E402

HASH_PATH = BUILD_DB_PATH.with_suffix(BUILD_DB_PATH.suffix + ".sha256")

VOLUMES = [
    {
        "id": "dengxuan_vol1",
        "book": "邓小平文选",
        "volume": 1,
        "source_file": "pdfs/邓小平文选/邓小平文选（第1卷）1994年版.pdf",
        "sidecar": "data/dengxuan_vol1_ocr.jsonl",
        "toc": "detect",
    },
    {
        "id": "dengxuan_vol2",
        "book": "邓小平文选",
        "volume": 2,
        "source_file": "pdfs/邓小平文选/邓小平文选（第2卷）1994年版.pdf",
        "sidecar": "data/dengxuan_vol2_ocr.jsonl",
        "toc": "detect",
    },
    {
        "id": "huxuan_vol1",
        "book": "胡锦涛文选",
        "volume": 1,
        "source_file": "pdfs/胡锦涛文选/胡锦涛文选（第一卷）_B_01016717_001.pdf",
        "sidecar": "data/huxuan_vol1_ocr.jsonl",
        "toc": "refresh_printed",
    },
    {
        "id": "huxuan_vol2",
        "book": "胡锦涛文选",
        "volume": 2,
        "source_file": "pdfs/胡锦涛文选/胡锦涛文选（第二卷）_B_01016720_001.pdf",
        "sidecar": "data/huxuan_vol2_ocr.jsonl",
        "toc": "refresh_printed",
    },
    {
        "id": "huxuan_vol3",
        "book": "胡锦涛文选",
        "volume": 3,
        "source_file": "pdfs/胡锦涛文选/胡锦涛文选（第三卷）_B_01016724_001.pdf",
        "sidecar": "data/huxuan_vol3_ocr.jsonl",
        "toc": "refresh_printed",
    },
    {
        "id": "zgl_vol3",
        "book": "治国理政",
        "volume": 3,
        "source_file": "pdfs/《治国理政》/《治国理政》第三卷.pdf",
        "sidecar": "data/zgl_vol3_ocr.jsonl",
        "toc": "detect",
    },
    # 卷2 扫描版（2022-09 版本）：书签为「页码书签」（1..569 → pdf−19），无篇章书签，
    # 目录由 OCR 正文识别 + 印刷目录页补齐；build_toc 会插入数字垃圾目录，本脚本覆盖之。
    {
        "id": "zgl_vol2",
        "book": "治国理政",
        "volume": 2,
        "source_file": "pdfs/《治国理政》/《治国理政》第二卷.pdf",
        "sidecar": "data/zgl_vol2_ocr.jsonl",
        "toc": "detect",
    },
    # 卷4 扫描版：134 条干净篇章书签（build_toc 写入），此处仅回填 printed_page。
    {
        "id": "zgl_vol4",
        "book": "治国理政",
        "volume": 4,
        "source_file": "pdfs/《治国理政》/《治国理政》第四卷.pdf",
        "sidecar": "data/zgl_vol4_ocr.jsonl",
        "toc": "refresh_printed",
    },
    # 《重要文献选编》6 册：上中下=volume 1/2/3。上/中为扫描卷(本地 rapidocr sidecar)，
    # 下册自带文本层(直接抽取为 sidecar)。均按「篇名（年月日）」识别篇章 + 印刷目录补齐。
    {"id": "xuanbian18_v1", "book": "十八大以来重要文献选编", "volume": 1,
     "source_file": "pdfs/十八大以来重要文献选编/十八大以来重要文献选编_906p_扫描_待定卷.pdf",
     "sidecar": "data/xuanbian_18_v906_ocr.jsonl", "toc": "detect"},
    {"id": "xuanbian18_v2", "book": "十八大以来重要文献选编", "volume": 2,
     "source_file": "pdfs/十八大以来重要文献选编/十八大以来重要文献选编_中册_850p_扫描.pdf",
     "sidecar": "data/xuanbian_18_zhong_ocr.jsonl", "toc": "detect"},
    {"id": "xuanbian18_v3", "book": "十八大以来重要文献选编", "volume": 3,
     "source_file": "pdfs/十八大以来重要文献选编/十八大以来重要文献选编_下_857p.pdf",
     "sidecar": "data/xuanbian_18_xia_ocr.jsonl", "toc": "detect"},
    {"id": "xuanbian19_v1", "book": "十九大以来重要文献选编", "volume": 1,
     "source_file": "pdfs/十九大以来重要文献选编/十九大以来重要文献选编_上.pdf",
     "sidecar": "data/xuanbian_19_shang_ocr.jsonl", "toc": "detect"},
    {"id": "xuanbian19_v2", "book": "十九大以来重要文献选编", "volume": 2,
     "source_file": "pdfs/十九大以来重要文献选编/十九大以来重要文献选编_中_layered.pdf",
     "sidecar": "data/xuanbian_19_zhong_ocr.jsonl", "toc": "detect"},
    {"id": "xuanbian19_v3", "book": "十九大以来重要文献选编", "volume": 3,
     "source_file": "pdfs/十九大以来重要文献选编/十九大以来重要文献选编_下_899p.pdf",
     "sidecar": "data/xuanbian_19_xia_ocr.jsonl", "toc": "detect"},
    # 《二十大以来重要文献选编（上）》：分层 PDF（内嵌扫描图 + 出版级文本层），文本层双份嵌入，
    # 由 scripts/_extract_xuanbian20_text.py 去重后抽为 sidecar。章节目录另由
    # scripts/build_xuanbian_toc.py 从印刷目录权威解析（detect 对选编召回低，仅作占位后被覆盖）。
    {"id": "xuanbian20_v1", "book": "二十大以来重要文献选编", "volume": 1,
     "source_file": "pdfs/二十大以来重要文献选编/二十大以来重要文献选编_上.pdf",
     "sidecar": "data/xuanbian_20_shang_ocr.jsonl", "toc": "detect"},
    # 习近平专题文献 5 本扫描学习纲要/概论（volume 一律 1）：正文走 GLM-4V OCR sidecar
    # （scripts/_ocr_xi_thematic.py + 16 页被内容过滤页由 _ocr_xi_fallback.py 本地 rapidocr 兜底）。
    # 目录为「提纲式」（绪论/第X章/一二三/1.2.，无篇名日期），detect 模式不适用，故 toc 一律
    # refresh_printed（仅注入正文＋众数法印刷页码）：经济思想有 78 条 PDF 书签，随后走 build_toc；
    # 其余四本无书签，随后走 scripts/build_xi_thematic_toc.py 从印刷「目录」页解析章节。
    {"id": "xi_econ", "book": "习近平经济思想学习纲要", "volume": 1,
     "source_file": "pdfs/习近平专题文献/习近平经济思想学习纲要.pdf",
     "sidecar": "data/xi_econ_ocr.jsonl", "toc": "refresh_printed"},
    {"id": "xi_eco", "book": "习近平生态文明思想学习纲要", "volume": 1,
     "source_file": "pdfs/习近平专题文献/习近平生态文明思想学习纲要.pdf",
     "sidecar": "data/xi_eco_ocr.jsonl", "toc": "refresh_printed"},
    {"id": "xi_dangjian", "book": "习近平总书记关于党的建设的重要思想概论", "volume": 1,
     "source_file": "pdfs/习近平专题文献/习近平总书记关于党的建设的重要思想概论.pdf",
     "sidecar": "data/xi_dangjian_ocr.jsonl", "toc": "refresh_printed"},
    {"id": "xi_culture", "book": "习近平文化思想学习纲要", "volume": 1,
     "source_file": "pdfs/习近平专题文献/习近平文化思想学习纲要.pdf",
     "sidecar": "data/xi_culture_ocr.jsonl", "toc": "refresh_printed"},
    {"id": "xi_fazhi", "book": "习近平法治思想学习纲要", "volume": 1,
     "source_file": "pdfs/习近平专题文献/习近平法治思想学习纲要.pdf",
     "sidecar": "data/xi_fazhi_ocr.jsonl", "toc": "refresh_printed"},
]


# 《马克思恩格斯全集（第二版）》(book=全集二版) 的 26 个扫描卷：从 manifest 自动追加注入 spec
# （文本层卷 28/36/42 走 build_textbook_index，不在此列）。GLM-4V 多模态 sidecar 由
# scripts/_ocr_quanji2_vision.py 产出（每卷 data/quanji2_vol<NN>_ocr.jsonl，{pdf_page,text}）。
# 统一用 toc="refresh_printed"：注入 OCR 正文 + 众数法检测印刷页码 + 回填既有目录的 printed_page，
# 不重建目录（保留 build_toc 已写入的洁净书签章节目录；少数垃圾书签卷的章节目录后续另行处理）。
def _append_quanji2_specs() -> None:
    import yaml

    manifest = ROOT / "config" / "manifest.yaml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    text_vols = {28, 36, 42}
    for item in data.get("全集二版", []):
        vol = item.get("volume")
        if not isinstance(vol, int) or vol in text_vols:
            continue
        VOLUMES.append({
            "id": f"quanji2_vol{vol}",
            "book": "全集二版",
            "volume": vol,
            "source_file": item["file"],
            "sidecar": f"data/quanji2_vol{vol:02d}_ocr.jsonl",
            "toc": "refresh_printed",
        })


_append_quanji2_specs()


# 《建党以来重要文献选编》26 册、《建国以来重要文献选编》20 册（book=同名 key）：均为高清扫描件，
# 每册自带干净的 PDF 书签（篇名带日期+责任者），从 manifest 自动追加注入 spec（免硬编码 46 条）。
# RapidOCR 本地 sidecar：data/jianguo_vol<NN>_ocr.jsonl / data/jiandang_vol<NN>_ocr.jsonl（{pdf_page,text}）。
# 统一 toc="refresh_printed"：注入 OCR 正文 + 众数法检测印刷页码 + 回填 build_toc 书签目录的
# printed_page（目录本身由 build_toc 从 PDF 书签生成，此处不重建）。
def _append_xuanbian_series_specs() -> None:
    import yaml

    manifest = ROOT / "config" / "manifest.yaml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    # book 键 → sidecar 前缀。2026-07-30 新增三套（同样是扫描件 + 篇名/年份级 PDF 书签，
    # 走 GLM-4V 转录 + RapidOCR 兜底，与两套选编完全同流程）。
    # 《周恩来年谱》不在此列：它是文本层 PDF，走 build_textbook_index，无需 sidecar 注入。
    # 值 = (sidecar 前缀, 目录模式)。
    # refresh_printed：PDF 书签本身可用（篇名级或年份级），目录由 build_toc 从书签建，
    #   这里只回填 printed_page。两套选编 / 陈云文集(121—150条篇名书签) / 毛泽东年谱(年份书签) 皆此类。
    # detect：书签不可用，须从 OCR 正文识别「篇名（日期）」并用印刷目录页补齐（同邓选卷1/2）。
    #   《周恩来选集》的 442/538 条书签从第 6 条起标题全是纯数字页码（逐页书签，非篇名），
    #   search.py 会正确地把纯数字标题过滤掉 → 走书签只剩「前言」1 条，故必须用 detect。
    series = {
        "建国以来重要文献选编": ("jianguo", "refresh_printed"),
        "建党以来重要文献选编": ("jiandang", "refresh_printed"),
        "周恩来选集": ("zhouxuan", "detect"),
        "陈云文集": ("chenyun", "refresh_printed"),
        "毛泽东年谱": ("maonianpu", "refresh_printed"),
        # 2026-07-31 第二批八套（GLM-4V 转录 + RapidOCR 兜底，sidecar 前缀与
        # scripts/_ocr_xuanbian_glm.py 的 SERIES 键一一对应）。
        # 全部 refresh_printed：本脚本只注入正文＋众数法印刷页码，目录另按三条路线建——
        #   ① 书签可用（陈云年谱/李大钊年谱/李大钊全集卷2-4/列宁年谱卷4）→ scripts/build_toc.py；
        #   ② 年谱无书签（斯大林年谱/邓小平年谱/列宁年谱卷1-3）→ scripts/build_nianpu_toc.py；
        #   ③ 著作集无书签（陈独秀文集/李大钊全集卷1、5）→ scripts/build_printed_toc_dots.py。
        # 这些脚本都在本脚本之后跑，会覆盖各自卷的 toc_entries。
        "斯大林年谱": ("stalin_np", "refresh_printed"),
        "列宁年谱": ("lenin_np", "refresh_printed"),
        "陈云年谱": ("chenyun_np", "refresh_printed"),
        "李大钊年谱": ("lidazhao_np", "refresh_printed"),
        "邓小平年谱": ("deng_np", "refresh_printed"),
        "陈独秀文集": ("chenduxiu", "refresh_printed"),
        "李大钊全集": ("lidazhao_qj", "refresh_printed"),
        "斯大林全集": ("stalin_qj", "refresh_printed"),
        "刘少奇年谱": ("liu_np", "refresh_printed"),
        "刘少奇选集": ("liu_xuan", "refresh_printed"),
        "中共中央文件选集（1921—1949）": ("party_files_1921", "refresh_printed"),
        "中共中央文件选集（1949—1966）": ("party_files_1949", "refresh_printed"),
    }
    for book, (prefix, toc_mode) in series.items():
        for item in data.get(book, []):
            vol = item.get("volume")
            if not isinstance(vol, int):
                continue
            # sidecar 优选 GLM 版（_glm.jsonl）：建国 vol15-20 由 GLM-4V 转录 + RapidOCR
            # 兜底拦截页/复读页；其余册仍是纯 RapidOCR 的 _ocr.jsonl。两者同格式，谁存在用谁。
            glm_side = ROOT / "data" / f"{prefix}_vol{vol:02d}_glm.jsonl"
            side = f"data/{prefix}_vol{vol:02d}_{'glm' if glm_side.exists() else 'ocr'}.jsonl"
            VOLUMES.append({
                "id": f"{prefix}_vol{vol:02d}",
                "book": book,
                "volume": vol,
                "source_file": item["file"],
                "sidecar": side,
                "toc": toc_mode,
                "expected_pages": item.get("page_count"),
                "header_page_numbers": book in {
                    "刘少奇年谱", "刘少奇选集", "中共中央文件选集（1921—1949）",
                    "中共中央文件选集（1949—1966）",
                },
                # 《斯大林全集》1953—1956 繁体排印本的页码印成汉字（「九三」=93），
                # 只有它需要汉字页码判据；其余书库一律阿拉伯数字，开关关闭以免误判。
                "cn_page_numbers": book == "斯大林全集",
            })


_append_xuanbian_series_specs()


# 2026-08-02 第三批：《资本论》3 卷 + 《习近平著作选读》2 卷 + 《习近平党建文选》2 卷
# + 5 种扫描的专题论述摘编/纲要。全部 GLM-4V 转录（sidecar 前缀＝_ocr_xuanbian_glm.py 的
# SERIES 键），从 manifest 自动生成 spec，免硬编码 12 条。
# 目录一律不在这里建（toc="refresh_printed" 只注入正文＋众数法印刷页码），随后由
# scripts/build_newbooks12_toc.py 按三种版式分别解析印刷目录页覆盖之：
#   ① 资本论＝第X篇/第X章/罗马数字节 多级目录；
#   ② 著作选读/党建文选＝「篇名（日期）」篇章目录；
#   ③ 论述摘编/学习纲要＝「一、专题名（页码）」专题级目录。
# 文本层的三部（力戒形式主义 / 生态文明 / 作风建设）走 build_textbook_index，不在此列。
def _append_newbooks12_specs() -> None:
    import yaml

    manifest = ROOT / "config" / "manifest.yaml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    series = {
        "资本论": "ziben",
        "习近平著作选读": "xuandu",
        "习近平党建文选": "dangjian_wx",
        "习近平关于全面依法治国论述摘编": "zb_yifa",
        "习近平关于全面从严治党论述摘编": "zb_congyan",
        "习近平关于科技创新论述摘编": "zb_keji",
        "习近平关于全面深化改革论述摘编": "zb_gaige",
        "习近平外交思想学习纲要": "gy_waijiao",
        "论坚持党对一切工作的领导": "xi_lingdao",
        "论党的宣传思想工作": "xi_xuanchuan",
        "论中国共产党历史": "xi_dangshi",
        "论把握新发展阶段、贯彻新发展理念、构建新发展格局": "xi_xinfazhan",
        "论党的自我革命": "xi_ziwogeming",
        "习近平关于党的群众路线教育实践活动论述摘编": "zb_qunzhong",
        "习近平关于总体国家安全观论述摘编": "zb_anquan",
        "习近平关于网络强国论述摘编": "zb_wangluo",
        "习近平关于社会主义精神文明建设论述摘编": "zb_jingshen",
        "习近平关于树立和践行正确政绩观论述摘编": "zb_zhengji",
    }
    for book, prefix in series.items():
        for item in data.get(book, []) or []:
            vol = item.get("volume")
            if not isinstance(vol, int):
                continue
            VOLUMES.append({
                "id": f"{prefix}_vol{vol:02d}",
                "book": book,
                "volume": vol,
                "source_file": item["file"],
                "sidecar": f"data/{prefix}_vol{vol:02d}_glm.jsonl",
                "toc": "refresh_printed",
                # 这批书的版心页码常与页眉排在同一行（「专题名 7」/「62 书名」）。
                # 只认独立数字行的话检出点会稀疏一个数量级，而这批扫描件**有漏页**
                # （实测《依法治国》缺印刷第 68、70 页，《党建文选》第二卷缺第 157、158 页），
                # 稀疏检出点在漏页处两侧无法各自锚定，整段只能留空。全族打开；
                # 该判据是「独立数字行找不到时」才试的兜底，且下游还有
                # 「偏移支持度 ≥3 才采信」把关，误检进不了库。
                "header_page_numbers": True,
            })


_append_newbooks12_specs()


# 黑格尔著作集 8 部 16 册：mimo-v2.5 严格 JSON Schema 逐页转录，目录随后由
# scripts/build_hegel_toc.py 从同一份保真转录中的印刷目录构建。这里仅注入正文、
# 归一化检索文本和印刷页码，并回填可能存在的旧目录（正式目录会被后续脚本原子覆盖）。
def _append_hegel_specs() -> None:
    import yaml

    catalog = ROOT / "config" / "hegel_volumes.yaml"
    data = yaml.safe_load(catalog.read_text(encoding="utf-8")) or {}
    for item in data.get("volumes") or []:
        volume_id = str(item.get("id") or "").strip()
        volume = item.get("volume")
        if not volume_id or not isinstance(volume, int):
            continue
        VOLUMES.append({
            "id": volume_id,
            "book": str(item["book"]),
            "volume": volume,
            "source_file": str(item["file"]),
            "sidecar": f"data/hegel/{volume_id}.jsonl",
            "toc": "refresh_printed",
        })


_append_hegel_specs()

# 篇首页特征：开头(去页码后)即「篇名（一九××年…日/月）」。日期可为时间段
# （如「一九四一年四月十五日—六月十日」），尾部放宽到 12 字。
_DATE = re.compile(
    r"[（(]\s*[一二三四五六七八九十百零〇○二〇\d]{2,}\s*年[^）)]{0,16}?[日月][^）)]{0,12}[)）]"
)
_LEAD_NUM = re.compile(r"^[\s\d]+")
_PAGE_NUM_LINE = re.compile(r"^\s*(\d{1,4})\s*$")
# 标题里不应出现的字符：句中标点（正文特征）与目录页特征（省略号/页码连线）
_TITLE_BAD = set("。！？；，、⋯…·.0123456789—–-")


def load_sidecar(path: Path) -> dict[int, str]:
    if not path.exists():
        raise SystemExit(f"缺少 OCR sidecar：{path}（请先本地跑 scripts/_ocr_scan_volume.py 并上传）")
    texts: dict[int, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        o = json.loads(line)
        texts[int(o["pdf_page"])] = o.get("text") or ""
    return texts


# ---- 汉字数字页码（《斯大林全集》1953—1956 繁体排印本）----
# 这套书的页码印成汉字且是**逐位写**的：「九三」=93、「三六〇」=360，不是「九十三」那种
# 十百进位写法。OCR 还常把它和页眉并成一行（「斯大林全集第九卷九二」）。不认它的话，
# 全书 4883 页的 printed_page 几乎全为空，引文只能标「此为PDF页码」——这套书的引文价值
# 就废了一半。故单加一条判据，并用 spec 开关限定只对本书系生效，不动其它书库。
_CN_DIGITS = {"〇": "0", "○": "0", "O": "0", "o": "0", "零": "0", "一": "1", "二": "2",
              "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}
_CN_NUM_TAIL = re.compile(r"([〇○Oo零一二三四五六七八九]{1,4})\s*$")
# 行内含句读 = 正文，不可能是页眉页脚
_CN_LINE_BAD = set("。！？；，、：「」『』（）()《》")


def detect_page_number_cn(text: str, page_count: int) -> int | None:
    """从页眉/页脚解析**汉字数字**页码。整行只有汉字数字，或短行以汉字数字收尾。"""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for l in lines[:3] + lines[-3:]:
        if len(l) > 20 or any(ch in _CN_LINE_BAD for ch in l):
            continue
        m = _CN_NUM_TAIL.search(l)
        if not m:
            continue
        head = l[: m.start()].strip()
        # 要么整行就是数字，要么前缀是页眉（书名/卷次那类短词，且不含阿拉伯数字）
        if head and (len(head) > 12 or any(ch.isdigit() for ch in head)):
            continue
        v = int("".join(_CN_DIGITS[ch] for ch in m.group(1)))
        if 1 <= v <= page_count + 60:
            return v
    return None


# ---- 页眉行内页码（习近平专题论述摘编一族）----
# 这一族书的版心 folio 不单独成行，而是**排在页眉那一行里**：
#   单页（recto）「一、依法治国是……的本质要求和重要保障 7」  ← 专题名 + 页码
#   双页（verso）「62 习近平关于全面依法治国论述摘编」        ← 页码 + 书名
# 只认独立数字行的话，这类书 130—243 页里只有零星几十页能检出，其余全靠邻居外推；
# 而这批扫描件**有漏页**（实测《依法治国》缺印刷第 68、70 页），外推在漏页处必然错，
# 于是整段只能留空 —— 46/134 页没有页码，引文一半标不出页。改为直接读页眉里的数字后，
# 检出点密度上一个数量级，漏页处也能各自锚定，两侧偏移各自正确。
# 安全边界：只看首尾各 2 行、行长 ≤44、除数字外须有 ≥4 个汉字、数字必须贴在行首或行尾。
# 误检仍会被下游「偏移支持度 ≥3 才采信」的判据滤掉。
_HEAD_NUM_TAIL = re.compile(r"^(?P<txt>.*?[^\d\s])\s*(?P<num>\d{1,4})$")
# Require a real separator after a leading folio, but allow the running title
# itself to begin with a year (for example ``2 1841年初版序言``).  The
# existing short-line and >=4 Han-character checks below remain in force.
_HEAD_NUM_LEAD = re.compile(r"^(?P<num>\d{1,4})\s+(?P<txt>\S.*)$")


def detect_page_number_header(text: str, page_count: int) -> int | None:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for l in lines[:2] + lines[-2:]:
        if len(l) > 44:
            continue
        for rx in (_HEAD_NUM_TAIL, _HEAD_NUM_LEAD):
            m = rx.match(l)
            if not m:
                continue
            if len(re.findall(r"[一-鿿]", m.group("txt"))) < 4:
                continue
            v = int(m.group("num"))
            if 1 <= v <= page_count + 60:
                return v
    return None


def detect_page_number(text: str, page_count: int, cn_numerals: bool = False,
                       header_numerals: bool = False) -> int | None:
    """从页眉/页脚找独立数字行（前 3 行与后 3 行），返回印刷页码。
    cn_numerals=True 时，阿拉伯数字找不到再试汉字数字（仅繁体旧版书需要）。
    header_numerals=True 时，再试「页眉行内数字」（习近平专题论述摘编一族）。"""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return None
    for l in lines[:3] + lines[-3:]:
        m = _PAGE_NUM_LINE.match(l)
        if m:
            v = int(m.group(1))
            if 1 <= v <= page_count + 60:
                return v
    if cn_numerals:
        return detect_page_number_cn(text, page_count)
    if header_numerals:
        return detect_page_number_header(text, page_count)
    return None


def detect_chapter(text: str) -> str | None:
    """若该页为篇首（开头即 标题+日期），返回篇名；否则 None。"""
    flat = re.sub(r"\s+", "", text)[:80]
    flat = _LEAD_NUM.sub("", flat)  # 去掉行首印刷页码
    m = _DATE.search(flat)
    if not m or m.start() < 2 or m.start() > 44:
        return None
    title = flat[: m.start()].strip()
    if any(ch in _TITLE_BAD for ch in title):
        return None
    if 2 <= len(title) <= 44:
        return title
    return None


def build_rows(spec: dict, texts: dict[int, str]) -> tuple[list[tuple], str]:
    """生成该卷 pages 行（含印刷页码）。返回 (rows, 页码策略说明)。"""
    book, vol, source_file = spec["book"], spec["volume"], spec["source_file"]
    n = max(texts) if texts else 0

    cn_numerals = bool(spec.get("cn_page_numbers"))
    header_numerals = bool(spec.get("header_page_numbers"))
    detections: list[tuple[int, int]] = []
    for i in range(1, n + 1):
        v = detect_page_number(texts.get(i, ""), n, cn_numerals, header_numerals)
        if v is not None:
            detections.append((i, v))

    # 邻居一致性过滤：偏移与**前后两个检测点都不同**的孤立检测点一律丢弃。
    # 起因：《党建文选》第二卷因扫描漏页而存在 10 和 8 两段偏移，两个偏移在全卷都
    # 「有 ≥3 页支持」，于是 pdf346 把印刷的 338 误认成 336（恰好落在另一段的偏移上）
    # 而通过了支持度判据 —— 重号 336、引文整整错 2 页。真正的分段切换处，新偏移会被
    # **后一个**检测点认同，故不会被这条规则误伤；孤立的误读则前后都不认，稳稳被剔掉。
    if len(detections) >= 3:
        kept: list[tuple[int, int]] = []
        for k, (i, v) in enumerate(detections):
            off = i - v
            prev_off = detections[k - 1][0] - detections[k - 1][1] if k > 0 else None
            next_off = detections[k + 1][0] - detections[k + 1][1] if k + 1 < len(detections) else None
            if prev_off is not None and next_off is not None and off != prev_off and off != next_off:
                continue
            kept.append((i, v))
        dropped = len(detections) - len(kept)
        detections = kept
    else:
        dropped = 0

    det_map = dict(detections)
    offsets = Counter(i - v for i, v in detections)
    # 判定「是否恒定偏移」时，只把**得到 ≥3 页支持的偏移**计入分母。
    # 起因：《斯大林全集》页码印成汉字，OCR 会系统性丢位——三位数「一二三」读成「二三」
    # 就产生一个 +100 的假偏移，两位丢一位产生 +10。这些假偏移每个只占几页却把真偏移的
    # 占比从 68% 稀释到 54%，卡在 60% 门槛之下，整卷退化成逐页模式，于是误检直接被写成
    # 页码（实测每卷 33—56 个重号页码——错的页码比没有页码更糟）。孤立检测点本来就是
    # 噪声，不该参与「主偏移够不够权威」的表决。
    trusted_n = sum(c for c in offsets.values() if c >= 3)
    strategy = "per-page"
    const_offset: int | None = None
    if detections:
        top = offsets.most_common(2)
        best, cnt = top[0]
        runner = top[1][1] if len(top) > 1 else 0
        base = trusted_n or len(detections)
        # 「一卷一个恒定偏移」对一页一面的扫描件通常成立，但**并非总是**：《列宁年谱》卷二
        # 实测前段偏移 +4、后段 −3（印刷目录锚点可证：1905年印7在 pdf11，1907年印385在
        # pdf382），中间还夹着 −1 的过渡段——多半是扫描时漏掉了几页。此时众数占 65%
        # 仍能过原来的 60% 门槛，于是全卷被按 −3 覆盖，前 300 页引文页码整体错 7 页。
        # 故加一条：若第二偏移也有可观支持（≥15% 检测点），判定为「分段偏移」，
        # 不用常量，改走逐页检测＋邻居偏移补全——后者对分段偏移天然正确，
        # 且在段边界老实地留 None，不会编造页码。
        # 汉字页码书的「次偏移」多半不是真分段，而是同一个丢位假象：三位页码丢首位
        # （「一二三」→「二三」）会让偏移整整多出 100。实测《斯大林全集》卷一是 19 vs 119、
        # 卷六是 8 vs 108，差值恰为 100 的整数倍；而真正的分段（扫描漏页）差值是小数目
        # （列宁年谱卷二 +4 vs −3 差 7、本书卷五 8 vs 9 差 1）。故仅对汉字页码书排除这类
        # 「差值为 100 的整数倍」的次偏移，别的书库判据不变。
        runner_off = top[1][0] if len(top) > 1 else None
        if (cn_numerals and runner_off is not None
                and abs(runner_off - best) >= 100 and (runner_off - best) % 100 == 0):
            runner = 0
        multi_segment = runner >= max(10, base * 0.15)
        if cnt >= max(20, base * 0.6) and not multi_segment:
            const_offset = best
            strategy = f"const-offset={best}（{cnt}/{base} 可信检测点吻合，共 {len(detections)} 点）"
        elif multi_segment:
            strategy = (f"分段偏移（主 {best}×{cnt} / 次 {top[1][0]}×{runner}，"
                        f"可信 {base} / 共 {len(detections)} 点）→ 逐页+邻居补全")

    rows: list[tuple] = []
    for i in range(1, n + 1):
        raw = texts.get(i, "")
        if const_offset is not None:
            printed = str(i - const_offset) if i - const_offset >= 1 else None
        else:
            # 取**过滤后**的检测点，不要在这里重新 detect 一遍：重新检测会把上面刚被
            # 「前后都不认」判掉的孤立误读原样放回来（《党建文选》卷二 pdf346 把印刷的
            # 338 读成 336，因本卷同时存在 8 和 10 两段偏移，仅凭支持度判据拦不住，
            # 于是重号 336、引文错 2 页）。
            v = det_map.get(i)
            # 只采信「偏移得到 ≥3 页支持」的检测；孤立点多是 OCR 误读（汉字页码丢位尤甚），
            # 写进去就是一个错页码，不如留空交给下面的邻居偏移补全。
            if v is not None and offsets[i - v] < 3:
                v = None
            printed = str(v) if v is not None else None
        rows.append((book, vol, source_file, i, printed, raw, normalize(raw)))

    if const_offset is None:
        # 邻居偏移补全：检测点缺失页取前/后最近检测点的偏移，两侧一致才回填；
        # 不一致（无页码插页带，偏移在此跳变）保持 None。首/尾延伸段用单侧偏移。
        # 比 fill_missing 的顺序补全更稳：开篇未检出段、卷末索引段都能按邻段偏移补齐。
        # 只用「偏移获得 ≥3 个检测点支持」的可信锚点（前置目录页自带页码会产生杂散偏移）
        trusted = [(i, v) for i, v in detections if offsets[i - v] >= 3]
        det_pages = [i for i, _v in trusted]
        det_off = {i: i - v for i, v in trusted}
        import bisect
        filled = 0
        for idx in range(len(rows)):
            b, v_, sf, i, printed, raw, norm = rows[idx]
            if printed is not None or not norm:
                continue
            k = bisect.bisect_left(det_pages, i)
            off_prev = det_off[det_pages[k - 1]] if k > 0 else None
            off_next = det_off[det_pages[k]] if k < len(det_pages) else None
            off = None
            if off_prev is not None and off_next is not None:
                if off_prev == off_next:
                    off = off_prev
            else:
                off = off_prev if off_prev is not None else off_next
            if off is not None and i - off >= 1:
                rows[idx] = (b, v_, sf, i, str(i - off), raw, norm)
                filled += 1
        strategy += f"，邻居偏移补全 {filled} 页"
    if dropped:
        strategy += f"（另剔除 {dropped} 个前后都不认的孤立误读）"
    return rows, strategy


# 篇名后的脚注/题注符号（原书在篇名后加 * 表示有题解），OCR 会把它当成标题的一部分。
# 只剥标题「尾部」，不动标题内部的标点，故不会伤到《说真话，鼓真劲》这类含逗号的真篇名。
_TITLE_TAIL_NOISE = re.compile(r"[*＊※'’‘\"”`·\s]+$")


def clean_chapter_title(title: str, book: str = "") -> str:
    """清理从正文/印刷目录识别出的篇名：剥尾部脚注符号 + 剥被带进来的页眉。

    页眉那一路：正文识别是按「页首即篇名（日期）」判定的，而扫描页的页眉恰在页首，
    于是《周恩来选集》有 5 条标题被识别成「周恩来选集下卷不中断铁路轮船交通」。
    用 spec 里的 book 键去剥（不写死书名，换书自动适配）。
    """
    t = str(title or "").strip()
    if book:
        t = re.sub(rf"^{re.escape(book)}\s*[上中下]?\s*卷?\s*", "", t)
    t = _TITLE_TAIL_NOISE.sub("", t)
    return t.strip()


def detect_toc(rows: list[tuple]) -> list[dict]:
    """从正文页识别篇章（标题+日期开头的页）。"""
    toc: list[dict] = []
    for _book, _vol, _sf, pdf_page, printed, raw, _norm in rows:
        if not printed:  # 仅正文页（有印刷页码）参与
            continue
        title = detect_chapter(raw)
        if title:
            title = clean_chapter_title(title, _book)
        if title and len(title) >= 2:
            toc.append({"title": title, "pdf_page": pdf_page, "printed": printed})
    toc.sort(key=lambda e: e["pdf_page"])
    dedup: list[dict] = []
    for e in toc:
        if dedup and dedup[-1]["title"] == e["title"]:
            continue
        dedup.append(e)
    return dedup


# ---- 印刷目录页解析（邓1/邓2 等无书签卷的目录权威来源）----
# 目录页版式：标题（可跨行，行尾常带点引线/页码区间），下一行为「（一九××年…日）」日期括注。
_TOC_TRAIL = re.compile(r"[·⋯…\.、：:\s\d—一–\-]+$")
_TOC_HEADER = re.compile(r"^(目录|邓小平文选|习近平谈治国理政|治国理政|第[一二三四五]卷)$")
_TOC_SECTION = re.compile(r"^[一二三四五六七八九十]{1,3}、")


def _flat_norm(s: str) -> str:
    return normalize(re.sub(r"\s+", "", s))


def parse_printed_toc(texts: dict[int, str], first_body_pdf: int) -> list[str]:
    """从前置目录页解析篇名列表（按出现顺序）。只取标题，不信目录页里的页码
    （OCR 常把「33—44」并成「3344」），定位交给正文匹配。"""
    start = None
    for i in range(1, min(first_body_pdf, 30)):
        if any(l.strip() == "目录" for l in (texts.get(i) or "").splitlines()):
            start = i
            break
    if start is None:
        # 回退：目录页特征 = 单页含 ≥2 条「日期括注独行」（扫描 OCR 偶尔丢失「目录」标题行）
        for i in range(1, min(first_body_pdf, 30)):
            lines = [l.strip() for l in (texts.get(i) or "").splitlines() if l.strip()]
            ndates = sum(1 for l in lines
                         if _DATE.search(re.sub(r"\s+", "", l))
                         and len(re.sub(r"[（(].*?[)）]", "", re.sub(r"\s+", "", l)).strip()) <= 2)
            if ndates >= 2:
                start = i
                break
    if start is None:
        return []
    # 目录页的页眉行（如「2 周恩来选集 上卷」「目录 3」）既非日期行、也不匹配 _TOC_HEADER，
    # 若不剔除会被当成标题片段拼到下一条篇名前面。这里数据驱动识别：去掉数字与空白后
    # 在多页重复出现的短行即页眉（不写死书名，换书自动适配）。
    head_count: dict[str, int] = {}
    for i in range(start, first_body_pdf):
        for line in (texts.get(i) or "").splitlines():
            key = re.sub(r"[\s\d]+", "", line)
            if 2 <= len(key) <= 20:
                head_count[key] = head_count.get(key, 0) + 1
    running_heads = {k for k, n in head_count.items() if n >= 2}

    titles: list[str] = []
    acc: list[str] = []
    for i in range(start, first_body_pdf):
        for line in (texts.get(i) or "").splitlines():
            l = line.strip()
            if not l or _TOC_HEADER.match(l):
                continue
            if re.sub(r"[\s\d]+", "", l) in running_heads:
                continue
            if _TOC_SECTION.match(l) and not acc:
                continue  # 专题头（治国理政），不计入篇名
            flat = re.sub(r"\s+", "", l)
            m_date = _DATE.search(flat)
            if m_date:
                bare = re.sub(r"[（(].*?[)）]", "", flat)
                if len(bare.strip()) <= 2:
                    # 两行式版式（邓选/治国理政）：本行只有日期括注 → 收束前面累积的标题行
                    title = _TOC_TRAIL.sub("", "".join(acc)).strip()
                    if 2 <= len(title) <= 50:
                        titles.append(title)
                    acc = []
                    continue
                # 单行式版式（《周恩来选集》印刷目录）：「篇名(一九××年×月×日)……起页-止页」
                # 篇名、日期、点引线、页码全在同一行。取日期括注之前的部分作篇名；若此前还累积
                # 了折行的标题片段，一并接上（长篇名会被排版折成两行，后半行才带日期）。
                head = _TOC_TRAIL.sub("", ("".join(acc) + flat[:m_date.start()])).strip()
                acc = []
                if 2 <= len(head) <= 50:
                    titles.append(head)
                continue
            acc.append(l)
    return [t for t in titles if t != "注释"]


def merge_printed_toc(toc: list[dict], texts: dict[int, str], rows: list[tuple],
                      first_body_pdf: int) -> tuple[list[dict], int]:
    """用印刷目录补齐正文检测漏掉的篇章。返回 (合并后的目录, 补齐条数)。"""
    import difflib

    book_key = rows[0][0] if rows else ""
    # 印刷目录解析出的篇名同样要剥脚注符号/页眉，否则补齐进来的条目与正文识别的条目
    # 写法不一致（一个带 *、一个不带），既难看又会让下面的 match() 判重失效。
    parsed = [clean_chapter_title(t, book_key) for t in parse_printed_toc(texts, first_body_pdf)]
    parsed = [t for t in parsed if len(t) >= 2]
    if not parsed:
        return toc, 0

    def match(a: str, b: str) -> bool:
        na, nb = _flat_norm(a), _flat_norm(b)
        if not na or not nb:
            return False
        if na == nb or na in nb or nb in na:
            return True
        return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.75

    max_page = max((r[3] for r in rows), default=0)
    merged: list[dict] = []
    added = 0
    det = list(toc)  # 已按 pdf_page 升序
    for k, title in enumerate(parsed):
        hit = next((e for e in det if match(title, e["title"])), None)
        if hit is not None:
            det.remove(hit)
            merged.append(hit)
            continue
        # 未检出 → 在相邻已定位条目之间的页窗口内按「页首含标题」搜索
        lo = merged[-1]["pdf_page"] + 1 if merged else first_body_pdf
        nxt = next((e for e in det if any(match(t, e["title"]) for t in parsed[k + 1:])), None)
        hi = nxt["pdf_page"] - 1 if nxt else max_page
        key = _flat_norm(title)[:10]
        found = None
        for _b, _v, _sf, p, pr, raw, _n in rows:
            if p < lo or p > hi or not pr:
                continue
            head = _flat_norm(re.sub(r"\s+", "", raw)[:70])
            if key and key in head[:44]:
                found = (p, pr)
                break
        if found:
            merged.append({"title": title, "pdf_page": found[0], "printed": found[1]})
            added += 1
    # 检出但不在印刷目录里的（目录页 OCR 烂掉等）也保留，按页序插回
    merged.extend(det)
    merged.sort(key=lambda e: e["pdf_page"])
    return merged, added


def update_hash(db_path: Path = BUILD_DB_PATH) -> None:
    digest = hashlib.sha256()
    with db_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    db_path.with_suffix(db_path.suffix + ".sha256").write_text(digest.hexdigest() + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="注入扫描卷 OCR 正文与页码/目录。")
    ap.add_argument("--only", nargs="*", help="只处理指定卷 id（默认全部五卷）")
    ap.add_argument("--db", type=Path, default=BUILD_DB_PATH,
                    help="候选数据库路径；默认 data/corpus.sqlite")
    args = ap.parse_args()
    specs = [s for s in VOLUMES if not args.only or s["id"] in args.only]
    if not specs:
        raise SystemExit("没有匹配的卷。可选：" + ", ".join(s["id"] for s in VOLUMES))

    conn = sqlite3.connect(str(args.db))
    try:
        for spec in specs:
            texts = load_sidecar(ROOT / spec["sidecar"])
            # 完整性校验的页数以 **PDF 自身** 为准。
            # 两个理由：① 首次建库时 pages 表还没有这一卷，从库里取到的是 None，完整性
            # 无从校验，而 build_rows 用 max(texts) 定页数、缺页取 ""，sidecar 中间的漏页
            # 会被**静默写成空白页**（正文缺一段却毫无告警，最难事后发现）；② 卷的页数
            # 可能合法地变化——《李大钊年谱》原是上下册合订的单一 PDF，因印刷页码重号
            # 拆成两个文件后卷一从 1829 页变 957 页，若拿库里的旧值当权威就会永远拦着不让重建。
            # PDF 一定在（source_file 就是阅读器用的那个文件），读页数很廉价。
            expected = spec.get("expected_pages")
            if not expected:
                try:
                    import fitz  # noqa: PLC0415
                    with fitz.open(ROOT / spec["source_file"]) as _doc:
                        expected = _doc.page_count
                except Exception as exc:  # noqa: BLE001
                    print(f"[{spec['id']}] 提示：无法读取 PDF 页数（{exc}），改用库中既有页数校验")
                    expected = conn.execute(
                        "SELECT MAX(pdf_page) FROM pages WHERE book=? AND volume=?",
                        (spec["book"], spec["volume"]),
                    ).fetchone()[0]
            if expected and (not texts or max(texts) < expected or len(texts) < expected):
                missing = sorted(set(range(1, (expected or 0) + 1)) - set(texts))
                raise SystemExit(
                    f"[{spec['id']}] OCR sidecar 不完整：{len(texts)}/{expected} 页"
                    f"（max={max(texts) if texts else 0}，缺 {len(missing)} 页"
                    f"{'：' + ','.join(map(str, missing[:20])) + ('…' if len(missing) > 20 else '') if missing else ''}）。"
                    f"请先补跑 OCR 再注入。"
                )
            rows, strategy = build_rows(spec, texts)
            nonempty = sum(1 for r in rows if r[6])
            conn.execute(
                "DELETE FROM pages WHERE book=? AND volume=?", (spec["book"], spec["volume"])
            )
            conn.executemany(
                "INSERT INTO pages (book, volume, source_file, pdf_page, printed_page, raw_text, normalized_text) "
                "VALUES (?,?,?,?,?,?,?)",
                rows,
            )
            msg = f"[{spec['id']}] pages {len(rows)} 行（非空 {nonempty}），页码策略：{strategy}"

            if spec["toc"] == "detect":
                toc = detect_toc(rows)
                first_body = next((r[3] for r in rows if r[4]), 1)
                toc, added = merge_printed_toc(toc, texts, rows, first_body)
                conn.execute(
                    "DELETE FROM toc_entries WHERE book=? AND volume=?",
                    (spec["book"], spec["volume"]),
                )
                conn.executemany(
                    "INSERT INTO toc_entries (book, volume, source_file, title, pdf_page, printed_page, level, kind, sort_order) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    [
                        (spec["book"], spec["volume"], spec["source_file"], e["title"],
                         e["pdf_page"], e["printed"], 1, "body", k + 1)
                        for k, e in enumerate(toc)
                    ],
                )
                msg += f"；识别目录 {len(toc)} 条（印刷目录补齐 {added} 条）"
            elif spec["toc"] == "refresh_printed":
                # 书名页书签会把全名带进界面目录（治国理政文案分流要求），删除之；不动正文篇名。
                conn.execute(
                    "DELETE FROM toc_entries WHERE book=? AND volume=? AND title LIKE '%习近平谈治国理政%'",
                    (spec["book"], spec["volume"]),
                )
                cur = conn.execute(
                    "UPDATE toc_entries SET printed_page = ("
                    "  SELECT p.printed_page FROM pages p"
                    "  WHERE p.source_file = toc_entries.source_file AND p.pdf_page = toc_entries.pdf_page)"
                    " WHERE book=? AND volume=?",
                    (spec["book"], spec["volume"]),
                )
                msg += f"；目录 printed_page 回填 {cur.rowcount} 条"
            print(msg)
        conn.commit()
    finally:
        conn.close()
    update_hash(args.db)
    print("完成。")


if __name__ == "__main__":
    main()
