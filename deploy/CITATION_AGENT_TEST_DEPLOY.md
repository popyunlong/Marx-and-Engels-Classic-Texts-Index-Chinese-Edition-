# 论文插注校注 Agent：管理员隔离测试上线手册

## 结论边界

- 正式栏目继续使用 `/citation-assistant`、`citation_assistant.sqlite3`、`citation_assistant/` 和原 `marx-search-citation-worker.service`，不迁移、不改 worker 配额。
- 管理员测试版使用 `/admin/citation-agent-test`、`citation_agent_test.sqlite3`、`citation_agent_test/` 和两个新服务。
- 新服务单元没有 `[Install]`，初次安装后不会随开机自动启动。未得到管理员明确批准前，代码中也没有会员入口或 `member_live` 分支。
- `CITATION_AGENT_TEST_MODE=off` 时测试版只做确定性核验；正式栏目始终不读取这个开关。

## 独立目录与账号

以下命令只创建明确命名的目录和账号；执行前请按实际服务器路径复核：

```bash
sudo groupadd --system marx-citation-agent-queue
sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin marx-citation-agent
sudo usermod -a -G marx-citation-agent-queue www-data
sudo usermod -a -G marx-citation-agent-queue marx-citation-agent
sudo install -d -o root -g root -m 0755 /opt/marx-citation-agent/releases
sudo install -d -o root -g marx-citation-agent-queue -m 2770 /var/lib/marx-citation-agent/queue
```

Agent 发布包只能包含 `citation_agent_release_manifest.txt` 列出的两个文件。使用 `python scripts/build_citation_agent_release.py --output <临时目录>/citation-agent.tar.gz` 构建，并复核包内 `SHA256SUMS`。不得把主站环境文件、数据库、论文、PDF、缓存、证书、个人书或未跟踪文件打入包。Agent 使用 `/opt/marx-citation-agent/venv`，不得向主站 `.venv` 安装依赖；当前 Agent worker 只使用 Python 标准库。

## 离线预检（不连接生产任务库）

在候选 release 中运行：

```bash
python -m pytest tests/test_citation_agent_isolated.py tests/test_citation_agent_shadow.py tests/test_reader_frontend.py -q
python scripts/citation_agent_preflight.py
python scripts/citation_recall_benchmark.py
python scripts/citation_agent_three_article_web_regression.py <王文.docx> <潘文.docx> <第三篇.docx>
systemd-analyze verify deploy/marx-search-citation-agent-test-worker.service deploy/marx-citation-agent.service
```

再使用临时 `APPDATA_DIR`、最小复制语料和测试账号完成匿名黄金集、DOCX/PDF 版式及权限测试。此阶段不得连接生产任务库或放入真实论文。

## 功能关闭状态部署

1. 备份且只备份正式引文任务数据库和两份相关配置，记录每个文件的 SHA-256；不要复制宽泛数据目录。
2. 把两个 `.env.example` 分别安装为 `/etc/marx-citation-agent-bridge.env`（`root:www-data`、0640）和 `/etc/marx-citation-agent.env`（`root:marx-citation-agent`、0640）。两者均保持 `CITATION_AGENT_TEST_MODE=off`。
3. Agent 专用密钥只写入 `/etc/marx-citation-agent.env`。主站和 bridge 环境文件不得出现该密钥。
4. 安装但不要启动两个新 service；验证正式首页、搜索、阅读器、AI 对话、登录、会员权限和一条普通引文任务。
5. 原子切换主站 release，并使用既有零停机重载；不要重启或修改原 citation worker。

## 管理员真实测试

先启动限定域名的 loopback CONNECT 代理。示例代理专用地址为 `127.0.0.2:18080`（不是主站的 `127.0.0.1`），且只允许 `api.deepseek.com:443`；Agent service 的 systemd IP 白名单也只放行 `127.0.0.2`，因此无法访问绑定在 `127.0.0.1` 的主站或其他内网服务。模型域名变更时必须人工同步修改代理白名单和 `CITATION_AGENT_ALLOWED_HOSTS`。

切换顺序：

1. 先把 Agent 专用环境改为 `admin_live`，启动 `marx-citation-agent.service`，确认它只能访问 loopback 代理、只能读写脱敏队列。
2. 再把 bridge 环境改为 `admin_live`，启动 `marx-search-citation-agent-test-worker.service`。
3. 从 `/admin/citation-agent-test` 新建管理员任务。旧任务与正式栏目任务不会被测试 worker 认领。
4. 监控网站延迟、主站内存、两个 worker 的内存/CPU、队列深度和 5xx。4GB 服务器上测试 worker 会额外加载一份语料，因此只在受控测试窗口启动；资源不足时应停止测试，而不是提高上限挤压主站。

管理员测试版当前固定使用 `deepseek-v4-flash`。直接引文与观点转述分别拥有记录、工具和时间预算；普通长句不再挤占直接引文任务。最高优先级的首批记录开启思考，其余批次及第二轮修正使用快速模式。只有真实超时或工具额度耗尽才可标记 `budget_exhausted`，记录截断必须写入 `deferred_record_count`，不得冒充预算耗尽。模型只负责生成严格受限的检索动作；它给出的书名、页码或判断一律没有证据效力，最终候选仍须通过本地所选文库的逐字、范围和印刷页核验。若需完全关闭思考，只改 Agent 专用环境的 `CITATION_AGENT_THINKING=disabled` 并单独重启 Agent 服务，不重启网站进程。

观点模式只接收已有脚注形成的本地页码定位线索；普通无引号正文不进入观点预算。此类结果显示为 `paraphrase + locator_only + writeback_mode=none`，只能人工复核，不能自动采用或写回。Agent worker 在清理或领取任务前初始化独立队列表，因此全新队列文件和明确重建后的空队列均可冷启动，不得依赖网站或确定性 worker 抢先建表。

三个入口必须按输出契约分别验收：`generate`（插注）只输出 Word，只有唯一／参考文献消歧、逐字一致且锚点安全的候选可自动插入；新增上标、标号和注文均为 `#5B9BD5` 浅蓝色。`audit`（校注）不得修改原脚注或尾注，只在正文引文处写入真实 Word 批注，并从该最终 Word 生成批注 PDF。`both`（插注＋校注）在同一个 Word 副本中同时保留浅蓝色新增注释与正文批注，再从这一副本生成 PDF。含引用域的正文只有在批注范围完全位于安全普通文本节点时才能校注，否则保持网页只读；任何 PDF 批注无法唯一定位时不得发布 PDF。观点依据不参加“一键采信直接引文”，也不自动写入任何文件。

发布前必须以生产服务账号执行 `scripts/citation_agent_preflight.py --require-pdf`。PDF 失败时 Word 必须仍可下载；
`retry-pdf` 只允许 `audit` 和 `both` 任务从已完成的最终 Word 单独恢复，并必须拒绝重复转换、跨用户任务和语料／模板版本已变更的任务。

## 回滚与故障降级

第一回滚动作：把 bridge 改回 `off`，停止 `marx-citation-agent.service`。等待中的管理员测试任务可由测试 worker 继续完成确定性流程；正式网站和正式引文任务不受影响。

若队列损坏，只停止两个新服务，保存故障副本后重建明确路径 `/var/lib/marx-citation-agent/queue/queue.sqlite3`；不要删除任何主站数据库或宽泛目录。代码回滚切回上一 release 原子链接。测试数据库只有新增结构，无需破坏性回滚。

## 批准门

验收报告至少包含：全部专项与主站相关回归、三篇 SHA-256 固定黄金集分别运行插注／校注／组合模式的九条流程、自动采用准确率和范围／所有权隔离率 100%、直接引文任务均非 `budget_exhausted` 且延期数为 0、断网／超时／无效 JSON／重复动作／队列损坏／资源限制故障注入，以及 Word、WPS、打包 LibreOffice 和 PDF 版式结果。插注任务不得生成 PDF；校注任务的原注释部件必须不变；组合 PDF 必须保留浅蓝色插注并精确定位全部页边批注。生产仅使用最小匿名夹具，不上传三篇完整论文。没有管理员书面批准，不新增会员入口，不增加 `member_live` 配置，不替换正式栏目 URL。
