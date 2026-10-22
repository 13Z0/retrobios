// Screenshots at 390 and 1440 px, light and dark.
//   node shots.mjs <base url> <out dir> <page>...
import { chromium } from 'playwright-core';
import { mkdir } from 'node:fs/promises';

const [base, out, ...pages] = process.argv.slice(2);
await mkdir(out, { recursive: true });
const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome' });
for (const scheme of ['light', 'dark']) {
	for (const width of [390, 1440]) {
		const context = await browser.newContext({
			viewport: { width, height: width === 390 ? 844 : 900 },
			deviceScaleFactor: 1,
			colorScheme: scheme,
		});
		const page = await context.newPage();
		for (const path of pages) {
			await page.goto(new URL(path, base).href, { waitUntil: 'networkidle' });
			const name = path.replace(/^\/|\/$/g, '').replace(/[/?=&]+/g, '_') || 'home';
			await page.screenshot({ path: `${out}/${name}-${width}-${scheme}.png`, fullPage: false });
		}
		await context.close();
	}
}
await browser.close();
