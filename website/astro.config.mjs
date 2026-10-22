// The RetroBIOS site. Pages, navigation and data files are written by
// scripts/generate_site.py; this folder holds the engine's configuration,
// the components and the styles.
import { copyFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import sitemap from '@astrojs/sitemap';
import { satteri } from '@astrojs/markdown-satteri';
import starlightSidebarTopics from 'starlight-sidebar-topics';
import starlightLinksValidator from 'starlight-links-validator';
import sidebar from './src/generated/sidebar.json' with { type: 'json' };
import lastmod from './src/generated/lastmod.json' with { type: 'json' };

const SITE = 'https://abdess.github.io';
const BASE = '/retrobios/';

// The sitemap keeps the address MkDocs gave it: sitemap.xml, beside the index.
const sitemapAlias = {
	name: 'retrobios-sitemap-alias',
	hooks: {
		'astro:build:done': async ({ dir }) => {
			const root = fileURLToPath(dir);
			await copyFile(`${root}sitemap-index.xml`, `${root}sitemap.xml`);
		},
	},
};

export default defineConfig({
	site: SITE,
	base: BASE,
	trailingSlash: 'always',
	markdown: {
		processor: satteri({
			features: {
				headingAttributes: true,
				definitionList: true,
				// Straight quotes: curly ones would change a copied command.
				smartPunctuation: false,
			},
		}),
	},
	integrations: [
		starlight({
			title: 'RetroBIOS',
			titleDelimiter: '-',
			description:
				'Source-verified BIOS and firmware packs for RetroArch, Batocera, Recalbox, Lakka, RetroPie, EmuDeck, RetroBat, RetroDECK, RomM, BizHawk, ROCKNIX, KNULLI, AmberELEC, EmuELEC and MiSTer FPGA.',
			// The brand mark is white on the indigo of the old header: indigo on a light page.
			logo: { light: './src/assets/logo-light.svg', dark: './src/assets/logo-dark.svg', replacesTitle: false },
			favicon: '/assets/brand/favicon-32.png',
			head: [
				{ tag: 'link', attrs: { rel: 'icon', type: 'image/svg+xml', href: `${BASE}assets/brand/favicon.svg` } },
				{ tag: 'link', attrs: { rel: 'apple-touch-icon', sizes: '180x180', href: `${BASE}assets/brand/apple-touch-icon.png` } },
			],
			social: [{ icon: 'github', label: 'RetroBIOS on GitHub', href: 'https://github.com/Abdess/retrobios' }],
			customCss: ['./src/styles/retrobios.css'],
			components: {
				PageTitle: './src/components/PageTitle.astro',
				MarkdownContent: './src/components/MarkdownContent.astro',
				Footer: './src/components/Footer.astro',
			},
			routeMiddleware: ['./src/routeData.ts'],
			tableOfContents: { minHeadingLevel: 2, maxHeadingLevel: 3 },
			// Commands wrap instead of scrolling, so the end of a one-liner stays visible.
			expressiveCode: { defaultProps: { wrap: true } },
			lastUpdated: false,
			pagination: true,
			// Measured on the ten queries of tools/search_gate.py, inside a range
			// where all ten answer: Starlight's defaults (0.1, 0.1, 2, 9) let a guide
			// that cites scph5501 once outrank the file's own card. A long page
			// is penalised a little more, and how often a word appears counts.
			pagefind: {
				ranking: { pageLength: 0.3, termFrequency: 0.5, termSaturation: 1.4, termSimilarity: 9 },
			},
			plugins: [
				starlightSidebarTopics(sidebar.topics, sidebar.options),
				starlightLinksValidator(),
			],
		}),
		// A page that states its own date (an emulator page: the day its
		// profile was last read against the source) gives it to the sitemap.
		// The others get no lastmod rather than the build date.
		sitemap({
			serialize(item) {
				const date = lastmod[new URL(item.url).pathname];
				return date ? { ...item, lastmod: date } : item;
			},
		}),
		sitemapAlias,
	],
});
