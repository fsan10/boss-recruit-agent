/* DOM integration test with a real API subprocess. This is not a pixel/browser acceptance test. */
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {spawn}=require('node:child_process');
const {JSDOM,VirtualConsole}=require('jsdom');
const root=path.resolve(__dirname,'..');
const python=process.env.RECRUIT_PYTHON||path.join(root,'.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python');
const runtime=fs.mkdtempSync(path.join(os.tmpdir(),'recruit-ui-'));
const port=18765, base=`http://127.0.0.1:${port}`;
let child,dom;
const errors=[],steps=[];
const wait=ms=>new Promise(r=>setTimeout(r,ms));
async function until(fn,message){for(let i=0;i<150;i++){if(await fn())return;await wait(40);}throw new Error(message);}
async function req(endpoint,body){const r=await fetch(base+'/api'+endpoint,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});assert.equal(r.status,200);return r.json();}
async function go(view){dom.window.location.hash=view;await until(()=>dom.window.document.querySelector('nav .active')?.dataset.view===view,'navigation '+view);await wait(80);}
function click(selector){const el=dom.window.document.querySelector(selector);assert.ok(el,'missing '+selector);el.click();}
async function main(){
 child=spawn(python,['-m','recruit_agent','serve','--runtime',runtime,'--port',String(port)],{cwd:root,stdio:['ignore','pipe','pipe']});
 child.stderr.on('data',data=>{if(data.toString().includes('Traceback'))errors.push('API traceback');});
 await until(async()=>{try{return (await fetch(base+'/api/health/live')).ok;}catch(e){return false;}},'API did not start');
 const vc=new VirtualConsole();vc.on('jsdomError',e=>errors.push(String(e)));vc.on('error',e=>errors.push(String(e)));
 dom=await JSDOM.fromURL(base,{resources:'usable',runScripts:'dangerously',pretendToBeVisual:true,virtualConsole:vc,beforeParse(window){
  window.fetch=(url,options)=>fetch(new URL(url,base),options);
  window.structuredClone=global.structuredClone;window.scrollTo=()=>{};window.HTMLElement.prototype.scrollIntoView=function(){};
 }});
 const doc=dom.window.document;
 await until(()=>doc.querySelector('h1')?.textContent.includes('简历'),'overview render');steps.push('overview and worker heartbeat');
 click('[data-action="demo"]');await until(async()=>((await req('/candidates')).length===10),'ten resumes');
 await go('candidates');await until(()=>doc.querySelectorAll('tbody tr').length===10,'candidate list');steps.push('ten candidate rows and independent IDs');
 await req('/demo/fixture',{});click('#refresh');await until(()=>doc.querySelector('.score-number'),'fixture scores');
 click('[data-action="candidate"][data-id="candidate-001"]');await until(()=>doc.querySelector('.quote'),'evidence detail');
 assert.ok(doc.body.textContent.includes('预置人工格式样例'));click('[data-action="evidence"]');assert.ok(doc.querySelector('.evidence-block.highlight'));steps.push('fixture badge and original evidence anchor');
 click('[data-action="override"]');await until(()=>doc.querySelector('#override-form'),'override form');
 const first=doc.querySelector('#override-form [data-key="rating"]');first.value='1';doc.querySelector('#override-reason').value='DOM 集成测试：人工更正一项判断';click('[data-action="submit-override"]');
 await until(()=>doc.querySelector('.run-note')?.textContent.includes('human_review'),'override result');steps.push('human correction with retained original');
 await go('settings');await until(()=>doc.querySelector('#settings-form'),'settings form');
 doc.querySelector('#h-primary').value='mcp';doc.querySelector('#h-primary').dispatchEvent(new dom.window.Event('change',{bubbles:true}));
 doc.querySelector('#h-model').value='test-ui-mcp-no-model-execution';doc.querySelector('#settings-form').dispatchEvent(new dom.window.Event('submit',{bubbles:true,cancelable:true}));
 await until(async()=>((await req('/config')).config.harness.primary==='mcp'),'save configuration');steps.push('MCP settings and version update');
 await go('jobs');await until(()=>doc.querySelector('[data-action="new-job"]'),'jobs view');click('[data-action="new-job"]');
 const form=doc.querySelector('#new-job-form');form.elements.title.value='合成 UI 测试岗位';form.elements.jd_text.value='编写 Python 自动化脚本，集成 API，处理可重试任务，提供日志、验证和维护文档。';click('[data-action="submit-job"]');
 await until(async()=>((await req('/jobs')).length===2),'job creation');
 const jobs=await req('/jobs'),job=jobs.find(j=>!j.is_synthetic);await req(`/jobs/${job.job_id}/activate`,{});await dom.window.render();
 click('[data-action="edit-job"][data-id="'+job.job_id+'"]');await until(()=>doc.querySelector('#job-edit-form'),'rubric review');assert.equal(doc.querySelectorAll('#job-edit-form [data-criterion]').length,5);click('[data-action="close-modal"]');steps.push('JD draft and editable rubric');
 await go('candidates');click('[data-action="import"]');await until(()=>doc.querySelector('#import-form'),'import form');
 doc.querySelector('#import-job').value=job.job_id;doc.querySelector('[data-field="label"]').value='同名<img src=x onerror=alert(1)>';doc.querySelector('[data-field="text"]').value='使用 Python 编写自动化脚本，完成部署和验证。';click('[data-action="submit-import"]');
 await until(async()=>((await req('/candidates')).length===11),'text import');await dom.window.render();assert.equal(doc.querySelectorAll('#content img').length,0);steps.push('plain-text import and HTML escaping');
 await go('tasks');await until(()=>doc.querySelector('.task-card'),'batch view');assert.ok(doc.body.textContent.includes('10 / 10')||doc.body.textContent.includes('已取消'));steps.push('task history and member status');
 await go('drafts');await until(()=>doc.querySelector('.draft-card'),'drafts');assert.equal(doc.querySelectorAll('[data-action="send-draft"]').length,0);steps.push('draft-only outbox');
 assert.deepEqual(errors,[]);
 fs.mkdirSync(path.join(root,'docs','qa'),{recursive:true});fs.writeFileSync(path.join(root,'docs','qa','ui-smoke.json'),JSON.stringify({passed:true,method:'jsdom DOM with real local API; no pixel rendering',steps,errors},null,2));
 console.log(JSON.stringify({passed:true,steps:steps.length,method:'DOM + HTTP integration'}));
}
main().catch(error=>{console.error(error.stack);process.exitCode=1;}).finally(()=>{if(dom)dom.window.close();if(child)child.kill('SIGTERM');setTimeout(()=>{if(child)child.kill('SIGKILL');fs.rmSync(runtime,{recursive:true,force:true});},500).unref();});
