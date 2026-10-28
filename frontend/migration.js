// Definition exchange. A pending batch can be resumed after restarting the app.
let mgCatalog=null,mgPreview=null,mgBatch=null,mgEditRevision=0;
const mgTypeNames={environment:'被测环境',executor:'执行机',asset_source:'资产源',workdir:'项目工作目录',pytest_entry:'pytest 测试入口',ui_suite:'UI 套件 / 入口',ui_device:'UI 设备',runtime_requirement:'公共运行要求',locust_scenario:'Locust 场景',dataset:'视觉数据集'};
async function renderMigration(id){
  const view=routeVersion;
  $('app').innerHTML='<h1>安装与配置迁移</h1><div class="card">加载中…</div>';mgPreview=null;mgBatch=null;
  try{
    const [catalog,batches]=await Promise.all([j('/api/config/migration/catalog'),j('/api/config/migration/imports')]);
    if(view!==routeVersion)return;mgCatalog=catalog;
    const choices=(group,label)=>`<section class="mg-export-group" aria-labelledby="mg-export-${group}-heading"><h3 id="mg-export-${group}-heading">${label}</h3><div class="mg-export-items">${catalog[group].map(r=>`<label><input type="checkbox" class="mg-export-${group}" value="${r.id}" checked><span>${esc(r.name)} ${r.archived?'（归档）':r.enabled===false||r.enabled===0?'（停用）':''}</span></label>`).join('')||'<p class="muted mg-export-empty">暂无记录</p>'}</div></section>`;
    $('app').innerHTML=`<h1>安装与配置迁移</h1>
      <div class="card"><p>另一台电脑解压平台包后运行 <code>install.bat</code> 安装，再用 <code>start.bat</code> 启动。需要 64 位 Python 3.11 或以上。</p><p><a href="/static/help/deployment.html" target="_blank" rel="noopener">安装与迁移说明 →</a> · <a href="#/projects">登记本机项目、Python 与测试入口 →</a></p><p class="muted">迁移的是用例、套件与计划定义。源码、测试依赖、真实资产和历史报告独立保管；新安装默认空白配置。导入不会连接设备或运行测试。</p></div>
      <div id="mg-message" role="status" aria-live="polite"></div>
      <div class="card"><h2>导出定义</h2><p>选择需要带到另一台电脑的内容。套件和计划引用的用例会自动包含；本机资源仅导出名称与类型，导入后重新关联。</p><div class="mg-export-scroll" role="region" aria-label="导出内容选择" tabindex="0"><div class="mg-export-grid">${choices('cases','测试用例')}${choices('suites','测试套件')}${choices('plans','测试计划')}</div></div><p><button class="primary" onclick="mgExport()">导出选中项 JSON</button></p></div>
      <div class="card"><h2>导入自己的用例与计划</h2><p>选择平台导出的 JSON，或填入模板后编辑成自己的定义。先保存到待关联批次，再选择本机资源、预览并确认新增。</p><input id="mg-file" type="file" accept=".json,application/json" onchange="mgReadFile(this)"><p><button onclick="mgFillTemplate()">填入示例模板</button> <button onclick="mgDownloadTemplate()">下载 JSON 模板</button></p><label>批次名称<input id="mg-name" value="自有用例与计划"></label><label>迁移 JSON（可编辑）<textarea id="mg-document" rows="9" style="width:100%;font-family:monospace" placeholder="选择 JSON 文件，或点击填入示例模板"></textarea></label><p><button class="primary" onclick="mgStage()">导入到待关联批次</button></p></div>
      <div class="card"><h2>导入批次</h2><table><thead><tr><th>批次</th><th>状态</th><th>操作</th></tr></thead><tbody>${batches.map(b=>`<tr><td>${esc(b.name)} <span class="muted">#${b.id}</span></td><td>${b.status==='committed'?'已提交':'待关联 / 待确认'}</td><td><button onclick="location.hash='#/migration/${b.id}'">${b.status==='committed'?'查看回执':'关联 / 继续导入'}</button>${b.status==='pending'?` <button onclick="mgDiscard(${b.id})">放弃待导入批次</button>`:''}</td></tr>`).join('')||'<tr><td colspan="3">暂无导入批次</td></tr>'}</tbody></table></div>
      <div id="mg-batch"></div>`;
    if(id)await mgOpenBatch(id);
  }catch(e){if(view===routeVersion)$('app').innerHTML=`<div class="card err">迁移页面加载失败：${esc(e.message)}。更新后请重启原平台服务并刷新。</div>`;}
}
function mgMessage(text){$('mg-message').textContent=text;$('mg-message').scrollIntoView({block:'center'});}
function mgDownload(data,name){const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
async function mgReadFile(input){
  try{const file=input.files[0];if(!file)return;if(file.size>5*1024*1024)throw new Error('文件不能超过 5 MB');$('mg-document').value=await file.text();$('mg-name').value=file.name.replace(/\.json$/i,'').slice(0,120);}
  catch(e){mgMessage('读取失败：'+e.message);}
}
async function mgFillTemplate(){try{$('mg-document').value=JSON.stringify(await j('/api/config/migration/template'),null,2);}catch(e){mgMessage(e.message);}}
async function mgDownloadTemplate(){try{mgDownload(await j('/api/config/migration/template'),'platform-definitions-template.json');}catch(e){mgMessage(e.message);}}
async function mgExport(){
  const ids=group=>[...document.querySelectorAll('.mg-export-'+group+':checked')].map(n=>Number(n.value));
  try{const doc=await j('/api/config/migration/export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({case_ids:ids('cases'),suite_ids:ids('suites'),plan_ids:ids('plans')})});mgDownload(doc,'platform-definitions.json');mgMessage('定义已导出，关联依赖已包含。');}
  catch(e){mgMessage('导出失败：'+e.message);}
}
async function mgStage(){
  try{
    const text=$('mg-document').value;if(new TextEncoder().encode(text).length>5*1024*1024)throw new Error('文件不能超过 5 MB');
    const result=await j('/api/config/migration/imports',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:$('mg-name').value.trim(),document:JSON.parse(text)})});
    location.hash='#/migration/'+result.id;
  }catch(e){mgMessage('导入失败：'+e.message);}
}
async function mgOpenBatch(id){
  mgBatch=await j('/api/config/migration/imports/'+id);mgPreview=null;
  if(mgBatch.status==='committed'){$('mg-batch').innerHTML=mgReceiptHtml(mgBatch.result);return;}
  $('mg-batch').innerHTML=`<div class="card"><h2>关联本机资源 · ${esc(mgBatch.name)}</h2><p>原电脑的目录与资源 ID 不会直接使用。下拉项来自当前平台；缺少资源时先到“资源与环境”登记，再返回本批次。相同名称的建议关联仍需核对。</p><table><thead><tr><th>迁移资源</th><th>类型</th><th>目标电脑的资源</th></tr></thead><tbody>${mgBatch.document.resources.map((r,i)=>{const selected=String(mgBatch.mapping[r.id]||''),values=mgCatalog.targets[r.type]||[];return `<tr><td>${esc(r.name)}</td><td>${esc(mgTypeNames[r.type]||r.type)}</td><td><select id="mg-map-${i}" onchange="mgInvalidate()"><option value="">（待关联）</option>${selected&&!values.some(v=>v.value===selected)?`<option value="${esc(selected)}" selected>原关联已不可用，请重新选择</option>`:''}${values.map(v=>`<option value="${esc(v.value)}" ${v.value===selected?'selected':''}>${esc(v.label)}</option>`).join('')}</select></td></tr>`;}).join('')||'<tr><td colspan="3">没有需要关联的资源</td></tr>'}</tbody></table><label>新增记录名称前缀（可留空；同名自动加导入序号）<input id="mg-prefix" maxlength="40" value="${esc(mgBatch.prefix||'')}" oninput="mgInvalidate()"></label><p><button class="primary" onclick="mgPreviewBatch()">保存关联并预览</button></p><div id="mg-preview" role="status"></div></div>`;
  $('mg-batch').scrollIntoView({block:'start'});
}
function mgInvalidate(){mgEditRevision++;mgPreview=null;if($('mg-preview'))$('mg-preview').innerHTML='<p>关联已变化，请重新预览后提交。</p>';}
async function mgPreviewBatch(){
  const revision=mgEditRevision,batchId=mgBatch.id;
  const mapping={};mgBatch.document.resources.forEach((r,i)=>{if($('mg-map-'+i).value)mapping[r.id]=$('mg-map-'+i).value;});
  try{
    const r=await j('/api/config/migration/imports/'+batchId+'/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mapping,prefix:$('mg-prefix').value})});if(revision!==mgEditRevision||!mgBatch||mgBatch.id!==batchId||!$('mg-preview'))return;mgPreview=r;
    $('mg-preview').innerHTML=`<h3>${r.ok?'导入预览通过':'尚未就绪'}</h3>${r.errors.length?`<ul class="err">${r.errors.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:''}${r.warnings.length?`<ul>${r.warnings.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:''}<p>将新增用例 ${r.counts.cases}、套件 ${r.counts.suites}、计划 ${r.counts.plans}；不会覆盖已有记录，也不会执行测试。</p>${Object.entries(r.names).map(([kind,values])=>values.length?`<p>${({cases:'用例',suites:'套件',plans:'计划'})[kind]}：${values.map(v=>esc(v.name)).join('、')}</p>`:'').join('')}${r.ok?'<button class="primary" onclick="mgCommit()">确认新增这些记录</button>':''}`;
  }catch(e){mgPreview=null;$('mg-preview').textContent='预览失败：'+e.message;}
}
function mgReceiptHtml(r){return `<div class="card"><h2>导入完成</h2><p>已新增 ${r.cases.length} 个用例、${r.suites.length} 个套件、${r.plans.length} 个计划。未运行测试，请在计划中预检后执行。</p>${r.plans.map(p=>`<p><a href="#/plan-editor/${p.id}">${esc(p.name)} →</a></p>`).join('')}<a href="#/cases">查看测试用例 →</a> · <a href="#/suites">查看测试套件 →</a></div>`;}
async function mgCommit(){
  if(!mgPreview||!mgPreview.ok)return;
  try{const result=await j('/api/config/migration/imports/'+mgBatch.id+'/commit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({fingerprint:mgPreview.fingerprint})});mgPreview=null;await renderMigration(String(result.batch_id));}
  catch(e){mgPreview=null;$('mg-preview').textContent='提交未完成：'+e.message+'。请重新预览。';}
}
async function mgDiscard(id){
  try{await j('/api/config/migration/imports/'+id,{method:'DELETE'});if(location.hash==='#/migration')await renderMigration();else location.hash='#/migration';}
  catch(e){mgMessage('放弃批次失败：'+e.message);}
}
