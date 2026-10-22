// Page weight and search measurements in a real browser.
//   node measure.mjs weight <base url> <page>...
//   node measure.mjs search <base url> starlight|mkdocs <query>...
// Transfer sizes are bytes on the wire (gzip from serve.mjs), by type.
import { chromium } from 'playwright-core';

const [mode, base, ...rest] = process.argv.slice(2);
const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome' });

const track = (page) => {
	const rows = [];
	page.on('response', async (response) => {
		const request = response.request();
		const headers = await response.allHeaders();
		let bytes = Number(headers['content-length'] ?? 0);
		if (!bytes) {
			try { bytes = (await response.body()).length; } catch { bytes = 0; }
		}
		rows.push({ url: response.url(), type: request.resourceType(), bytes });
	});
	return rows;
};

const sum = (rows, test) => rows.filter(test).reduce((total, row) => total + row.bytes, 0);

if (mode === 'weight') {
	for (const path of rest) {
		const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
		const page = await context.newPage();
		const rows = track(page);
		await page.goto(new URL(path, base).href, { waitUntil: 'networkidle' });
		await page.waitForTimeout(500);
		const dom = await page.evaluate(() => document.getElementsByTagName('*').length);
		const kb = (n) => (n / 1024).toFixed(1);
		console.log(JSON.stringify({
			page: path,
			html_kb: kb(sum(rows, (r) => r.type === 'document')),
			js_kb: kb(sum(rows, (r) => r.type === 'script')),
			css_kb: kb(sum(rows, (r) => r.type === 'stylesheet')),
			other_kb: kb(sum(rows, (r) => !['document', 'script', 'stylesheet'].includes(r.type))),
			total_kb: kb(sum(rows, () => true)),
			requests: rows.length,
			dom_nodes: dom,
		}));
		await context.close();
	}
} else if (mode === 'search') {
	const [engine, ...queries] = rest;
	const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
	const page = await context.newPage();
	const rows = track(page);
	await page.goto(base, { waitUntil: 'networkidle' });
	await page.waitForTimeout(500);
	const before = rows.length;
	const first = [];
	for (const [n, query] of queries.entries()) {
		const mark = rows.length;
		let results;
		if (engine === 'starlight-ui') {
			// The reader's path: open the dialog, type, wait for the result list.
			if (n === 0) await page.click('button[data-open-modal]');
			const input = page.locator('dialog input[type="text"], dialog input[type="search"]').first();
			await input.fill(query);
			await page.waitForSelector('.pagefind-ui__result', { timeout: 15000 });
			await page.waitForTimeout(1200);
			results = await page.evaluate(() => {
				const items = [...document.querySelectorAll('.pagefind-ui__result')];
				return {
					count: document.querySelector('.pagefind-ui__message')?.textContent ?? '',
					top: items.slice(0, 5).map((r) => `${r.querySelector('.pagefind-ui__result-link')?.textContent?.trim()} | ${r.querySelector('.pagefind-ui__result-link')?.getAttribute('href')}`),
				};
			});
		} else if (engine === 'starlight') {
			results = await page.evaluate(async ({ query, root, ranking }) => {
				if (!window.__pf) {
					window.__pf = await import(`${root}pagefind/pagefind.js`);
					if (ranking) await window.__pf.options({ ranking });
				}
				const search = await window.__pf.search(query);
				const top = await Promise.all(search.results.slice(0, 5).map((r) => r.data()));
				return { count: search.results.length, top: top.map((d) => `${d.meta.title} | ${d.url}`) };
			}, { query, root: new URL('/retrobios/', base).href, ranking: process.env.RB_RANKING ? JSON.parse(process.env.RB_RANKING) : null });
		} else {
			// Material: open the search through its share link (?q=) and read the result list.
			await page.goto(`${base}?q=${encodeURIComponent(query)}`, { waitUntil: 'networkidle' });
			await page.waitForFunction(
				(q) => document.querySelector('input[data-md-component="search-query"]')?.value === q
					&& /matching|No matching/.test(document.querySelector('.md-search-result__meta')?.textContent ?? ''),
				query, { timeout: 30000 },
			);
			await page.waitForTimeout(800);
			results = await page.evaluate(() => {
				const items = [...document.querySelectorAll('.md-search-result__item > a')];
				const meta = document.querySelector('.md-search-result__meta')?.textContent ?? '';
				return { count: meta, top: items.slice(0, 5).map((a) => `${a.querySelector('h1,h2')?.textContent?.trim()} | ${a.getAttribute('href')}`) };
			});
		}
		await page.waitForTimeout(300);
		const transfer = sum(rows.slice(mark), () => true);
		if (n === 0) first.push(...rows.slice(before));
		console.log(JSON.stringify({ query, transfer_kb: (transfer / 1024).toFixed(1), ...results }));
	}
	console.log(JSON.stringify({ first_search_kb: (sum(first, () => true) / 1024).toFixed(1), first_search_requests: first.length }));
	await context.close();
}
await browser.close();
