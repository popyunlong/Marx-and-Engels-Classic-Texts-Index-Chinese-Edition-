# 用户荐书审核与永久入库

`user_recommended` 的历史 `public_from` / `public_until` 保留在原不可变数据版本中，仅供审计。有效公开资格统一由 `app._book_is_public` 判断：荐书不再到期，`available: false` 仍然下架。现有账号、会员和 AI 权限不变。

## 后台与持久任务

后台“用户荐书”提供审核通过、退回原因、失败重试、已完成页数和公开书目入口。文件转存/归档状态与入库状态分开。以原 PDF SHA-256 为唯一任务身份，同文件重复上传、重复审核不重复建书。

入库表和事件表在个人文库数据库中按需建立；历史已发布记录从当前加载的书目关联，原审核人未知时明确记录为 `historical_import`，不编造历史审核日期。

主程序的已激活执行器每次领取一个任务；180 秒租约允许重启后恢复。PDF 下载校验大小和指纹，各页准备结果原子保存。旧租约不能覆盖新执行器进度。失败保留原件和已处理页，不自动发布。

Linux 默认处理目录为数据盘 `/home/data/marx-book-imports/recommendation-processing`；可用 `RECOMMENDATION_WORK_DIR` 覆盖。开始前检查空间并保留至少 2 GiB；正式数据构建沿用更严格的不可变发布空间门禁。优先利用 PDF 文字层；不足时调用已有离线 OCR 环境，不自动下载模型。可分别通过 `RECOMMENDATION_OCR_PYTHON`、`RECOMMENDATION_OCR_MODELS` 指向已配置环境。

## 原页校对与待发布

处理完成进入 `review`，不能直接成为已发布。校对文件包含：

- `source_sha256` 和核实过的 `metadata`（书名、作者、译者、出版地、出版社、年份、版本、荐书归属）。
- `toc`、`reviewed_pdf_pages`、`body_samples`（首中尾）、`bibliography_pages`。
- 需要时填写 `text_corrections`、`printed_pages`、`auxiliary_pages`。每个修改页必须属于已核验原页；未知印刷页码留空，界面使用 PDF 页码。`auxiliary_pages` 支持目录、版权页、封面、原件空白页。

使用正式版本中的 `scripts/review_recommendation.py --id <荐书编号> --review <校对文件> --reviewer <审核人>` 生成 `reviewed-package.json` 并进入 `ready`。该命令重新核验 PDF、连续页序、书目信息、目录目标和样本文字，记录包指纹，但不发布。

正式发布必须由指定协调者从通过门禁的 `main` 快进 `production`，从干净且已推送的 `production` 调用 `deploy/release.ps1`。数据使用 `config/book_import_batch.json` 和现有 `scripts/book_import_deploy.py`，保留旧正文行、目录和研究任务兼容关系。禁止通过旧自动入库发布器修改在线数据。

只有主程序指向该版本，且正式发布流水最后确认 `promote`，执行器才把对应 `ready` 记录转为 `published`。预热候选、发布失败和未完成切换都不能提前标记。回退后后台按照实际在线书目显示“等待重新发布”，历史事件保留。

## 2026-10-14 批次

`config/recommendation_review_20261014.json` 是原件和校对清单，不是自动发布绑定。两本原件及处理包保存在该清单指定的服务器数据目录；不把正文和 PDF 提交到代码仓库。

1. 第一版只发布永久规则及后台流程，恢复原有 12 册，逐册检查阅读、范围选择、引文检索、目录和 AI。
2. 第一版验收后，登记荐书 10、14 的审核及校对记录，复用本批已核验页面，避免重复识别。将两本 `ready` 包绑定到新的 `book_import_batch.json`，`expected_parent_release` 必须填写第一版实际发布身份；包指纹须与清单一致。
3. 第二版按现有不可变数据机制追加两册；逐册首中尾各在全部著作、用户荐书及单书范围验证准确页码。新目录逐项验证，旧目录首中末抽查。AI 单书及荐书分组引用必须能打开实际正文，无证据时不得产生引文。
4. 完成桌面、手机、AI 实际对话、引文导出、发布失败和规定回退演练，以及仓库所有发布门禁后才能验收上线。回退只使用 `deploy/rollback_release.ps1`，不可手改在线来源或代理上游。

本次准备核验了《技术批判理论》306 页、89 个目录入口，以及《历史与阶级意识》474 页、15 个目录入口。原件包含外文版本页码与中文印刷页码，不以目录数字推算正文页码。稀疏页已检查；空白页为原件空白，目录页另行标记。源数据准备完成不等于线上验收完成。
