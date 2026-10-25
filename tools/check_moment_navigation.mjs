// Navigation-only regression: synthetic Moment/Thread HTTP fixtures, local Auth; no model calls.
import { createRequire } from 'node:module';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
const require=createRequire(path.join(process.cwd(),'apps/mobile/package.json'));
const {chromium,expect}=require('@playwright/test');
const browser=await chromium.launch({channel:'msedge',headless:true});
const context=await browser.newContext({viewport:{width:430,height:932}});
const page=await context.newPage();
const warnings=[]; page.on('console', m=>{if(m.type()==='warning'||m.type()==='error')warnings.push(m.text());});
const momentId='ae1c3c91-2104-4e55-b6c2-511458860c71',threadId='c37c9396-a940-472a-ae45-86d3a8af41f2';
const history=[{id:'message-one',thread_id:threadId,role:'user',content:'合成导航测试：想聊一本新书。',evidence_moment_ids:[],created_at:'2026-10-25T02:00:00Z'},{id:'message-two',thread_id:threadId,role:'assistant',content:'合成旧对话：你想从哪一章说起？',evidence_moment_ids:[],created_at:'2026-10-25T02:01:00Z'}];
const fixture={id:momentId,content:'合成时刻入口：读书的一天。',mode:'chat',input_type:'text',memory_enabled:true,thread_id:threadId,created_at:'2026-10-25T02:00:00Z',messages:history};
const capture={...fixture,id:'52ee9136-1d27-4d03-8cf9-570103534417',content:'合成新时刻：今天想整理书架。',thread_id:null,messages:[]};
let threadLoads=0, sentPayload=null;
await context.route('**/*',async route=>{
 const u=new URL(route.request().url());
 if(!['127.0.0.1','localhost'].includes(u.hostname)&&u.protocol.startsWith('http'))return route.abort();
 if(u.port==='18000'){
  if(u.pathname==='/chat'&&route.request().method()==='POST'){
   sentPayload=route.request().postDataJSON();
   return route.fulfill({json:{thread_id:'new-synthetic-thread',moment_id:capture.id,message:{...history[1],id:'new-response',content:'合成新会话回复。'},evidence_moment_ids:[],used_long_term_memory:false}});
  }
  if(u.pathname==='/moments')return route.fulfill({json:[fixture,capture]});
  if(u.pathname===`/moments/${capture.id}`)return route.fulfill({json:capture});
  if(u.pathname===`/moments/${momentId}`)return route.fulfill({json:fixture});
  if(u.pathname===`/threads/${threadId}/messages`){threadLoads++;return route.fulfill({json:history});}
  if(u.pathname.endsWith('/resumed'))return route.fulfill({json:{ok:true}});
 }
 return route.continue();
});
try{
 await page.goto('http://127.0.0.1:8081/');
 await page.getByPlaceholder('邮箱').fill(process.env.NAV_EMAIL);
 await page.getByPlaceholder('密码').fill(process.env.NAV_PASSWORD);
 await page.getByText('登录',{exact:true}).click();
 await expect(page.getByText('留一下',{exact:true})).toBeVisible();
 await page.getByText('时刻',{exact:true}).click();
 await page.getByText(fixture.content,{exact:true}).click();
 await expect(page.getByText('继续聊聊',{exact:true})).toBeVisible();
 await page.getByText('继续聊聊',{exact:true}).click();
 await expect.poll(()=>new URL(page.url()).pathname).toBe('/');
 await expect.poll(()=>threadLoads).toBeGreaterThan(0);
 await expect(page.getByText(history[1].content,{exact:true})).toBeVisible({timeout:8000});
 await page.getByText('时刻',{exact:true}).click();
 await page.getByText(capture.content,{exact:true}).click();
 await page.getByText('聊聊这个',{exact:true}).click();
 await expect.poll(()=>new URL(page.url()).pathname).toBe('/');
 await expect(page.getByPlaceholder('写点什么…')).toHaveValue(capture.content);
 await expect(page.getByText(history[1].content,{exact:true})).toHaveCount(0);
 await page.getByLabel('发送').click();
 await expect.poll(()=>sentPayload?.source_moment_id).toBe(capture.id);
 expect(sentPayload.thread_id).toBeNull();
 await expect(page.getByText('合成新会话回复。',{exact:true})).toBeVisible();
 const result={passed:true,checks:['resume_navigation','history_loaded','capture_resets_old_history','capture_draft','send_uses_correct_source'],threadLoads};
 await writeFile('.acceptance/moment-navigation-checks.json',JSON.stringify(result,null,2));
 console.log(JSON.stringify(result));
}catch(error){
 await writeFile('.acceptance/nav-failure.json',JSON.stringify({url:page.url(),threadLoads,warnings,error:String(error),body:await page.locator('body').innerText()},null,2));
 console.log(JSON.stringify({passed:false,url:page.url(),threadLoads,error:String(error)}));
 process.exitCode=1;
}finally{await browser.close();}
