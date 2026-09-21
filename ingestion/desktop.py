from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, Signal
from PySide6.QtGui import QFont, QFontDatabase, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPushButton, QScrollArea, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QWidget)

from .client import Client, Tunnel
from .settings import load, protect, save


class Signals(QObject):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(str)


class Work(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function, self.signals = function, Signals()

    def run(self):
        try:
            self.signals.done.emit(self.function(self.signals.progress.emit))
        except Exception as exc:
            self.signals.failed.emit(str(exc))


LABELS = {"uploading": "上传中", "processing": "识别与核验", "review": "需要核对",
          "assembling": "准备候选数据", "ready": "合格，等待发布", "published": "已上线",
          "pilot": "试跑", "done": "已处理", "glm": "GLM 识别", "mimo": "MiMo 核验",
          "ocr": "OCR 兜底", "verify": "修复复核"}


class Window(QMainWindow):
    def __init__(self, auto_connect=True):
        super().__init__()
        # Explicit Chinese font also makes offscreen QA render faithfully on Windows.
        import os
        font_file = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc"
        if font_file.exists():
            font_id = QFontDatabase.addApplicationFont(str(font_file))
            families = QFontDatabase.applicationFontFamilies(font_id)
            if families:
                QApplication.instance().setFont(QFont(families[0], 10))
        self.setWindowTitle("文库扩展工作台")
        self.resize(1240, 820)
        self.setAcceptDrops(True)
        self.pool, self.works = QThreadPool(), set()
        self.client = self.tunnel = None
        self.snapshot = {}
        self.books = []
        self.current = self.page_data = None
        self.upload_cancel = threading.Event()
        self.upload_running = self.refresh_running = False
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        title = QLabel("文库扩展工作台")
        title.setStyleSheet("font-size:25px;font-weight:600;color:#193d59")
        outer.addWidget(title)
        outer.addWidget(QLabel("拖入 PDF 或文件夹 · 断点续传 · 校注优先 · 合格后自动发布"))
        connection = QHBoxLayout()
        self.connection = QLabel("尚未连接服务器")
        connection.addWidget(self.connection, 1)
        self.button(connection, "连接设置", self.connect_dialog)
        self.button(connection, "刷新", self.refresh)
        outer.addLayout(connection)
        toolbar = QHBoxLayout()
        self.button(toolbar, "添加 PDF", self.add_files)
        self.button(toolbar, "添加文件夹", self.add_folder)
        self.button(toolbar, "停止本地上传", self.upload_cancel.set)
        self.button(toolbar, "设置批次预算 / 继续", self.budget)
        self.button(toolbar, "导出核验报告", self.export_report)
        outer.addLayout(toolbar)
        self.summary = QLabel("先试跑每本 5 页，再按实测结果设置批次预算。")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("background:#eaf3fa;padding:12px;border-radius:6px")
        outer.addWidget(self.summary)
        splitter = QSplitter(Qt.Horizontal)
        outer.addWidget(splitter, 1)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["书名", "总页数", "处理状态", "完成 / 待核对", "试跑"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setColumnWidth(0, 340)
        self.table.itemSelectionChanged.connect(self.select_book)
        splitter.addWidget(self.table)
        review = QWidget()
        panel = QVBoxLayout(review)
        panel.addWidget(QLabel("页面核对：原始页图与识别文本"))
        actions = QHBoxLayout()
        self.page_number = QSpinBox()
        self.page_number.setMinimum(1)
        actions.addWidget(self.page_number)
        self.button(actions, "查看此页", self.show_page)
        self.button(actions, "下一异常页", self.next_issue)
        panel.addLayout(actions)
        self.image = QLabel("选择书目后查看原图")
        self.image.setAlignment(Qt.AlignCenter)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.image)
        panel.addWidget(scroll, 1)
        self.page_status = QLabel("")
        self.page_status.setWordWrap(True)
        panel.addWidget(self.page_status)
        self.text = QTextEdit()
        self.text.setPlaceholderText("逐字对照原图；有争议的文字不会自动上线。")
        panel.addWidget(self.text, 1)
        form = QFormLayout()
        self.label, self.reason = QLineEdit(), QLineEdit()
        form.addRow("印刷页码", self.label)
        form.addRow("修订依据", self.reason)
        panel.addLayout(form)
        self.blank = QCheckBox("已对照原图，确认本页完全无文字")
        panel.addWidget(self.blank)
        self.button(panel, "保存已核对的修订", self.revise)
        splitter.addWidget(review)
        splitter.setSizes([720, 480])
        bottom = QHBoxLayout()
        self.button(bottom, "暂停所选书目", lambda: self.control("pause"))
        self.button(bottom, "继续所选书目", lambda: self.control("resume"))
        self.button(bottom, "重试异常页", lambda: self.control("retry"))
        self.button(bottom, "核对书目信息", self.metadata_dialog)
        outer.addLayout(bottom)
        self.status = QLabel("关闭窗口后，服务器继续处理已经上传完成的文件。")
        outer.addWidget(self.status)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(5000)
        self.setStyleSheet("QMainWindow{background:#f8fafc} QPushButton{padding:7px 12px} QTableWidget,QTextEdit,QLineEdit{background:white} QLabel{color:#203448}")
        if auto_connect and load().get("encrypted_token"):
            QTimer.singleShot(200, self.connect_saved)

    def button(self, layout, title, action):
        b = QPushButton(title)
        b.clicked.connect(action)
        layout.addWidget(b)
        return b

    def work(self, function, done, quiet=False):
        w = Work(function)
        self.works.add(w)
        def finish(value):
            self.works.discard(w)
            done(value)
        def failed(message):
            self.works.discard(w)
            self.refresh_running = False
            self.status.setText(message)
            if not quiet:
                QMessageBox.warning(self, "操作未完成", message)
        w.signals.done.connect(finish)
        w.signals.failed.connect(failed)
        w.signals.progress.connect(self.status.setText)
        self.pool.start(w)

    def connect_saved(self):
        cfg = load()
        def run(progress):
            tunnel = Tunnel(cfg["host"], cfg["user"], cfg["key"])
            try:
                client = Client(f"http://127.0.0.1:{tunnel.port}", protect(cfg["encrypted_token"], decrypt=True))
                snapshot = client.request("GET")
                return tunnel, client, snapshot
            except Exception:
                tunnel.close()
                raise
        def done(value):
            if self.tunnel:
                self.tunnel.close()
            self.tunnel, self.client, snapshot = value
            self.connection.setText("已加密连接 " + cfg["host"])
            self.display(snapshot)
        self.work(run, done)

    def connect_dialog(self):
        cfg = load()
        d = QDialog(self)
        d.setWindowTitle("服务器连接")
        layout = QFormLayout(d)
        fields = {}
        for key, label, default in [("host", "服务器地址", ""), ("user", "SSH 用户", "root"),
                                     ("key", "本机私钥路径", str(Path.home() / ".ssh/id_marx_cloud_ed25519"))]:
            fields[key] = QLineEdit(cfg.get(key, default))
            layout.addRow(label, fields[key])
        token = QLineEdit()
        token.setEchoMode(QLineEdit.Password)
        token.setPlaceholderText("已有令牌可留空；令牌使用 Windows 用户加密保存")
        layout.addRow("入库访问令牌", token)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(d.accept)
        buttons.rejected.connect(d.reject)
        layout.addRow(buttons)
        if d.exec() == QDialog.Accepted:
            try:
                value = {key: field.text().strip() for key, field in fields.items()}
                value["encrypted_token"] = protect(token.text().strip()) if token.text().strip() else cfg["encrypted_token"]
                save(value)
                self.connect_saved()
            except Exception as exc:
                QMessageBox.warning(self, "连接设置", str(exc))

    def refresh(self):
        if not self.client or self.refresh_running:
            return
        self.refresh_running = True
        self.work(lambda _: self.client.request("GET"), self.display, quiet=True)

    def display(self, snapshot):
        self.refresh_running = False
        self.snapshot = snapshot
        selected_id = self.current["id"] if self.current else None
        self.books = snapshot["books"]
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.books))
        for i, b in enumerate(self.books):
            counts = b.get("counts", {})
            values = [b["name"].split(" (")[0], str(b["pages"]), "已暂停" if b["paused"] else LABELS.get(b["status"], b["status"]),
                      f"{counts.get('done',0)} / {counts.get('review',0)}", f"{b['pilot_done']} / {min(5,b['pages'])}"]
            for j, text in enumerate(values):
                self.table.setItem(i, j, QTableWidgetItem(text))
            if selected_id == b["id"]:
                self.table.selectRow(i)
                self.current = b
        self.table.blockSignals(False)
        state = snapshot.get("scheduler", {}).get("admission", {})
        batches = snapshot.get("batches", [])
        cost = sum(b["used_or_reserved_yuan"] for b in batches)
        pilot = any(b["pilot"] for b in batches)
        self.summary.setText(f"{state.get('reason','等待调度')}　｜　{len(self.books)} 本，{sum(b['pages'] for b in self.books)} 页　｜　估算用量与预留 ¥{cost:.3f}" +
                             ("　｜　试跑结束后等待设置批次预算" if pilot else ""))

    def select_book(self):
        row = self.table.currentRow()
        if 0 <= row < len(self.books):
            self.current = self.books[row]
            self.page_number.setMaximum(max(1, self.current["pages"]))
            if self.current.get("error"):
                self.status.setText(self.current["error"])

    def metadata_dialog(self):
        if not self.current or not self.client:
            return
        book = self.current["id"]
        old = json.loads(self.current.get("metadata") or "{}")
        d = QDialog(self)
        d.setWindowTitle("对照版权页核对书目")
        layout = QFormLayout(d)
        fields = {}
        for k, label in [("title", "书名"), ("editor", "作者 / 编者"), ("publisher", "出版社"),
                         ("year", "出版年份"), ("edition", "版次"), ("reason", "核对依据")]:
            fields[k] = QLineEdit(str(old.get(k) or ""))
            layout.addRow(label, fields[k])
        page = QSpinBox()
        page.setRange(1, max(1, self.current["pages"]))
        page.setValue(self.page_number.value())
        layout.addRow("版权页对应 PDF 页码", page)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(d.accept)
        buttons.rejected.connect(d.reject)
        layout.addRow(buttons)
        if d.exec() == QDialog.Accepted:
            data = {k: f.text().strip() for k, f in fields.items()}
            data.update(reviewed_against_image=True, evidence_page=page.value())
            self.work(lambda _: self.client.request("POST", f"/books/{book}/metadata", data), lambda _: self.refresh())

    def add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "添加 PDF", "", "PDF (*.pdf)")
        if paths:
            self.enqueue(paths)

    def add_folder(self):
        path = QFileDialog.getExistingDirectory(self, "添加整个文件夹")
        if path:
            self.enqueue([path])

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        self.enqueue([u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()])

    def enqueue(self, paths):
        if not self.client:
            QMessageBox.information(self, "连接服务器", "请先连接服务器。")
            return
        if self.upload_running:
            QMessageBox.information(self, "上传中", "当前本地上传完成后可继续添加，服务器会并行管理已上传的书目。")
            return
        self.upload_cancel.clear()
        self.upload_running = True
        def run(progress):
            try:
                files = set()
                for value in paths:
                    path = Path(value)
                    if path.is_dir():
                        files.update(p.resolve() for p in path.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf")
                    elif path.is_file() and path.suffix.lower() == ".pdf":
                        files.add(path.resolve())
                if not files:
                    raise ValueError("没有找到 PDF 文件")
                batch = self.client.request("POST", "/batches", {"title": "桌面导入 " + __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M")})["id"]
                for path in sorted(files):
                    if self.upload_cancel.is_set():
                        break
                    self.client.upload(batch, path, progress=progress, cancelled=self.upload_cancel.is_set)
                return self.client.request("GET")
            finally:
                self.upload_running = False
        self.work(run, self.display)

    def control(self, action):
        if self.current and self.client:
            book = self.current["id"]
            self.work(lambda _: self.client.request("POST", f"/books/{book}/control", {"action": action}), lambda _: self.refresh())

    def budget(self):
        if not self.client:
            return
        batches = self.snapshot.get("batches", [])
        if not batches:
            return
        names = [b["title"] + " [" + b["id"][:8] + "]" for b in batches]
        name, ok = QInputDialog.getItem(self, "选择批次", "预算包含本批已经产生的调用用量", names, 0, False)
        if not ok:
            return
        batch = batches[names.index(name)]
        amount, ok = QInputDialog.getDouble(self, "批次调用费用上限", "先查看试跑报告。设置后继续余量识别；达到上限自动暂停。\n金额（人民币）：", 10, .01, 100000, 2)
        if ok:
            self.work(lambda _: self.client.request("POST", f"/batches/{batch['id']}/budget", {"yuan": amount}), lambda _: self.refresh())

    def export_report(self):
        if not self.client:
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出完整核验报告", "扩库核验报告.json", "JSON (*.json)")
        if path:
            def done(data):
                Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                self.status.setText("核验报告已保存：" + path)
            self.work(lambda _: self.client.request("GET", "/report"), done)

    def show_page(self):
        if not self.current or not self.client or not self.current["pages"]:
            return
        book, page = self.current["id"], self.page_number.value()
        def run(_):
            path = f"/books/{book}/pages/{page}"
            return self.client.request("GET", path), self.client.request("GET", path + "/image", raw=True)
        def done(value):
            if not self.current or self.current["id"] != book or self.page_number.value() != page:
                return
            self.page_data, image = value
            pix = QPixmap()
            pix.loadFromData(image)
            self.image.setPixmap(pix.scaledToWidth(480, Qt.SmoothTransformation))
            self.text.setPlainText(self.page_data["text"])
            self.label.setText(self.page_data["label"])
            self.reason.clear()
            self.blank.setChecked(False)
            self.page_status.setText(LABELS.get(self.page_data["stage"], self.page_data["stage"]) + " " + self.page_data["error"])
        self.work(run, done)

    def next_issue(self):
        if not self.current or not self.client:
            return
        book = self.current["id"]
        def done(value):
            issues = [p["page"] for p in value["pages"] if p["stage"] == "review"]
            if issues:
                later = [p for p in issues if p > self.page_number.value()]
                self.page_number.setValue(later[0] if later else issues[0])
                self.show_page()
            else:
                self.status.setText("这本书目前没有需要人工核对的页面。")
        self.work(lambda _: self.client.request("GET", f"/books/{book}/pages"), done)

    def revise(self):
        if not self.page_data or not self.current:
            return
        if self.page_data["book"] != self.current["id"] or self.page_data["page"] != self.page_number.value():
            QMessageBox.information(self, "请刷新页面", "请先查看当前页，再保存修订。")
            return
        data = {"text": self.text.toPlainText(), "original_text": self.page_data["text"],
                "label": self.label.text(), "reason": self.reason.text(),
                "reviewed_against_image": True, "confirmed_blank": self.blank.isChecked()}
        path = f"/books/{self.current['id']}/pages/{self.page_data['page']}/revision"
        self.work(lambda _: self.client.request("POST", path, data), lambda _: self.show_page())

    def closeEvent(self, event):
        self.upload_cancel.set()
        self.timer.stop()
        if self.tunnel:
            self.tunnel.close()
        event.accept()


def main():
    app = QApplication([])
    app.setApplicationName("文库扩展工作台")
    w = Window()
    w.show()
    app.exec()


if __name__ == "__main__":
    main()
