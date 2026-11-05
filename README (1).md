# 対面英語会議 ローカル翻訳セット

Windows ノートPC（Core i7-1340P / 16GB、CPU動作）で、会議室の英語を**手元だけで**日本語に訳すための導入手順とスクリプト一式です。音声も文字もPCの外に出ません。

```
会議室の声 → マイク → ライブキャプション（英語の文字起こし）
          → LiveCaptions-Translator → mt_proxy.py → Ollama（翻訳モデル）
          → 英語字幕＋日本語訳を表示
```

## 使うモデル

| 会議用の名前 | 元のモデル | 容量 | 位置づけ |
|---|---|---|---|
| `mt-plamo` | PLaMo 3 Translate 2B（Q4_K_M） | 1.8GB | **本命**。日英に強い翻訳専用モデル。小さいので速い |
| `mt-tg` | TranslateGemma 4B | 3.3GB | 翻訳専用。PLaMo のライセンスが使えない場合の第一候補 |
| `mt-qwen` | Qwen3.5 4B | 3.3GB | 汎用モデル。比較用・予備 |

追加で試せるモデル（`setup.ps1 -SkipInstall -Models <名前>` で追加）：
- `hf.co/pfnet/plamo-3-translate-0.8b-GGUF:Q4_K_M` → `mt-plamo-08`（さらに軽い）
- `hf.co/pfnet/plamo-3-translate-8b-GGUF:Q4_K_M` → `mt-plamo-8b`（5.3GB。CPUでは1文5秒以上かかる見込みで、Translator の8秒制限に近い）
- `gemma4:e2b` → `mt-gemma`

> **PLaMo 3 Translate のライセンス（PLaMo Community License）**
> - 個人・非商用の利用：無料、登録不要
> - 商用利用（業務の会議を含む）：直近年度の売上が10億円以下なら、PFN の登録フォームを提出すれば無料
> - **売上が10億円を超える会社では、別途ライセンス契約が必要**
>
> 会社で使う場合は、事前に社内の担当部署に確認してください。TranslateGemma（Gemma 利用規約）と Qwen3.5（Apache 2.0）には、このような売上の条件はありません。

## 同梱ファイル

| ファイル | 用途 |
|---|---|
| `setup.ps1` | 初回セットアップ。Ollama / .NET 8 / Python のインストールと、モデルの取得 |
| `Modelfile.plamo` / `.translategemma` / `.qwen` / `.gemma` | 会議用にパラメータを固定したモデルの定義 |
| `bench.ps1` | モデルごとの速度と訳の比較 |
| `mt_proxy.py` | Translator と Ollama の間に入るローカルプロキシ |
| `fixes.tsv` | ライブキャプションの決まった聞き間違いを翻訳前に直す置換表 |
| `glossary.tsv` | 訳語を固定する用語集（PLaMo のみ） |
| `start_meeting.ps1` | 会議直前の起動と確認 |

### mt_proxy.py の役割

1. **モデルに合わせて入力形式を変換する**
   - PLaMo：専用の PFML3 形式を組み立て、Ollama にそのまま（raw で）渡します。**直前に確定した2文とその訳を履歴として一緒に渡す**ので、ライブキャプションが文を途中で区切っても文脈に沿った訳になります。発言が90秒途切れると、話題が変わったとみなして履歴を消します。
   - TranslateGemma：専用のプロンプト形式に変換します。
   - Qwen / Gemma：Translator の指示をそのまま使います。
2. **CPU の無駄打ちを減らす**：Translator は話している途中の文も数回に分けて送ってきます。処理待ちの間に新しい要求が来たら、古い要求は捨てて最新の1件だけを訳します。
3. **聞き間違いの置換**（`fixes.tsv`）と**用語集**（`glossary.tsv`。原文にその語が含まれる時だけモデルに渡します）
4. **待ち受けは 127.0.0.1 のみ**で、Python の標準ライブラリだけで動きます。

---

## 導入手順

### 0. 事前確認

- Windows 11 22H2 以降であること（`setup.ps1` が自動で確認します）
- 会社のPCなら、Ollama / Python / LiveCaptions-Translator（Apache-2.0）のインストール可否と、**PLaMo のライセンス**（上記）を確認する
- 空きディスク容量：**約15GB**（既定の3モデルで約8.4GB、アプリ類で約2GB、余裕分）

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
- Ollama の設定（同時処理は1件、モデルの常駐は60分、外部から接続できない状態かの確認）
- 3モデルを取得し、会議用モデル `mt-plamo` / `mt-tg` / `mt-qwen` を作成（PLaMo は Hugging Face から直接取得されます）

> PLaMo のライセンスが使えない場合は、`.\setup.ps1 -Models translategemma:4b,qwen3.5:4b` で PLaMo を除いて実行し、以降は `-Model mt-tg` を指定してください。

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
| API Setting → API URL | `http://127.0.0.1:11500`（プロキシ） |
| API Setting → Model Name | 何でも可（プロキシが差し替えます） |
| Target Language | `ja-JP` |
| API Interval | 5〜8（話している途中の文を訳す頻度。小さいほどCPU負荷が上がる） |
| Context Aware | オフ（PLaMo では文脈をプロキシが渡すので不要） |

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
- 遅い場合は、Modelfile の `num_thread` を 6〜10 の間で変え、`.\setup.ps1 -SkipInstall -Models <元のモデル名>` で作り直します

### 7. オフラインでのリハーサル（重要）

1. `.\start_meeting.ps1`（PLaMo 以外を使う場合は `-Model mt-tg` などを付ける）
2. **Wi-Fi を切る**
3. 会議室（または同じくらいの広さの部屋）で、離れた位置から英語を流すか、人に話してもらう
4. 確認すること
   - 英語字幕がどこまで崩れるか（崩れる場合はマイクの位置や種類を見直す）
   - 日本語訳の遅れが積み重ならないか（プロキシのウィンドウに1文ごとの秒数と、使った履歴・用語の数が出ます）
   - 決まって間違える語があれば `fixes.tsv` に追記する（例：`cute too<TAB>Q2`）
   - 訳語を固定したい社名・製品名・略語があれば `glossary.tsv` に追記する（例：`churn<TAB>解約率`）

### 8. 会議当日

1. 10分前に起動します（暖機まで自動で行います）。会議の概要を一言添えると、PLaMo の訳が安定します。
   ```powershell
   .\start_meeting.ps1 -Context 'Quarterly sales review between the Tokyo and US teams.'
   ```
   訳を短く読みやすくしたい場合は `-Form plain`（だ・である調）を付けます。
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
| 訳に `[ERROR] ... timeout (> 8 seconds)` と出る | より軽いモデル（`mt-plamo-08` など）にする／`num_thread` を調整する／API Interval を大きくする／`-History 1` で履歴を減らす |
| 訳に `[ERROR] ... BadGateway` と出る | Ollama が止まっている。プロキシのウィンドウのエラーを確認し、`start_meeting.ps1` を実行し直す |
| PLaMo の取得に失敗する／`unknown model architecture` と出る | Ollama を最新版に更新する（`winget upgrade Ollama.Ollama`）。それでも動かなければ `-Model mt-tg` で運用する |
| PLaMo の訳が空になる・崩れる | プロキシを `--add-bos` 付きで起動して比較する（`py mt_proxy.py --mode plamo --model mt-plamo --add-bos`） |
| 前の文の内容が訳に混ざる | 話題が変わった直後に起きやすい。`-History 1` または `-History 0` で起動する |
| 訳が何も出ない | ライブキャプションの言語が英語か、マイクの音声を含めるがオンか確認。プロキシのウィンドウにログが出ているか確認 |
| 英語字幕自体が崩れる | マイクを話者に近づける。外付けの会議用スピーカーフォンを使う |
| 1文目だけ遅い | 暖機されていない。`start_meeting.ps1` を実行し直す |
| 「別のモデルでプロキシが起動中」と出る | 古いプロキシのウィンドウを `Ctrl+C` で閉じてから再実行する |
| `ollama` コマンドが見つからない | PowerShell を開き直す（インストール直後は PATH が反映されていない） |
| 日本語が文字化けする | `.ps1` は UTF-8（BOM付き）で保存されています。メモ帳などで編集した場合は「UTF-8（BOM付き）」で保存し直す |
