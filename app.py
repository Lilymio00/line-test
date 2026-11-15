# -*- coding: utf-8 -*-
"""
台南市開放資料 → LINE 官方帳號廣播推播工具
Gradio + FastAPI（PWA + RWD），可部署於 Render
"""
import os
import re
from datetime import datetime
from io import StringIO

import gradio as gr
import pandas as pd
import requests
import urllib3
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from linebot.v3.messaging import (
    ApiClient,
    BroadcastRequest,
    Configuration,
    MessagingApi,
    TextMessage,
)
from linebot.v3.messaging.exceptions import ApiException

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

DEFAULT_URL = (
    "https://data.tainan.gov.tw/File/ResourceCsvDownload/"
    "24b8d897-7971-45f9-8489-b4918ee41ed7"
)

LINE_MAX_CHARS = 4800      # LINE 單則文字上限 5000，保留緩衝
LINE_MAX_BUBBLES = 5       # 一次 broadcast 最多 5 則訊息
MAX_VALUE_LEN = 80         # 單一欄位值最長字數

# ------------------------------------------------------------------ 資料處理
def fetch_dataframe(url: str) -> pd.DataFrame:
    """下載 CSV 並回傳 DataFrame（等同原本 requests.get + read_csv + dropna）"""
    resp = requests.get(url.strip(), verify=False, timeout=30)
    resp.raise_for_status()
    text = resp.content.decode("utf-8-sig", errors="replace")
    df = pd.read_csv(StringIO(text))
    return df.dropna().reset_index(drop=True)


# ------------------------------------------------------------------ 訊息排版
EMOJI_RULES = [
    (("名稱", "名", "標題", "店", "場所", "項目"), "🏷️"),
    (("地址", "位置", "地點", "路"), "📍"),
    (("電話", "聯絡", "手機"), "📞"),
    (("時間", "日期", "時段", "年", "月"), "🕒"),
    (("區", "里", "鄉", "鎮", "市"), "🗺️"),
    (("價", "費", "金額", "票"), "💰"),
    (("網址", "連結", "網站", "URL", "url"), "🔗"),
    (("經度", "緯度", "座標", "lat", "lng"), "🧭"),
    (("類", "種", "型"), "📂"),
    (("數", "量", "人"), "🔢"),
    (("備註", "說明", "內容", "描述"), "📝"),
]
KEYCAPS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
TITLE_HINTS = ("名稱", "標題", "名", "場所", "店", "項目")


def pick_emoji(col: str) -> str:
    for keys, emoji in EMOJI_RULES:
        if any(k in col for k in keys):
            return emoji
    return "🔹"


def pick_title_col(columns) -> str:
    for hint in TITLE_HINTS:
        for c in columns:
            if hint in str(c):
                return c
    return columns[0]


def clean_value(v) -> str:
    s = re.sub(r"\s+", " ", str(v)).strip()
    if s.endswith(".0") and s[:-2].lstrip("-").isdigit():
        s = s[:-2]
    return s if len(s) <= MAX_VALUE_LEN else s[: MAX_VALUE_LEN - 1] + "…"


def format_item(idx: int, row: pd.Series, title_col: str) -> str:
    badge = KEYCAPS[idx - 1] if idx <= len(KEYCAPS) else f"▪️{idx}."
    lines = [f"{badge} {clean_value(row[title_col])}"]
    for col, val in row.items():
        if col == title_col:
            continue
        v = clean_value(val)
        if v:
            lines.append(f"  {pick_emoji(str(col))} {col}：{v}")
    return "\n".join(lines)


def build_messages(df: pd.DataFrame, count: int, title: str, add_footer: bool):
    """把前 count 筆資料排版成 1~5 則 LINE 訊息（自動依字數切段）"""
    sub = df.head(int(count))
    if sub.empty:
        return []

    title_col = pick_title_col(list(sub.columns))
    now = datetime.now().strftime("%Y/%m/%d %H:%M")
    header = f"📢 {title}\n🗓️ {now}\n📊 共 {len(sub)} 筆資訊\n━━━━━━━━━━━━"
    divider = "\n┈┈┈┈┈┈┈┈┈┈┈┈\n"
    footer = "\n━━━━━━━━━━━━\n✨ 以上資訊僅供參考，謝謝您的關注！" if add_footer else ""

    items = [format_item(i, r, title_col) for i, (_, r) in enumerate(sub.iterrows(), 1)]

    bubbles, cur = [], header
    for it in items:
        candidate = cur + divider + it
        if len(candidate) + len(footer) > LINE_MAX_CHARS and cur != header:
            bubbles.append(cur)
            cur = it
        else:
            cur = candidate
    bubbles.append(cur + footer)

    if len(bubbles) > LINE_MAX_BUBBLES:
        bubbles = bubbles[:LINE_MAX_BUBBLES]
    return bubbles


def messages_preview(bubbles) -> str:
    if not bubbles:
        return "（沒有可預覽的內容）"
    parts = []
    for i, b in enumerate(bubbles, 1):
        parts.append(f"【第 {i}/{len(bubbles)} 則・{len(b)} 字】\n{b}")
    return "\n\n==========\n\n".join(parts)


# ------------------------------------------------------------------ Gradio 動作
def action_preview(url, count, title, footer):
    try:
        df = fetch_dataframe(url or DEFAULT_URL)
    except Exception as e:
        return "", f"❌ 下載資料失敗：{e}"
    bubbles = build_messages(df, count, title or "最新資訊", footer)
    total = len(df)
    note = f"✅ 已載入 {total} 筆資料，預覽前 {min(int(count), total)} 筆，共 {len(bubbles)} 則訊息。"
    if int(count) > total:
        note += f"（資料只有 {total} 筆）"
    return messages_preview(bubbles), note


def action_send(api_key, url, count, title, footer, confirm):
    api_key = (api_key or "").strip()
    if not api_key:
        return "⚠️ 請先輸入 LINE Channel Access Token。"
    if not confirm:
        return "⚠️ 請勾選「我確認要廣播給所有好友」後再發送。"
    try:
        df = fetch_dataframe(url or DEFAULT_URL)
    except Exception as e:
        return f"❌ 下載資料失敗：{e}"

    bubbles = build_messages(df, count, title or "最新資訊", footer)
    if not bubbles:
        return "⚠️ 沒有可發送的資料。"

    try:
        with ApiClient(Configuration(access_token=api_key)) as client:
            MessagingApi(client).broadcast(
                BroadcastRequest(messages=[TextMessage(text=b) for b in bubbles])
            )
        return f"✅ 已成功廣播 {len(bubbles)} 則訊息（共 {min(int(count), len(df))} 筆資料）！"
    except ApiException as e:
        hint = {
            401: "Token 無效或已過期。",
            403: "此帳號沒有廣播權限。",
            429: "已超過本月訊息額度或請求過於頻繁。",
        }.get(e.status, "")
        return f"❌ LINE API 錯誤（{e.status}）{hint}\n{e.body}"
    except Exception as e:
        return f"❌ 發送失敗：{e}"


# ------------------------------------------------------------------ 前端（PWA + RWD）
HEAD = """
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#06C755">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="LINE推播">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/static/icons/icon-192.png">
<link rel="apple-touch-icon" href="/static/icons/icon-192.png">
<script>
if ("serviceWorker" in navigator) {
  window.addEventListener("load", function () {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(console.warn);
  });
}
</script>
"""

CSS = """
.gradio-container { max-width: 960px !important; margin: 0 auto !important;
  padding-left: max(12px, env(safe-area-inset-left)) !important;
  padding-right: max(12px, env(safe-area-inset-right)) !important; }
#app-title { text-align: center; }
#app-title h1 { font-size: clamp(1.4rem, 4.5vw, 2.1rem); margin-bottom: .2rem; }
#app-title p { color: #666; font-size: clamp(.85rem, 2.8vw, 1rem); }
.line-btn { background: #06C755 !important; color: #fff !important; border: none !important; }
.line-btn:hover { background: #05a647 !important; }
#preview textarea { font-family: ui-monospace, "Noto Sans TC", monospace; line-height: 1.55; }
@media (max-width: 768px) {
  .gradio-container { padding-top: 8px !important; }
  .row, .form { flex-wrap: wrap !important; }
  .row > * { min-width: 100% !important; }
  button { min-height: 46px; font-size: 1rem !important; }
}
"""

with gr.Blocks(title="LINE 廣播推播工具", head=HEAD, css=CSS, theme=gr.themes.Soft(primary_hue="green")) as demo:
    gr.Markdown(
        "# 📢 LINE 廣播推播工具\n台南市開放資料 → 自動排版 → 一鍵廣播給所有好友",
        elem_id="app-title",
    )

    with gr.Group():
        api_key = gr.Textbox(
            label="🔑 LINE Channel Access Token",
            type="password",
            placeholder="貼上您的 Channel Access Token（不會被儲存）",
        )
        url = gr.Textbox(label="🔗 CSV 資料網址", value=DEFAULT_URL)

    with gr.Row():
        count = gr.Slider(1, 50, value=10, step=1, label="📨 投放則數（資料筆數）")
        title = gr.Textbox(label="🏷️ 訊息標題", value="台南市最新資訊")

    with gr.Row():
        footer = gr.Checkbox(value=True, label="✨ 加入結尾語")
        confirm = gr.Checkbox(value=False, label="✅ 我確認要廣播給所有好友")

    with gr.Row():
        btn_preview = gr.Button("👀 載入並預覽", variant="secondary")
        btn_send = gr.Button("🚀 發送廣播", elem_classes="line-btn")

    status = gr.Markdown()
    preview = gr.Textbox(label="📱 訊息預覽", lines=18, max_lines=30, show_copy_button=True, elem_id="preview")

    btn_preview.click(action_preview, [url, count, title, footer], [preview, status])
    btn_send.click(action_send, [api_key, url, count, title, footer, confirm], status)


# ------------------------------------------------------------------ FastAPI + PWA 路由
app = FastAPI(title="LINE Broadcast")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/healthz")
def healthz():
    return PlainTextResponse("ok")


@app.get("/manifest.json")
def manifest():
    return FileResponse(os.path.join(STATIC_DIR, "manifest.json"), media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return FileResponse(
        os.path.join(STATIC_DIR, "sw.js"),
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@app.get("/offline.html")
def offline():
    return FileResponse(os.path.join(STATIC_DIR, "offline.html"))


app = gr.mount_gradio_app(app, demo, path="/")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    uvicorn.run(app, host="0.0.0.0", port=port)
