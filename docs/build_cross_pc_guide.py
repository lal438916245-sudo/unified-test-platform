"""Build the offline cross-PC guide/checklist and a portable acceptance kit."""
import hashlib
import json
from pathlib import Path
import zipfile

import markdown
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / 'docs'
MATERIALS = DOCS / '异机验收材料'
OUTPUT = ROOT / 'frontend/help/cross-pc-acceptance.html'
RELEASE = ROOT / '_ci_out/releases/20261018-cross-pc-acceptance'

STYLE = '''
:root{--ink:#183d35;--muted:#647970;--green:#087b60;--line:#d9e5df}
*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:90px}
body{margin:0;background:#f2f6f3;color:var(--ink);font:15px/1.85 "Microsoft YaHei",sans-serif}
header{position:sticky;top:0;z-index:3;background:#fff;border-bottom:1px solid var(--line);padding:12px 24px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
header strong{margin-right:auto}button,.button{cursor:pointer;background:white;border:1px solid var(--line);border-radius:6px;padding:6px 10px;font:13px "Microsoft YaHei",sans-serif;color:var(--ink)}button.primary{background:var(--green);color:white}
aside{position:fixed;top:80px;bottom:0;width:245px;padding:22px;overflow:auto}aside a{display:block;padding:7px 5px;font-size:13px;text-decoration:none}a{color:var(--green)}
main{margin-left:245px;max-width:1220px;padding:26px 30px 80px}.hero{background:#174e40;color:white;border-radius:14px;padding:26px 32px;margin-bottom:20px}.hero h1{margin:0;font-size:29px}.hero p{margin-bottom:0;color:#cbe3d6}
article,section.form{background:white;border:1px solid var(--line);border-radius:12px;padding:22px 30px;margin-bottom:22px}h2{font-size:23px;margin-top:36px;border-top:1px solid var(--line);padding-top:22px}h3{font-size:17px;margin-top:25px}
pre{background:#edf4ef;border:1px solid var(--line);border-radius:8px;padding:14px;overflow:auto;line-height:1.6}code{font:13px/1.7 Consolas,"Microsoft YaHei",monospace;overflow-wrap:anywhere}p code,li code,td code{background:#eef5f0;padding:1px 4px}
.table-wrap{overflow:auto;margin:18px 0}table{border-collapse:collapse;width:100%;font-size:13px}th,td{border:1px solid var(--line);padding:9px 10px;vertical-align:top}th{background:#edf5ef;text-align:left}li{margin:7px 0}.muted{color:var(--muted);font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}label{font-size:13px}input,textarea,select{font:13px/1.6 "Microsoft YaHei",sans-serif;border:1px solid #b9cec1;border-radius:5px;padding:5px;background:#fff;color:var(--ink)}label input,label textarea{display:block;width:100%}textarea{resize:vertical;min-height:48px}td textarea{min-width:180px;width:100%}select{min-width:85px}
#summary{padding:10px 14px;background:#edf5ef;border-radius:6px;margin:14px 0}tr[data-state="失败"]{background:#fff0ef}tr[data-state="通过"]{background:#eff9f1}tr[data-state="阻塞"]{background:#fff8e4}.print-value{display:none}.checkbox-note{padding:12px;background:#fff6df;border-radius:8px}footer{color:var(--muted);font-size:12px;padding:10px}
@media(max-width:1000px){aside{display:none}main{margin:0;padding:20px 12px}article,section.form{padding:18px}.hero{padding:24px}header{padding:10px}}
@media print{header,aside,.no-print{display:none!important}body{background:white;font-size:10pt}main{margin:0;padding:0;max-width:none}article,section.form{border:0;padding:0}.hero{background:white;color:#173e30;border:1px solid #aaa;padding:16px}.hero p{color:#333}.table-wrap{overflow:visible}table{font-size:9pt}pre{white-space:pre-wrap;font-size:9pt}h2,h3{break-after:avoid}tr{break-inside:avoid}thead{display:table-header-group}.form label{break-inside:avoid}select,input,textarea{display:none!important}.print-value{display:block;white-space:pre-wrap;overflow-wrap:anywhere}a{color:inherit;text-decoration:none}@page{size:A4;margin:14mm}}
'''

SCRIPT = r'''
const KEY='platform-cross-pc-acceptance-20261018',allowed=['未测','通过','失败','阻塞','不适用'];
const fields=[...document.querySelectorAll('[data-save]')];
const get=()=>({format:'platform-cross-pc-acceptance-result',version:1,exported_at:new Date().toISOString(),values:Object.fromEntries(fields.map(f=>[f.dataset.save,f.value]))});
function refresh(){
 const counts=Object.fromEntries(allowed.map(s=>[s,0]));
 document.querySelectorAll('select[data-check]').forEach(f=>{counts[f.value]++;f.closest('tr').dataset.state=f.value;});
 document.getElementById('summary').textContent=Object.entries(counts).map(([s,n])=>s+' '+n+' 项').join(' · ');
 fields.forEach(f=>{const p=f.nextElementSibling;if(p&&p.classList.contains('print-value'))p.textContent=f.value||'（未填写）';});
}
function save(){refresh();try{localStorage.setItem(KEY,JSON.stringify(get()));}catch(e){document.getElementById('storage-note').textContent='浏览器不支持当前文件的本地记忆，请使用“导出填写结果”保存。';}}
function restore(result){
 if(!result||result.format!=='platform-cross-pc-acceptance-result'||result.version!==1||!result.values||typeof result.values!=='object')throw new Error('填写结果格式不匹配');
 fields.forEach(f=>{const v=result.values[f.dataset.save];if(typeof v==='string'&&(!f.matches('select')||allowed.includes(v)))f.value=v;});refresh();
}
fields.forEach(f=>{const p=document.createElement('span');p.className='print-value';f.after(p);f.addEventListener('input',save);f.addEventListener('change',save);});
try{const old=localStorage.getItem(KEY);if(old)restore(JSON.parse(old));}catch(e){}refresh();
document.getElementById('export-results').onclick=()=>{const u=URL.createObjectURL(new Blob([JSON.stringify(get(),null,2)],{type:'application/json;charset=utf-8'}));const a=document.createElement('a');a.href=u;a.download='异机验收填写结果-'+new Date().toISOString().slice(0,10)+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);};
document.getElementById('load-results').onchange=async function(){try{const f=this.files[0];if(!f)return;if(f.size>1024*1024)throw new Error('填写结果不能超过 1 MB');restore(JSON.parse(await f.text()));save();}catch(e){alert('导入失败：'+e.message);}this.value='';};
document.getElementById('print').onclick=()=>{refresh();window.print();};
document.getElementById('reset').onclick=()=>{if(confirm('清空本页填写内容？建议先导出结果。')){fields.forEach(f=>f.value=f.matches('select')?'未测':'');save();}};
'''


def convert(source):
    return BeautifulSoup(markdown.markdown(source.read_text(encoding='utf-8'),extensions=['tables','fenced_code','toc']), 'html.parser')


def build():
    guide=convert(DOCS/'异机完整验收操作手册.md');guide.h1.decompose()
    checklist=convert(DOCS/'异机完整验收检查清单.md')
    table=checklist.find('table');rows=table.find('tbody').find_all('tr')
    options=''.join('<option>'+s+'</option>' for s in ('未测','通过','失败','阻塞','不适用'))
    for row in rows:
        cells=row.find_all('td');ident=cells[0].get_text(strip=True)
        cells[3].clear();cells[3].append(BeautifulSoup(f'<select data-check="{ident}" data-save="status-{ident}" aria-label="{ident} 状态">{options}</select>','html.parser'))
        cells[4].clear();cells[4].append(BeautifulSoup(f'<textarea data-save="note-{ident}" aria-label="{ident} 证据或备注" rows="2"></textarea>','html.parser'))
    metadata=['执行人 / 复核人','目标电脑 / Windows','Python 路径 / 版本 / 位数','安装包 / SHA256','平台目录','项目目录','数据目录 / DB','端口 / URL','安装方式','真实项目 / 计划 / Run / Job','验收引擎范围','证据目录']
    inputs=''.join(f'<label>{name}<input data-save="meta-{i}"></label>' for i,name in enumerate(metadata))
    nav=''.join(f'<a href="#{h["id"]}">{h.get_text()}</a>' for h in guide.find_all('h2'))+'<a href="#checklist">12. 填写检查清单</a>'
    for soup in (guide,):
        for t in soup.find_all('table'):
            wrapper=soup.new_tag('div',attrs={'class':'table-wrap'});t.wrap(wrapper)
    document=f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>异机完整验收 · 操作手册与检查清单</title><style>{STYLE}</style></head>
<body><header><strong>异机完整验收 / 2026.10.18</strong><a class="button" href="#checklist">填写检查清单</a><button id="export-results" class="primary">导出填写结果</button><label class="button no-print">导入填写结果<input id="load-results" type="file" accept=".json" style="display:none"></label><button id="print">打印 / 保存 PDF</button><button id="reset">清空填写</button></header><aside>{nav}</aside>
<main><div class="hero"><h1>异机完整验收<br>操作手册与检查清单</h1><p>安装 → 本机资源 → 自有定义导入 → 预检 → 执行 → 日志与报告 → 重启恢复</p></div><article>{guide}</article>
<section class="form" id="checklist"><h2>12. 填写检查清单</h2><p>共 {len(rows)} 项。状态按实际预期判定，故意失败计划运行 failed 时该验收项可以通过。选测项目不参加时填写“不适用”并说明原因。</p><p class="checkbox-note">只有示例通过时填写“基础链路通过，真实项目待验收”。真实项目与所选引擎通过后再填写完整通过。</p><div class="grid">{inputs}</div><p class="muted" id="storage-note">填写内容尝试保存在当前浏览器。本地文件路径 / 浏览器变化时不保证自动恢复，请导出 JSON 随证据带回。</p><div id="summary" aria-live="polite"></div><div class="table-wrap">{table}</div>
<label>问题记录：编号、复现步骤、预期与实际、Run / Job、证据及处理结果<textarea data-save="issues" rows="6"></textarea></label><label>验收结论：基础 / 真实项目 / 所选引擎结果、不适用原因、是否改源码、执行与复核日期<textarea data-save="conclusion" rows="6"></textarea></label></section><footer>离线单文件 · 无外部字体或脚本 · 可导出填写结果和打印。文档与校准材料不代表目标电脑已完成真实验收。</footer></main><script>{SCRIPT}</script></body></html>'''
    OUTPUT.parent.mkdir(parents=True,exist_ok=True);OUTPUT.write_text(document,encoding='utf-8')
    RELEASE.mkdir(parents=True,exist_ok=True)
    files={
        '异机完整验收操作手册.html':OUTPUT,
        '异机完整验收操作手册.md':DOCS/'异机完整验收操作手册.md',
        '异机完整验收检查清单.md':DOCS/'异机完整验收检查清单.md',
        **{p.relative_to(MATERIALS).as_posix():p for p in MATERIALS.rglob('*') if p.is_file() and '__pycache__' not in p.parts},
    }
    archive=RELEASE/'异机完整验收操作包.zip'
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as z:
        for name,path in sorted(files.items()):z.writestr('异机验收/'+name,path.read_bytes())
    result=dict(html=str(OUTPUT),checklist_items=len(rows),archive=str(archive),files=list(files),bytes=archive.stat().st_size,sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
    (RELEASE/'manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':build()
