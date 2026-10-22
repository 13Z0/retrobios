// Screenshot of one element of a page, to look at a component closely.
//   node shot_element.mjs <url> <css selector> <out.png> [width] [light|dark]
import { chromium } from 'playwright-core';

const [url, selector, out, width = '390', scheme = 'light'] = process.argv.slice(2);
const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome' });
const page = await browser.newPage({ viewport: { width: Number(width), height: 900 }, colorScheme: scheme });
await page.goto(url, { waitUntil: 'networkidle' });
const element = page.locator(selector).first();
await element.scrollIntoViewIfNeeded();
await page.waitForTimeout(300);
await element.screenshot({ path: out });
await browser.close();
