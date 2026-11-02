"""管理者專用（由 convert-translator.ps1 呼叫）：轉換完成後，用模型自帶的對話範本產生各來源語言的提示文字，
存成 prompt_templates.json，讓一般電腦執行時不需要 transformers。
用法：python translator_prepare.py <Hugging Face 模型代號> <輸出資料夾>
"""
import json
import sys
from pathlib import Path

repo, out = sys.argv[1], Path(sys.argv[2])
from transformers import AutoProcessor, AutoTokenizer

try:
    tok = AutoProcessor.from_pretrained(repo)
except Exception:
    tok = AutoTokenizer.from_pretrained(repo)
SOURCES = ["en", "ja", "ko", "fr", "de", "es", "it", "pt", "ru", "vi", "th", "id", "ms", "nl", "pl", "tr", "ar", "hi"]
TARGET = "zh-TW"
MARK = "@@TEXT@@"
templates = {}
for src in SOURCES:
    msgs = [{"role": "user", "content": [{"type": "text", "source_lang_code": src, "target_lang_code": TARGET,
                                          "text": MARK}]}]
    try:
        s = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
    except Exception as e:
        print(f"  [略過] {src}：{e}")
        continue
    if s.startswith("<bos>"):
        s = s[len("<bos>"):]  # 執行時分詞器會自動加上開頭符號
    if MARK not in s:
        print(f"  [略過] {src}：範本中找不到文字位置")
        continue
    templates[src] = s
(out / "prompt_templates.json").write_text(json.dumps({"target": TARGET, "mark": MARK, "templates": templates},
                                                      ensure_ascii=False, indent=1), encoding="utf-8")
print(f"已產生 {len(templates)} 種來源語言的提示範本")
print("範例（en）：\n" + templates.get("en", "")[:600])
