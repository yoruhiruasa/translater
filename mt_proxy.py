"""
mt_proxy.py - LiveCaptions-Translator と Ollama の間に入るローカル専用プロキシ

役割:
  1. モデルに合わせて入力形式を変換する
       --mode plamo       : PLaMo 3 Translate（PFML3 形式。直前の文を履歴として渡す）
       --mode tg          : TranslateGemma（専用プロンプト形式）
       --mode passthrough : Qwen / Gemma など（Translator のプロンプトをそのまま使う）
  2. 話している途中の「古い」翻訳要求を捨て、最新の1件だけを CPU で処理する
  3. ライブキャプションの決まった聞き間違いを、翻訳前に置換する（--fix）
  4. 用語集で訳語を固定する（--glossary、plamo モードのみ）
  5. Ollama の応答を Translator が読み込める形に整える

LiveCaptions-Translator 側の設定:
  Translate API = Ollama / API URL = http://127.0.0.1:11500 / Model = 何でもよい

標準ライブラリだけで動きます（pip install 不要）。
待ち受けは 127.0.0.1 のみ。外部への通信は行いません。
"""
import argparse
import difflib
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---- TranslateGemma（Ollama のモデルページ記載の形式。テキスト前は空行2つ） ----
TG_TEMPLATE = (
    "You are a professional {sl} ({sc}) to {tl} ({tc}) translator. "
    "Your goal is to accurately convey the meaning and nuances of the original {sl} text "
    "while adhering to {tl} grammar, vocabulary, and cultural sensitivities.\n"
    "Produce only the {tl} translation, without any additional explanations or commentary. "
    "Please translate the following {sl} text into {tl}:\n\n\n{text}"
)
LANG_NAMES = {"en": "English", "ja": "Japanese"}

# ---- PLaMo 3 Translate（PFML3。Hugging Face のモデルカード記載の形式） ----
P_TAG, P_MSG, P_KEY, P_VAL = "<|plamo:tag|>", "<|plamo:msg|>", "<|plamo:key|>", "<|plamo:val|>"
P_INDEX, P_ITEM, P_BOS = "<|plamo:index|>", "<|plamo:item|>", "<|plamo:bos|>"
PLAMO_STOP = [P_TAG, P_BOS]
PLAMO_TOKEN_RE = re.compile(r"<\|plamo:[a-z]+\|>")

MARKER = "\U0001F524"  # Translator が原文を囲む 🔤
EOS_CHARS = ".?!。？！"

state = {"latest": 0, "last_src": None, "last_resp": None, "skipped": 0, "skipped_partial": 0,
         "done": 0, "last_time": 0.0, "last_gen_end": 0.0}
history = deque(maxlen=8)       # 確定した文の (原文, 訳文)。plamo モードで使う

# Ollama への通信では、社内プロキシの設定（環境変数・Windows のレジストリ）を一切使わない。
# 既定の urllib は 127.0.0.1 宛てでもプロキシ経由にすることがあり、
# その場合は会議の文字起こしが社内プロキシのサーバーに送られてしまうため。
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
state_lock = threading.Lock()   # state / history の読み書き用
gen_lock = threading.Lock()     # 生成は常に1件ずつ（CPU の取り合いを防ぐ）
args = None
fixes = []
glossary = []


def log(msg):
    if not args.quiet:
        print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def read_tsv(path):
    """# で始まる行と空行を除き、タブ区切りの2列を返す。"""
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line.strip() or line.lstrip().startswith("#") or "\t" not in line:
                continue
            a, b = line.split("\t", 1)
            if a.strip() and b.strip():
                rows.append((a.strip(), b.strip()))
    return rows


def word_re(term):
    return re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.I)


def load_fixes(path):
    """聞き間違い<TAB>正しい語。大文字小文字は区別しない。"""
    return [(word_re(w), r) for w, r in read_tsv(path)]


def load_glossary(path):
    """英語<TAB>日本語。原文に英語が含まれる行だけを毎回モデルに渡す。"""
    return [(word_re(en), en, ja) for en, ja in read_tsv(path)]


def apply_fixes(text):
    for pat, right in fixes:
        text = pat.sub(right, text)
    return text


def extract_source(messages):
    """Translator が送る最後の user メッセージから原文を取り出す。"""
    for m in reversed(messages or []):
        if m.get("role") == "user":
            return m.get("content", "").replace(MARKER, "").strip()
    return ""


def clean_plamo_text(text):
    """PFML3 の予約記号を含む入力は受け付けられないので無害化する。"""
    return text.replace("<|plamo:", "<|").replace("\n", " ").strip()


def is_complete(text):
    return bool(text) and text[-1] in EOS_CHARS


def norm(text):
    """比較用：小文字にし、末尾の句読点を除く（"...on this test." と "...on this test and ..." を同じ文の続きと判定するため）"""
    return text.lower().strip().rstrip(".?!。？！,、 ")


def similar(a, b):
    """同じ文の書き直し（途中経過・聞き直し・後ろへの継ぎ足し）とみなせるか。"""
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    if a == b or a.startswith(b) or b.startswith(a):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.75


def remember(src, out):
    """確定した文（文末が . ? !）だけを履歴に残す。同じ文の書き直しなら置き換える。"""
    if args.history <= 0 or not is_complete(src) or not out:
        return
    with state_lock:
        if history and similar(history[-1][0], src):
            history[-1] = (src, out)
        else:
            history.append((src, out))
        state["last_time"] = time.time()


def recent_history(src):
    """今の文と重複しない、直近 N 文の履歴を返す。しばらく発言がなければ話題が変わったとみなして消す。"""
    with state_lock:
        if history and time.time() - state["last_time"] > args.history_ttl:
            history.clear()
        # 今の文に含まれる（＝今の文の一部として訳し直す）履歴は渡さない。渡すと、モデルが訳済みとみなして省略する
        items = [h for h in history if not similar(h[0], src) and norm(h[0]) not in norm(src)]
    return items[-args.history:] if args.history > 0 else []


def build_pfml3(src, hist, gloss):
    sl, tl = args.src, args.tgt
    s = (P_BOS if args.add_bos else "") + P_TAG + "dataset" + P_MSG + "translation"
    if args.context:
        s += P_TAG + "context" + P_MSG + clean_plamo_text(args.context)
    for h_src, h_out in hist:
        s += P_TAG + "input" + P_KEY + "lang" + P_VAL + sl + P_MSG + clean_plamo_text(h_src)
        s += P_TAG + "output" + P_KEY + "lang" + P_VAL + tl + P_MSG + clean_plamo_text(h_out)
    if gloss:
        s += P_TAG + "glossary" + "".join(P_INDEX + clean_plamo_text(en) + P_ITEM + clean_plamo_text(ja)
                                           for en, ja in gloss)
    s += P_TAG + "input" + P_KEY + "lang" + P_VAL + sl + P_MSG + clean_plamo_text(src)
    s += P_TAG + "output" + P_KEY + "lang" + P_VAL + tl
    if args.form != "none" and tl == "ja":
        s += P_KEY + "form" + P_VAL + args.form
    return s + P_MSG


def call_ollama(path, body):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        args.upstream.rstrip("/") + path, data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    with DIRECT.open(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def make_response(content, base=None):
    resp = dict(base or {})
    for k in ("response", "context", "thinking"):  # /api/generate の項目は Translator に不要
        resp.pop(k, None)
    resp["model"] = resp.get("model", args.model)
    resp["message"] = {"role": "assistant", "content": content}
    # Translator は load_duration を 32bit 整数で読むため、大きな値だと読み込みに失敗する
    if isinstance(resp.get("load_duration"), int) and resp["load_duration"] > 2_000_000_000:
        resp["load_duration"] = 0
    resp.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    resp["done"] = True
    return resp


def translate(req_body):
    src = apply_fixes(extract_source(req_body.get("messages")))
    with state_lock:
        state["latest"] += 1
        my_id = state["latest"]

    if not src:
        return make_response("")

    t_arrive = time.perf_counter()
    with gen_lock:
        wait = time.perf_counter() - t_arrive
        complete = is_complete(src)
        with state_lock:
            superseded = my_id != state["latest"]
            same_as_last = src == state["last_src"]
            last = state["last_resp"]
            since_gen = time.time() - state["last_gen_end"]
        # 待っている間に新しい要求が来ていたら、この（古い）要求は生成しない
        if (superseded or same_as_last) and last is not None:
            with state_lock:
                state["skipped"] += 1
            return last
        # 話している途中の文は、前回の生成から partial_gap 秒たつまで訳さない（CPU の負荷を下げる）。
        # 文末（. ? !）まで確定した文は常にすぐ訳す。
        if not complete and last is not None and since_gen < args.partial_gap:
            with state_lock:
                state["skipped_partial"] += 1
            return last

        options = {"temperature": args.temperature, "num_predict": args.max_tokens}
        if args.threads:
            options["num_thread"] = args.threads
        extra = ""
        if args.mode == "plamo":
            hist = recent_history(src)
            gloss = [(en, ja) for pat, en, ja in glossary if pat.search(src)][:12]
            options["stop"] = PLAMO_STOP
            body = {"model": args.model, "prompt": build_pfml3(src, hist, gloss),
                    "raw": True, "stream": False}
            path = "/api/generate"
            extra = f" 履歴{len(hist)}" + (f" 用語{len(gloss)}" if gloss else "")
        elif args.mode == "tg":
            sl, tl = LANG_NAMES.get(args.src, args.src), LANG_NAMES.get(args.tgt, args.tgt)
            prompt = TG_TEMPLATE.format(sl=sl, sc=args.src, tl=tl, tc=args.tgt, text=src)
            body = {"model": args.model, "stream": False,
                    "messages": [{"role": "user", "content": prompt}]}
            path = "/api/chat"
        else:  # passthrough: Translator のプロンプトをそのまま使い、モデル名だけ差し替え
            body = dict(req_body)
            body["stream"] = False
            body["model"] = args.model
            msgs = [dict(m) for m in body.get("messages", [])]
            for m in reversed(msgs):
                if m.get("role") == "user":
                    m["content"] = f"{MARKER} {src} {MARKER}"
                    break
            body["messages"] = msgs
            body.setdefault("think", False)
            path = "/api/chat"

        body["keep_alive"] = args.keep_alive
        body["options"] = options

        t0 = time.perf_counter()
        resp = call_ollama(path, body)
        dt = time.perf_counter() - t0
        if args.mode == "plamo":
            out = resp.get("response", "")
        else:
            out = (resp.get("message") or {}).get("content", "")
        out = PLAMO_TOKEN_RE.sub("", out.replace(MARKER, "")).replace("\n", " ").strip()
        result = make_response(out, resp)

        with state_lock:
            state["last_src"], state["last_resp"] = src, result
            state["done"] += 1
            state["last_gen_end"] = time.time()
        if args.mode == "plamo":
            remember(src, out)

        tok = resp.get("eval_count") or 0
        ev = (resp.get("eval_duration") or 0) / 1e9
        rate = f"{tok / ev:.1f} tok/s" if ev else "-"
        warn = "  ※8秒超: Translator側でタイムアウトします" if wait + dt > 8 else ""
        kind = "確定" if complete else "途中"
        log(f"{kind} 生成{dt:4.1f}s 待ち{wait:4.1f}s {rate:>11}{extra} | {src[:60]}  →  {out[:50]}{warn}")
        return result


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # 標準のアクセスログは出さない
        pass

    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # Translator 側が古い要求を取り消した

    def _forward(self, method, body=None):
        req = urllib.request.Request(args.upstream.rstrip("/") + self.path, data=body, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with DIRECT.open(req, timeout=30) as r:
                self._send(r.status, r.read(), r.headers.get("Content-Type", "application/json"))
        except urllib.error.HTTPError as e:
            self._send(e.code, e.read())
        except Exception as e:
            self._send(502, {"error": str(e)})

    def do_GET(self):
        if self.path == "/proxy/status":
            with state_lock:
                self._send(200, {"mode": args.mode, "model": args.model,
                                 "translated": state["done"], "skipped": state["skipped"],
                                 "skipped_partial": state["skipped_partial"],
                                 "history": len(history)})
            return
        self._forward("GET")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if self.path == "/proxy/reset":  # 履歴を消す（話題が変わった時など）
            with state_lock:
                history.clear()
            self._send(200, {"history": 0})
            return
        if self.path.rstrip("/") != "/api/chat":
            self._forward("POST", raw)
            return
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
            self._send(200, translate(body))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            log(f"Ollama がエラーを返しました（{e.code}）: {detail}")
            self._send(502, {"error": f"ollama error {e.code}: {detail}"})
        except urllib.error.URLError as e:
            log(f"Ollama に接続できません: {e}")
            self._send(502, {"error": f"ollama unreachable: {e}"})
        except Exception as e:
            log(f"エラー: {e!r}")
            self._send(500, {"error": str(e)})


def main():
    global args, fixes, glossary
    p = argparse.ArgumentParser(description="LiveCaptions-Translator 用ローカル翻訳プロキシ")
    p.add_argument("--mode", choices=["plamo", "tg", "passthrough"], default="plamo",
                   help="plamo: PLaMo 3 Translate / tg: TranslateGemma / passthrough: Qwen・Gemma など")
    p.add_argument("--model", default="mt-plamo", help="Ollama のモデル名（既定: mt-plamo）")
    p.add_argument("--port", type=int, default=11500)
    p.add_argument("--upstream", default="http://127.0.0.1:11434", help="Ollama の URL")
    p.add_argument("--src", default="en", help="原文の言語コード（既定: en）")
    p.add_argument("--tgt", default="ja", help="訳文の言語コード（既定: ja）")
    p.add_argument("--temperature", type=float, help="既定: plamo は 0、それ以外は 0.1")
    p.add_argument("--max-tokens", type=int, default=200)
    p.add_argument("--keep-alive", default="60m")
    p.add_argument("--fix", help="聞き間違い置換表（TSV）のパス")
    p.add_argument("--glossary", help="用語集（英語<TAB>日本語 の TSV）のパス。plamo モードのみ")
    p.add_argument("--history", type=int, default=2,
                   help="直前の確定文を何文まで履歴として渡すか（plamo モード。0で無効。既定: 2）")
    p.add_argument("--history-ttl", type=float, default=90,
                   help="この秒数発言がなければ履歴を消す（既定: 90）")
    p.add_argument("--context", default="", help="会議の概要（例: 'Quarterly sales review meeting.'）。plamo モード")
    p.add_argument("--form", choices=["none", "plain", "polite"], default="none",
                   help="日本語の文体（plamo モード）。plain=常体、polite=です・ます調")
    p.add_argument("--threads", type=int, default=0,
                   help="Ollama が使う CPU スレッド数（0=Modelfile の設定。1340P では 4〜8 で調整）")
    p.add_argument("--partial-gap", type=float, default=4.0,
                   help="話している途中の文を訳す最短間隔（秒。既定 4。0 で毎回訳す）。確定した文は常にすぐ訳す")
    p.add_argument("--add-bos", action="store_true",
                   help="先頭に <|plamo:bos|> を付ける（通常は Ollama が自動で付けるので不要）")
    p.add_argument("--quiet", action="store_true", help="原文・訳文をコンソールに表示しない")
    args = p.parse_args()

    if args.temperature is None:
        args.temperature = 0.0 if args.mode == "plamo" else 0.1
    if args.fix:
        fixes = load_fixes(args.fix)
    if args.glossary and args.mode == "plamo":
        glossary = load_glossary(args.glossary)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)  # ローカルのみ
    server.daemon_threads = True
    info = (f"mode={args.mode}, model={args.model}, 置換={len(fixes)}件, "
            f"途中の文の間隔={args.partial_gap:g}秒, スレッド={args.threads or 'Modelfile'}")
    if args.mode == "plamo":
        info += f", 用語集={len(glossary)}件, 履歴={args.history}文, 文体={args.form}"
    print(f"mt_proxy: http://127.0.0.1:{args.port}  →  {args.upstream}  ({info})")
    print("Translator の API URL にこのアドレスを設定してください。Ctrl+C で終了。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n終了します。")
        sys.exit(0)


if __name__ == "__main__":
    main()
