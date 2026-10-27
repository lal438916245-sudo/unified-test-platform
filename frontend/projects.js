// Local project registration; general case/plan forms keep controlled references.
let prData={projects:[],resources:[],paths:[]}, prProjectId=null, prResourceId=null,prResourceReferenceSeq=0;
async function renderProjects(){
  const view=routeVersion;
  $('app').innerHTML='<h1>项目与测试入口</h1><div class="card">加载中…</div>';
  try{
    const [projects,resources,paths]=await Promise.all([j('/api/config/projects'),j('/api/config/project-resources'),j('/api/config/machine-paths')]);
    if(view!==routeVersion)return;
    prData={projects,resources,paths};prProjectId=null;prResourceId=null;
    const state=r=>r.enabled?'启用':'停用';
    const actions=(r,type)=>`<button onclick="prEdit('${type}',${r.id})">编辑</button> <button onclick="prCheck('${type}',${r.id})">检查配置</button>`;
    const projectRows=projects.map(p=>`<tr><td><strong>${esc(p.name)}</strong><div class="muted">${esc(p.key)} · rev ${p.revision}</div></td><td><code>${esc(p.root_path)}</code></td><td>${state(p)}</td><td>${actions(p,'project')} <button onclick="prDiscover(${p.id})">发现 pytest / UI 用例</button> <button onclick="prProjectReferences(${p.id})">查看引用</button> <button onclick="prProjectReferences(${p.id},true)">删除登记</button></td></tr>`).join('');
    const resourceActions=r=>`<button onclick="prResourceReferences(${r.id})">查看引用</button> <button onclick="prResourceReferences(${r.id},true)">删除登记</button>`;
    const runtimeRows=resources.filter(r=>r.kind==='runtime').map(r=>`<tr><td><strong>${esc(r.name)}</strong><div class="muted">${esc(r.key)} · rev ${r.revision}</div></td><td><code>${esc(r.config.python_path)}</code><div class="muted">${esc((r.config.modules||[]).join(', '))}</div></td><td>${state(r)}</td><td>${actions(r,'resource')} <button onclick="prUseRuntime(${r.id})">关联执行机</button> ${resourceActions(r)}</td></tr>`).join('');
    const entryRows=resources.filter(r=>r.kind!=='runtime').map(r=>`<tr><td><strong>${esc(r.name)}</strong><div class="muted">${esc(r.key)} · rev ${r.revision}</div></td><td>${esc((projects.find(p=>p.id===r.project_id)||{}).name||'项目不可用')} · ${esc(r.kind)}</td><td><code>${esc(r.kind==='matcheval'?r.config.tpl_dir+' / '+r.config.scenes_dir:r.config.path)}</code></td><td>${state(r)}</td><td><div class="pr-entry-actions">${actions(r,'resource')} ${resourceActions(r)}</div></td></tr>`).join('');
    $('app').innerHTML=`<h1>项目与测试入口</h1>
      <div class="card"><p>在平台所在电脑登记项目目录与 Python。测试入口使用项目内相对路径；视觉脚本与图片资产还可填写本机绝对路径。保存后立即生效。</p><p class="muted">配置目录属于当前运行平台的电脑。登记与静态发现不会运行测试或 fixture；执行自己信任的测试代码。已使用的引用可停用，历史定义与报告保留。</p><a href="#/env">返回资源与环境 →</a> · <a href="#/cases">管理测试用例 →</a> · <a href="#/projects" onclick="document.getElementById('pr-machine-paths').scrollIntoView();return false">本机路径配置 ↓</a></div>
      <div id="pr-message" role="status" aria-live="polite"></div>
      <div class="card"><div class="row"><h2>项目</h2><button onclick="prEdit('project',null)">新增项目</button></div><p class="muted">未被引用的项目可删除登记，本机文件保留；已有引用的项目只能停用。“查看引用”包含停用、归档与历史记录。</p><table><thead><tr><th>项目</th><th>本机目录</th><th>状态</th><th>操作</th></tr></thead><tbody>${projectRows||'<tr><td colspan="4">暂无登记项目</td></tr>'}</tbody></table></div>
      <div id="pr-project-references" role="status" aria-live="polite"></div>
      <div class="card" id="pr-project-form" hidden><div class="row"><h2 id="pr-project-heading">新增项目</h2><button style="margin-left:auto" onclick="prCloseForms()">取消</button></div><div class="form-grid"><label>项目名称<input id="pr-project-name"></label><label>本机项目根目录<input id="pr-root" placeholder="填写本机项目目录"></label><label>项目备注<input id="pr-notes"></label><label class="inline"><input id="pr-project-enabled" type="checkbox" checked>启用项目</label></div><p><button class="primary" onclick="prSaveProject()">保存项目</button></p></div>
      <div id="pr-discovery"></div>
      <div class="card"><div class="row"><h2>运行环境</h2><button onclick="prNewRuntime()">新增运行环境</button></div><p class="muted">登记 Python 与补充依赖后，点击“关联执行机”选择允许的引擎和默认项目。依赖填写导入模块名，例如 requests、pymongo，不填写 pip 命令。</p><table><thead><tr><th>运行环境</th><th>解释器 / 依赖</th><th>状态</th><th>操作</th></tr></thead><tbody>${runtimeRows||'<tr><td colspan="4">暂无登记运行环境</td></tr>'}</tbody></table></div>
      <div id="pr-runtime-form-host"></div>
      <div class="card"><div class="row"><h2>测试入口与资产</h2><button onclick="prEdit('resource',null)">新增测试入口</button></div><p class="muted">pytest 可登记具体文件::类::函数，airtest 登记使用 platform_poco 的 UI 文件，locust 登记脚本，matcheval 登记图片目录与兼容评估脚本。入口登记后，还需在“测试用例”保存业务用例定义。</p><div class="pr-entry-scroll" role="region" aria-label="测试入口与资产列表" tabindex="0"><table class="pr-entry-table"><colgroup><col style="width:20%"><col style="width:17%"><col style="width:33%"><col style="width:7%"><col style="width:23%"></colgroup><thead><tr><th>入口</th><th>项目 / 引擎</th><th>相对路径</th><th>状态</th><th>操作</th></tr></thead><tbody>${entryRows||'<tr><td colspan="5">暂无测试入口</td></tr>'}</tbody></table></div></div>
      <div id="pr-resource-references" role="status" aria-live="polite"></div>
      <div id="pr-entry-form-host"></div>
      <div class="card" id="pr-machine-paths"><h2>本机路径配置（兼容已有用例）</h2><p>更换电脑或移动目录后，在此重新绑定旧引用。无需修改已有用例和计划；新项目仍使用上方的项目与入口登记。</p><p class="muted">路径保存到当前平台数据库，优先于旧环境变量。留空表示未配置；保存只检查文件 / 目录，不执行脚本。修改后请重新预检，已排队任务会校验配置变化。启动平台本身的 Python 由启动脚本参数或当前 Python 命令选择。</p><table><thead><tr><th>类别 / 引用</th><th>本机文件 / 目录</th><th>状态</th><th>操作</th></tr></thead><tbody>${paths.map((p,i)=>`<tr><td>${esc(p.group)} · ${esc(p.label)}<div class="muted">${esc(p.key)} · rev ${p.revision}</div></td><td><input id="pr-machine-${i}" aria-label="${esc(p.label)}" value="${esc(p.path)}" style="min-width:240px;width:100%"></td><td>${p.available?'可用':p.path?'路径已失效':'未配置'}</td><td><button onclick="prSavePath(${i})">保存路径</button><span id="pr-machine-result-${i}" role="status"></span></td></tr>`).join('')}</tbody></table></div>`;
  }catch(e){if(view===routeVersion)$('app').innerHTML=`<div class="card err">项目配置加载失败：${esc(e.message)}。如刚更新平台，请重启原平台进程后刷新。</div>`;}
}
function prFields(config={}){
  const kind=$('pr-kind').value;
  if(kind==='runtime')$('pr-resource-fields').innerHTML=`<label>本机 Python 解释器<input id="pr-python" value="${esc(config.python_path||'')}" placeholder="例如 D:\\venvs\\qa\\Scripts\\python.exe"></label><label>补充依赖模块（逗号分隔）<input id="pr-modules" value="${esc((config.modules||[]).join(','))}" placeholder="requests,pymongo"></label>`;
  else if(kind==='matcheval')$('pr-resource-fields').innerHTML=`<label>模板目录（项目相对 / 本机绝对路径）<input id="pr-tpl" value="${esc(config.tpl_dir||'')}" placeholder="assets/tpl"></label><label>场景目录（项目相对 / 本机绝对路径）<input id="pr-scenes" value="${esc(config.scenes_dir||'')}" placeholder="assets/scenes"></label><label>兼容 match_eval 的脚本路径<input id="pr-script" value="${esc(config.script||'')}" placeholder="tools/match_eval.py 或本机绝对路径"></label><label>Airtest 源码根（可选，含 airtest/）<input id="pr-source" value="${esc(config.airtest_source||'')}" placeholder="留空使用该 Python 已安装的 Airtest"></label><p class="muted">均支持项目内相对路径或本机绝对路径。真值遵循 tpl_{id}_{scene}.png / scene_{scene}.png 文件名约定，脚本需支持现有 CLI / CSV 格式。指定源码根时平台会优先加载该 Airtest 包。</p>`;
  else $('pr-resource-fields').innerHTML=`<label style="grid-column:1/-1">项目内相对入口<input id="pr-path" value="${esc(config.path||'')}" placeholder="${kind==='pytest'?'tests/test_example.py::test_example':kind==='locust'?'load/locustfile.py':'tests/test_ui.py'}"></label>${kind==='airtest'?`<label class="inline"><input id="pr-service" type="checkbox" ${config.requires_service?'checked':''}>需要被测服务</label><p class="muted">UI 测试使用 platform_poco fixture；已有项目可通过一次 fixture 适配复用连接。不会自动复位或启动客户端。</p>`:''}`;
}
function prCloseForms(){
  $('pr-project-form').hidden=true;
  $('pr-runtime-form-host').innerHTML='';
  $('pr-entry-form-host').innerHTML='';
  prProjectId=null;prResourceId=null;
}
function prEdit(type,id){
  prCloseForms();
  if(type==='project'){
    const p=prData.projects.find(p=>p.id===id)||{};prProjectId=id;
    $('pr-project-heading').textContent=id?'编辑项目 · '+p.key:'新增项目';
    $('pr-project-name').value=p.name||'';$('pr-root').value=p.root_path||'';$('pr-notes').value=p.notes||'';$('pr-project-enabled').checked=p.enabled!==0;
    $('pr-project-form').hidden=false;focusForm('pr-project-form');return;
  }
  const r=prData.resources.find(r=>r.id===id)||{};prResourceId=id;
  const runtime=type==='runtime'||r.kind==='runtime',subject=runtime?'运行环境':'测试入口 / 资产';
  const host=$(runtime?'pr-runtime-form-host':'pr-entry-form-host');
  host.innerHTML=`<div class="card" id="pr-resource-form">
    <div class="row"><h2 id="pr-resource-heading">${id?'编辑':'新增'}${subject}${id?' · '+esc(r.key):''}</h2><button style="margin-left:auto" onclick="prCloseForms()">取消</button></div>
    <div class="form-grid"><label>${runtime?'运行环境名称':'入口名称'}<input id="pr-resource-name" value="${esc(r.name||'')}"></label>
    ${runtime?'<input id="pr-kind" type="hidden" value="runtime">':`<label>类型<select id="pr-kind" onchange="prFields()" ${id?'disabled':''}><option value="pytest">pytest</option><option value="airtest">airtest + Poco</option><option value="locust">locust</option><option value="matcheval">matcheval 资产</option></select></label>
    <label id="pr-project-label">所属项目<select id="pr-project" ${id?'disabled':''}>${prData.projects.length?prData.projects.map(p=>`<option value="${p.id}">${esc(p.name)}${p.enabled?'':'（停用）'}</option>`).join(''):'<option value="">请先新增项目</option>'}</select></label>`}
    <label class="inline"><input id="pr-resource-enabled" type="checkbox" ${r.enabled!==0?'checked':''}>启用条目</label></div>
    <div id="pr-resource-fields" class="form-grid" style="margin-top:12px"></div><p><button class="primary" onclick="prSaveResource()">${runtime?'保存运行环境':'保存测试入口'}</button></p><p id="pr-resource-result" role="status"></p></div>`;
  $('pr-kind').value=runtime?'runtime':r.kind||'pytest';
  if(r.project_id)$('pr-project').value=String(r.project_id);
  prFields(r.config||{});focusForm('pr-resource-form');
}
function prNewRuntime(){prEdit('runtime',null);}
async function prSaveProject(){
  const data={name:$('pr-project-name').value.trim(),root_path:$('pr-root').value.trim(),notes:$('pr-notes').value,enabled:$('pr-project-enabled').checked?1:0};
  try{await j('/api/config/projects'+(prProjectId?'/'+prProjectId:''),{method:prProjectId?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});await renderProjects();$('pr-message').textContent='项目已保存。';}
  catch(e){$('pr-message').textContent='保存项目失败：'+e.message;$('pr-message').scrollIntoView({block:'center'});}
}
function prReferenceHtml(result,deleting=false){
  const resource=!!result.resource,p=result.resource||result.project,refs=result.references||[];
  const host=resource?'pr-resource-references':'pr-project-references';
  const subject=resource?(p.kind==='runtime'?'运行环境':'测试入口 / 资产'):'项目';
  const remove=resource?'prConfirmResourceDelete':'prConfirmDelete',disable=resource?'prDisableResource':'prDisableProject';
  return `<div class="card"><h2>${deleting?'删除登记':subject+'引用'} · ${esc(p.name)}</h2><p>${esc(result.message)}</p>${refs.length?`<table><thead><tr><th>类型</th><th>名称 / 编号</th><th>状态</th><th>引用关系</th></tr></thead><tbody>${refs.map(r=>`<tr><td>${esc(r.type)}</td><td>${esc(r.name)} <span class="muted">#${r.id}</span></td><td>${esc(r.state)}</td><td>${esc(r.reason)}</td></tr>`).join('')}</tbody></table>`:'<p>不会删除项目目录、Python 解释器、脚本、素材或报告文件。</p>'}<p>${result.can_delete&&deleting?`<button onclick="${remove}(${p.id})">确认删除登记</button> `:''}${!result.can_delete&&p.enabled?`<button onclick="${disable}(${p.id})">停用${subject}</button> `:''}<button onclick="$('${host}').innerHTML=''">${deleting?'取消':'关闭'}</button></p></div>`;
}
async function prProjectReferences(id,deleting=false){
  const host=$('pr-project-references');host.textContent='正在检查项目引用…';host.scrollIntoView({block:'center'});
  try{host.innerHTML=prReferenceHtml(await j('/api/config/projects/'+id+'/references'),deleting);}
  catch(e){host.textContent='引用检查失败：'+e.message;}
}
async function prConfirmDelete(id){
  const host=$('pr-project-references');
  try{
    const response=await fetch(API+'/api/config/projects/'+id,{method:'DELETE',headers:{'X-Platform-Config':'1'}});
    const result=await response.json();
    if(response.status===409){host.innerHTML=prReferenceHtml(result,true);return;}
    if(!response.ok)throw new Error(response.status+' '+(result.detail||'删除失败'));
    await renderProjects();$('pr-message').textContent='项目登记已删除，本机目录和文件保留。';
  }catch(e){host.textContent='删除失败：'+e.message;}
}
async function prDisableProject(id){
  try{
    await j('/api/config/projects/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:0})});
    await renderProjects();$('pr-message').textContent='项目已停用，引用记录和历史报告保留。';await prProjectReferences(id);
  }catch(e){$('pr-message').textContent='停用项目失败：'+e.message;}
}
async function prResourceReferences(id,deleting=false){
  const host=$('pr-resource-references'),sequence=++prResourceReferenceSeq;
  host.textContent='正在检查引用…';host.scrollIntoView({block:'center'});
  try{const result=await j('/api/config/project-resources/'+id+'/references');if(sequence===prResourceReferenceSeq&&host.isConnected)host.innerHTML=prReferenceHtml(result,deleting);}
  catch(e){if(sequence===prResourceReferenceSeq&&host.isConnected)host.textContent='引用检查失败：'+e.message;}
}
async function prConfirmResourceDelete(id){
  const host=$('pr-resource-references');
  try{
    const response=await fetch(API+'/api/config/project-resources/'+id,{method:'DELETE',headers:{'X-Platform-Config':'1'}});
    const result=await response.json();
    if(response.status===409){host.innerHTML=prReferenceHtml(result,true);return;}
    if(!response.ok)throw new Error(response.status+' '+(result.detail||'删除失败'));
    await renderProjects();$('pr-message').textContent='入口 / 运行环境登记已删除，本机文件保留。';
  }catch(e){host.textContent='删除失败：'+e.message;}
}
async function prDisableResource(id){
  try{
    await j('/api/config/project-resources/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:0})});
    await renderProjects();$('pr-message').textContent='入口 / 运行环境已停用，引用记录和历史报告保留。';await prResourceReferences(id);
  }catch(e){$('pr-message').textContent='停用失败：'+e.message;}
}
async function prSaveResource(){
  const kind=$('pr-kind').value;
  let config;
  if(kind==='runtime')config={python_path:$('pr-python').value.trim(),modules:$('pr-modules').value.split(',').map(s=>s.trim()).filter(Boolean)};
  else if(kind==='matcheval')config={tpl_dir:$('pr-tpl').value.trim(),scenes_dir:$('pr-scenes').value.trim(),script:$('pr-script').value.trim(),airtest_source:$('pr-source').value.trim(),ground_truth:'fixture-filename'};
  else config={path:$('pr-path').value.trim(),...(kind==='airtest'?{requires_service:$('pr-service').checked}:{})};
  const data={name:$('pr-resource-name').value.trim(),kind,project_id:kind==='runtime'?0:Number($('pr-project').value),config,enabled:$('pr-resource-enabled').checked?1:0};
  try{const r=await j('/api/config/project-resources'+(prResourceId?'/'+prResourceId:''),{method:prResourceId?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});await renderProjects();$('pr-message').textContent='已保存 '+r.key+'。'+(kind==='runtime'?'请点击“关联执行机”选择引擎。':'现在可在测试用例中选择已登记入口。');}
  catch(e){$('pr-resource-result').textContent='保存失败：'+e.message;}
}
async function prCheck(type,id){
  $('pr-message').textContent='正在检查配置…';
  try{const r=await j('/api/config/'+(type==='project'?'projects':'project-resources')+'/'+id+'/precheck',{method:'POST'});$('pr-message').innerHTML=cfgPrecheckHtml(r);}
  catch(e){$('pr-message').textContent='检查失败：'+e.message;}
  $('pr-message').scrollIntoView({block:'center'});
}
async function prDiscover(id){
  $('pr-discovery').textContent='静态读取中…';
  try{
    const r=await j('/api/config/projects/'+id+'/discover',{method:'POST'});
    prData.discovered=r.candidates;
    $('pr-discovery').innerHTML=`<div class="card"><h2>发现的候选入口${r.partial?'（已截断）':''}</h2><p class="muted">${esc(r.note)}</p><table><thead><tr><th>类型</th><th>入口</th><th>操作</th></tr></thead><tbody>${r.candidates.map((c,i)=>`<tr><td>${esc(c.engine)}</td><td><code>${esc(c.path)}</code></td><td><button onclick="prFillDiscovery(${id},${i})">填入登记表</button></td></tr>`).join('')||'<tr><td colspan="3">未发现候选用例，也可手动登记文件入口</td></tr>'}</tbody></table></div>`;
  }catch(e){$('pr-discovery').textContent='发现失败：'+e.message;}
  $('pr-discovery').scrollIntoView({block:'start'});
}
function prFillDiscovery(project,i){const c=prData.discovered[i];prEdit('resource',null);$('pr-project').value=String(project);$('pr-kind').value=c.engine;$('pr-resource-name').value=c.function;prFields({path:c.path});}
async function prUseRuntime(id){
  const runtime=prData.resources.find(r=>r.id===id);
  cfgForm={type:'executor',id:null,data:{name:runtime.name,python_ref:runtime.key,allowed_runners:['pytest']}};
  cfgFocusPending=true;
  location.hash='#/env';
}
function plPytestEntryChange(i){
  const entry=((plCatalog&&plCatalog.pytest_entries)||[]).find(r=>r.key===$(`pl-s${i}-entry`).value);
  if(entry){$(`pl-s${i}-args`).value=entry.selector+' -o addopts= --tb=short';$(`pl-s${i}-cwd-root`).value=entry.root_key;}
}
async function prSavePath(i){
  const row=prData.paths[i],result=$(`pr-machine-result-${i}`);
  result.textContent=' 保存中…';
  try{
    const saved=await j('/api/config/machine-paths/'+encodeURIComponent(row.key),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:$(`pr-machine-${i}`).value.trim()})});
    prData.paths[i]=saved;
    $(`pr-machine-${i}`).value=saved.path;
    result.closest('tr').querySelector('.muted').textContent=saved.key+' · rev '+saved.revision;
    result.textContent=' 已保存 · '+(saved.available?'可用':'未配置');
    result.parentElement.previousElementSibling.textContent=saved.available?'可用':'未配置';
  }catch(e){result.textContent=' 保存失败：'+e.message;}
}
