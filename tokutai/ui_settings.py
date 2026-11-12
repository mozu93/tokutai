"""設定画面。"""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QLineEdit,
    QMessageBox, QPushButton, QSpinBox, QVBoxLayout,
)

from . import autostart
from .graph import GraphClient, GraphError
from .settings import Settings


class SettingsDialog(QDialog):
    def __init__(self, s: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("設定")
        self.setMinimumWidth(480)
        self.s = s

        self.tenant = QLineEdit(s.tenant_id)
        self.client = QLineEdit(s.client_id)
        self.folder = QLineEdit(s.folder_name)
        self.kw_create = QLineEdit(s.kw_create)
        self.kw_done = QLineEdit(s.kw_done)
        self.warn_docs = QSpinBox(); self.warn_docs.setRange(1, 60); self.warn_docs.setValue(s.warn_docs_days)
        self.warn_proc = QSpinBox(); self.warn_proc.setRange(1, 60); self.warn_proc.setValue(s.warn_process_days)
        self.warn_defect = QSpinBox(); self.warn_defect.setRange(1, 60); self.warn_defect.setValue(s.warn_defect_days)
        self.mgr1 = QLineEdit(s.manager_emails[0])
        self.mgr2 = QLineEdit(s.manager_emails[1])
        self.notify = QCheckBox("管理職へ警告メールを自動送信する"); self.notify.setChecked(s.notify_enabled)
        self.sync_min = QSpinBox(); self.sync_min.setRange(5, 720); self.sync_min.setSuffix(" 分"); self.sync_min.setValue(s.sync_minutes)
        self.init_days = QSpinBox(); self.init_days.setRange(1, 365); self.init_days.setSuffix(" 日"); self.init_days.setValue(s.initial_days)
        self.auto = QCheckBox("Windows起動時に自動で起動する"); self.auto.setChecked(s.autostart)

        root = QVBoxLayout(self)
        for title, rows in (
            ("接続", [("テナントID", self.tenant), ("クライアントID", self.client), ("Outlookのフォルダ名", self.folder)]),
            ("判定（件名キーワード）", [("案件作成メール", self.kw_create), ("処理完了メール", self.kw_done)]),
            ("警告（営業日）", [("書類未着の基準日数", self.warn_docs), ("未処理の基準日数", self.warn_proc),
                                  ("補正待ち（不備連絡後）の基準日数", self.warn_defect)]),
            ("通知", [("管理職1の宛先", self.mgr1), ("管理職2の宛先", self.mgr2), ("", self.notify)]),
            ("動作", [("同期の間隔", self.sync_min), ("初回の取り込み範囲", self.init_days), ("", self.auto)]),
        ):
            box = QGroupBox(title)
            form = QFormLayout(box)
            for label, w in rows:
                form.addRow(label, w)
            root.addWidget(box)

        test = QPushButton("接続テスト")
        test.clicked.connect(self._test)
        row = QHBoxLayout(); row.addWidget(test); row.addStretch()
        root.addLayout(row)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    def _save(self) -> None:
        mails = [self.mgr1.text().strip(), self.mgr2.text().strip()]
        for m in mails:
            if m and "@" not in m:
                QMessageBox.warning(self, "設定", f"メールアドレスの形式が正しくありません: {m}")
                return
        s = self.s
        s.tenant_id, s.client_id = self.tenant.text().strip(), self.client.text().strip()
        s.folder_name = self.folder.text().strip()
        s.kw_create, s.kw_done = self.kw_create.text().strip(), self.kw_done.text().strip()
        s.warn_docs_days, s.warn_process_days = self.warn_docs.value(), self.warn_proc.value()
        s.warn_defect_days = self.warn_defect.value()
        s.manager_emails = mails
        s.notify_enabled = self.notify.isChecked()
        s.sync_minutes, s.initial_days = self.sync_min.value(), self.init_days.value()
        s.autostart = self.auto.isChecked()
        try:
            autostart.set_enabled(s.autostart)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "設定", f"自動起動の登録を切り替えられませんでした: {e}")
        self.accept()

    def _test(self) -> None:
        tenant, client, folder = self.tenant.text().strip(), self.client.text().strip(), self.folder.text().strip()
        if not (tenant and client and folder):
            QMessageBox.warning(self, "接続テスト", "テナントID・クライアントID・フォルダ名を入力してください。")
            return
        try:
            gc = GraphClient(tenant, client)
            gc.find_folder_id(folder, interactive=True)
        except GraphError as e:
            QMessageBox.critical(self, "接続テスト", str(e))
            return
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "接続テスト", f"予期しないエラー: {e}")
            return
        QMessageBox.information(self, "接続テスト", f"サインインに成功し、フォルダ「{folder}」が見つかりました。")
