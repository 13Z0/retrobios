// axe-core (WCAG 2.2 A/AA rules) in light and dark, one line per page and scheme.
//   node axe.mjs <base url> <page>...
import { chromium } from 'playwright-core';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const axeSource = await readFile(require.resolve('axe-core/axe.min.js'), 'utf8');
const [base, ...pages] = process.argv.slice(2);
const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome' });
for (const scheme of ['light', 'dark']) {
	const context = await browser.newContext({ viewport: { width: 390, height: 844 }, colorScheme: scheme });
	const page = await context.newPage();
	for (const path of pages) {
		await page.goto(new URL(path, base).href, { waitUntil: 'networkidle' });
		await page.addScriptTag({ content: axeSource });
		const result = await page.evaluate(() =>
			window.axe.run(document, { runOnly: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'] }));
		const violations = result.violations.map((v) => `${v.id}(${v.nodes.length})`);
		console.log(JSON.stringify({ page: path || '/', scheme, violations }));
	}
	await context.close();
}
await browser.close();
