// The RetroBIOS site. Pages, navigation and data files are written by
// scripts/generate_site.py; this folder holds the engine's configuration,
// the components and the styles.
import { readFileSync } from 'node:fs';
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
import stackTables from './src/plugins/rehype-stack-tables.mjs';
import shortCode from './src/plugins/rehype-short-code.mjs';
import unbrokenTokens from './src/plugins/ec-unbroken-tokens.mjs';

const SITE = 'https://abdess.github.io';
const BASE = '/retrobios/';

// Old addresses that lead on to a page: {"/from/": "/to/"}, site paths
// written by the generator. A checkout that has not run it builds with none.
// Astro puts the base before the old address but not before the new one.
const readRedirects = () => {
	let redirects;
	try {
		redirects = JSON.parse(readFileSync(new URL('./src/generated/redirects.json', import.meta.url), 'utf-8'));
	} catch (error) {
		if (error.code === 'ENOENT') return {};
		throw error;
	}
	const withBase = (to) => (/^https?:/.test(to) || to.startsWith(BASE) ? to : BASE + to.replace(/^\//, ''));
	const withoutBase = (from) => (from.startsWith(BASE) ? `/${from.slice(BASE.length)}` : from);
	return Object.fromEntries(Object.entries(redirects).map(([from, to]) => [withoutBase(from), withBase(to)]));
};

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
	redirects: readRedirects(),
	markdown: {
		processor: satteri({
			hastPlugins: [stackTables(), shortCode()],
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
				Header: './src/components/Header.astro',
				Search: './src/components/Search.astro',
			},
			routeMiddleware: ['./src/routeData.ts'],
			tableOfContents: { minHeadingLevel: 2, maxHeadingLevel: 3 },
			// Commands wrap instead of scrolling, so the end of a one-liner stays
			// visible, and never inside a word that holds a hyphen (`--platform`,
			// `nintendo-64dd`: src/plugins/ec-unbroken-tokens.mjs). A command block
			// is titled (Terminal, PowerShell) and drawn without the three window
			// dots. Starlight writes the dots into each theme after the global
			// style overrides, so they are set here.
			expressiveCode: {
				defaultProps: { wrap: true },
				plugins: [unbrokenTokens()],
				customizeTheme(theme) {
					theme.styleOverrides.frames = { ...theme.styleOverrides.frames, terminalTitlebarDotsOpacity: '0' };
					return theme;
				},
			},
			lastUpdated: false,
			pagination: true,
			// Measured on the queries of tools/search_gate.py, at the centre of a
			// range where all of them answer. How often a word appears counts more
			// than in Starlight's defaults (0.1, 0.1, 2, 9), a page's title weighs
			// ten times its text, so "dolphin" finds Dolphin, and the file names of
			// a system page (its `files` field, written by sitestarlight.py) count
			// half, so "scph5501" finds the Sony page without a file name winning
			// every other query. Two fields name the page's subject in other words
			// and weigh half a title: `aliases`, the ids setups file a system under
			// (psx, dreamcast, gba), and `devices`, the handhelds and boxes a
			// setup's own site names (R36S, Miyoo Mini). Two more weigh as much as
			// a title, since each names what a reader types to reach the page:
			// `consoles`, the consoles a manufacturer page holds (Neo Geo,
			// WonderSwan Color), and `terms`, the words of a task or a message on
			// screen (bios not found, mame version, check dump). All of them are
			// attributes on an empty element, so no text of theirs reaches an
			// excerpt.
			pagefind: {
				ranking: {
					pageLength: 0.1,
					termFrequency: 0.5,
					termSaturation: 1.4,
					termSimilarity: 9,
					metaWeights: { title: 10, files: 0.5, aliases: 5, devices: 5, consoles: 10, terms: 10 },
				},
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
