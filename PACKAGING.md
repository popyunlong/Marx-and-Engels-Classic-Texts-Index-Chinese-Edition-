# 打包说明

本项目的正式发布物固定为两个平台原生包：

- Windows: `dist/马恩文集全集检索程序.exe`
- macOS: `dist/马恩文集全集检索程序.app`

打包时只包含这些运行时资源：

- `templates/`
- `static/`
- `data/corpus.sqlite`
- `config/`

`pdfs/` 不参与发布打包，发布包只负责检索，不负责重建索引。

## 本地构建

### Windows

在项目根目录运行：

```powershell
./scripts/build_windows.ps1
```

脚本会自动：

- 安装运行时依赖与打包依赖
- 调用 `PyInstaller --onefile --windowed`
- 输出单文件 `exe`

### macOS

在项目根目录运行：

```bash
bash ./scripts/build_macos.sh
```

脚本会自动：

- 安装运行时依赖与打包依赖
- 将 `marx_multisize.ico` 转成 macOS 需要的 `.icns`
- 调用 `PyInstaller --onefile --windowed`
- 输出 `.app`

## 图标

- 桌面应用图标统一来自 `marx_multisize.ico`
- macOS 构建时会先生成 `build/icons/marx_multisize.icns`
- 浏览器页签图标继续使用 `static/favicon.ico`

## GitHub Actions

工作流文件在 `.github/workflows/build-packages.yml`，会同时构建：

- Windows 单文件 `exe`
- macOS `.app`

注意：

- 工作流默认启用了 Git LFS checkout
- 如果要在 GitHub 上构建，`data/corpus.sqlite` 需要通过 Git LFS 提交，否则 CI 中拿不到数据库
- `pdfs/` 无需上传到仓库，也不会参与打包

## 运行期约定

- 打包后应用优先读取可执行文件同级的 `data/corpus.sqlite`
- 如果同级目录没有外置数据库，则回退到打包进程序内部的数据库资源
- 现有心跳监控、自动打开浏览器、无黑框启动逻辑保持不变
