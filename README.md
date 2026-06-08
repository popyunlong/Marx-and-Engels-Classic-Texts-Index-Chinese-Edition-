# 马恩《文集》《全集》检索

这是一个本地运行的资料检索程序，使用 Flask 提供浏览器界面，支持：

- 引文检索
- 命中页定位
- 完整资料版中的原 PDF 打开、页内高亮、目录导航

## 运行模式

- `纯检索模式`
  仅要求 `data/corpus.sqlite` 和对应校验文件存在。
- `完整资料版`
  需要 `config/`、`data/`、`pdfs/` 资料齐全，并且已激活。

程序启动时会检查：

- `data/corpus.sqlite`
- `data/corpus.sqlite.sha256`
- `config/manifest.yaml`
- `config/volumes.yaml`
- `pdfs/` 是否存在

如果数据库校验失败，程序不会信任该资料包。

## 本地开发运行

```powershell
py -3.10 -m pip install -r requirements.txt -r requirements-build.txt
py -3.10 scripts/write_release_metadata.py --data-dir data --data-version local-dev
py -3.10 app.py
```

也可以直接运行：

```powershell
run.bat
```

## Windows 完整资料版打包

先生成完整资料发布目录：

```powershell
./scripts/build_windows.ps1
```

输出目录示例：

```text
release/windows-full-2026.04.21/
  program/
    马恩文集全集检索程序.exe
  assets/
    config/
    data/
    pdfs/
```

如果已经安装 Inno Setup 6，可以继续生成安装器：

```powershell
./scripts/build_windows_installer.ps1
```

## 激活

当前实现为第一阶段的本地离线激活：

- 未激活时允许检索
- 未激活时禁止打开原 PDF
- 激活信息保存在用户本机的应用数据目录
- 首页会显示机器指纹
- 卖家可用 `scripts/make_activation_code.py <机器指纹>` 生成激活码

## 说明

- `scripts/write_release_metadata.py` 会为 `corpus.sqlite` 生成 `sha256` 校验文件和 `release.json`
- `build_windows.ps1` 会固定依赖版本并生成可重复的 Windows 完整资料版目录
- `installer/windows_full.iss` 是 Inno Setup 安装脚本

## 云端网页版部署

项目已经带有服务器部署骨架，适合上线云端网页版：

- Ubuntu 24.04 云主机
- Python venv + systemd
- Caddy 反向代理 + HTTPS
- 站内账号、会员权限和支付宝电脑网站支付

部署说明见：[DEPLOY_SERVER.md](D:/claudecode文件夹/【增强】马恩《文集》《全集》检索/DEPLOY_SERVER.md)
