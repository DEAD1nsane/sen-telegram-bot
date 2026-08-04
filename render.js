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
    args: ['--no-sandbox', '--disable-setuid-sandbox']
  });

  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1',
    isMobile: true
  });

  const page = await context.newPage();

  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => false });
  });

  try {
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
    // Give extra breathing room for client-side hydro without hanging on networkidle
    await page.waitForTimeout(5000);
    await page.screenshot({ path: 'output.png', fullPage: true });
    console.log("Screenshot saved as output.png");
  } catch (err) {
    console.error("Error loading page:", err);
  } finally {
    await browser.close();
  }
})();
