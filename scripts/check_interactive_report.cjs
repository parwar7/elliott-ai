const {chromium}=require('C:/Users/Parwa/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const {pathToFileURL}=require('url');
const path=require('path');
(async()=>{
 const browser=await chromium.launch({headless:true,channel:'msedge'});
 const page=await browser.newPage({viewport:{width:1450,height:1050}}), errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto(pathToFileURL(path.resolve('reports/TSLA_interactive_20260909_v2/TSLA_report.html')).href);
 await page.waitForFunction(()=>window.reportReady===true);
 await page.screenshot({path:'reports/TSLA_interactive_20260909_v2/desktop.png',fullPage:true});
 for(const tf of ['monthly','weekly','daily','4h','1h','15m']){
  await page.selectOption('#tf',tf);
  await page.waitForFunction(t=>document.querySelector('#chart').data[0].x.length===JSON.parse(document.querySelector('#payload').textContent).datasets[t].candles.length,tf);
 }
 await page.selectOption('#tf','daily');
 await page.selectOption('#role','alternative');
 await page.selectOption('#indicator','macd');
 await page.selectOption('#scale','log');
 await page.waitForFunction(()=>document.querySelector('#chart').layout.yaxis.type==='log');
 const state=await page.evaluate(()=>({traces:document.querySelector('#chart').data.length,labels:document.querySelector('#chart').layout.annotations.map(x=>x.text)}));
 if(state.traces!==4 || state.labels.some(x=>/^A\?|^B\?|^C\?/.test(x)))throw Error('Toggle failed');
 await page.selectOption('#role','both');await page.selectOption('#scale','linear');await page.selectOption('#indicator','rsi14');
 await page.setViewportSize({width:390,height:844});
 await page.waitForFunction(()=>document.querySelector('#chart .svg-container').getBoundingClientRect().width<=390);
 await page.waitForTimeout(500);
 await page.screenshot({path:'reports/TSLA_interactive_20260909_v2/mobile.png',fullPage:true});
 const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);
 if(overflow||errors.length)throw Error(JSON.stringify({overflow,errors}));
 console.log(JSON.stringify({result:'PASS',timeframes:6,toggles:'PASS',mobileOverflow:false,pageErrors:errors}));
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
