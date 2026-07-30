const { chromium } = require('playwright');
const TelegramBot = require('node-telegram-bot-api');

const token = process.env.TELEGRAM_TOKEN;
const bot = new TelegramBot(token, { polling: true });

const targetDomains = ['stake.us', 'stake.com', 'shuffle.us', 'shuffle.com', 'gamba.com', 'thrill.com', 'duel.com'];

bot.on('message', async (msg) => {
  const text = msg.text || '';
  const isMatch = targetDomains.some(domain => text.includes(domain) && (text.includes('modal=') || text.includes('bet') || text.includes('ref=')));

  if (!isMatch) return;

  const urlMatch = text.match(/https?:\/\/[^\s]+/);
  if (!urlMatch) return;

  const url = urlMatch[0];
  const chatId = msg.chat.id;

  const browser = await chromium.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox']
  });

  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 }
  });

  const page = await context.newPage();

  try {
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForTimeout(5000);
    await page.screenshot({ path: 'win.png', fullPage: true });
    
    await bot.sendPhoto(chatId, 'win.png', { reply_to_message_id: msg.message_id });
  } catch (err) {
    console.error("Error processing link:", err);
  } finally {
    await browser.close();
  }
});
