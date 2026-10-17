const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { pathToFileURL } = require('node:url');

(async () => {
  const directory = path.resolve(__dirname, '..');
  const service = spawn(process.env.PYTHON || 'python3', ['server.py', '--port', '0'], { cwd: directory, stdio: ['ignore', 'pipe', 'pipe'] });
  let browser;
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'dashboard-browser-'));
  try {
    const url = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('Server did not start')), 15000);
      let output = '';
      service.once('exit', code => { clearTimeout(timeout); reject(new Error(`Server exited: ${code}`)); });
      service.stderr.on('data', () => {});
      service.stdout.on('data', chunk => {
        output += chunk;
        const match = output.match(/http:\/\/127\.0\.0\.1:\d+/);
        if (match) { clearTimeout(timeout); resolve(match[0]); }
      });
    });
    browser = await chromium.launch({ headless: true, ...(process.env.BROWSER_EXECUTABLE ? { executablePath: process.env.BROWSER_EXECUTABLE } : {}) });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(url, { waitUntil: 'networkidle' });
    await page.waitForSelector('.project-row');
    assert.equal(await page.locator('.project-row').count(), 4);
    assert.match(await page.locator('#data-banner').innerText(), /演示数据/);
    assert.equal(await page.locator('.performance-panel .chart svg').count(), 3);
    assert.ok((await page.locator('.performance-panel .chart svg').first().boundingBox()).width > 200);
    assert.equal(await page.locator('.bound-card').count(), 3);
    assert.match(await page.locator('#trace-sessions').innerText(), /尚未启用/);
    assert.equal(await page.locator('[data-version]').count(), 2);
    await page.locator('[data-version="v2"]').click();
    assert.match(await page.locator('#version-detail').innerText(), /用复测检验改动/);
    await page.getByRole('button', { name: 'CPU', exact: true }).click();
    assert.equal(await page.locator('.project-row').count(), 2);
    assert.equal(await page.locator('.performance-panel').count(), 2);
    await page.getByRole('button', { name: 'GPU', exact: true }).click();
    assert.equal(await page.locator('.project-row').count(), 2);
    assert.equal(await page.locator('.performance-panel').count(), 1);
    await page.getByRole('button', { name: '全部项目' }).click();
    await page.getByRole('searchbox').fill('Compute');
    assert.equal(await page.locator('.project-row').count(), 1);
    await page.locator('.project-row').click();
    assert.equal(await page.getByRole('dialog').isVisible(), true);
    assert.match(await page.locator('#dialog-body').innerText(), /evidence\.json/);
    await page.keyboard.press('Escape');
    await page.getByRole('searchbox').fill('no-matching-project');
    assert.equal(await page.locator('.project-row').count(), 0);
    assert.equal(await page.locator('#no-performance').isVisible(), true);
    await page.getByRole('searchbox').fill('');
    await page.getByRole('button', { name: '刷新记录', exact: true }).click();
    await page.waitForFunction(() => !document.getElementById('refresh-button').disabled);
    await page.screenshot({ path: path.join(directory, 'artifacts/desktop.png'), fullPage: true });
    if (process.env.UPDATE_PREVIEW === '1') {
      fs.mkdirSync(path.join(directory, 'docs'), { recursive: true });
      await page.screenshot({ path: path.join(directory, 'docs/preview.png') });
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(200);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.ok((await page.locator('.performance-panel .chart svg').first().boundingBox()).width > 200);
    await page.screenshot({ path: path.join(directory, 'artifacts/mobile.png'), fullPage: true });
    const html = await (await page.request.get(url + '/download.html')).text();
    const offlineFile = path.join(temp, 'snapshot.html');
    fs.writeFileSync(offlineFile, html);
    const offline = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
    offline.on('pageerror', error => errors.push(error.message));
    const outbound = [];
    offline.on('request', request => { if (/^https?:/.test(request.url())) outbound.push(request.url()); });
    await offline.goto(pathToFileURL(offlineFile).href, { waitUntil: 'load' });
    await offline.waitForSelector('.project-row');
    assert.equal(await offline.locator('.project-row').count(), 4);
    assert.equal(await offline.locator('.chart svg').count(), 3);
    assert.match(await offline.locator('#trace-sessions').innerText(), /离线快照不包含个人会话/);
    assert.equal(await offline.locator('#refresh-button').isDisabled(), true);
    assert.deepEqual(outbound, []);
    assert.deepEqual(errors, []);
    console.log('Browser checks passed: desktop, mobile, filters, charts, detail, learning, refresh, trace opt-in, and offline export.');
  } finally {
    if (browser) await browser.close();
    service.kill('SIGTERM');
    fs.rmSync(temp, { recursive: true, force: true });
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
