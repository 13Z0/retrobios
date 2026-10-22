// End-to-end check of the two interactive pages, in a real browser.
//   node check_tools.mjs <base url>
import { chromium } from 'playwright-core';
import assert from 'node:assert/strict';

const base = process.argv[2] ?? 'http://127.0.0.1:4321/retrobios/';
const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome' });
const page = await browser.newPage();
const errors = [];
page.on('pageerror', (error) => errors.push(error.message));

// Hash lookup: an MD5 lists every layer, a SHA1 permalink opens the card.
await page.goto(`${base}files/?hash=490f666e1afb15b7362b406ed1cea246`, { waitUntil: 'networkidle' });
const lookup = await page.textContent('#rb-file-lookup-status');
assert.match(lookup, /match/);
console.log('lookup md5:', lookup.slice(0, 160).replace(/\s+/g, ' '));
await page.goto(`${base}files/?sha1=0555c6fae8906f3f09baf5988f00e55f88e9f30b`, { waitUntil: 'networkidle' });
assert.ok(page.url().includes('systems/sony/#sha1-0555c6fae890'), page.url());
console.log('permalink sha1 ->', page.url());

// Picker on Get your files: Batocera, PlayStation only.
const transfers = [];
page.on('response', async (response) => {
	if (response.url().includes('/data/picker/')) transfers.push([response.url(), (await response.body()).length]);
});
await page.goto(`${base}which-pack/?platform=batocera&system=sony-playstation`, { waitUntil: 'networkidle' });
await page.waitForSelector('.rb-pick-output:not([hidden])');
const result = await page.textContent('.rb-pick-result');
const command = await page.textContent('.rb-pick-cmd code');
const rows = await page.locator('.rb-pick-output tbody tr').count();
console.log('picker:', result);
console.log('command:', command);
console.log('rows:', rows, 'fetched:', transfers.map(([url, bytes]) => `${url.split('/').pop()} ${bytes} B`).join(', '));
assert.match(command, /--platform batocera --system sony-playstation$/);
assert.ok(rows > 0);

// Unchecking everything empties the result; "All" restores the full command.
await page.click('[data-none]');
assert.match(await page.textContent('.rb-pick-result'), /Choose at least one system/);
await page.click('[data-all]');
assert.match(await page.textContent('.rb-pick-cmd code'), /--platform batocera$/);
assert.deepEqual(errors, []);
console.log('ok');
await browser.close();
