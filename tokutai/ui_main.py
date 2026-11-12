"""受付簿のメイン画面とトレイ常駐。"""
from __future__ import annotations

import logging
import threading
from datetime import date

from PyQt6.QtCore import QDate, QItemSelectionModel, QObject, QRunnable, Qt, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QBrush, QColor, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPushButton,
    QSystemTrayIcon, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)
from sqlalchemy import select

from . import db, service
from .corporate import CompanyNormalizer, load_table
from . import paths, settings as settings_mod
from .db import Case
from .graph import GraphClient
from .ui_settings import SettingsDialog

log = logging.getLogger(__name__)

COLUMNS = ["状態", "警告", "経過日数", "案件番号", "会社名", "案件作成日", "不備連絡日", "書類到着日", "処理済日", "備考"]
COL_STATUS, COL_NO = COLUMNS.index("状態"), COLUMNS.index("案件番号")
RED_BG = QColor(255, 205, 205)
GRAY_FG = QColor(130, 130, 130)


def _icon(color: str) -> QIcon:
    pm = QPixmap(32, 32)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(2, 2, 28, 28)
    p.end()
    return QIcon(pm)


class _Signals(QObject):
    done = pyqtSignal(object)


class _SyncTask(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.signals = _Signals()

    def run(self):
        try:
            res = self.fn()
        except Exception as e:  # noqa: BLE001
            log.exception("同期で予期しないエラー")
            res = service.SyncResult(errors=[f"予期しないエラー: {e}"])
        self.signals.done.emit(res)


def _fmt(d: date | None) -> str:
    return d.strftime("%Y-%m-%d") if d else ""


class DateDialog(QDialog):
    def __init__(self, title: str, label: str, parent=None, initial: date | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.edit = QDateEdit(QDate.currentDate() if initial is None else QDate(initial.year, initial.month, initial.day))
        self.edit.setCalendarPopup(True)
        self.edit.setDisplayFormat("yyyy-MM-dd")
        form = QFormLayout(self)
        form.addRow(label, self.edit)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def value(self) -> date:
        return self.edit.date().toPyDate()


class EditDatesDialog(QDialog):
    """日付の入れ間違いを、その場で直す。チェックを外すと、その日付は空になる。"""
    FIELDS = (("received_date", "案件作成日"), ("defect_date", "不備連絡日"),
              ("docs_date", "書類到着日"), ("done_date", "処理済日"))

    def __init__(self, case: Case, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"日付を修正 {case.case_no}")
        form = QFormLayout(self)
        self._rows: dict[str, tuple[QCheckBox, QDateEdit]] = {}
        for attr, label in self.FIELDS:
            cur = getattr(case, attr)
            chk = QCheckBox("あり"); chk.setChecked(cur is not None)
            ed = QDateEdit(QDate(cur.year, cur.month, cur.day) if cur else QDate.currentDate())
            ed.setCalendarPopup(True)
            ed.setDisplayFormat("yyyy-MM-dd")
            ed.setEnabled(cur is not None)
            chk.toggled.connect(ed.setEnabled)
            row = QHBoxLayout(); row.addWidget(chk); row.addWidget(ed, 1)
            form.addRow(label, row)
            self._rows[attr] = (chk, ed)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def values(self) -> dict[str, date | None]:
        return {a: (ed.date().toPyDate() if chk.isChecked() else None)
                for a, (chk, ed) in self._rows.items()}


class AddCaseDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("案件を追加")
        self.no = QLineEdit()
        self.company = QLineEdit()
        self.recv = QDateEdit(QDate.currentDate())
        self.recv.setCalendarPopup(True)
        self.recv.setDisplayFormat("yyyy-MM-dd")
        form = QFormLayout(self)
        form.addRow("案件番号", self.no)
        form.addRow("会社名", self.company)
        form.addRow("案件作成日", self.recv)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)


class MainWindow(QMainWindow):
    def __init__(self, start_minimized: bool = False):
        super().__init__()
        self.setWindowTitle("特退金脱退 処理漏れ防止")
        self.resize(1200, 640)
        self.st = settings_mod.load()
        self.norm = CompanyNormalizer(load_table(paths.ensure_abbr_file()))
        self._client: GraphClient | None = None
        self._client_key: tuple[str, str] | None = None
        self._lock = threading.Lock()
        self._known_alerts: set[tuple[str, str]] = set()
        self._first_check = True
        self._running = False
        self._alert_count = 0
        self._last_error: list[str] = []
        self._totals = {"new": 0, "done": 0, "notified": 0}
        self._last_update = "-"
        self.undo_stack = service.UndoStack()

        self._build_ui()
        self._build_tray()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.sync)
        self._apply_timer()
        self.reload()
        if not start_minimized:
            self.show_front()
        QTimer.singleShot(1500, self.sync)  # 起動直後に1回実行

    # ---------- UI ----------
    def _build_ui(self) -> None:
        w = QWidget()
        self.setCentralWidget(w)
        v = QVBoxLayout(w)

        self.banner = QLabel()
        self.banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.banner)

        top = QHBoxLayout()
        self.filter = QComboBox()
        self.filter.addItems(service.FILTERS)
        if self.st.view_filter in service.FILTERS:
            self.filter.setCurrentText(self.st.view_filter)
        self.filter.currentIndexChanged.connect(self._filter_changed)
        self.search = QLineEdit()
        self.search.setPlaceholderText("案件番号・会社名・備考で検索（検索中は処理済も表示）")
        self.search.textChanged.connect(self.reload)
        top.addWidget(QLabel("表示:"))
        top.addWidget(self.filter)
        top.addWidget(self.search, 1)
        v.addLayout(top)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hh = self.table.horizontalHeader()
        hh.setStretchLastSection(True)
        hh.setSectionsClickable(True)
        hh.sectionClicked.connect(self._header_clicked)
        self._show_sort_indicator()
        self.table.setColumnWidth(COLUMNS.index("会社名"), 240)
        self.table.setColumnWidth(COL_STATUS, 130)
        self.table.setColumnWidth(COL_NO, 160)
        self._restore_column_order()
        hh.setSectionsMovable(True)  # 見出しのドラッグで列の順番を変えられる
        hh.sectionMoved.connect(self._save_column_order)
        self.table.cellDoubleClicked.connect(lambda *_: self.edit_dates())
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        v.addWidget(self.table, 1)

        # よく使う操作だけをボタンにし、選んだ案件の状態に合わせて使えるものだけ有効にする。
        # それ以外は「その他」にまとめる（右クリックでも同じ操作ができる）。
        hint = QLabel("案件の行を選んで操作します（Ctrl・Shiftキーで複数選択すると、まとめて処理できます）。ダブルクリックで日付の修正、右クリックでメニューが開きます。")
        hint.setStyleSheet("color:#666;")
        v.addWidget(hint)
        btns = QHBoxLayout()
        self.btn_arrived = QPushButton("書類が届いた"); self.btn_arrived.clicked.connect(self.mark_arrived)
        self.btn_defect = QPushButton("不備を連絡した"); self.btn_defect.clicked.connect(self.mark_defect)
        self.btn_done = QPushButton("処理済にする"); self.btn_done.clicked.connect(self.toggle_done)
        self.btn_undo = QPushButton("元に戻す"); self.btn_undo.clicked.connect(self.undo_last)
        QShortcut(QKeySequence.StandardKey.Undo, self, activated=self.undo_last)
        self.btn_sync = QPushButton("今すぐ更新"); self.btn_sync.clicked.connect(self.sync)
        self.btn_more = QPushButton("その他")
        more = QMenu(self.btn_more)
        for item in (
            ("案件を追加", self.add_case), None,
            ("日付を修正", self.edit_dates), ("会社名を編集", self.edit_company),
            ("備考を編集", self.edit_note), None,
            ("書類到着を取り消す", self.cancel_arrived), ("不備を取り消す", self.cancel_defect), None,
            ("CSV出力", self.export_csv), ("設定", self.open_settings),
        ):
            if item is None:
                more.addSeparator()
            else:
                more.addAction(item[0], item[1])
        self.btn_more.setMenu(more)
        for b in (self.btn_arrived, self.btn_defect, self.btn_done):
            btns.addWidget(b)
        btns.addStretch()
        btns.addWidget(self.btn_undo)
        btns.addWidget(self.btn_sync)
        btns.addWidget(self.btn_more)
        v.addLayout(btns)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self._update_buttons()

        self.status = QLabel()
        self.statusBar().addWidget(self.status, 1)

    def _build_tray(self) -> None:
        self.icon_ok, self.icon_warn = _icon("#2e9e4f"), _icon("#d62f2f")
        self.setWindowIcon(self.icon_ok)
        self.tray = QSystemTrayIcon(self.icon_ok, self)
        menu = QMenu()
        a_open = QAction("開く", self); a_open.triggered.connect(self.show_front)
        a_sync = QAction("今すぐ更新", self); a_sync.triggered.connect(self.sync)
        a_quit = QAction("終了", self); a_quit.triggered.connect(self.quit_app)
        for a in (a_open, a_sync):
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction(a_quit)
        self._menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self.show_front() if r == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.setToolTip("特退金脱退 処理漏れ防止")
        self.tray.show()

    def _center_on_screen(self) -> None:
        area = QApplication.primaryScreen().availableGeometry()
        w, h = min(self.width(), area.width()), min(self.height(), area.height())
        self.resize(w, h)
        self.move(area.x() + (area.width() - w) // 2, area.y() + (area.height() - h) // 2)

    def show_front(self) -> None:
        self._center_on_screen()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e) -> None:  # ウィンドウを閉じても常駐する
        e.ignore()
        self.hide()

    def quit_app(self) -> None:
        self.tray.hide()
        QApplication.instance().quit()

    # ---------- データ表示 ----------
    def _selection(self) -> list[tuple[str, str]]:
        """選んでいる案件（案件番号, 状態）を、一覧の上から順に返す。"""
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [(self.table.item(r, COL_NO).text(), self.table.item(r, COL_STATUS).text()) for r in rows]

    def _selected_no(self) -> str | None:
        """1件だけ選んでいるときの案件番号。0件・複数件のときは案内を出して None。"""
        sel = self._selection()
        if not sel:
            QMessageBox.information(self, "案件の選択", "一覧で案件を選んでください。")
            return None
        if len(sel) > 1:
            QMessageBox.information(self, "案件の選択", "この操作は、案件を1件だけ選んで行ってください。")
            return None
        return sel[0][0]

    def reload(self) -> None:
        today = date.today()
        with db.session() as s:
            cases = list(s.scalars(select(Case)))
        alerts = {a.case_no: a for a in service.collect_alerts(cases, today, self.st)}
        rows = service.select_rows(cases, alerts, self.filter.currentText(), self.search.text(),
                                   self.st.sort_column, self.st.sort_ascending, today)
        keep = {no for no, _ in self._selection()}  # 並びが変わっても、同じ案件の選択を保つ
        self.table.setRowCount(len(rows))
        for i, c in enumerate(rows):
            a = alerts.get(c.case_no)
            ed = service.elapsed_days(c, today)
            vals = {"状態": c.status, "警告": a.kind if a else "", "経過日数": "" if ed is None else str(ed),
                    "案件番号": c.case_no, "会社名": c.company, "案件作成日": _fmt(c.received_date),
                    "不備連絡日": _fmt(c.defect_date), "書類到着日": _fmt(c.docs_date),
                    "処理済日": _fmt(c.done_date), "備考": c.note}
            for j, name in enumerate(COLUMNS):
                it = QTableWidgetItem(vals[name])
                if a:
                    it.setBackground(QBrush(RED_BG))
                    it.setForeground(QBrush(QColor(0, 0, 0)))
                elif c.done_date:
                    it.setForeground(QBrush(GRAY_FG))
                self.table.setItem(i, j, it)
        sm = self.table.selectionModel()
        sm.clearSelection()
        for i, c in enumerate(rows):
            if c.case_no in keep:
                sm.select(self.table.model().index(i, 0),
                          QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        self._alert_count = len(alerts)
        if alerts:
            self.banner.setText(f"警告 {len(alerts)} 件")
            self.banner.setStyleSheet("background:#d62f2f;color:white;font-size:18px;font-weight:bold;padding:8px;")
        else:
            self.banner.setText("警告なし")
            self.banner.setStyleSheet("background:#2e9e4f;color:white;font-size:18px;font-weight:bold;padding:8px;")
        self.tray.setIcon(self.icon_warn if alerts else self.icon_ok)
        self._popup_new_alerts(alerts)
        self._update_status()
        self._update_buttons()

    def _filter_changed(self, *_args) -> None:
        self.st.view_filter = self.filter.currentText()
        settings_mod.save(self.st)
        self.reload()

    def _restore_column_order(self) -> None:
        """前回の列の並び順を戻す。保存された内容が今の列と合わないときは無視する。"""
        order = self.st.column_order
        if sorted(order) != sorted(COLUMNS):
            return
        hh = self.table.horizontalHeader()
        for visual, name in enumerate(order):
            hh.moveSection(hh.visualIndex(COLUMNS.index(name)), visual)

    def _save_column_order(self, *_args) -> None:
        hh = self.table.horizontalHeader()
        self.st.column_order = [COLUMNS[hh.logicalIndex(v)] for v in range(len(COLUMNS))]
        settings_mod.save(self.st)

    def _header_clicked(self, col: int) -> None:
        """見出しのクリックで、昇順 → 降順 → 標準の順に切り替える。"""
        name = COLUMNS[col]
        if self.st.sort_column != name:
            self.st.sort_column, self.st.sort_ascending = name, True
        elif self.st.sort_ascending:
            self.st.sort_ascending = False
        else:
            self.st.sort_column, self.st.sort_ascending = "", True
        settings_mod.save(self.st)
        self._show_sort_indicator()
        self.reload()

    def _show_sort_indicator(self) -> None:
        hh = self.table.horizontalHeader()
        if self.st.sort_column in COLUMNS:
            order = (Qt.SortOrder.AscendingOrder if self.st.sort_ascending
                     else Qt.SortOrder.DescendingOrder)
            hh.setSortIndicator(COLUMNS.index(self.st.sort_column), order)
            hh.setSortIndicatorShown(True)
        else:
            hh.setSortIndicatorShown(False)

    def _update_buttons(self) -> None:
        """選んだ案件の状態に合わせて、押せるボタンと文言を切り替える。複数選択では件数を添える。"""
        sel = self._selection()
        arrive, _ = service.bulk_targets(sel, service.BULK_ARRIVED)
        defect, _ = service.bulk_targets(sel, service.BULK_DEFECT)
        done, _ = service.bulk_targets(sel, service.BULK_DONE)
        statuses = {st for _, st in sel}
        multi = len(sel) > 1

        def label(text: str, n: int) -> str:
            return f"{text}（{n}件）" if multi and n else text

        self.btn_arrived.setEnabled(bool(arrive))
        base = "補正書類が届いた" if statuses == {db.STATUS_DEFECT} else "書類が届いた"
        self.btn_arrived.setText(label(base, len(arrive)))
        self.btn_defect.setEnabled(bool(defect))
        self.btn_defect.setText(label("不備を連絡した", len(defect)))
        all_done = bool(sel) and statuses == {db.STATUS_DONE}
        self.btn_done.setEnabled(bool(sel))
        self.btn_done.setText(label("処理済を取り消す", len(sel)) if all_done else label("処理済にする", len(done)))
        last = self.undo_stack.peek_label()
        self.btn_undo.setEnabled(last is not None)
        self.btn_undo.setToolTip(f"直前の操作「{last}」を元に戻す（Ctrl+Z）" if last else "元に戻せる操作はありません")

    def _popup_new_alerts(self, alerts: dict) -> None:
        keys = {(a.case_no, a.kind) for a in alerts.values()}
        new = keys - self._known_alerts
        if new and (self._first_check or True):
            self.tray.showMessage("特退金脱退 警告", f"新しい警告が{len(new)}件あります。",
                                  QSystemTrayIcon.MessageIcon.Warning, 8000)
        self._known_alerts = keys
        self._first_check = False

    def _update_status(self) -> None:
        t = self._totals
        err = f"　｜　⚠ {self._last_error[0]}" if self._last_error else ""
        self.status.setText(
            f"最終更新 {self._last_update}　新規 {t['new']}件　処理済 {t['done']}件　通知送信 {t['notified']}件{err}")

    # ---------- 同期 ----------
    def _apply_timer(self) -> None:
        self.timer.start(max(5, self.st.sync_minutes) * 60 * 1000)

    def _get_client(self) -> GraphClient:
        key = (self.st.tenant_id, self.st.client_id)
        if self._client is None or self._client_key != key:
            self._client = GraphClient(*key)
            self._client_key = key
        return self._client

    def sync(self, *_args) -> None:
        if self._running:
            return
        if not self.st.is_connectable():
            self._last_error = ["設定画面で接続情報を入力してください。"]
            self.reload()
            return
        self._running = True
        self.btn_sync.setEnabled(False)

        def job():
            with self._lock:
                return service.run_sync(self.st, self._get_client(), self.norm, interactive=False)

        task = _SyncTask(job)
        task.signals.done.connect(self._sync_done)
        QThreadPool.globalInstance().start(task)

    def _sync_done(self, res: service.SyncResult) -> None:
        self._running = False
        self.btn_sync.setEnabled(True)
        self._totals["new"] += res.new_cases
        self._totals["done"] += res.done_cases
        self._totals["notified"] += res.notified
        self._last_error = res.errors
        self._last_update = date.today().strftime("%m/%d ") + __import__("datetime").datetime.now().strftime("%H:%M")
        if res.errors:
            self.tray.showMessage("特退金脱退 エラー", res.errors[0], QSystemTrayIcon.MessageIcon.Critical, 8000)
        self.reload()

    # ---------- 操作 ----------
    def _update_cases(self, label: str, nos: list[str], fn) -> None:
        """案件を更新し、元に戻せるよう履歴に残す。"""
        with db.session() as s:
            service.apply_undoable(s, self.undo_stack, label + (f"（{len(nos)}件）" if len(nos) > 1 else ""), nos, fn)
        self.reload()

    def _update_case(self, label: str, case_no: str, fn) -> None:
        self._update_cases(label, [case_no], fn)

    def undo_last(self) -> None:
        with db.session() as s:
            res = service.undo(s, self.undo_stack)
        if res is None:
            return
        label, restored, skipped = res
        self.reload()
        if skipped:
            QMessageBox.warning(
                self, "元に戻す",
                f"「{label}」を{restored}件戻しました。\n{skipped}件は、その後に内容が変わっているため戻していません。")
        else:
            self.statusBar().showMessage(f"「{label}」を元に戻しました。", 6000)

    def _bulk(self, title: str, action: str, label: str, apply) -> None:
        """選んだ案件のうち対象のものに、同じ日付をまとめて記録する。日付は1回だけ聞く。"""
        sel = self._selection()
        if not sel:
            QMessageBox.information(self, "案件の選択", "一覧で案件を選んでください。")
            return
        targets, skipped = service.bulk_targets(sel, action)
        if not targets:
            QMessageBox.information(self, title, "選んだ案件は、すでにこの操作ができない状態です。")
            return
        dlg = DateDialog(title, label, self)
        if not dlg.exec():
            return
        if len(sel) > 1:
            msg = f"{len(targets)}件に、{label} {dlg.value():%Y-%m-%d} を記録します。"
            if skipped:
                msg += f"\n（状態が合わない{skipped}件は対象外です）"
            if QMessageBox.question(self, title, msg) != QMessageBox.StandardButton.Yes:
                return
        d = dlg.value()
        self._update_cases(title, targets, lambda c: apply(c, d))

    def mark_arrived(self) -> None:
        def apply(c, d):
            c.docs_date = d
            c.defect_date = None  # 補正書類が届いたので不備は解消
        self._bulk("書類が届いた", service.BULK_ARRIVED, "書類到着日", apply)

    def cancel_arrived(self) -> None:
        no = self._selected_no()
        if no and QMessageBox.question(self, "到着を取り消す", f"{no} の書類到着日を取り消しますか？") \
                == QMessageBox.StandardButton.Yes:
            self._update_case("書類到着の取り消し", no, lambda c: setattr(c, "docs_date", None))

    def mark_defect(self) -> None:
        self._bulk("不備を連絡した", service.BULK_DEFECT, "不備を相手に連絡した日", service.record_defect)

    def cancel_defect(self) -> None:
        no = self._selected_no()
        if no and QMessageBox.question(self, "不備を取り消す", f"{no} の不備連絡日を取り消しますか？") \
                == QMessageBox.StandardButton.Yes:
            self._update_case("不備の取り消し", no, lambda c: setattr(c, "defect_date", None))

    def edit_dates(self) -> None:
        no = self._selected_no()
        if not no:
            return
        with db.session() as s:
            dlg = EditDatesDialog(s.get(Case, no), self)
        if dlg.exec():
            vals = dlg.values()
            self._update_case("日付の修正", no, lambda c: [setattr(c, k, v) for k, v in vals.items()])

    def _table_menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if item is None:
            return
        if item.row() not in {i.row() for i in self.table.selectionModel().selectedRows()}:
            self.table.setCurrentCell(item.row(), COL_STATUS)
        menu = QMenu(self)
        for b in (self.btn_arrived, self.btn_defect, self.btn_done):
            menu.addAction(b.text(), b.click).setEnabled(b.isEnabled())
        menu.addSeparator()
        for text, fn in (
            ("日付を修正", self.edit_dates), ("会社名を編集", self.edit_company),
            ("備考を編集", self.edit_note), ("書類到着を取り消す", self.cancel_arrived),
            ("不備を取り消す", self.cancel_defect),
        ):
            menu.addAction(text, fn)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "CSV出力", f"tokutai_{date.today():%Y%m%d}.csv", "CSVファイル (*.csv)")
        if not path:
            return
        with db.session() as s:
            cases = list(s.scalars(select(Case)))
        try:
            n = service.export_csv(cases, path)
        except OSError as e:
            QMessageBox.warning(self, "CSV出力", f"書き出せませんでした: {e}")
            return
        QMessageBox.information(self, "CSV出力", f"{n}件を書き出しました。\n（絞り込みに関係なく、すべての案件です）")

    def toggle_done(self) -> None:
        sel = self._selection()
        if sel and all(st == db.STATUS_DONE for _, st in sel):  # 全部が処理済なら、取り消す
            n = len(sel)
            text = f"{sel[0][0]} の処理済を取り消しますか？" if n == 1 else f"{n}件の処理済を取り消しますか？"
            if QMessageBox.question(self, "処理済を取り消す", text) == QMessageBox.StandardButton.Yes:
                self._update_cases("処理済の取り消し", [no for no, _ in sel], lambda c: setattr(c, "done_date", None))
            return
        self._bulk("処理済にする", service.BULK_DONE, "処理済日", lambda c, d: setattr(c, "done_date", d))

    def add_case(self) -> None:
        dlg = AddCaseDialog(self)
        if not dlg.exec():
            return
        no = dlg.no.text().strip().upper()
        if not no:
            QMessageBox.warning(self, "案件を追加", "案件番号を入力してください。")
            return
        with db.session() as s:
            if s.get(Case, no):
                QMessageBox.warning(self, "案件を追加", "同じ案件番号がすでに登録されています。")
                return
            c = Case(case_no=no, company=dlg.company.text().strip(),
                     received_date=dlg.recv.date().toPyDate(), note="手動追加")
            s.add(c)
            s.commit()
            self.undo_stack.push("案件の追加", {no: (None, service.snapshot(c))})
        self.reload()

    def _edit_text(self, title: str, label: str, attr: str, multiline: bool) -> None:
        no = self._selected_no()
        if not no:
            return
        with db.session() as s:
            cur = getattr(s.get(Case, no), attr) or ""
        if multiline:
            text, ok = QInputDialog.getMultiLineText(self, title, label, cur)
        else:
            text, ok = QInputDialog.getText(self, title, label, text=cur)
        if ok:
            self._update_case(f"{label}の編集", no, lambda c: setattr(c, attr, text.strip()))

    def edit_note(self) -> None:
        self._edit_text("備考を編集", "備考", "note", True)

    def edit_company(self) -> None:
        self._edit_text("会社名を編集", "会社名", "company", False)

    def open_settings(self) -> None:
        dlg = SettingsDialog(self.st, self)
        if dlg.exec():
            settings_mod.save(self.st)
            self._apply_timer()
            self._client = None
            self.reload()
            self.sync()
