"""Build a standalone installation/migration help page; no platform data access."""
from pathlib import Path
import markdown

ROOT=Path(__file__).resolve().parents[1]
source=ROOT/'docs/安装与配置迁移.md'
body=markdown.markdown(source.read_text(encoding='utf-8'),extensions=['tables','fenced_code'])
document='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>安装与配置迁移</title><style>body{font:16px/1.85 "Microsoft YaHei",sans-serif;color:#17332f;background:#f5f8f7;max-width:960px;margin:auto;padding:32px}main{background:white;border:1px solid #dfe9e4;border-radius:12px;padding:30px}h2{margin-top:36px}pre{overflow:auto;background:#edf4f1;padding:16px;border-radius:8px}code{font-family:Consolas,monospace}a{color:#087c67}</style><main><a href="../index.html#/migration">返回迁移页面 →</a>'''+body+'</main></html>'
(ROOT/'frontend/help/deployment.html').write_text(document,encoding='utf-8')
print('Built frontend/help/deployment.html')
