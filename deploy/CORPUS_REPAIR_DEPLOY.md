# 全站引文文字自动修复：部署与运行手册

本功能采用“只读生产语料 → 独立审核库/候选库 → 管理员逐项审核 → 整批确认 → 蓝绿切换”的路径。夜间任务没有生产数据库写权限；未经管理员整批确认，发布服务不会执行任何切换。

## 首次部署

1. 先按网站现有蓝绿发布流程部署本次代码。不要直接覆盖正在服务的目录。
2. 在服务器 `/etc/marx-corpus-repair.env` 写入以下部署专用配置，并设置为 `root:root`、权限 `0600`：

   ```ini
   MIMO_API_KEY=
   MIMO_BASE_URL=https://api.xiaomimimo.com/v1
   MIMO_MODEL=mimo-v2.5
   DEEPSEEK_API_KEY=
   DEEPSEEK_BASE_URL=https://api.deepseek.com
   CORPUS_REPAIR_DEEPSEEK_MODEL=deepseek-v4-flash
   ```

3. 在 `/opt/marx-search` 执行：

   ```bash
   sudo ./deploy/install_corpus_repair_service.sh
   ```

安装程序会核实 cgroup v2 和实际块设备，创建低权限账号与独立数据目录，并根据数据库、PDF、输出目录的真实设备写入 I/O 限速。默认只安装、不启用两个定时器，也不会重启网站。

安装后再执行一次网站现有的蓝绿发布，使主站进程在新沙箱中只开放审核库所需的数据目录。不要用普通重启替代蓝绿发布；候选实例不健康时应继续保留当前网站。

4. 验证安装结果：

   ```bash
   systemctl cat marx-corpus-repair.service
   systemctl list-timers marx-corpus-repair.timer marx-corpus-repair-promote.timer --no-pager
   systemctl show marx-corpus-repair.service -p User -p CPUQuotaPerSecUSec -p MemoryHigh -p MemoryMax -p TasksMax -p IOWeight
   ```

首次不要在 01:00–06:00 以外手工启动完整任务。程序自身也会按北京时间再次检查时间窗，窗外直接退出，不会先建立全量扫描计划。

5. 确认服务配置、API 密钥和网站蓝绿发布均无误后，再明确启用：

   ```bash
   sudo ./deploy/install_corpus_repair_service.sh --enable
   ```

没有 `--enable` 时，安装操作本身绝不会安排后台运行。

## 正常运行

- 北京时间每日 01:00 启动，06:00 前停止并保存断点。
- 前三晚分别最多处理 100、500、1,000 页；只要当晚出现网站、资源、外部接口或前台渲染警告，试运行档位就不会自动升级。
- 第三阶段按各来源的可疑页比例、平均风险和有效文字量自动分为高/中/低可靠性顺序处理；读者主动报告的文字问题始终优先于该分层。
- 当前台发生 PDF 冷页渲染时，后台只终止自身 OCR 子进程并保存断点；网页请求不获取后台锁，也不等待后台释放资源。
- 管理员在控制台“引文文字自动修复”区域处理不确定项。批准、驳回、暂缓都会记录操作人和时间。
- 所有待商榷项处理完后，下一次夜间任务构建并校验候选库；管理员点击“确认整批候选并申请无停机上线”后，发布定时器才会尝试蓝绿切换。

## 查看状态与暂停

```bash
systemctl status marx-corpus-repair.service --no-pager
journalctl -u marx-corpus-repair.service -n 100 --no-pager
/opt/marx-search/.venv/bin/python /opt/marx-search/scripts/corpus_repair.py status \
  --output-root /home/data/marx-search-corpus-repair
```

需要立即停止后续夜间运行时：

```bash
systemctl disable --now marx-corpus-repair.timer
```

停止任务不会影响网站，也不会删除断点。恢复前先排查控制台中的暂停原因，再执行：

```bash
systemctl enable --now marx-corpus-repair.timer
```

## 失败与回退

- 资源限制无法准确施加、磁盘不足、可用内存不足或网站探测异常时，任务拒绝启动或暂停。
- 线上数据库哈希在批次期间变化时，本批候选作废，不会尝试合并旧基线。
- 候选实例不健康、双实例内存不足、存在交换活动或 Caddy 配置无法唯一确认时，原网站与原数据库保持不动。
- 切流后的任一检查失败会恢复备份数据库并把流量切回健康实例。备份保存在 `/home/data/marx-search-corpus-repair/backups/<批次号>/`。
- 若生产机长期不满足门槛，保持定时器停用；将生产库只读快照和 PDF 放到独立作业机运行同一扫描程序，仍不得降低生产网站的保护阈值。

日志、审核库和报告不得保存 API 密钥、完整 PDF、完整页面截图或完整书稿。外部接口缓存只保存严格 JSON 结果和局部裁剪的哈希；完整页面仅在本机 OCR 子进程的临时目录中短暂存在，并在每页结束或中断后删除。
