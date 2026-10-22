// Raw Pagefind results with their scores, to see why a page ranks where it does.
//   node search_scores.mjs <base url> <query> [ranking json]
import { chromium } from 'playwright-core';

const [base, query, ranking] = process.argv.slice(2);
const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome' });
const page = await browser.newPage();
await page.goto(base, { waitUntil: 'networkidle' });
const rows = await page.evaluate(async ({ root, query, ranking, top }) => {
	const pagefind = await import(`${root}pagefind/pagefind.js`);
	if (ranking) await pagefind.options({ ranking: JSON.parse(ranking) });
	const search = await pagefind.search(query);
	const shown = search.results.slice(0, top);
	const data = await Promise.all(shown.map((result) => result.data()));
	return shown.map((result, index) => ({
		score: result.score.toFixed(3),
		url: data[index].url,
		words: data[index].word_count,
		hits: result.words.length,
	}));
}, { root: base, query, ranking, top: Number(process.env.TOP || 8) });
for (const row of rows) console.log(row.score, row.words, row.hits, row.url);
await browser.close();
