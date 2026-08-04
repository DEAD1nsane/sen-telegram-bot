const { chromium } = require('playwright-extra');
const stealth = require('puppeteer-extra-plugin-stealth')();
chromium.use(stealth);

async function getProxies() {
  const res = await fetch('https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=ipport&country=us&protocol=http');
  const text = await res.text();
  return text.trim().split(/\r?\n/).filter(Boolean);
}

(async () => {
  const url = process.argv[2];
  if (!url) {
    console.error("Please provide a URL");
    process.exit(1);
  }

  const proxies = await getProxies();
  if (proxies.length === 0) {
    console.error("No proxies available");
    process.exit(1);
  }

  let success = false;

  for (const proxyIpPort of proxies) {
    console.log(`Trying proxy: ${proxyIpPort}`);
    let browser;
    try {
      browser = await chromium.launch({
        headless: true,
        args: ['--no-sandbox', '--disable-setuid-sandbox'],
        proxy: { server: `http://${proxyIpPort}` }
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

      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
      
      try {
        const frame = page.frameLocator('iframe[src*="challenges.cloudflare.com"]');
        await frame.locator('label.cb-lb').click({ timeout: 5000 });
      } catch (e) {
        // Ignore if not present
      }

      await page.waitForTimeout(10000);
      await page.screenshot({ path: 'win.png', fullPage: true });
      console.log("Screenshot saved as win.png using proxy:", proxyIpPort);
      success = true;
      await browser.close();
      break;
    } catch (err) {
      console.log(`Proxy ${proxyIpPort} failed:`, err.message);
      if (browser) await browser.close();
    }
  }

  if (!success) {
    console.error("All proxies failed to load the page.");
  }
})();
