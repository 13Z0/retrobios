// What each page knows of its place in the site, computed once per page:
// - breadcrumbs: the tab and the groups above the page;
// - previous and next: the reading order runs on from one tab into the next,
//   so the last Start here page leads to Your setup;
// - a pruned sidebar: a group that does not hold the page becomes one link
//   to its first page, so an emulator page does not carry the 600 links of
//   every other emulator (the equivalent of Material's navigation.prune).
import { defineRouteMiddleware, type StarlightRouteData } from '@astrojs/starlight/route-data';
import sidebar from './generated/sidebar.json' with { type: 'json' };

type Entry = StarlightRouteData['sidebar'][number];
type Link = Extract<Entry, { type: 'link' }>;
type Item = { label: string; link?: string; items?: Item[] };
export type Crumb = { label: string; href?: string };

const BASE = import.meta.env.BASE_URL.replace(/\/?$/, '/');
const withBase = (link: string) => (link.startsWith(BASE) ? link : BASE + link.replace(/^\//, ''));

const flatten = (items: Item[]): { label: string; link: string }[] =>
	items.flatMap((item) => (item.items ? flatten(item.items) : item.link && !item.link.startsWith('http') ? [{ label: item.label, link: item.link }] : []));

// Every listed page in reading order, tab after tab.
const READING_ORDER = sidebar.topics.flatMap((topic) =>
	flatten(topic.items as Item[]).map((entry) => ({ ...entry, label: entry.label === 'Overview' ? topic.label : entry.label })),
);

const holdsCurrent = (entry: Entry): boolean =>
	entry.type === 'link' ? entry.isCurrent : entry.entries.some(holdsCurrent);

const firstLink = (entry: Entry): string | undefined =>
	entry.type === 'link' ? entry.href : entry.entries.map(firstLink).find(Boolean);

const prune = (entries: Entry[]): Entry[] =>
	entries.map((entry) => {
		if (entry.type === 'link') return entry;
		if (holdsCurrent(entry)) return { ...entry, collapsed: false, entries: prune(entry.entries) };
		const href = firstLink(entry);
		return href ? { type: 'link', label: entry.label, href, isCurrent: false, badge: entry.badge, attrs: {} } : entry;
	});

// The groups above the current page, outermost first.
const trail = (entries: Entry[], path: string): Crumb[] | undefined => {
	for (const entry of entries) {
		if (entry.type === 'link') {
			if (entry.isCurrent) return [];
			continue;
		}
		const below = trail(entry.entries, path);
		if (below) return [{ label: entry.label, href: firstLink(entry) }, ...below];
	}
	return undefined;
};

// A page the navigation does not list (part of a split page) sits under the
// group holding the pages of its folder.
const folderTrail = (entries: Entry[], folder: string): Crumb[] => {
	for (const entry of entries) {
		if (entry.type !== 'group') continue;
		const links = entry.entries.map(firstLink).filter(Boolean) as string[];
		if (links.some((href) => href.startsWith(folder))) return [{ label: entry.label, href: links[0] }];
	}
	return [];
};

const asLink = (entry: { label: string; link: string }): Link => ({
	type: 'link',
	label: entry.label,
	href: withBase(entry.link),
	isCurrent: false,
	badge: undefined,
	attrs: {},
});

export const onRequest = defineRouteMiddleware(async (context, next) => {
	await next();
	const route = context.locals.starlightRoute;
	const path = context.url.pathname;

	const topic = context.locals.starlightSidebarTopics?.topics.find((t) => t.isCurrent);
	const crumbs: Crumb[] = [];
	if (topic && withBase(topic.link) !== path) {
		crumbs.push({ label: topic.label, href: withBase(topic.link) });
		const segments = path.slice(BASE.length).split('/').filter(Boolean);
		const groups = trail(route.sidebar, path) ?? (segments.length > 1 ? folderTrail(route.sidebar, `${BASE}${segments[0]}/`) : []);
		crumbs.push(...groups.filter((crumb) => crumb.href !== path));
	}
	context.locals.rbBreadcrumbs = crumbs;

	const position = READING_ORDER.findIndex((entry) => withBase(entry.link) === path);
	if (position !== -1) {
		if (!route.pagination.prev && position > 0) route.pagination.prev = asLink(READING_ORDER[position - 1]);
		if (!route.pagination.next && position < READING_ORDER.length - 1) route.pagination.next = asLink(READING_ORDER[position + 1]);
	}

	route.sidebar = prune(route.sidebar);
});
