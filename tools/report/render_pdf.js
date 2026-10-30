// 把 report_pdf.html 渲染成手机宽度 PDF（120mm 宽）。用法：SRC=页面.html OUT=输出.pdf NODE_PATH=$(npm root -g) node render_pdf.js
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch(); const p = await b.newPage({ viewport: { width: 454, height: 945 }, deviceScaleFactor: 2 });
  const errs=[]; p.on('pageerror', e => errs.push(e.message));
  await p.goto('file://' + process.env.SRC); await p.waitForTimeout(800);
  console.log('scrollW', await p.evaluate(()=>document.documentElement.scrollWidth));
  await p.emulateMedia({ media: 'print' });
  await p.pdf({ path: process.env.OUT, width: '120mm', height: '250mm', printBackground: true, margin: {top:'0',bottom:'0',left:'0',right:'0'} });
  console.log(errs.join('\n')||'no errors'); await b.close();
})();
