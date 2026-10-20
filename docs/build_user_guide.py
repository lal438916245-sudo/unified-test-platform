"""Build the portable HTML manual from Markdown and real platform screenshots.

Documentation build only. Requires markdown and beautifulsoup4; the generated
HTML needs neither Python nor network access. Does not read/write platform DB.
"""
from __future__ import annotations

import base64
import html
import json
from pathlib import Path
import re

import markdown
from bs4 import BeautifulSoup


DOCS = Path(__file__).resolve().parent
SOURCE = DOCS / "平台使用说明.md"
OUTPUT = DOCS.parent / "frontend" / "help" / "user-guide.html"

STYLE = r"""
:root{--ink:#17332f;--muted:#627771;--brand:#087c67;--line:#dfe9e4;--soft:#f2f7f3;--paper:#fff;--nav:264px}
*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:100px}
body{margin:0;color:var(--ink);background:#f6f8f5;font:16px/1.85 "Microsoft YaHei UI","Microsoft YaHei","Noto Sans SC",sans-serif}
a{color:var(--brand);text-underline-offset:3px}button{font:inherit;cursor:pointer}
.top{height:72px;position:fixed;inset:0 0 auto;background:rgba(255,255,255,.97);border-bottom:1px solid var(--line);z-index:20;display:flex;align-items:center;padding:0 32px;gap:16px}
.brand{font-size:17px;font-weight:750;letter-spacing:.3px}.brand b{display:inline-block;background:var(--brand);color:white;border-radius:8px;width:30px;text-align:center;margin-right:10px}
.top small{color:var(--muted)}.actions{display:flex;gap:10px;margin-left:auto;align-items:center}.actions button,.actions a{border:1px solid var(--line);border-radius:7px;background:white;color:var(--ink);padding:5px 12px;text-decoration:none;font-size:13px}
.actions .primary{color:white;background:var(--brand);border-color:var(--brand)}.menu{display:none}
aside{position:fixed;left:0;top:72px;bottom:0;width:var(--nav);padding:28px 16px 28px 24px;border-right:1px solid var(--line);overflow-y:auto;background:#f8faf7}
.nav-title{font-size:11px;font-weight:700;color:var(--muted);letter-spacing:2px;padding:0 12px 15px}
aside nav a{display:flex;gap:11px;align-items:baseline;font-size:13px;line-height:1.55;color:#4e6660;text-decoration:none;padding:9px 11px;margin:3px 0;border-radius:7px}
aside nav a:hover,aside nav a.active{background:#e4f0e9;color:#07634f}aside nav a.active{font-weight:700}.num{font:12px/1.8 Consolas,monospace;opacity:.65;min-width:19px}
.nav-foot{border-top:1px solid var(--line);padding:18px 11px;margin-top:18px;color:var(--muted);font-size:11px}
main{margin-left:var(--nav);padding:108px 42px 80px;max-width:1360px}
.hero{background:linear-gradient(120deg,#123e35,#1a6050);color:white;border-radius:16px;padding:36px 40px;margin-bottom:28px;box-shadow:0 9px 25px #163a3110}
.kicker{font:11px/1.6 Consolas,monospace;letter-spacing:2px;color:#b6d8cb}.hero h1{font-size:34px;line-height:1.45;margin:12px 0}.hero p{font-size:14px;color:#d6e8e0;max-width:720px;margin:0 0 24px}.pills{display:flex;flex-wrap:wrap;gap:8px}.pills span{border:1px solid #ffffff35;background:#ffffff0c;padding:3px 11px;border-radius:18px;font-size:12px}
article{background:var(--paper);border:1px solid var(--line);border-radius:14px;padding:18px 36px 36px;box-shadow:0 2px 10px #15372d04}
article>p:first-child{color:var(--muted)}h2{font-size:24px;line-height:1.5;margin:48px 0 18px;padding:22px 0 13px;border-top:1px solid var(--line);scroll-margin-top:95px}h3{font-size:18px;margin:30px 0 12px;scroll-margin-top:95px}
p{margin:14px 0}strong{font-weight:750;color:#0e4d3e}li{margin:7px 0;padding-left:4px}ol,ul{padding-left:25px}
blockquote{margin:22px 0;background:#eff7f2;border-left:4px solid #2b9a78;border-radius:0 8px 8px 0;padding:10px 20px;font-size:14px}blockquote p{margin:6px 0}
code{font:13px/1.7 Consolas,"Microsoft YaHei",monospace;background:#edf3f0;color:#166349;border:1px solid #e3ece6;border-radius:4px;padding:2px 5px;overflow-wrap:anywhere}
pre{background:#142d28;color:#dfede7;padding:20px 22px;border-radius:9px;overflow:auto;line-height:1.7;font-size:13px}pre code{color:inherit;background:transparent;border:0;padding:0;overflow-wrap:normal}
.table-wrap{overflow-x:auto;margin:20px 0;border:1px solid var(--line);border-radius:9px}table{width:100%;border-collapse:collapse;font-size:13px;line-height:1.8}th,td{text-align:left;vertical-align:top;padding:11px 14px;border-bottom:1px solid var(--line)}th{background:#edf4ef;font-weight:700;color:#284f42}tr:last-child td{border-bottom:0}tr:nth-child(even) td{background:#fafcf9}td:first-child{min-width:120px}
figure{margin:28px 0 34px;border:1px solid #d5e3db;border-radius:10px;overflow:hidden;background:#f6f9f5;break-inside:avoid}figure img{display:block;width:100%;height:auto;background:white;cursor:zoom-in;outline-offset:-3px}figcaption{font-size:12px;color:#536c60;padding:11px 16px;line-height:1.65;border-top:1px solid var(--line)}
.footer{font-size:12px;color:var(--muted);padding:22px 2px}.footer strong{font-weight:500}
dialog{border:0;border-radius:10px;padding:12px;background:#f5f8f5;max-width:97vw;max-height:96vh;box-shadow:0 12px 70px #0005}dialog::backdrop{background:#102a24cf}dialog img{display:block;max-width:93vw;max-height:82vh;object-fit:contain}dialog button{border:1px solid #c5d5c9;background:white;border-radius:6px;padding:4px 14px;float:right;margin-bottom:8px}dialog p{clear:both;font-size:12px;margin:8px 0 0}
@media(min-width:1600px){main{margin-left:calc(var(--nav) + 4vw);padding-right:50px}}
@media(max-width:1050px){:root{--nav:220px}.top{padding:0 20px}.top small{display:none}main{padding:94px 22px 60px}article{padding:16px 24px 30px}.hero{padding:28px}.hero h1{font-size:28px}}
@media(max-width:760px){.top{height:62px;padding:0 14px}.brand{font-size:14px}.brand b{display:none}.actions .home{display:none}.actions button,.actions a{font-size:12px;padding:4px 9px}.menu{display:block}.actions{gap:6px}aside{display:none;top:62px;width:280px;box-shadow:8px 0 30px #0002;z-index:18}body.show-nav aside{display:block}main{margin:0;padding:82px 12px 30px}.hero{padding:23px 22px;border-radius:10px}.hero h1{font-size:25px}article{padding:12px 17px 24px;border-radius:10px}body{font-size:15px}h2{font-size:21px}td,th{padding:9px 10px}.table-wrap{margin-left:-4px;margin-right:-4px}}
@media print{html{scroll-behavior:auto}.top,aside,.footer,dialog{display:none!important}body{background:white;font-size:10pt;color:#111}main{margin:0;padding:0;max-width:none}.hero{background:white;color:#183c2c;box-shadow:none;border:1px solid #b8d1c2;padding:22px;break-inside:avoid}.hero p,.kicker{color:#4d685a}.pills span{border:1px solid #c5d8cb;color:#315240}article{border:0;padding:0;box-shadow:none}h2{font-size:17pt;break-after:avoid;margin-top:24px;padding-top:14px}h3{break-after:avoid}pre{white-space:pre-wrap;font-size:9pt;border:1px solid #ddd;background:#fafafa;color:#111}.table-wrap{overflow:visible}table{font-size:9pt}thead{display:table-header-group}tr{break-inside:avoid}figure{break-inside:avoid}a{color:inherit;text-decoration:none}@page{size:A4;margin:16mm}}
"""

SCRIPT = r"""
const dialog=document.getElementById('image-dialog');
document.querySelectorAll('figure img').forEach(img=>{
  const open=()=>{dialog.querySelector('img').src=img.src;dialog.querySelector('img').alt=img.alt;dialog.querySelector('p').textContent=img.alt;dialog.showModal();};
  img.addEventListener('click',open);img.addEventListener('keydown',e=>{if(e.key==='Enter')open();});
});
dialog.querySelector('button').addEventListener('click',()=>dialog.close());
dialog.addEventListener('click',e=>{if(e.target===dialog)dialog.close();});
document.getElementById('print').addEventListener('click',()=>window.print());
document.getElementById('menu').addEventListener('click',()=>document.body.classList.toggle('show-nav'));
document.querySelectorAll('aside nav a').forEach(a=>a.addEventListener('click',()=>document.body.classList.remove('show-nav')));
const links=[...document.querySelectorAll('aside nav a')];
const observer=new IntersectionObserver(entries=>{entries.forEach(e=>{if(e.isIntersecting){links.forEach(a=>a.classList.toggle('active',a.hash==='#'+e.target.id));}});},{rootMargin:'-90px 0px -65% 0px'});
document.querySelectorAll('article h2').forEach(h=>observer.observe(h));
"""


def main():
    converter = markdown.Markdown(extensions=["tables", "fenced_code", "toc", "sane_lists"],
                                  extension_configs={"toc": {"permalink": False}})
    soup = BeautifulSoup(converter.convert(SOURCE.read_text(encoding="utf-8")), "html.parser")
    soup.h1.decompose()
    for link in soup.find_all('a', href=True):
        if link['href'] == '安装与配置迁移.md':
            link['href'] = 'deployment.html'
    images = []
    for img in soup.find_all("img"):
        asset = (DOCS / img["src"]).resolve()
        if not asset.is_relative_to(DOCS) or not asset.is_file():
            raise ValueError(f"Missing or uncontrolled screenshot: {asset.name}")
        images.append({"file": asset.relative_to(DOCS).as_posix(), "caption": img.get("alt", ""), "bytes": asset.stat().st_size})
        img["src"] = "data:image/png;base64," + base64.b64encode(asset.read_bytes()).decode("ascii")
        img["tabindex"] = "0"
        img["title"] = "点击放大真实截图；Esc 关闭"
        figure = soup.new_tag("figure")
        caption = soup.new_tag("figcaption")
        caption.string = img.get("alt", "")
        parent = img.parent
        parent.replace_with(figure)
        figure.append(img.extract())
        figure.append(caption)
    for table in soup.find_all("table"):
        wrap = soup.new_tag("div", attrs={"class": "table-wrap"})
        table.wrap(wrap)
    headings = soup.find_all("h2")
    nav = []
    for heading in headings:
        label = heading.get_text()
        number, title = re.match(r"(\d+)\.\s*(.+)", label).groups()
        nav.append(f'<a href="#{html.escape(heading["id"], quote=True)}"><span class="num">{int(number):02d}</span><span>{html.escape(title)}</span></a>')
    document = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="统一游戏测试平台使用说明：四类引擎、资源配置、用例与套件、计划预检执行、报告与排错，含真实平台截图。">
<title>统一游戏测试平台 · 使用说明与操作步骤</title><style>{STYLE}</style></head>
<body><header class="top"><div class="brand"><b>测</b>统一游戏测试平台</div><small>使用说明与操作步骤</small><div class="actions"><button class="menu" id="menu">目录</button><a class="home" href="http://127.0.0.1:8000" target="_blank" rel="noopener">打开平台 ↗</a><button id="print" class="primary">打印 / 保存 PDF</button></div></header>
<aside aria-label="章节目录"><div class="nav-title">CONTENTS / 操作目录</div><nav>{''.join(nav)}</nav><div class="nav-foot">本机版本 · 2026.10.18<br>{len(headings)} 个章节 · {len(images)} 张真实截图<br>点击截图可放大查看<br>离线单文件，无需外部资源</div></aside>
<main><section class="hero"><div class="kicker">PLATFORM FIELD GUIDE / 2026.10</div><h1>平台使用说明<br>与功能操作步骤</h1><p>从资源配置到一次完整验收。以 MMO 注册进房为例，说明用例、套件、计划怎样衔接，以及如何读取预检、日志与证据报告。</p><div class="pills"><span>pytest</span><span>Locust</span><span>MatchEval</span><span>Airtest + Poco</span><span>{len(images)} 张真实截图</span></div></section>
<article>{soup}</article><footer class="footer">更新至 2026-10-18；含原平台历史截图与独立实例接入验收截图。截图保留采集时状态，历史成功结果不代表客户端当前在线。<br>Markdown 源稿：docs/平台使用说明.md · 截图目录：docs/images/user-guide-20261015/</footer></main>
<dialog id="image-dialog" aria-label="平台截图放大查看"><button>关闭 ×</button><img alt=""><p></p></dialog><script>{SCRIPT}</script></body></html>'''
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(document, encoding="utf-8")
    parsed = BeautifulSoup(document, "html.parser")
    ids = {x["id"] for x in parsed.find_all(id=True)}
    assert all(a["href"][1:] in ids for a in parsed.select("aside nav a"))
    assert len(images) == 15 and len(headings) == 16
    assert all(img["src"].startswith("data:image/png;base64,") for img in parsed.select("figure img"))
    manifest = {"title": "统一游戏测试平台使用说明", "date": "2026-10-18", "chapters": len(headings),
                "screenshots": images, "html_bytes": OUTPUT.stat().st_size,
                "source": SOURCE.name, "output": str(OUTPUT.relative_to(DOCS.parent))}
    (DOCS / "user-guide-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in manifest.items() if k != "screenshots"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
