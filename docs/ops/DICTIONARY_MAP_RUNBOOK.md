# 大辞典导航与概念地图

开发、数据整理、MiMo 调度、质量审查、压力与异常测试均在本地执行。生产只读取已发布的地图文件，不执行模型请求。地图不消耗站内 AI 额度。

## 两次发布

先发布独立导航修复提交，再发布地图功能及经过审查的地图版本。导航覆盖目录和详情，继续使用全站账号菜单与栏目切换。地图发布缺少绑定文件时关闭入口，辞典原有路由与正文照常工作。

只能由指定发布协调者执行仓库正式流程。不得从本功能分支、脏目录或未推送提交发布，也不得绕过生产锁直接修改服务器源文件、上游或服务。发布前重新比较线上运行提交、远端 main、production 和发布账本；发生变化则重新对齐并验收。

## 本地数据与费用

只读源是 `data/dictionary.sqlite`。不复制会员库、支付库、上传目录或正式任务队列。输出目录、持久账本和模型缓存必须位于 D 盘任务目录，不能提交到 Git。使用既有 Python 3.10 环境及 `requirements.txt` 中的依赖。

```powershell
python scripts/build_dictionary_graph.py --source data/dictionary.sqlite `
  --work D:/CodexData/data/dictionary-map-20261007/build `
  --output D:/CodexData/data/dictionary-map-20261007/graphs/NEW_VERSION `
  --id NEW_VERSION --mode pilot --budget 50
```

凭据仅从进程变量 `MIMO_API_KEY` 或显式传入的辞典专用配置读取。不得修改在线 AI 配置。先审查 40 条分层样本；完成试跑审查后使用 `--mode full --pilot-accepted`。同一任务所有版本必须复用同一 `--work` 和 `budget.sqlite`，不得通过更换账本重置预算。每次调用先预留最坏费用；实际用量未知时保留预留。费用上限被硬限制为 50 元，格式重试至多一次，也计入预算。服务商过滤结果不重试绕过；保留节点和原文提及，推断层留空。

检查点按源指纹及处理版本保存，响应按内容及模型缓存。操作系统文件锁阻止同一检查点并行写入。重复启动需使用新的不可变输出版本；已完成调用直接复用缓存。正常浏览地图不会启动建图脚本。

原文提及、页码明确的参见与 AI 解释分层。字面提及不声称同义或因果；AI 推断默认关闭。普通词、重复词目中的短词、非完整书名和缺少明确目标的层级关系采用保守过滤。来源仍有复核标记的词条保留节点，不生成关系。

## 产物与审查

每个版本含 `graph.sqlite`、`binding.json`、`report.json`。运行全部引文定位校验及本地性能验收：

```powershell
python scripts/audit_dictionary_graph.py --directory GRAPH_DIRECTORY `
  --source data/dictionary.sqlite --output LOCAL_AUDIT_JSON
```

另抽查至少 100 个不同关系 ID，覆盖原文层、推断层、关系类型及方向。`review.json` 必须记录真实审查者、审查时间、`graph_sha256`、`source_sha256`，以及 `rows` 内的 `id`、`quote_valid`、`relation_valid`。AI 代理检查不得写成人工审查。类型和方向正确率须至少 95%，引文必须全部可定位。发现问题须修正并重新审查相关类别；只通过引文自动校验不能替代语义审查。

完成后将版本 `binding.json` 作为 `config/dictionary_graph_release.json` 提交；文件只含版本 ID、图文件 SHA-256、辞典 SHA-256。本地打包：

```powershell
python scripts/dictionary_graph_deploy.py package --directory GRAPH_DIRECTORY --output GRAPH_TAR_GZ
```

发布包最多 100 MiB，仅含图文件、绑定和审查记录三项；拒绝路径穿越、链接、来源不符、未知审查 ID、少于 100 条或不达标审查。安装必须留出至少 5 GiB 磁盘。安装路径为共享数据下 `dictionary-graphs/VERSION/`，旧图保留。应用发布清单和账本记录绑定。

## 本地验收

`pytest` 使用仓库的隔离语料和临时用户配置。单独导入应用或执行部署冒烟也必须先布置隔离环境，不能指向个人既有业务库。浏览器测试的迷你应用不启动邮件、支付、任务消费者或模型。浏览器测试覆盖 390–1600px、长导航、移动端关系列表、证据、CSV、AI 层默认关闭、旧链接恢复及 60 节点限制。图形库仅在地图页面加载。

```powershell
python -m pytest tests/ -q
node --test tests/browser/ai_controls.cjs tests/browser/citation_scope.cjs tests/browser/dictionary_map.cjs
```

热缓存地图及路径接口目标 P95 ≤500ms；本地约定浏览器 60 节点两秒内可交互。性能报告必须写明设备与测试方式，不以本地结果声称线上负载已验证。

## 受控切换与独立关闭

协调者从干净、已推送、HEAD 等于 origin/production 的 production 执行 `deploy/release.ps1`，地图版本额外传 `-DictionaryGraphArchive`。整个远端事务持有 `/run/lock/marx-search-release.lock`，沿用低优先级候选实例。候选启动命令不带 `--with-worker`，并在启动命令中强制 `MARX_SKIP_STARTUP_MAINTENANCE=1` 和 `MARX_SKIP_SEARCH_WARM=1`，防止环境文件重新启用上传续跑；回退候选和部署冒烟也适用。正式实例保留正常维护行为。浏览器验收不得提交邮件、支付或收费模型任务。

新旧实例并行至少 30 分钟，至少 60 组配对样本；地图绑定变化与目录绑定变化一样必须通过服务器保存的观测证据。资源压力、健康失败或核心接口变慢时拒绝候选并保留原服务。导航的独立发布也须完成相同观察，不因没有地图绑定而缩短窗口。候选验收涵盖检索、阅读、账户会员、AI 流式导航保护、校注、研究动态、上传导出；正式站仅低频只读检查，收费或写入场景在本地隔离环境验收。

正式流量切换沿用现有排空保护；旧请求未完成则中止提交，恢复旧实例服务。切换后再观察至少 30 分钟，核对 `/api/runtime` 的应用及地图版本、发布账本、关键功能和资源情况。操作记录需明确实际时长与样本数。

仅地图异常可在同一发布锁内执行：

```sh
bash /opt/marx-search/current/app/deploy/dictionary_graph_control.sh /opt/marx-search EXPECTED_RELEASE off
```

此操作仅原子更新共享功能开关并记账，不重启服务。恢复 `on` 前验证当前绑定和审查记录。全站问题使用 `deploy/rollback_release.ps1`，同时指定预期当前版本和目标版本；回退会检查旧图是否仍存在且绑定正确，旧的无地图版本也可回退。旧发布、工作树、图文件和必要备份遵守仓库保留期限，不自动清除。
