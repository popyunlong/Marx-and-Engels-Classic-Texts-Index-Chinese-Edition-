# 打包说明

当前仓库面向 `Windows 完整资料版` 发行，默认交付方式为：

- 安装主程序
- 同时交付完整资料目录
- 资料目录中包含 `config/`、`data/`、`pdfs/`

## 资料完整性

发布前必须生成：

- `data/corpus.sqlite.sha256`
- `data/release.json`

命令：

```powershell
py -3.10 scripts/write_release_metadata.py --data-dir data --data-version 2026.04.21
```

程序启动时会先校验 `corpus.sqlite`，校验失败时不会进入正常资料模式。

## 生成完整资料目录

```powershell
./scripts/build_windows.ps1
```

该脚本会：

- 安装固定版本依赖
- 生成资料校验文件
- 调用 PyInstaller 构建主程序
- 生成 `release/windows-full-<日期>/` 目录

## 生成 Windows 安装器

要求本机已安装 `Inno Setup 6`，并且 `ISCC.exe` 在 `PATH` 中。

```powershell
./scripts/build_windows_installer.ps1
```

该脚本会：

- 先生成最新完整资料目录
- 同步到 `release/current/`
- 调用 `installer/windows_full.iss`
- 输出安装器到 `release/installer/`

## 运行期约定

安装后的主目录至少包含：

- `马恩文集全集检索程序.exe`
- `config/`
- `data/`
- `pdfs/`

运行时能力分为两档：

- `纯检索模式`
  资料不足或未激活时自动进入
- `完整资料版`
  数据库校验通过、资料目录完整且激活成功后启用

## 离线激活发码

用户首页会显示机器指纹。卖家可运行：

```powershell
py -3.10 scripts/make_activation_code.py "<机器指纹>"
```

生成对应的离线激活码后发给用户。

## 注意

- 当前离线激活仅做第一阶段门槛控制，不是强防破解方案
- 如果需要签名发行，请在生成安装器后追加代码签名步骤
- macOS 仍保留基础脚本，但不作为本轮销售发行主线
