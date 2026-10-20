// Offline guide/checklist acceptance. No platform service or business project.
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {pathToFileURL}=require('node:url');
const output=path.resolve('_ci_out/releases/20261018-cross-pc-acceptance/verification');
fs.mkdirSync(output,{recursive:true});
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.PLATFORM_BROWSER_EXECUTABLE});
 const context=await browser.newContext({viewport:{width:1440,height:1000},acceptDownloads:true});
 const page=await context.newPage(),errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 try{
  await page.goto(pathToFileURL(path.resolve('frontend/help/cross-pc-acceptance.html')).href);
  assert.equal(await page.locator('select[data-check]').count(),35);
  assert.equal(await page.locator('aside a').count(),12);
  await page.locator('input[data-save="meta-0"]').fill('目标机验收员');
  await page.locator('select[data-check="A01"]').selectOption('通过');
  await page.locator('select[data-check="A15"]').selectOption('通过');
  await page.locator('textarea[data-save="note-A15"]').fill('故意失败计划 failed；断言与日志符合预期。');
  await page.reload();
  assert.equal(await page.locator('input[data-save="meta-0"]').inputValue(),'目标机验收员');
  assert.equal(await page.locator('select[data-check="A15"]').inputValue(),'通过');
  const event=page.waitForEvent('download');await page.locator('#export-results').click();
  const file=path.join(output,'填写结果示例.json');await(await event).saveAs(file);
  const result=JSON.parse(fs.readFileSync(file,'utf8'));assert.equal(result.values['status-A15'],'通过');
  page.once('dialog',d=>d.accept());await page.locator('#reset').click();
  assert.equal(await page.locator('input[data-save="meta-0"]').inputValue(),'');
  assert.equal(await page.locator('select[data-check="A15"]').inputValue(),'未测');
  await page.locator('#load-results').setInputFiles(file);
  await page.waitForFunction(()=>document.querySelector('[data-save="meta-0"]').value==='目标机验收员');
  assert.equal(await page.locator('select[data-check="A15"]').inputValue(),'通过');
  const broken=await page.evaluate(()=>[...document.querySelectorAll('aside a')].filter(a=>!document.getElementById(a.hash.slice(1))).map(a=>a.hash));
  assert.deepEqual(broken,[]);
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.screenshot({path:path.join(output,'01-操作手册.png')});
  await page.locator('#checklist').scrollIntoViewIfNeeded();
  await page.screenshot({path:path.join(output,'02-可填写清单.png')});
  await page.emulateMedia({media:'print'});
  assert.equal(await page.locator('select[data-check="A15"]').isVisible(),false);
  assert.equal(await page.locator('select[data-check="A15"] + .print-value').innerText(),'通过');
  await page.emulateMedia({media:'screen'});await page.setViewportSize({width:390,height:844});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  assert.deepEqual(errors,[]);
  fs.writeFileSync(path.join(output,'browser-verification.json'),JSON.stringify({offline:true,checklistItems:35,persistence:true,export:true,reset:true,import:true,printValues:true,mobileNoOverflow:true,errors},null,2));
  process.stdout.write('Offline guide and 35-item checklist verified\n');
 }finally{await browser.close();}
})().catch(e=>{process.stderr.write(e.stack+'\n');process.exitCode=1;});
