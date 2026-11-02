# 影音轉逐字稿（transcribe）

專案位置：`D:\code_projects\transcribe`　｜　模型庫：`D:\models`（只放模型）　｜　媒體檔：`D:\mediafiles`（上傳檔與轉檔結果）

版本：見 `VERSION`（目前 0.14.1）　｜　開發規劃與進度：[`ROADMAP.md`](ROADMAP.md)

> **選型原則：不使用中國廠商開發的模型。**只收臺灣、美國、歐洲來源的模型與工具。

---

## 快速開始（每台電腦）

1. 安裝 **Python 3.10 以上，建議 3.11.9**（與母機相同；https://www.python.org/downloads/release/python-3119/ ，安裝時勾選「Add python.exe to PATH」）——這是唯一需要手動安裝的軟體。
   - Python 3.9 以下**不能用**：只能裝到舊版 OpenVINO，載入 `D:\models` 的模型（OpenVINO 2026.4 轉出）會直接當掉。setup 會檢查版本；電腦上同時有新舊版時自動挑新版，舊版 Python 建的 `.venv` 會改名為 `.venv-old-py39` 保留（不刪除），確認正常後可自行刪除。
   - 忘了勾「Add python.exe to PATH」也沒關係：setup 會透過 py 啟動器找到新裝的版本。
2. 雙擊專案根目錄的 **`setup.cmd`**。

`setup.cmd` 會開一個不會自動關閉的視窗，依序自動完成：

| 步驟 | 內容 |
|---|---|
| 1. 模型與媒體檔位置 | 設定 `MODELS_ROOT=D:\models`、`HF_HOME=D:\models\.hf-cache`；顯示媒體檔位置（預設 `D:\mediafiles`，可在網頁「參數設定」修改），資料夾不存在就自動建立 |
| 2. 環境初測 | 偵測 CPU／記憶體／顯示卡，決定等級與辨識引擎，報告存到 `env-reports\` |
| 3. 執行環境 | 建立專案 `.venv` 並安裝 `requirements.txt`（有變動才重裝）；ffmpeg 系統有就沿用，沒有才自動下載；順便更新 yt-dlp |
| 4. 辨識模型 | 準備 OpenVINO 辨識模型（轉逐字稿必備） |
| 5. 這台電腦 | **第一次**詢問電腦別名（例：辦公室筆電）與要用的功能（轉逐字稿必選；整理逐字稿、翻譯、分辨說話者可勾選） |
| 6. 準備模型 | 依「功能＋這台硬體＋效能實測」算出**模型配置方案**，只準備需要的模型（共用來源有就複製，可匿名下載的從 Hugging Face 下載） |
| 7. 配置方案報告 | 列出每個功能用哪個模型、哪個裝置；沒選用的模型與本專案用不到的資料夾只告知，程式**不會刪除任何檔案**；方案存到 `env-reports\<電腦名稱>.models.json` |

可重複執行，已完成的步驟會自動略過。

---

## 使用方式

| 用途 | 操作 |
|---|---|
| 確認／安裝執行環境（每台電腦第一次） | 雙擊 **`setup.cmd`**（命令列） |
| **轉逐字稿、效能實測、看結果** | 雙擊 **`start.cmd`**，瀏覽器會自動開啟 `http://127.0.0.1:8766` |

網頁介面（左上角漢堡按鈕收合／展開選單；字型 A-／A／A+ 與外觀「自動／明亮／暗黑」在選單裡；LOGO 下顯示版本；手機與電腦都能看）：

| 頁面 | 內容 |
|---|---|
| 轉逐字稿 | 拖曳或選擇影音檔（可多個），或**貼上影片網址**（YouTube、Facebook、Bilibili 等公開影片）→ 顯示下載與處理進度；可選模型與語言，預設採用本機建議配置；「清除已完成」或 × 可把完成的任務從清單移除（只是不顯示，檔案與任務紀錄都保留） |
| 任務紀錄 | 所有轉檔任務：可加標籤（#專案、#會議…，點標籤即篩選）、勾選多筆批次處理（加／移除標籤、分辨說話者、翻譯、下載字幕 ZIP、刪除）；顯示建立日期時間與來源，可搜尋（名稱／網址）、篩選（狀態、來源、期間、有分辨說話者／翻譯）、排序（建立時間、名稱、錄音長度、辨識耗時、狀態、來源，升冪／降冪）。任務頁**影片在最上面**，播放器仿 YouTube：控制列疊在畫面下緣（進度條、播放、音量、時間、字幕開關、倍速 0.25～2×、全螢幕），播放中閒置會自動隱藏；點畫面播放／暫停、雙擊全螢幕；鍵盤：空白鍵播放／暫停、←／→ 5 秒、F 全螢幕、C 字幕、M 靜音。下方依序是資訊、下載 SRT／純文字／MP3（或原始錄音）、逐字稿（跟著播放標亮、點任一句跳到該處） |
| 本機效能 | 執行效能實測並以白話解讀：這台 1 小時錄音要多久、效能評等、時間比例圖、各配置比較、長檔切段與檔案格式建議 |
| 參數設定 | 媒體檔位置（預設 `D:\mediafiles`）：顯示目前位置、可用空間、任務數；可修改或還原預設，舊檔案不會被搬移或刪除。模型位置（預設 `D:\models`）：顯示各模型是否就緒，可修改（會先檢查新位置有沒有模型，改完需重新開啟 start.cmd） |

- 一次只處理一個工作（轉檔或實測），其餘排隊，避免顯示卡同時被搶用。
- **觀看模式／學習模式**（0.9.0）：任務頁上方切換。學習模式影片在左、逐字稿在右（影片上不疊字幕），播到哪句就標亮哪句，點句子跳到該處，適合雙語對照學習；學習模式雙擊影片切回觀看模式；進入學習模式時左側選單自動收合，讓影片更大。
- **字幕外觀**（0.9.4）：播放器「Aa」按鈕可調字幕大小、位置（下方／上方）、原文與中文譯文顏色、背景，設定會記住。
- **翻譯成中文**（0.9.0）：外文影片在任務頁按「翻譯成中文（雙語對照）」，用本機的 Google TranslateGemma 4B 翻譯（不上傳到網路），完成後「字幕」可選原文／中文／雙語對照，並可下載中文字幕與雙語字幕。需要管理者先執行一次 `admin-download-translator.cmd` 準備模型（見下方「管理者一次性作業」）。
- **專有名詞庫與校對**（0.11.0）：
  - 「⚙ 辨識設定」選「專有名詞庫：港務」，辨識時會提示模型專有名詞、辨識後自動修正常見誤寫（本身也是常用詞的誤寫只提醒、不自動換）。已完成的任務可在任務頁按「套用詞庫」。
  - **整理稿**（需要本機 LLM）：任務頁「整理稿」按「整理逐字稿」（或在「⚙ 辨識設定 → 整理逐字稿」選「轉完自動整理」），把一句一行的字幕整理成有標點、依語意分段、有小標題（■）的純文字，**整理稿直接取代純文字檔「檔名.txt」**（之後校對字幕也不會蓋回一句一行；需要一句一行版本可在整理稿區由時間軸即時下載），可預覽、複製全文；SRT 字幕不動。不改寫、不刪減：分兩步——先每約 450 字請模型加標點（檢查沒改字、真的有加標點，不合格重試，仍不行該小段改用保守整理並註明），再只請模型決定分段位置與小標題、文字由程式組回。校對後可「重新整理」。
  - 任務頁「校對模式」（與觀看／學習模式並列，可全螢幕）：影片下方顯示上一句／這一句（黃字，可直接編輯）／下一句；Enter 存檔並繼續播放、Esc 取消、鍵盤左右鍵（編輯中用 Tab／Shift+Tab 或 Alt+左右鍵）換句、Ctrl+Space 重播。每句改完立即存檔，打字中的內容暫存在瀏覽器，存檔失敗會自動重試。學習模式的逐字稿也可直接點文字修改。
  - 校對途中每改 10 句，自動檢查改過的地方有沒有要加入詞庫的詞，列在播放器右側「📖 詞庫候選」，勾選後一次加入，並可立即套用到這句起還沒校對的句子（「自動替換」的馬上更正字幕；「只提醒」的在字幕編輯列下方標示「⚠「船隻」可能是 船席」；「只加正確寫法」不改現有逐字稿，只幫之後的辨識）。
  - 側邊欄「＋ 自己新增」：當下沒錯也能先記下專有名詞（正確寫法必填、常見誤寫選填、處理方式三選一）；先在字幕上選取文字再按，會自動帶入。每一項顯示前後文（刪除線＝原稿、黃字＝校對後），可用「＋◀／▶＋」把前後的字一起帶進來（例：電力→電信 擴成 中華電力→中華電信）；處理方式可選「只加正確寫法（當辨識提示，不記誤寫）」「記誤寫，只提醒」「記誤寫，自動替換」，誤寫是兩個字以下或常用詞時預設不記誤寫。
  - 「正確率報告」：校對一段（例如前 10 分鐘）後產生，顯示專有名詞正確率、字錯率與模型聽錯的寫法，可一鍵加入詞庫。
  - 「從校對結果挑選加入詞庫」：列出你改過的每一處（誤寫→正確寫法），逐筆勾選、可修改寫法、可設「只提醒」，加入詞庫的常見誤寫或新增詞。
- **分辨說話者**（0.8.0）：
  - 轉逐字稿時在「分辨說話者」選「自動判斷人數」或指定 2～6 人（知道人數時請指定，較準）。
  - 已完成的任務也能在任務頁按「分辨說話者」補做，只分析聲音、不重新轉文字；分錯時可改人數「重新分辨」。
  - 結果：SRT 每句前加 `[說話者 1]`；純文字依說話者分段；網頁逐字稿用顏色區分。可在任務頁把「說話者 1」改成真實姓名，字幕一起更新。
  - 第一次使用會自動安裝元件（約 1～2 GB；母機實測約 12 分鐘，大部分時間在寫入檔案，請勿關閉 start.cmd 視窗），之後不用再裝。
  - 有 Intel 顯示卡時用顯示卡計算（母機 4 分 19 秒影片約 14 秒）；沒有時用 CPU，會比轉文字多花一些時間。
  - 第一版限制：一句話中途換人時，整句歸給說得較多的那位。
- **網址下載**（0.7.0）：
  - 下載 **mp4 影片**（優先 H.264，最高 1080p），存在 `D:\mediafiles\downloads\<任務>\`；任務頁以影片播放並疊字幕，可「下載影片」，也會顯示來源網址。
  - 辨識直接讀影片裡的聲音，不另存聲音檔或 MP3。只有聲音的網站則下載聲音。
  - 影片標題會先轉成正體中文再當檔名，並自動處理 Windows 不允許的字元（換成全形）、表情符號、過長路徑。
  - 一次一部影片：貼播放清單或頻道網址會提示改貼單一影片網址；直播中的影片要等結束後再下載。
  - **需要登入的影片**（會員限定、私人社團、Instagram 等，0.10.0）：在 Chrome 安裝本專案的「逐字稿登入同步」擴充功能（見下方），程式遇到需要登入時會自動帶入你本人的登入狀態重試。Udemy 等有 DRM 防拷的內容不會破解，請改用上傳。
  - 下載 YouTube 需要 JavaScript 執行環境（Deno 或 Node.js）；缺少時會提示安裝方式。
  - 下載工具 yt-dlp 會在背景自動更新（一天最多一次，不影響啟動速度）。
  - 下載內容請限於你有權使用的範圍，並遵守各平台使用條款。
- **完成提示音**：任務完成時播放「叮—咚」，失敗時播放低音；停在任何一頁或切到其他視窗都會提醒，分頁標題也會顯示「✓ 完成」。可在選單的「完成提示音」關閉或試聽。
- **取消**：處理中或排隊中的任務按「取消」（會先確認）；處理中的任務會在目前這一段（約 30 秒音訊）辨識完後停止。
- **刪除**：完成、失敗或已取消的任務按垃圾桶圖示或「刪除」，**確認後**永久刪除該任務資料夾（字幕、純文字、MP3、網頁上傳的原始檔）。只刪媒體檔位置 `uploads\` 底下程式產生的資料夾，不會動到其他位置的原始檔。
- **從清單移除（×）**：只是不在「處理中與最近完成」顯示，檔案與任務紀錄都保留。
- 網頁只在本機（127.0.0.1）提供，其他電腦連不進來。
- 關閉 `start.cmd` 的視窗就會結束程式；處理中的任務會中斷，需重新上傳。

> 0.6.8 起，轉逐字稿與效能實測一律在網頁介面操作，不再提供 `transcribe.cmd`、`benchmark.cmd`。

### Chrome 擴充功能「逐字稿登入同步」（需要登入的影片才需要）
1. Chrome 網址列輸入 `chrome://extensions`，打開右上角「開發人員模式」。
2. 按「載入未封裝項目」，選擇專案的 `chrome_extension` 資料夾。
3. 按 Chrome 右上角拼圖圖示，把「逐字稿登入同步」釘選出來；點它、按「立即同步」（需先開啟 `start.cmd`）。之後每 30 分鐘自動同步。
4. 在影片頁面或連結上按右鍵，選「轉逐字稿」即可直接加入佇列。

- 預設同步 YouTube（含 Google）、Facebook、Instagram、Threads；X、Bilibili 可在小視窗勾選（會詢問權限）。
- 登入資料只送到本機程式、存在專案 `data\cookies.txt`，不上傳、不寫入紀錄；**等同帳號憑證，不要複製給別人**，其他電腦請各自安裝。
- 「參數設定」可看同步狀態、解除配對（換電腦或重裝擴充功能時）、刪除登入資料。

### 這台電腦：別名、功能與模型配置方案（0.14.0）
網頁「參數設定 → 這台電腦」：
- **電腦別名**：例如「辦公室筆電」「會議室桌機」，顯示在左側選單下方與配置方案，比電腦名稱好認。
- **要用的功能**：轉逐字稿必選；整理逐字稿與詞庫判讀（本機 LLM）、翻譯成中文、分辨說話者可勾選。按「儲存功能」後，缺少且能自動取得的模型會**立刻在背景準備**；取消勾選不會刪除已有的模型。
- **模型配置方案**：每個功能用哪個模型、哪個裝置、為什麼（依效能實測、依記憶體建議…）與是否就緒。
- 專案需要哪些模型記在專案根目錄的 **`models.json`**（隨程式更新）；這台實際採用的方案存在 `env-reports\<電腦名稱>.models.json`。
- 共用來源：環境變數 `MODELS_SOURCE`（例：`\\NAS\models`、`E:\models`）有設定時，缺少的模型優先從那裡複製。

### 效能實測（每台電腦做一次）
開啟 `start.cmd` → 網頁「**本機效能**」→「開始實測」，替這台電腦做健檢：

| 部分 | 內容 | 時間 |
|---|---|---|
| 1. 快速健檢（必跑） | 用 3 分鐘標準樣本（`samples\benchmark_sample.mp3`）測：mp3／m4a／wav／mp4 的處理速度；Breeze／turbo × 顯示卡／CPU 的速度與記憶體 | 約 5–10 分鐘 |
| 2. 長檔切段測試（勾選「包含長檔測試」才跑） | 取 `samples\` 最長的測試錄音前 30 分鐘，比較不切段與每 10 分鐘切段的耗時、記憶體、字數差異、重複句、切點空白 | 約 6–10 分鐘 |

- 報告內容：**建議配置**（模型、裝置、切段長度、檔案格式）、**效能評等**（非常快／快／普通／偏慢／比即時還慢）、**預估處理時間表**（30 分鐘／1 小時／2 小時 × m4a／mp3／影片）、各配置速度與記憶體、長檔切段比較、各格式前處理時間。
- 結果存在 `env-reports\<電腦名稱>.benchmark.md`（給人看，可比較各台電腦）與 `.benchmark.json`；**轉逐字稿時會自動採用這台的建議模型、裝置與切段長度**。
- 測試錄音放在 `samples\`（隨專案發佈）。標準樣本 `samples\benchmark_sample.mp3` 由母機第一次實測時從 `samples\` 最短的錄音自動擷取 3 分鐘；若內容不宜外流，請換成公開內容的錄音。

### 轉檔說明
- 支援格式：mp4、mov、mkv、avi、webm、m4v、wmv、flv、ts；mp3、m4a、wav、aac、flac、ogg、wma、opus、amr。
- **辨識直接從原始檔解碼**（不經過 MP3，避免二次壓縮損失）；上傳後馬上開始辨識。mp3、m4a、aac、ogg、opus 直接保留原檔、不另存 MP3；wav、flac、影片在字幕完成後才另存一份 MP3 供保存。
- **長檔自動切段**：依本機實測建議。記憶體充足的電腦：1 小時內不切段（最快），超過 1 小時每 30 分鐘切一段（中斷可續跑）；記憶體不足 12GB 或切段明顯較好的電腦：每 10 分鐘切一段。未實測時：超過 15 分鐘每 10 分鐘切。
- 結果放在 `D:\mediafiles\uploads\<日期時間>_<檔名>\`（媒體檔位置可在「參數設定」修改）：`.mp3`（保存用）、`.srt`（字幕）、`.txt`（純文字）、`.json`（含時間）、`job.json`（耗時與設定）。任務頁可按「開啟資料夾」。
- 第一次在顯示卡上載入模型約需 1～3 分鐘；之後有快取（`.cache\ov\`）會快很多。
- 進階（命令列，選用）：`.venv\Scripts\python -m app.cli 檔案 --model turbo --device CPU --lang zh --prompt "專有名詞"`

---

## 辨識模型與引擎

| 模型 | 特性 | 授權 |
|---|---|---|
| **Breeze-ASR-25**（聯發創新基地，臺灣） | 主力：臺灣華語、中英夾雜最準，時間戳適合字幕 | Apache 2.0 |
| **Whisper large-v3-turbo**（OpenAI，美國） | 備援與交叉比對；速度較快 | MIT |
| Breeze-ASR-26 | 臺語辨識（之後視需要加入） | 以官方公告為準 |
| pyannote community-1（法國） | 說話者辨識（階段 6） | CC-BY-4.0 |

| 等級 | 硬體 | 辨識引擎 | 模型格式／位置 |
|---|---|---|---|
| **A** | NVIDIA，VRAM ≥ 8GB | faster-whisper（CUDA） | CT2：`D:\models\asr\ct2\` |
| **B** | NVIDIA，VRAM 4–8GB | faster-whisper（CUDA） | CT2：`D:\models\asr\ct2\` |
| **C** | Intel 顯示卡（含內顯） | **OpenVINO（GPU）**，失敗自動改 CPU | OpenVINO：`D:\models\asr\ov\` |
| **D** | 其他（純 CPU） | **OpenVINO（CPU）** | OpenVINO：`D:\models\asr\ov\` |

> OpenVINO 為 Intel 開源推論工具，Intel 顯示卡可加速，任何 x86 CPU 也能執行；做法與 mov_downloader 專案一致。

**共同流程**：ffmpeg 轉 16kHz 單聲道 → 辨識 → OpenCC `s2tw` 轉正體字（只轉字形，不換詞） →（選用）專有名詞校正、說話者辨識。

---

## OpenVINO 模型從哪來

`setup.cmd` 依序尋找，找到就複製到 `D:\models\asr\ov\`：

1. 已存在 → 略過
2. 本機 keyboard_free 已轉好的模型（`%APPDATA%\KeyboardFree\models\ov\`）
3. 共用來源：設定環境變數 `MODELS_SOURCE`（例：`\\NAS\models` 或 `E:\models`），會從 `<來源>\asr\ov\` 複製
4. 都沒有 → 由管理者雙擊 **`admin-convert-openvino.cmd`** 轉換一次（約 20–40 分鐘，轉換工具約 2GB，裝在獨立的 `.venv-convert`，不需 Hugging Face 帳號）

**標準部署方式**：管理者把母機上完整的 `D:\models` 複製給使用者，使用者複製到自己的 `D:\models` 後，再雙擊 `setup.cmd` 開始所有動作。

---

## 管理者一次性作業

### 說話者辨識模型（pyannote）
一般使用者不需要這一步，也不需要 Hugging Face 帳號。
1. 登入 Hugging Face，開啟 `pyannote/speaker-diarization-community-1`，按同意條款。
2. Settings → Access Tokens 建立 **Read** 類型的 Token。
3. 雙擊 `admin-download-pyannote.cmd`，貼上 Token（不會顯示、不會存檔）。
4. 模型存到 `D:\models\diarization\`，其他電腦複製這個資料夾即可。

### 本機 LLM（Gemma 4，校對判讀詞庫、整理逐字稿用；選用）
- `setup.cmd` 會依這台電腦的記憶體建議大小並詢問是否下載；也可在網頁「參數設定 → 本機 LLM」選擇、下載（背景進行，中斷再按一次會續傳）。
- 建議：記憶體 16 GB 以下 → Gemma 4 E2B；16～63 GB → **Gemma 4 E4B**；64 GB 以上 → Gemma 4 26B-A4B（約 15 GB）。
- 來源：Hugging Face `OpenVINO/gemma-4-*-it-int4-ov`（Intel 官方轉好的 OpenVINO int4 版，Apache-2.0，不需帳號），放在 `D:\models\llm\`。完全在本機執行、不上傳；閒置 10 分鐘自動釋放記憶體。
- 校對模式的「📖 詞庫候選」會改由本機 LLM 判讀（可在設定關閉，改回規則判斷）。

### 翻譯模型（TranslateGemma 4B，雙語對照用）
1. 登入 Hugging Face，開啟 https://huggingface.co/google/translategemma-4b-it 按同意條款。
2. 到 https://huggingface.co/settings/tokens 建立 Read 類型的 Token（可沿用下載 pyannote 的同一個）。
3. 雙擊 `admin-download-translator.cmd`，貼上 Token（畫面不顯示），約 20–40 分鐘：下載約 8GB 原始模型並轉成 OpenVINO int8。
4. 模型存到 `D:\models\llm\translategemma-4b-it\`，其他電腦複製 `D:\models\llm` 即可，不需要帳號。完成後原始模型快取（`D:\models\.hf-cache\hub\models--google--translategemma-4b-it`）與 `.venv-convert` 可自行刪除。

### OpenVINO 模型轉換
只有在沒有任何電腦有現成 OpenVINO 模型時才需要：雙擊 `admin-convert-openvino.cmd`。

### 備份到 GitHub
雙擊 **`admin-backup-github.cmd`**：把專案備份到 [beartolove/ai_solution](https://github.com/beartolove/ai_solution) 的 `transcribe` 資料夾（**公開儲存庫**）。
- 第一次：沒有 Git 會提示安裝（winget）；在專案旁邊建立工作複本 `D:\code_projects\ai_solution`；推送時會開瀏覽器請你登入 GitHub。
- 之後每次改版雙擊一次即可；內容沒變就不會提交。提交說明為「transcribe v版號 備份（日期、電腦）」。
- **不會上傳**：`.venv`、`.cache`、`tools`、`data`（設定與登入資料）、`env-reports`、`samples`（測試錄音與效能實測樣本，含內部會議）。
- 專案本身不執行 git（檔案是硬連結），所有 git 動作都在工作複本裡進行。
- 使用者資料（`D:\mediafiles` 的轉檔與校對成果）與 `D:\models` 不在 GitHub 備份範圍，請另外複製到 NAS 或外接硬碟。

### 效能實測當掉時的診斷
網頁「本機效能」只顯示「Failed to fetch」、`start.cmd` 視窗沒有任何錯誤就結束，通常是程式在底層當掉（例如 OpenVINO 版本不符）。雙擊 **`admin-debug-benchmark.cmd`**：在命令列跑一次效能實測，連同 Python／OpenVINO 版本、可用裝置與當掉位置一起寫入 `env-reports\debug-benchmark.log`。

---

## 資料夾結構

```
D:\models\                        ← 只放模型
├─ asr\
│  ├─ ov\                         ← OpenVINO（等級 C/D）
│  │  ├─ breeze-asr-25\
│  │  └─ large-v3-turbo\
│  ├─ ct2\                        ← faster-whisper（等級 A/B）
│  └─ ggml\                       ← 舊版（0.2.x）下載，已不使用，可刪除
├─ diarization\                   ← pyannote 說話者辨識
├─ llm\translategemma-4b-it\      ← 翻譯模型（Google TranslateGemma 4B，OpenVINO int8）
└─ .hf-cache\

D:\code_projects\transcribe\
├─ setup.cmd                      ← 每台電腦第一次：確認／安裝執行環境
├─ start.cmd                      ← 開啟網頁介面（轉逐字稿、效能實測）
├─ app\                           ← 主程式（Python）
│  ├─ pipeline.py                 ← 任務流程：辨識 → 輸出 →（需要時）另存 MP3
│  ├─ audio.py / engine_ov.py / chunked.py / textout.py / config.py
│  ├─ downloader.py               ← 網址下載（yt-dlp，mp4 影片）
│  ├─ diarize.py                  ← 分辨說話者（pyannote，第一次使用自動安裝）
│  ├─ translate.py                ← 翻譯成中文（TranslateGemma 4B，OpenVINO）
│  ├─ cli.py / benchmark.py
│  └─ web.py                      ← 網頁介面後端（FastAPI）
├─ web\                           ← 網頁介面前端（index.html／style.css／app.js）
├─ chrome_extension\              ← Chrome 擴充功能「逐字稿登入同步」（載入未封裝項目）
├─ samples\                       ← 測試錄音與效能實測標準樣本（隨專案發佈）
├─ admin-download-pyannote.cmd    ← 管理者：下載說話者辨識模型
├─ admin-convert-openvino.cmd     ← 管理者：轉換 OpenVINO 模型
├─ admin-download-translator.cmd  ← 管理者：下載並轉換翻譯模型 TranslateGemma
├─ admin-debug-benchmark.cmd      ← 管理者：效能實測當掉時的診斷紀錄
├─ admin-backup-github.cmd        ← 管理者：備份到 GitHub（beartolove/ai_solution 的 transcribe）
├─ models.json                    ← 本專案需要的模型清單（各功能的候選模型與條件）
├─ requirements.txt               ← 共用 Python 套件
├─ requirements-nvidia.txt        ← NVIDIA 電腦額外套件
├─ README.md / CHANGELOG.md / CLAUDE.md / ROADMAP.md / VERSION
├─ glossary\                      ← 專有名詞庫（港務.csv）
├─ scripts\
│  ├─ setup.ps1                   ← setup.cmd 主流程
│  ├─ env-check.ps1               ← 環境初測
│  ├─ check-tools.ps1             ← 執行環境（.venv、套件、ffmpeg）
│  ├─ prepare-ov-models.ps1       ← 準備 OpenVINO 模型
│  ├─ model-report.ps1            ← 顯示這台的模型配置方案（只告知）
│  ├─ download-models.ps1         ← 下載 faster-whisper 模型（NVIDIA）
│  ├─ convert-openvino.ps1        ← 管理者：轉換 OpenVINO
│  └─ download-pyannote.ps1       ← 管理者：下載 pyannote
├─ data\                          ← 本機設定 settings.json、yt-dlp 更新紀錄、登入資料 cookies.txt（不進版控、不打包）
├─ .venv\  tools\  env-reports\   ← 自動產生，不進版控

D:\mediafiles\                    ← 媒體檔（上傳檔與轉檔結果，不放在專案內）
├─ uploads\                       ← 上傳的檔案，每個任務一個資料夾：原始檔、.srt、.txt、.json、.mp3、job.json
├─ benchmark\                     ← 效能實測暫存（benchmark_日期時間\）
└─ downloads\                     ← 從網路下載的影音，每個任務一個資料夾（另有 source_url.json 記錄來源網址）
```

媒體檔位置的優先順序：網頁「參數設定」→ 環境變數 `MEDIA_ROOT` → 預設 `D:\mediafiles`（沒有 D 槽時暫存在專案的 `mediafiles\`，請到「參數設定」改到其他磁碟）。0.6.6 以前的任務在專案的 `jobs\`，確認已複製到 `D:\mediafiles\uploads\` 後可自行刪除。

程式端只讀 `MODELS_ROOT`：

```python
import os
root = os.environ.get("MODELS_ROOT", r"D:\models")
model_dir = os.path.join(root, "asr", "ov", "breeze-asr-25")
```

---

## 注意事項
- 模型與執行環境的 OpenVINO 版本必須相容：模型若比執行環境新，程式會在載入前停下並說明（0.13.7 起），不會再整個當掉。母機轉換新模型、升級 OpenVINO 後，其他電腦重新執行 `setup.cmd` 即可跟上。
- 下載 YouTube 需要 JavaScript 執行環境（Deno 或 Node.js）。setup 只會提示、不強制安裝；有 Node.js 的電腦可直接沿用。
- 舊版留下的模型（`asr\ggml\`、`asr\hf\`、`asr\onnx\`）會在 setup 第 5 步標為「已停用」並列出路徑，需要空間時自行手動刪除。
- 當作複製來源的母機，請保留「其他電腦需要」的模型（例如 Intel 電腦上的 faster-whisper 模型，是給 NVIDIA 電腦用的）。
- 手動執行任一 .ps1：`powershell -ExecutionPolicy Bypass -File .\scripts\xxx.ps1`（PowerShell 執行指令碼必須加 `.\`）。
