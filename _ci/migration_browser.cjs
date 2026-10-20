// UI acceptance on the installed, isolated service; no real business system.
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const output=path.resolve(process.env.PLATFORM_MIGRATION_BROWSER_OUTPUT||'_ci_out/migration-browser');
fs.mkdirSync(output,{recursive:true});
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.PLATFORM_BROWSER_EXECUTABLE,headless:true});
 const page=await browser.newPage({viewport:{width:1400,height:1000}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 const button=name=>page.getByRole('button',{name,exact:true});
 try{
  await page.goto((process.env.PLATFORM_MIGRATION_TEST_URL||'http://127.0.0.1:18085')+'/#/migration');
  await button('填入示例模板').click();await page.waitForFunction(()=>document.querySelector('#mg-document').value.includes('unified-test-platform/config'));
  const doc=JSON.parse(await page.locator('#mg-document').inputValue()),suffix=Date.now().toString().slice(-7);
  doc.cases[0].name='网页自有用例 '+suffix;doc.suites[0].name='网页自有套件 '+suffix;doc.plans[0].name='网页自有计划 '+suffix;
  const file=path.join(output,'own-definitions.json');fs.writeFileSync(file,JSON.stringify(doc,null,2));
  await page.locator('#mg-document').fill('');await page.locator('#mg-file').setInputFiles(file);
  await page.waitForFunction(()=>document.querySelector('#mg-document').value.includes('网页自有用例'));
  await button('导入到待关联批次').click();await page.locator('#mg-map-0').waitFor();
  const batchUrl=page.url();
  await button('保存关联并预览').click();await page.getByRole('heading',{name:'尚未就绪',exact:true}).waitFor();
  assert.equal(await button('确认新增这些记录').count(),0);
  await page.locator('#mg-map-0').selectOption({label:'Own offline environment'});
  await page.locator('#mg-map-1').selectOption({label:'Own pytest executor'});
  await page.locator('#mg-map-2').selectOption({label:'Own pytest entry'});
  await page.locator('#mg-prefix').fill('网页 · ');await button('保存关联并预览').click();await page.getByRole('heading',{name:'导入预览通过',exact:true}).waitFor();
  await page.reload();await page.locator('#mg-map-0').waitFor();assert.notEqual(await page.locator('#mg-map-0').inputValue(),'');
  await button('保存关联并预览').click();await page.getByRole('heading',{name:'导入预览通过',exact:true}).waitFor();
  await page.locator('#mg-prefix').fill('Changed · ');assert.equal(await button('确认新增这些记录').count(),0);
  await page.locator('#mg-prefix').fill('网页 · ');await button('保存关联并预览').click();await page.getByRole('heading',{name:'导入预览通过',exact:true}).waitFor();
  await page.locator('#mg-batch').screenshot({path:path.join(output,'01-import-preview.png')});
  await button('确认新增这些记录').click();await page.getByRole('heading',{name:'导入完成',exact:true}).waitFor();
  await page.locator('#mg-batch').screenshot({path:path.join(output,'02-import-receipt.png')});
  const downloadPromise=page.waitForEvent('download');await button('导出选中项 JSON').click();const download=await downloadPromise;
  const exported=path.join(output,'exported-definitions.json');await download.saveAs(exported);
  const exportedDoc=JSON.parse(fs.readFileSync(exported,'utf8'));assert.equal(exportedDoc.format,'unified-test-platform/config');
  assert.ok(exportedDoc.cases.some(c=>c.name==='网页 · '+doc.cases[0].name));
  await page.getByRole('link',{name:'网页 · '+doc.plans[0].name+' →',exact:true}).click();await page.locator('#pl-name').waitFor();
  await button('预检 / 执行预览').click();await page.locator('#pl-precheck-host .pre').waitFor();
  await button('保存并执行').click();await page.waitForURL(/#\/run\/\d+/);await page.getByText('success',{exact:true}).first().waitFor({timeout:30000});
  await page.screenshot({path:path.join(output,'03-own-plan-success.png'),fullPage:true});
  assert.deepEqual(errors,[]);
  const result={batchUrl,runUrl:page.url(),userAuthoredFile:true,pendingResumed:true,mappingEditInvalidatedPreview:true,exportDownloaded:true,ownPlanSucceeded:true,errors};
  fs.writeFileSync(path.join(output,'verification.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result,null,2));
 }catch(e){await page.screenshot({path:path.join(output,'failure.png'),fullPage:true}).catch(()=>{});console.error((await page.locator('main').innerText().catch(()=>''))+'\n'+e.stack);process.exitCode=1;}
 finally{await page.close();await browser.close();}
})();
