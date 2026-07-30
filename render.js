const { chromium } = require('playwright');

(async () => {
  const url = process.argv[2];
  if (!url) {
    console.error("Please provide a URL");
    process.exit(1);
  }

  const isUsa = url.includes('stake.us') || url.includes('shuffle.us');
  const proxy = isUsa 
    ? undefined // Or configure a US proxy if needed
    : { server: 'http://your-norway-proxy-ip:port' }; // Optional/Adjust as needed for Norway routing

  const browser = await chromium.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox']
  });

  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 },
    // If routing location-specific requests:
    geolocation: isUsa ? { latitude: 37.7749, longitude: -122.4194 } : { latitude: 59.9139, longitude: 10.7522 },
    permissions: ['geolocation']
  });

  const page = await context.newPage();
  await page.goto(url, { waitUntil: 'networkidle', timeout: 60000 });
  await page.screenshot({ path: 'win.png', fullPage: true });
  await browser.close();
  console.log("Screenshot saved as win.png");
})();
