# 马恩《文集》《全集》检索

本项目是一个本地运行的文献检索程序，使用 Flask 提供本地 Web 界面，检索数据来自内置的 `data/corpus.sqlite`。

## 发布产物

- Windows: 单文件 `exe`
- macOS: 单文件 `.app`

正式发布包只包含运行所需资源：

- `templates/`
- `static/`
- `config/`
- `data/corpus.sqlite`

`pdfs/` 不参与发布打包。

## 本地运行

```powershell
py -3.10 -m pip install -r requirements.txt
py -3.10 app.py
```

浏览器打开 `http://127.0.0.1:5000/` 即可使用。

## 本地打包

Windows:

```powershell
./scripts/build_windows.ps1
```

macOS:

```bash
bash ./scripts/build_macos.sh
```

## GitHub Actions 打包

仓库已包含工作流 `.github/workflows/build-packages.yml`，推送到 GitHub 后可以直接在 GitHub Actions 上构建 Windows 和 macOS 包。

关键前提：

- `data/corpus.sqlite` 需要通过 Git LFS 提交
- 首次推送前先运行 `git lfs install`
- 推送到 `main` 或 `master` 分支会自动触发构建
- 也可以在 GitHub 网页端手动触发 `Build Packages`

## 推送到 GitHub

建议步骤：

```powershell
git init
git lfs install
git add .
git commit -m "Initial import"
git branch -M main
git remote add origin <你的 GitHub 仓库地址>
git push -u origin main
```

推送完成后，到 GitHub 仓库的 `Actions` 页面查看构建结果，并在 artifacts 中下载：

- `windows-exe`
- `macos-app`
