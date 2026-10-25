// Browser regression for product fixes; local Auth, synthetic API fixtures, zero model calls.
import {createRequire} from 'node:module';
import {writeFile} from 'node:fs/promises';
import path from 'node:path';
const require=createRequire(path.join(process.cwd(),'apps/mobile/package.json'));
const {chromium,expect}=require('@playwright/test');
const browser=await chromium.launch({channel:'msedge',headless:true});
const context=await browser.newContext({viewport:{width:430,height:932}});
const page=await context.newPage();const checks=[],errors=[];
page.on('pageerror',e=>errors.push(e.message));
async function passed(check){checks.push({check,passed:true});await writeFile('.acceptance/product-ui-fixes-checks.json',JSON.stringify(checks,null,2));console.log('PASS',check);}
let savedRequest=null,releaseSave,deleted=false,pagination=false;
const gate=new Promise(r=>{releaseSave=r;});
const moment={id:'eeaf7d53-8f97-4aef-a5e5-71be09cd3e6d',content:'合成设置测试记录',created_at:'2026-10-25T01:00:00Z',thread_id:null,memory_enabled:true,input_type:'text',mode:'capture',messages:[]};
const memory={id:'fbaf7d53-8f97-4aef-a5e5-71be09cd3e6d',content:'合成旧记忆',memory_type:'interest',status:'active',memory_sources:[]};
const pages=Array.from({length:51},(_,i)=>({...moment,id:`00000000-0000-4000-8000-${String(i+1).padStart(12,'0')}`,content:`合成分页记录${i+1}`}));
await context.route('**/*',async route=>{
 const u=new URL(route.request().url()),method=route.request().method();
 if(u.protocol.startsWith('http')&&!['localhost','127.0.0.1'].includes(u.hostname))return route.abort();
 if(u.port!=='18000')return route.continue();
 if(u.pathname==='/moments'&&method==='POST'){savedRequest=route.request().postDataJSON();await gate;return route.fulfill({json:moment});}
 if(u.pathname==='/moments')return route.fulfill({json:pagination?(u.searchParams.has('before_id')?pages.slice(50):pages.slice(0,50)):(deleted?[]:[moment])});
 if(u.pathname===`/moments/${moment.id}`){
  if(method==='PATCH'){moment.content=route.request().postDataJSON().content;return route.fulfill({json:moment});}
  if(method==='DELETE'){deleted=true;return route.fulfill({json:{deleted:true}});}
  return route.fulfill({json:moment});
 }
 if(u.pathname.endsWith('/memory')&&method==='PATCH')return route.fulfill({status:503,json:{detail:'设置暂时无法保存，请重试。'}});
 if(u.pathname==='/chat')return route.fulfill({json:{thread_id:'synthetic-thread',moment_id:moment.id,message:{id:'reply',role:'assistant',content:'合成聊天缓存：这是清空前的内容。',evidence_moment_ids:[]},evidence_moment_ids:[],used_long_term_memory:false}});
 if(u.pathname==='/account/data')return route.fulfill({json:{ok:true,deleted:{}}});
 if(u.pathname==='/memories')return route.fulfill({json:[memory]});
 if(u.pathname.startsWith('/memories/'))return route.fulfill({status:503,json:{detail:'记忆操作暂时失败，请重试。'}});
 if(u.pathname.endsWith('/feedback'))return route.fulfill({status:503,json:{detail:'反馈暂时无法保存。'}});
 if(u.pathname.startsWith('/insights/'))return route.fulfill({json:{id:'synthetic-insight',title:'合成发现',body:'合成正文',insight_evidence:[],trigger_type:'user_query',verification_status:'WEAK'}});
 if(u.pathname==='/jobs'||u.pathname==='/insights'||u.pathname==='/weekly-reports')return route.fulfill({json:[]});
 if(u.pathname==='/events'||u.pathname==='/weekly-reports/ensure')return route.fulfill({json:{ok:true}});
 throw new Error('Unexpected fixture request: '+u.pathname);
});
try{
 await page.goto('http://127.0.0.1:8081/');
 await page.getByPlaceholder('邮箱').fill(process.env.NAV_EMAIL);await page.getByPlaceholder('密码').fill(process.env.NAV_PASSWORD);
 await page.getByText('登录',{exact:true}).click();await expect(page.getByText('留一下',{exact:true})).toBeVisible();
 const input=page.getByPlaceholder('写点什么…');
 await input.fill('合成第一条待保存内容');await page.getByLabel('发送').click();
 await expect.poll(()=>savedRequest?.content).toBe('合成第一条待保存内容');
 await input.fill('合成第二条尚未提交的草稿');releaseSave();
 await expect(page.getByText('树洞收到了',{exact:true}).first()).toBeVisible();
 await expect(input).toHaveValue('合成第二条尚未提交的草稿');await passed('save_keeps_new_draft');
 await page.getByText('聊一会儿',{exact:true}).click();await input.fill('合成聊天缓存测试');await page.getByLabel('发送').click();
 await expect(page.getByText('合成聊天缓存：这是清空前的内容。',{exact:true})).toBeVisible();
 await page.getByText('我的',{exact:true}).click();await page.getByText('清空个人数据',{exact:true}).click();
 await page.getByText('再次点击确认清空',{exact:true}).click();
 await expect(page.getByText('EchoTrace',{exact:true})).toBeVisible();
 await expect(page.getByText('合成聊天缓存：这是清空前的内容。',{exact:true})).toHaveCount(0);
 await expect(input).toHaveValue('');await passed('clear_resets_chat_history_and_draft');
 await page.getByText('时刻',{exact:true}).click();await page.getByText(moment.content,{exact:true}).click();
 await page.getByRole('switch').click();await expect(page.getByText('设置暂时无法保存，请重试。',{exact:true})).toBeVisible();
 expect(errors).toEqual([]);await passed('permission_failure_has_notice_without_unhandled_error');
 await page.getByText('编辑原文',{exact:true}).click();await page.getByLabel('编辑时刻原文').fill('合成修改后的原文');
 await page.getByText('保存修改',{exact:true}).click();await expect(page.getByText('合成修改后的原文',{exact:true})).toBeVisible();await passed('edit_original_record');
 await page.getByText('删除时刻',{exact:true}).click();await page.getByText('确认删除时刻',{exact:true}).click();
 await expect(page.getByText('还没有留下什么。第一条不用想太多，随便说点什么就好',{exact:true})).toBeVisible();await passed('delete_original_record_and_reset_views');
 pagination=true;await page.goto('http://127.0.0.1:8081/moments');
 await page.getByText('加载更早的时刻',{exact:true}).click();
 await expect(page.getByText('合成分页记录51',{exact:true})).toBeVisible();await passed('history_load_more');
 await page.goto('http://127.0.0.1:8081/me');await page.getByText('纠正',{exact:true}).click();
 await page.locator('textarea').fill('合成旧记忆纠正');await page.getByText('保存',{exact:true}).click();
 await expect(page.getByText('记忆操作暂时失败，请重试。',{exact:true})).toBeVisible();await passed('memory_edit_failure_notice');
 await page.getByText('取消',{exact:true}).click();await page.getByText('删除',{exact:true}).click();await page.getByText('再次点击确认删除',{exact:true}).click();
 await expect(page.getByText('记忆操作暂时失败，请重试。',{exact:true})).toBeVisible();await passed('memory_delete_failure_notice');
 await page.goto('http://127.0.0.1:8081/insight/synthetic-insight');await page.getByText('符合',{exact:true}).click();
 await expect(page.getByText('反馈没有保存成功，请稍后重试。',{exact:true})).toBeVisible();expect(errors).toEqual([]);await passed('feedback_failure_notice');
}catch(e){await writeFile('.acceptance/product-ui-failure.json',JSON.stringify({error:String(e),body:await page.locator('body').innerText(),errors},null,2));throw e;}
finally{releaseSave();await browser.close();}
