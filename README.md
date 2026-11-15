# LINE 廣播推播工具（Gradio + PWA + RWD）

## 本機執行
```bash
pip install -r requirements.txt
python app.py        # http://localhost:7860
```

## 部署到 Render
1. 將本資料夾上傳到 GitHub 新 repo
2. Render → New → Blueprint（或 Web Service）→ 選該 repo
3. Web Service 設定：Build `pip install -r requirements.txt`，Start `python app.py`
4. 部署完成後用手機瀏覽器開啟，選「加入主畫面」即可當 App 使用

## 注意
- Channel Access Token 只在使用者輸入時使用，不會寫入檔案或環境變數。
- broadcast 會發給所有好友並消耗訊息額度，請先「預覽」再發送。
- 單次最多 5 則訊息、每則 5000 字，程式會自動分段。
