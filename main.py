import json
import re
import subprocess
import sys
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from typing import List, Optional

from croniter import croniter
from PySide6.QtCore import QEvent, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QMenu,
    QStyle,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


DATE_FMT = "%Y-%m-%d %H:%M:%S"
DATA_FILE = Path("tasks.json")


@dataclass
class Task:
    task_id: str
    name: str
    cron_expr: str
    command: str
    enabled: bool = True
    last_run: Optional[str] = None
    next_run: Optional[str] = None
    last_status: str = "未运行"


class TaskDialog(QDialog):
    def __init__(self, parent: QWidget, task: Optional[Task] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("任务配置")
        self.resize(480, 180)

        self.name_input = QLineEdit()
        self.cron_input = QLineEdit()
        self.command_input = QLineEdit()
        self.enabled_input = QCheckBox("启用")
        self.enabled_input.setChecked(True)

        hint = QLabel("Cron 示例: */5 * * * *  （每 5 分钟）")
        hint.setStyleSheet("color: #666;")

        form = QFormLayout()
        form.addRow("名称", self.name_input)
        form.addRow("Cron 表达式", self.cron_input)
        form.addRow("命令", self.command_input)
        form.addRow("", self.enabled_input)
        form.addRow("", hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.setLayout(layout)

        if task:
            self.name_input.setText(task.name)
            self.cron_input.setText(task.cron_expr)
            self.command_input.setText(task.command)
            self.enabled_input.setChecked(task.enabled)

    def get_payload(self) -> Optional[dict]:
        name = self.name_input.text().strip()
        cron_expr = self.cron_input.text().strip()
        command = self.command_input.text().strip()
        enabled = self.enabled_input.isChecked()

        if not name or not cron_expr or not command:
            QMessageBox.warning(self, "参数错误", "名称、Cron 表达式和命令都不能为空。")
            return None

        if not croniter.is_valid(cron_expr):
            QMessageBox.warning(self, "Cron 错误", "请输入有效的 Cron 表达式。")
            return None

        return {
            "name": name,
            "cron_expr": cron_expr,
            "command": command,
            "enabled": enabled,
        }

    def accept(self) -> None:
        if self.get_payload() is None:
            return
        super().accept()


class SchedulerEngine(QWidget):
    task_updated = Signal()
    log_generated = Signal(str, str)

    def __init__(self, tasks: List[Task]) -> None:
        super().__init__()
        self.tasks = tasks
        self.lock = threading.Lock()

        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def recalc_next_run(self, task: Task, base_time: Optional[datetime] = None) -> None:
        if not task.enabled:
            task.next_run = None
            return
        now = base_time or datetime.now()
        itr = croniter(task.cron_expr, now)
        next_dt = itr.get_next(datetime)
        task.next_run = next_dt.strftime(DATE_FMT)

    def tick(self) -> None:
        now = datetime.now()
        due_tasks: List[Task] = []
        with self.lock:
            for task in self.tasks:
                if not task.enabled or not task.next_run:
                    continue
                try:
                    next_dt = datetime.strptime(task.next_run, DATE_FMT)
                except ValueError:
                    task.last_status = "next_run 时间格式错误"
                    self.recalc_next_run(task)
                    continue
                if now >= next_dt:
                    due_tasks.append(task)
            for task in due_tasks:
                # 先预计算下一次执行，避免命令执行时间较长导致重复触发
                self.recalc_next_run(task, now)

        if due_tasks:
            self.task_updated.emit()
        for task in due_tasks:
            self.execute_task(task)

    def execute_task(self, task: Task, manual: bool = False) -> None:
        def run() -> None:
            start = datetime.now()
            cmd = task.command
            reason = "手动触发" if manual else "定时触发"
            self.log_generated.emit(
                f"[{start.strftime(DATE_FMT)}] [{task.name}] 开始执行 ({reason}): {cmd}",
                "info",
            )
            try:
                proc = subprocess.Popen(
                    cmd,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )

                def stream_output(pipe, stream_name: str) -> None:
                    if pipe is None:
                        return
                    buf = ""
                    while True:
                        ch = pipe.read(1)
                        if ch == "":
                            if buf:
                                self.log_generated.emit(f"[{task.name}] {buf}", stream_name)
                            break
                        if ch in ("\r", "\n"):
                            if buf:
                                self.log_generated.emit(f"[{task.name}] {buf}", stream_name)
                                buf = ""
                            continue
                        buf += ch

                t_out = threading.Thread(
                    target=stream_output, args=(proc.stdout, "stdout"), daemon=True
                )
                t_err = threading.Thread(
                    target=stream_output, args=(proc.stderr, "stderr"), daemon=True
                )
                t_out.start()
                t_err.start()
                t_out.join()
                t_err.join()

                return_code = proc.wait()
                end = datetime.now()
                status = "成功" if return_code == 0 else f"失败({return_code})"

                with self.lock:
                    task.last_run = end.strftime(DATE_FMT)
                    task.last_status = status
                    if manual and task.enabled:
                        self.recalc_next_run(task, end)

                self.log_generated.emit(
                    f"[{end.strftime(DATE_FMT)}] [{task.name}] 执行完成: {status}",
                    "info",
                )
            except Exception as exc:
                fail_time = datetime.now()
                with self.lock:
                    task.last_run = fail_time.strftime(DATE_FMT)
                    task.last_status = f"异常: {exc}"
                self.log_generated.emit(
                    f"[{fail_time.strftime(DATE_FMT)}] [{task.name}] 执行异常: {exc}",
                    "stderr",
                )
            finally:
                self.task_updated.emit()

        threading.Thread(target=run, daemon=True).start()


class MainWindow(QMainWindow):
    ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Python Cron GUI")
        self.resize(980, 620)
        self.is_quit_requested = False

        self.tasks = self.load_tasks()
        self.engine = SchedulerEngine(self.tasks)
        self.engine.task_updated.connect(self.on_tasks_updated)
        self.engine.log_generated.connect(self.append_log)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["名称", "Cron", "命令", "启用", "上次执行", "下次执行", "状态"]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setAlternatingRowColors(True)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)

        add_btn = QPushButton("新增")
        edit_btn = QPushButton("编辑")
        del_btn = QPushButton("删除")
        run_btn = QPushButton("立即执行")
        save_btn = QPushButton("保存")
        reload_btn = QPushButton("重载")

        add_btn.clicked.connect(self.add_task)
        edit_btn.clicked.connect(self.edit_task)
        del_btn.clicked.connect(self.delete_task)
        run_btn.clicked.connect(self.run_task_once)
        save_btn.clicked.connect(self.save_tasks)
        reload_btn.clicked.connect(self.reload_tasks)

        btn_layout = QHBoxLayout()
        for btn in [add_btn, edit_btn, del_btn, run_btn, save_btn, reload_btn]:
            btn_layout.addWidget(btn)
        btn_layout.addStretch(1)

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.addLayout(btn_layout)
        layout.addWidget(self.table, 2)
        layout.addWidget(QLabel("运行日志"), 0)
        layout.addWidget(self.log_box, 1)
        self.setCentralWidget(root)
        self.setup_tray()

        self.ensure_next_runs()
        self.refresh_table()

    def setup_tray(self) -> None:
        self.tray_icon = QSystemTrayIcon(self)
        tray_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.tray_icon.setIcon(tray_icon)
        self.setWindowIcon(tray_icon)
        self.tray_icon.setToolTip("Python Cron GUI")

        tray_menu = QMenu(self)
        show_action = QAction("显示主窗口", self)
        quit_action = QAction("退出程序", self)
        show_action.triggered.connect(self.show_from_tray)
        quit_action.triggered.connect(self.quit_from_tray)
        tray_menu.addAction(show_action)
        tray_menu.addSeparator()
        tray_menu.addAction(quit_action)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self.on_tray_activated)
        self.tray_icon.show()

    def on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_from_tray()

    def show_from_tray(self) -> None:
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def quit_from_tray(self) -> None:
        self.is_quit_requested = True
        self.tray_icon.hide()
        QApplication.instance().quit()

    def closeEvent(self, event) -> None:
        if self.is_quit_requested:
            event.accept()
            return
        self.hide()
        event.ignore()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and self.isMinimized():
            QTimer.singleShot(0, self.hide)

    def ensure_next_runs(self) -> None:
        for task in self.tasks:
            if task.enabled and not task.next_run:
                self.engine.recalc_next_run(task)

    def load_tasks(self) -> List[Task]:
        if not DATA_FILE.exists():
            return []
        try:
            raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            tasks = [Task(**item) for item in raw]
            return tasks
        except Exception:
            QMessageBox.warning(self, "读取失败", "任务文件损坏，将以空任务列表启动。")
            return []

    def save_tasks(self) -> None:
        try:
            data = [asdict(t) for t in self.tasks]
            DATA_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self.append_log(f"[{datetime.now().strftime(DATE_FMT)}] 任务已保存。")
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", str(exc))

    def reload_tasks(self) -> None:
        self.tasks = self.load_tasks()
        self.engine.tasks = self.tasks
        self.ensure_next_runs()
        self.refresh_table()
        self.append_log(f"[{datetime.now().strftime(DATE_FMT)}] 已重载任务。")

    def selected_index(self) -> Optional[int]:
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            return None
        return indexes[0].row()

    def add_task(self) -> None:
        dialog = TaskDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        payload = dialog.get_payload()
        if payload is None:
            return
        task = Task(task_id=str(uuid.uuid4()), **payload)
        self.engine.recalc_next_run(task)
        self.tasks.append(task)
        self.refresh_table()
        self.save_tasks()

    def edit_task(self) -> None:
        idx = self.selected_index()
        if idx is None:
            QMessageBox.information(self, "提示", "请先选中一条任务。")
            return
        current = self.tasks[idx]
        dialog = TaskDialog(self, current)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        payload = dialog.get_payload()
        if payload is None:
            return
        current.name = payload["name"]
        current.cron_expr = payload["cron_expr"]
        current.command = payload["command"]
        current.enabled = payload["enabled"]
        current.last_status = "已更新"
        self.engine.recalc_next_run(current)
        self.refresh_table()
        self.save_tasks()

    def delete_task(self) -> None:
        idx = self.selected_index()
        if idx is None:
            QMessageBox.information(self, "提示", "请先选中一条任务。")
            return
        task = self.tasks[idx]
        ok = QMessageBox.question(self, "确认删除", f"确定删除任务: {task.name} ?")
        if ok != QMessageBox.StandardButton.Yes:
            return
        self.tasks.pop(idx)
        self.refresh_table()
        self.save_tasks()

    def run_task_once(self) -> None:
        idx = self.selected_index()
        if idx is None:
            QMessageBox.information(self, "提示", "请先选中一条任务。")
            return
        self.engine.execute_task(self.tasks[idx], manual=True)

    def on_tasks_updated(self) -> None:
        self.refresh_table()
        self.save_tasks()

    def refresh_table(self) -> None:
        self.table.setRowCount(len(self.tasks))
        for row, task in enumerate(self.tasks):
            values = [
                task.name,
                task.cron_expr,
                task.command,
                "是" if task.enabled else "否",
                task.last_run or "-",
                task.next_run or "-",
                task.last_status,
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() ^ Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, col, item)

    @classmethod
    def ansi_to_html(cls, text: str, default_color: Optional[str] = None) -> str:
        fg_map = {
            30: "#000000",
            31: "#cd3131",
            32: "#0dbc79",
            33: "#e5e510",
            34: "#2472c8",
            35: "#bc3fbc",
            36: "#11a8cd",
            37: "#e5e5e5",
            90: "#666666",
            91: "#f14c4c",
            92: "#23d18b",
            93: "#f5f543",
            94: "#3b8eea",
            95: "#d670d6",
            96: "#29b8db",
            97: "#ffffff",
        }

        def apply_style(s: str, color: Optional[str], bold: bool) -> str:
            if not s:
                return ""
            styles = []
            if color:
                styles.append(f"color:{color}")
            if bold:
                styles.append("font-weight:600")
            style_attr = f" style=\"{';'.join(styles)}\"" if styles else ""
            return f"<span{style_attr}>{escape(s)}</span>"

        current_color = default_color
        current_bold = False
        html_parts: List[str] = []
        last = 0

        for match in cls.ANSI_RE.finditer(text):
            chunk = text[last : match.start()]
            html_parts.append(apply_style(chunk, current_color, current_bold))

            code_text = match.group(1).strip()
            codes = [0] if not code_text else [int(c) for c in code_text.split(";") if c]
            for code in codes:
                if code == 0:
                    current_color = default_color
                    current_bold = False
                elif code == 1:
                    current_bold = True
                elif code == 22:
                    current_bold = False
                elif code in fg_map:
                    current_color = fg_map[code]
                elif code == 39:
                    current_color = default_color
            last = match.end()

        html_parts.append(apply_style(text[last:], current_color, current_bold))
        return "".join(html_parts)

    def append_log(self, message: str, channel: str = "info") -> None:
        base_color = None
        if channel == "stderr":
            base_color = "#f14c4c"
        elif channel == "info":
            base_color = "#9cdcfe"

        rendered = self.ansi_to_html(message, default_color=base_color)
        self.log_box.moveCursor(QTextCursor.MoveOperation.End)
        self.log_box.insertHtml(rendered + "<br>")
        self.log_box.moveCursor(QTextCursor.MoveOperation.End)


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
