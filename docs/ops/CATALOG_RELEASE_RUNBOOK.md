# 目录版本发布与无回退验收

本流程扩展正式应用发布事务，不授权普通开发会话推进 production。指定协调者仍须完成主发布手册的门禁、演练、main 集成及 production 快进。

## 首次迁移必须分两次发布

1. 先发布兼容基础代码，**不添加** `config/catalog_release.json`。此时数据仍保持现网状态，发布元数据记录 `catalog_protocol: 1`，代码具备版本路径、历史目录读取及新回滚检查能力。
2. 验证基础版本后，将已审查基线包的 `{id, sha256}` 写入上述配置文件，提交、通过检查、集成 main，再发布绑定基线的版本。首个目录包必须与发布时共享目录和中文目录完全等价。禁止首次绑定时夹带修复。
3. 基线验收后，独立提交第一批修复包的绑定，再走完整发布流程。后续每批沿当前已上线目录版本构建。每批观察 30 分钟及 24 小时后再扩大。

先发布兼容代码是必要步骤：更早版本不认识带版本的阅读链接，不能作为绑定目录后的直接回滚目标。

## 构建、复核及交付物

- `scripts/snapshot_catalog.py --output <new-artifact-directory>` 先采集五分钟线上时延与磁盘等待基线，再通过短只读事务和非阻塞共享发布锁提取最新目录。数据库事务在网页文件传输前结束；网页每批最多 100 个，服务器使用最低 I/O/CPU 优先级并限速 2 MiB/s，不压缩、不分析、不落地临时副本。采集中每 30 秒检查健康、代表页面、5xx、p95 和磁盘等待；越过门槛立即中止并删除本地半成品。发布锁忙时等待下一窗口，不阻塞发布。压缩和完整校验只在本地进行；不复制用户数据或秘密配置。
- `scripts/catalog_bundle.py build --snapshot <snapshot> --output <new-version-directory> --version <id>` 固化基线。输出目录不能预先存在。
- 第一批使用 `scripts/prepare_catalog_repairs.py`；第二批试点使用 `scripts/prepare_catalog_pilot.py`。两者必须传入正确的 `--parent`、全新 `--work`、`--output` 和 `--version`。
- MEGA² IV/3 使用 `scripts/prepare_mega_iv3.py`，只能排在第二批之后作为独立候选。输入必须是指纹匹配的 1998 年 Text 卷原书 PDF；候选只允许修改 IV/3 目录首页和 9 个正文 HTML 的新增锚点，并保留旧分页链接。Text 卷没有的 Apparat、导论、缩略语和索引不得补成已上线内容；正文独立标题覆盖完成核验前仍标记为待核实。
- 每个版本检查逐来源的前后指纹、确切原书证据及逐文件清单。第一批只允许 3 个来源的 6 条目录记录和 2 个 HTML 文件变化。试点只允许《文集》5 卷的 12 个层级值及 3 个 HTML 文件变化。IV/3 候选重基时，10 个父文件和审定结果文件的前后指纹必须全部逐字节匹配；任一不一致即重新按原书生成和核验，不能沿用旧包。
- `scripts/scan_catalog_links.py --root <version> --output <report> --baseline <previous-report>` 扫描全部镜像，包括不在公开书单中的旧文件。不能将它的统计直接当作公开书目覆盖数；绝对 URL 和应用路由另做 HTTP 检查。
- `scripts/catalog_deploy.py pack --root <version> --archive <artifact.tar.gz>` 打包已经校验的目录版本。大文件保存在独立审计产物目录，不能进入 Git。
- 把绑定写入提交之前，必须审查包中 `catalog.json` 的全部差异与证据。绑定的 SHA-256 是该文件的文件哈希，不是目录 ID 或 TOC 哈希。

## 正式事务

在干净且已推送、与 origin/production 一致的 production 分支，由协调者运行：

```powershell
pwsh -File deploy/release.ps1 -ExpectedLive '<exact-live-release>' -CatalogArchive '<verified-catalog.tar.gz>' -DryRun -KeepArtifact
pwsh -File deploy/release.ps1 -ExpectedLive '<exact-live-release>' -CatalogArchive '<verified-catalog.tar.gz>' -KeepArtifact
```

兼容基础版本不传 `-CatalogArchive`。重复使用已安装且一致的目录版本时可以省略该参数。

服务器在同一全局发布锁内校验父版本、包哈希、原库目录与来源指纹、候选书目配置及本批原书 PDF 哈希。拒绝过期基线、漏卷、未授权变化和从绑定目录降回旧模式。目录包原子安装到共享数据目录的 `catalog-releases/<id>`，从不覆盖已有版本。

候选健康检查同时验证应用 ID、目录 ID/哈希和书库健康状态。每次正式发布都暂停在候选阶段，输出候选版本及一次性口令。协调者通过 SSH 转发候选端口做真实浏览器验收，把包含版本、检查项、时间及通过结果的 JSON 证据保存到审计目录，再用 `deploy/approve_candidate.ps1 -ReleaseId ... -Nonce ... -EvidenceFile ...` 放行。未收到匹配凭据则原服务继续，候选超时退出。验收后的目录获得独立只读凭据，旧页面可继续读取；未通过校验的安装包不能通过历史版本 URL 访问。两次切流均等待已有连接排空；候选端口被占用时推迟发布，不终止前一候选实例。

发布记录必须附上目录绑定及线上逐卷验收结果。基础代码测试、离线文件检查或测试书库截图都不能替代生产候选的真实目录、PDF、权限、书签与引用验证。

## 回滚和入库限制

- 使用 `deploy/rollback_release.ps1`，提供明确的当前版本和目标版本。先启动目标候选并验证，再切流和排空连接。
- 普通代码回滚只允许目录绑定相同的目标。撤销数据修复时，在最新目录上生成有证据的反向修复包，重新经 main/production 发布；不得恢复整库。
- 一次性基线迁移回滚只允许回到具有 `catalog_protocol: 1` 的兼容基础版本，并再次证明共享旧目录与基线等价。不能退回更早、不认识版本链接的代码。
- `book_changeset inject`、旧目录生成脚本和旧入库提升路径不能用于已绑定目录的生产更新。新增书目尚须配套经过审计的入库事务；在该事务完成前保持原提升路径停用。当前实现会拒绝来源变化，**不会自动合并新增资料，也不会自动退回旧目录**。
- 所有旧发布、目录包和备份保留。至少两个更新版本分别健康运行满 30 天之后，才可经复核做可恢复归档。本次不提供自动删除工具。

## 必须补齐的放行证据

逐卷原书与正文反向核查、所有受影响真实入口的桌面/手机验证、生产形态的候选/切流/排空/回滚故障演练，以及明确的发布协调者。任一未完成时只能交付候选，不能标记为已上线或全库核实通过。
