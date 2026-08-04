const { chromium } = require('playwright-extra');
const stealth = require('puppeteer-extra-plugin-stealth')();
chromium.use(stealth);

(async () => {
  const url = process.argv[2];
  if (!url) {
    console.error("Please provide a URL");
    process.exit(1);
  }

  const browser = await chromium.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox'],
    proxy: { server: 'socks5://127.0.0.1:9050' }
  });

  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1',
    isMobile: true
  });

  await context.addCookies([{
    name: 'cf_clearance',
    value: '9eecd37eb482f97afc12372be3c5360d24697aa295aa9176bda4137e072922b8df32cf014058a7f46fe439c48732ffc4',
    domain: '.stake.us',
    path: '/'
  }]);

  const page = await context.newPage();

  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => false });
  });
  
  try {
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
    
    try {
      const frame = page.frameLocator('iframe[src*="challenges.cloudflare.com"]');
      await frame.locator('label.cb-lb').click({ timeout: 5000 });
    } catch (e) {
      // Ignore if Cloudflare widget frame is not present or already passed
    }

    await page.waitForTimeout(15000);
    await page.screenshot({ path: 'win.png', fullPage: true });
    console.log("Screenshot saved as win.png");
  } catch (err) {
    console.error("Error loading page:", err);
  } finally {
    await browser.close();
  }
})();
