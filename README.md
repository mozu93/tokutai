# 特退金脱退 処理漏れ防止アプリ

仕様書: `特退金脱退_処理漏れ防止アプリ_仕様書.html`

## 起動

```
pip install -r requirements.txt
python main.py            # 画面を表示して起動
python main.py --minimized  # トレイに最小化して起動（自動起動時の引数）
```

## 初期設定

1. 「設定」で、テナントID・クライアントID（議員メール配信アプリと同じ値）、Outlookのフォルダ名、管理職2名の宛先を入力する。
2. 「接続テスト」でサインインし、フォルダが見つかることを確認する。
3. 「Windows起動時に自動で起動する」をオンにして保存する。

## データの場所

`%APPDATA%\TokutaiApp\`（環境変数 `TOKUTAI_HOME` で変更可）

| ファイル | 内容 |
|---|---|
| `tokutai.db` | 受付簿・取り込み済みメール・通知履歴 |
| `settings.json` | 設定 |
| `corporate_abbr.csv` | 法人略称表（編集可。列: abbr, formal, ambiguous） |
| `backup/` | 起動時の日次バックアップ（30世代） |
| `tokutai.log` | 取り込み・通知のログ |

## テスト

```
python -m pytest tests -q
```

## exe 化（任意）

```
pip install pyinstaller
pyinstaller --noconsole --onefile --name TokutaiApp --add-data "tokutai/data;tokutai/data" main.py
```
