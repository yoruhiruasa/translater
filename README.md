# 対面英語会議 ローカル翻訳セット

Windows ノートPC（Core i7-1340P / 16GB、CPU動作）で、会議室の英語を**手元だけで**日本語に訳すための導入手順とスクリプト一式です。音声も文字もPCの外に出ません。

```
会議室の声 → マイク → ライブキャプション（英語の文字起こし）
          → LiveCaptions-Translator → mt_proxy.py → Ollama（翻訳モデル）
          → 英語字幕＋日本語訳を表示
```

## 同梱ファイル

| ファイル | 用途 |
|---|---|
| `setup.ps1` | 初回セットアップ。Ollama / .NET 8 / Python のインストール、モデルの取得 |
| `Modelfile.translategemma` / `.qwen` / `.gemma` | 会議用にパラメータを固定したモデルの定義 |
| `bench.ps1` | モデルごとの速度と訳の比較 |
| `mt_proxy.py` | Translator と Ollama の間に入るローカルプロキシ |
| `fixes.tsv` | ライブキャプションの決まった聞き間違いを翻訳前に直す置換表 |
| `start_meeting.ps1` | 会議直前の起動と確認 |

### mt_proxy.py が必要な理由

1. **TranslateGemma のプロンプト形式に合わせる**：Translator は汎用LLM向けの指示を送りますが、TranslateGemma は決まった形式を前提にしています。プロキシがこの形式に書き換えます。
2. **CPU の無駄打ちを減らす**：Translator は話している途中の文も数回に分けて翻訳に送ります。プロキシは、処理待ちの間に新しい要求が来た古い要求を捨て、最新の1件だけを生成します。
3. **聞き間違いの置換**：`fixes.tsv` の置換を翻訳の前に適用します。
4. **待ち受けは 127.0.0.1 のみ**で、Python の標準ライブラリだけで動きます。

Qwen3.5 / Gemma 4 はプロキシなしでも動きます（Translator は思考モードをオフにして要求を送ります）。それでも2と3の効果があるので、プロキシ経由を推奨します。

---

## 導入手順

### 0. 事前確認

- Windows 11 22H2 以降であること（`setup.ps1` が自動で確認します）
- 会社のPCなら、Ollama / Python / LiveCaptions-Translator（Apache-2.0）のインストール可否を確認する
- 空きディスク容量：約10GB（3モデル分）

### 1. ファイルを置く

このフォルダを `C:\mt` などに置きます（以下 `C:\mt` として説明します）。

### 2. セットアップスクリプトを実行（約20〜40分、大半はダウンロード）

スタートメニューから「PowerShell」を開き、次を実行します。

```powershell
cd C:\mt
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1
```

スクリプトが行うこと：
- winget で Ollama、.NET 8 デスクトップランタイム、Python 3.12 をインストール
- Ollama の設定（同時処理1件、モデル常駐60分、外部から接続できない状態かの確認）
- `translategemma:4b` / `qwen3.5:4b` / `gemma4:e2b` を取得し、会議用モデル `mt-tg` / `mt-qwen` / `mt-gemma` を作成

> モデル名が変わっていて取得に失敗した場合は、ollama.com で正しい名前を確認し、`.\setup.ps1 -SkipInstall -Models <名前>` で再実行します。その際は Modelfile の `FROM` 行も合わせて書き換えてください。

### 3. LiveCaptions-Translator を入れる

1. GitHub の `SakiRinn/LiveCaptions-Translator` の **Releases** から最新版の zip をダウンロードします（.NET 8 は手順2で入るので、ランタイム同梱版でなくて構いません）。
2. `C:\mt\LiveCaptions-Translator\` に展開します（`start_meeting.ps1` はこの場所を既定で探します）。

### 4. ライブキャプションを設定（Translator を起動する前に）

1. `Win + Ctrl + L` でライブキャプションを起動します。初回は端末内での音声処理への同意と、言語ファイルのダウンロードを求められます。
2. 歯車アイコンから次のように設定します。
   - **位置 → 画面に重ねて表示**（Translator の README で必須とされている設定。これをしないと表示が崩れます）
   - **キャプションの言語 → 英語（米国）**
   - **マイクの音声を含める → オン**（対面会議では必須）
3. **設定 → システム → サウンド → 入力** で、使うマイクを既定のデバイスにします。
4. ライブキャプションを閉じます。

### 5. Translator の初回設定

`C:\mt\LiveCaptions-Translator\LiveCaptionsTranslator.exe` を起動し、設定画面で次のように設定します。

| 項目 | 値 |
|---|---|
| Translate API | **Ollama**（Google など**クラウド系を選ばない**） |
| API Setting → API URL | `http://127.0.0.1:11500`（プロキシ経由）／`http://127.0.0.1:11434`（直接） |
| API Setting → Model Name | 直接接続なら `mt-qwen` など。プロキシ経由なら何でも可 |
| Target Language | `ja-JP` |
| API Interval | 5〜8（話している途中の文を訳す頻度。小さいほどCPU負荷が上がる） |
| Context Aware | オフ（前の文脈も送るので遅くなる） |

> Translator は1回の翻訳に**8秒**を超えるとタイムアウトします。`bench.ps1` で8秒を超えるモデルは本番に使わないでください。

### 6. モデルを比較して選ぶ

普段の作業状態（ブラウザなどを開いたまま）で実行します。

```powershell
cd C:\mt
Set-ExecutionPolicy -Scope Process Bypass
.\bench.ps1
```

- 速度の目安：平均3秒以内なら快適、5秒前後なら実用、8秒を超える文があれば不適
- 訳の質：否定（not / unless）、数字（12 と 20）、慣用句（table that / take it offline）が正しく訳されているかを確認する
- 自分の分野の英文で試すには、1行1文のテキストファイル（UTF-8）を作り、`.\bench.ps1 -File my.txt` を実行
- 遅い場合は、Modelfile の `num_thread` を 6〜10 の間で変えて `ollama create` をやり直します

### 7. オフラインでのリハーサル（重要）

1. `.\start_meeting.ps1`（TranslateGemma 以外を使う場合は `-Model mt-qwen` などを付ける）
2. **Wi-Fi を切る**
3. 会議室（または同じくらいの広さの部屋）で、離れた位置から英語を流すか、人に話してもらう
4. 確認すること
   - 英語字幕がどこまで崩れるか（崩れる場合はマイクの位置や種類を見直す）
   - 日本語訳の遅れが積み重ならないか（プロキシのウィンドウに1文ごとの秒数が出ます）
   - 決まって間違える語があれば `fixes.tsv` に追記する（例：`cute too<TAB>Q2`）

### 8. 会議当日

1. 10分前に `.\start_meeting.ps1` を実行（暖機まで自動で行います）
2. 電源に接続し、電源モードを「最適なパフォーマンス」にする
3. 参加者に一言：「聞き取り補助として手元で字幕を出しています。音声の録音や外部送信はしていません」
4. 重くなったら Translator の一時停止ボタンで翻訳だけ止める（英語字幕は出続けます）

### 9. 会議後

- Translator を閉じ、プロキシのウィンドウで `Ctrl+C`
- 会議内容は `LiveCaptions-Translator\translation_history.db` に残ります。不要なら Translator の履歴画面から削除してください。

---

## 困ったとき

| 症状 | 対処 |
|---|---|
| 訳に `[ERROR] ... timeout (> 8 seconds)` と出る | より軽いモデル（`mt-gemma`）にする／`num_thread` を調整する／API Interval を大きくする |
| 訳が何も出ない | ライブキャプションの言語が英語か、マイクの音声を含めるがオンか確認。プロキシのウィンドウにログが出ているか確認 |
| 英語字幕自体が崩れる | マイクを話者に近づける。外付けの会議用スピーカーフォンを使う |
| 1文目だけ遅い | 暖機されていない。`start_meeting.ps1` を実行し直す |
| `ollama` コマンドが見つからない | PowerShell を開き直す（インストール直後は PATH が反映されていない） |
| 日本語が文字化けする | `.ps1` は UTF-8（BOM付き）で保存されています。メモ帳などで編集した場合は「UTF-8（BOM付き）」で保存し直す |
| モデル取得に失敗する | ollama.com でモデル名を確認し、Ollama を最新版に更新（`winget upgrade Ollama.Ollama`） |
