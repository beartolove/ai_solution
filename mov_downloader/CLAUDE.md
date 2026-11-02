# CLAUDE.md — 使用習慣與專案規範

> 本檔記錄使用者的固定要求，Claude 每次協作前請先閱讀並遵守；有新要求即補充於此。
> 1.24.0（2026-11-02）建立，參考 `D:\code_projects\transcribe\CLAUDE.md` 的做法。

## 語言
- 一律使用**正體中文、臺灣用語**（軟體、影片、資料夾、程式碼、預設、網路、檔案）。
- 簡體 → 正體一律用 OpenCC **`s2tw`**（只轉字形）。**不可用 `s2twp`**：它會把正確的詞也換掉（連接詞→連線詞、對象→物件、發佈→釋出；transcribe 2026-10-31 實測）。大陸用語只用 `subtitles.py` 的 `_TW_TERMS` 小詞表處理，只收「幾乎不會換錯」的詞。
- 每次回應前，先考量是否有可用的 Skill 能協助。

## 文件與版控
- 專案根目錄必須維護：
  - `README.md`：操作說明，功能一有變動就同步更新。
  - `CHANGELOG.md`：依 Keep a Changelog 格式，採語意化版號（MAJOR.MINOR.PATCH）。
  - `CLAUDE.md`：本檔。
  - `VERSION`：**唯一版本來源**，伺服器（`server.py`）讀它，`static/app.js` 的版本由伺服器填入，不要再寫死版號。
- 每次修改都要遞增版號並寫入 CHANGELOG；Chrome 擴充功能有改時也要遞增 `chrome_extension/manifest.json` 的版本。
- claude.ai 專案「影片下載器」裡的 README.md、CHANGELOG.md 要同步更新。

## 使用介面（UI）要求
- 必須兼顧 **RWD**（手機、平板、筆電、桌機）。
- **字型可放大縮小**，並記住使用者設定。
- 畫面簡單直覺，以**左側可收合選單**為主要導覽。
- 主介面 LOGO 旁顯示目前版本號；介面與伺服器版本不一致時標紅提示。

## 安裝與執行
- 使用者唯一需要手動安裝的是 Python（3.10 以上，建議 3.11.9）。其餘由 `start.bat` 自動完成，只裝當下功能用得到的東西。
- 語音辨識元件（openvino-genai）第一次使用時才安裝，版本一律 `>=2026.4`；載入 OpenVINO 模型前一律經 `app\ovcompat.py` 比對版本（版本太舊會在底層當掉、攔不到錯誤）。
- `start.bat`、`update.bat` 是 UTF-8＋**CRLF**；修改時一定要保持（變成 LF 時雙擊會一閃就消失，1.24.1 發生過）。改完用 `file start.bat` 確認顯示 with CRLF line terminators。
- 程式**不可自動刪除使用者的檔案**：舊環境改名保留（例：`.venv-old`），由使用者自行刪除。刪除影片等動作一定要使用者在網頁上按下並確認。

## 本機 AI 模型（1.25.0）
- 模型一律從「模型位置」讀取：`app\modelpaths.py` 的 `root()`（設定 `models_root` → 環境變數 `MODELS_ROOT` → `D:\models`），子資料夾與 transcribe 專案的 `models.json` 相同，不寫死路徑。新增模型時加進 `modelpaths.MODELS`，並對照 transcribe 的位置。
- **不可使用中國廠商開發的模型**（阿里 Qwen、SenseVoice、FunASR、DeepSeek、百度、騰訊、字節等），新增前先確認開發者國別與授權（沿用 transcribe 的原則）。
- 本程式**不刪除模型**。Gemma 4（Hugging Face 公開、不需帳號）可在設定頁下載（1.26.0，2026-11-02 使用者要求）；Breeze、turbo（需轉換）、TranslateGemma（需同意條款）由管理者準備或從 transcribe 母機複製。一般使用者不需要 Hugging Face 帳號或 Token。
- 本機 LLM（`app\local_llm.py`）同一時間只留一個模型在記憶體，閒置 10 分鐘釋放；載入前一律經 `ovcompat` 檢查。
- 測試本機模型的程式碼時，用假的 OpenVINO 模組在暫存複本測，不把使用者的模型或影片複製到雲端。

## 程式撰寫注意
- 設定、佇列、影片資訊、字幕、摘要等檔案一律用 `app\fsutil.py` 的 `atomic_write_text`／`atomic_write_json`（先寫暫存再替換），不要直接 `write_text` 覆寫。
- 本機 API 安全（`server.py` 的 `guard_origin`）：
  - 網頁只接受本機來源（127.0.0.1／localhost）的寫入請求。
  - Chrome 擴充功能**必須配對**：第一次同步 Cookie 自動配對（`data\ext_pair.json`），之後只認那個 ID；擴充功能只能用 `EXT_PATHS`（`/api/cookies`、`/api/enqueue`、`/api/ping`）。新增給擴充功能用的 API 時要加進 `EXT_PATHS`。
- Cookie 檔等同登入憑證：只存在 `data\`，不寫進紀錄，交給 yt-dlp 時用暫存複本。

## 備份（1.27.0，2026-11-02 使用者要求）
- 程式備份到 GitHub **公開**儲存庫 `beartolove/ai_solution` 的 `mov_downloader` 資料夾，用 `admin-backup-github.cmd`（工作複本 `D:\code_projects\ai_solution`，與 transcribe 共用；專案本身不執行 git）。
- 新增檔案時要想到它會公開：`.env`、`data\`（Cookie、密碼）、影片一律不可放進會上傳的位置。
- `scripts\backup-github.ps1` 必須是 UTF-8 **含 BOM**＋CRLF（Windows PowerShell 5.1 才讀得懂中文）。

## 使用者電腦的設定
- 母機的下載資料夾是 `D:\mov_dowoloader：download`，這是使用者刻意的名稱（2026-11-02 確認），不是打錯字，不要再提醒或更改。

## 與 transcribe 專案的關係
- `D:\code_projects\transcribe`（影音轉逐字稿）是較新的專案，很多做法（OpenVINO 相容、s2tw、配對、原子寫入）是從那邊學來的；兩個專案**各自獨立運作**，只參考做法，不互相讀取或呼叫檔案。

## 開發與測試
- 程式在使用者的 Windows 電腦執行。Claude 測試時在專案以外的暫存複本進行，不在專案資料夾留下測試資料；不可把使用者的影片、Cookie、`.env` 複製到雲端。

## 待辦（2026-11-02 與 transcribe 比較後的建議，尚未實作）
- 語音辨識直接從 MP4 解碼 16kHz（不先轉 MP3，避免再壓縮一次），長影片自動切段、每段存檔（參考 transcribe `chunked.py`）。
- Cookie 先不帶、需要登入才帶入重試；錯誤分類參考 transcribe `downloader.classify()`。
- 檔名依整條路徑長度控制在 240 字元內（參考 transcribe `naming.py`）。
- yt-dlp 每天背景自動更新一次。
- 加入 ROADMAP.md 與 GitHub 備份腳本。
