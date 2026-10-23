// Progressive enhancement of the generated pages: table captions, filters
// and keyboard scrolling, the hash lookup on files/, moved cards, a hash
// typed in the search, glossary terms on a tap, and the 404 page's hints.
// Which tables stack on a phone is decided when the page is built
// (src/plugins/rehype-stack-tables.mjs), so the layout is right on first paint.

const HASH_LENGTHS: Record<number, string> = { 8: 'CRC32', 32: 'MD5', 40: 'SHA1', 64: 'SHA256' };
const LAYER_LABELS: Record<string, string> = {
	collection: 'In the collection',
	'data-directory': 'In a data directory the packs ship',
	platform: 'Declared by a platform',
	emulator: 'Read by an emulator',
};
type Item = { algorithm: string; layer: string; name?: string; value: string; url: string; owner?: string };

const siteRoot = () => new URL(import.meta.env.BASE_URL.replace(/\/?$/, '/'), location.origin);
const normalizeHash = (raw: string) => raw.trim().toLowerCase().replace(/^0x/, '').replace(/\s+/g, '');
const isHash = (value: string) => /^[0-9a-f]+$/.test(value) && value.length in HASH_LENGTHS;

// Tables

// The heading a table sits under: the nearest one before it, looking up
// through the blocks that hold it, else the page title.
const headingText = (element: Element): string => {
	for (let block: Element | null = element; block && !block.matches('.sl-markdown-content'); block = block.parentElement) {
		for (let cursor = block.previousElementSibling; cursor; cursor = cursor.previousElementSibling) {
			const heading = /^H[1-6]$/.test(cursor.tagName)
				? cursor
				: cursor.classList.contains('sl-heading-wrapper')
					? cursor.querySelector('h1, h2, h3, h4, h5, h6')
					: null;
			if (heading?.textContent?.trim()) return heading.textContent.trim();
		}
	}
	return document.querySelector('h1')?.textContent?.trim() || document.title;
};

// Each table's name, once per page: two tables under one heading are told
// apart by their order, so no two regions share a label (axe landmark-unique).
const tableNames = new WeakMap<HTMLTableElement, string>();
const tableName = (table: HTMLTableElement): string => {
	let name = tableNames.get(table);
	if (name) return name;
	const tables = [...document.querySelectorAll<HTMLTableElement>('.sl-markdown-content table')];
	const seen = new Map<string, number>();
	for (const each of tables) {
		const heading = headingText(each);
		const count = (seen.get(heading) ?? 0) + 1;
		seen.set(heading, count);
		tableNames.set(each, count === 1 ? heading : `${heading}, table ${count}`);
	}
	name = tableNames.get(table) ?? headingText(table);
	tableNames.set(table, name);
	return name;
};

// A table that overflows is a named stop in the tab order, so a keyboard
// can scroll it (WCAG 2.1.1). Checked again when a closed block opens. All
// widths are read first and the attributes written after, so a page of
// twenty tables is laid out once, not twenty times.
const markScrollable = (tables: HTMLTableElement[]) => {
	const overflowing = tables.map((table) => table.scrollWidth > table.clientWidth + 1);
	tables.forEach((table, index) => setScrollable(table, overflowing[index]));
};

const setScrollable = (table: HTMLTableElement, overflows: boolean) => {
	if (overflows) {
		table.tabIndex = 0;
		table.setAttribute('role', 'region');
		table.setAttribute('aria-label', `${tableName(table)}, scrollable table`);
	} else if (table.getAttribute('role') === 'region') {
		table.removeAttribute('tabindex');
		table.removeAttribute('role');
		table.removeAttribute('aria-label');
	}
};

const filterTable = (table: HTMLTableElement, index: number, heading: string) => {
	const rows = [...table.querySelectorAll<HTMLTableRowElement>('tbody tr')];
	if (rows.length < 20) return;
	const controls = document.createElement('div');
	controls.className = 'rb-table-filter';
	const id = `rb-table-filter-${index}`;
	const label = document.createElement('label');
	label.htmlFor = id;
	label.textContent = `Filter ${heading}`;
	const input = document.createElement('input');
	input.id = id;
	input.type = 'search';
	input.autocomplete = 'off';
	input.placeholder = 'Name, platform, system, hash...';
	const count = document.createElement('span');
	count.className = 'rb-table-filter-count';
	count.setAttribute('aria-live', 'polite');
	count.textContent = `${rows.length} rows`;
	controls.append(label, input, count);
	table.before(controls);
	// Each row's text is read on the first keystroke, not on every page load.
	let searchable: { row: HTMLTableRowElement; text: string }[] | null = null;
	input.addEventListener('input', () => {
		searchable ??= rows.map((row) => ({ row, text: row.textContent!.normalize('NFKD').toLocaleLowerCase() }));
		const query = input.value.trim().normalize('NFKD').toLocaleLowerCase();
		let visible = 0;
		for (const entry of searchable) {
			entry.row.hidden = !!query && !entry.text.includes(query);
			if (!entry.row.hidden) visible += 1;
		}
		count.textContent = `${visible} of ${rows.length} rows`;
	});
};

const enhanceTables = () => {
	const tables = [...document.querySelectorAll<HTMLTableElement>('.sl-markdown-content table')];
	const enhance = (table: HTMLTableElement) => {
		for (const cell of table.querySelectorAll('thead th')) if (!cell.hasAttribute('scope')) cell.setAttribute('scope', 'col');
		const heading = tableName(table);
		if (!table.querySelector('caption') && !table.closest('.rb-meta-card')) {
			const caption = document.createElement('caption');
			caption.className = 'rb-visually-hidden';
			caption.textContent = heading;
			table.prepend(caption);
		}
		filterTable(table, tables.indexOf(table), heading);
	};
	// A table is set up and measured when it nears the screen. Doing all of
	// them at load relaid out every row of a 3,800-row page after its first
	// paint. One that appears later (a block opened, a tab chosen) is handled
	// then.
	const near = new Set<HTMLTableElement>();
	const observer = new IntersectionObserver(
		(entries) => {
			const shown = entries.filter((entry) => entry.isIntersecting).map((entry) => entry.target as HTMLTableElement);
			for (const table of shown) {
				if (!near.has(table)) enhance(table);
				near.add(table);
			}
			if (shown.length) requestAnimationFrame(() => markScrollable(shown));
		},
		{ rootMargin: '400px 0px' },
	);
	// Every table is measured once the page is drawn and idle, before anything
	// is written, so the read finds the layout done instead of forcing it: a
	// keyboard reaches a scrolling table anywhere on the page, not only near
	// the screen.
	const idle = window.requestIdleCallback ?? ((callback: () => void) => setTimeout(callback, 1));
	const start = () =>
		idle(() => {
			// A table in a closed block is not laid out, and measuring it would
			// lay it out: it is measured when its block opens.
			markScrollable(tables.filter((table) => table.checkVisibility?.() ?? true));
			tables.forEach((table) => observer.observe(table));
		});
	if (document.readyState === 'complete') start();
	else addEventListener('load', start, { once: true });
	let timer = 0;
	addEventListener('resize', () => {
		clearTimeout(timer);
		timer = window.setTimeout(() => markScrollable([...near]), 150);
	});
};

// files/?hash= reads one shard of the lookup, named by the first two hex
// characters of the value.

const lookupShard = (value: string): Promise<Item[]> =>
	fetch(new URL(`api/v1/lookup/${value.slice(0, 2)}.json`, siteRoot()))
		.then((response) => (response.ok ? response.json() : { items: [] }))
		.then((shard) => shard.items || []);

type Held = { name: string; path: string; sha1: string; size: number };
type Declared = { name?: string; destination?: string; required?: boolean; [hash: string]: unknown };

const json = (path: string) =>
	fetch(new URL(path, siteRoot())).then((response) => (response.ok ? response.json() : Promise.reject(response.status)));

const formatSize = (bytes: number) =>
	bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(1)} GB`
	: bytes >= 1024 ** 2 ? `${(bytes / 1024 ** 2).toFixed(1)} MB`
	: bytes >= 1024 ? `${(bytes / 1024).toFixed(1)} KB`
	: `${bytes} B`;

const code = (text: string) => {
	const element = document.createElement('code');
	element.textContent = text;
	return element;
};

const link = (url: string, ...content: (Node | string)[]) => {
	const element = document.createElement('a');
	element.href = new URL(url, siteRoot()).href;
	element.append(...content);
	return element;
};

const paragraph = (...content: (Node | string)[]) => {
	const element = document.createElement('p');
	element.append(...content);
	return element;
};

const strong = (text: string) => {
	const element = document.createElement('strong');
	element.textContent = text;
	return element;
};

// The card a collection match points at, read from its manufacturer's file
// of api/v1/systems/: the path in the collection and the size.
const heldFile = async (item: Item & { sha1?: string }): Promise<Held | undefined> => {
	const slug = /^systems\/([^/]+)\//.exec(item.url)?.[1];
	if (!slug || !item.sha1) return undefined;
	const data = await json(`api/v1/systems/${slug}.json`);
	for (const files of Object.values<Held[]>(data.items?.[0]?.consoles ?? {})) {
		const found = files.find((file) => file.sha1 === item.sha1);
		if (found) return found;
	}
	return undefined;
};

// A setup's own entry for the value, from its contract in api/v1/platforms/:
// where the file goes and whether the setup requires it. Recalbox writes
// several MD5 in one field, separated by commas.
const declaredEntry = async (item: Item, value: string): Promise<Declared | undefined> => {
	const system = decodeURIComponent(new URL(item.url, siteRoot()).hash.slice(1));
	const data = await json(`api/v1/platforms/${item.owner}.json`);
	const files: Declared[] = data.items?.[0]?.contract?.systems?.[system]?.files ?? [];
	const holds = (entry: Declared) =>
		String(entry[item.algorithm] ?? '').toLowerCase().split(',').map((hash) => hash.trim()).includes(value);
	return files.find((entry) => holds(entry) && entry.name === item.name) ?? files.find(holds);
};

const setupTable = (rows: { item: Item; entry?: Declared; setup: string }[]) => {
	const table = document.createElement('table');
	const head = table.createTHead().insertRow();
	for (const label of ['Setup', 'Name as declared', 'Destination', 'Hash matched', 'Required']) {
		const cell = document.createElement('th');
		cell.scope = 'col';
		cell.textContent = label;
		head.append(cell);
	}
	const body = table.createTBody();
	for (const { item, entry, setup } of rows) {
		const row = body.insertRow();
		const name = entry?.name ?? item.name ?? '';
		// EmuDeck lists a hash with no file name: any file that has it passes.
		const named = name && !name.includes(':');
		const destination = entry?.destination;
		const cells: (Node | string)[] = [
			link(item.url, setup),
			named ? code(name) : 'Any name',
			destination ? code(destination) : '-',
			item.algorithm.toUpperCase(),
			entry?.required === undefined ? '-' : entry.required ? 'Yes' : 'No',
		];
		cells.forEach((content, column) => {
			const cell = row.insertCell();
			cell.dataset.label = head.cells[column].textContent!;
			cell.append(content);
		});
	}
	table.dataset.rbStack = 'true';
	return table;
};

// The answer first: which file this is and where the collection holds it,
// then the setups that list it (where it goes, whether it is required),
// then the emulators that read it.
const renderMatches = async (status: HTMLElement, result: HTMLElement, value: string, items: Item[]) => {
	const byLayer = (layer: string) => items.filter((item) => item.layer === layer);
	const held = byLayer('collection');
	const platforms = byLayer('platform');
	const [files, entries, names] = await Promise.all([
		Promise.all(held.map((item) => heldFile(item).catch(() => undefined))),
		Promise.all(platforms.map((item) => declaredEntry(item, value).catch(() => undefined))),
		json('data/picker/index.json').catch(() => []) as Promise<{ platform: string; name: string }[]>,
	]);
	const setupName = new Map(names.map((entry) => [entry.platform, entry.name]));
	status.textContent = `${items.length.toLocaleString('en')} ${items.length === 1 ? 'match' : 'matches'} for ${HASH_LENGTHS[value.length]} ${value}.`;
	const blocks: Node[] = [];
	held.forEach((item, index) => {
		const file = files[index];
		const name = file?.name ?? item.name ?? value;
		blocks.push(
			file
				? paragraph('This is ', code(name), ', held in the collection at ', code(file.path), '.')
				: paragraph('This is ', code(name), ', held in the collection.'),
			paragraph(
				...(file ? ['SHA1 ', code(file.sha1), `, ${formatSize(file.size)}. `] : []),
				link(item.url, 'Open its card'),
				'.',
			),
		);
	});
	if (!held.length) blocks.push(paragraph('No file in the collection has this hash.'));
	const shipped = byLayer('data-directory');
	if (shipped.length) {
		const list = document.createElement('ul');
		for (const item of shipped) {
			const row = document.createElement('li');
			row.append(link(item.url, code(item.name || item.value)));
			list.append(row);
		}
		blocks.push(paragraph(strong(LAYER_LABELS['data-directory'])), list);
	}
	if (platforms.length) {
		const owners = new Set(platforms.map((item) => item.owner));
		blocks.push(
			paragraph(strong(`Setups that list it (${owners.size})`)),
			setupTable(platforms.map((item, index) => ({
				item,
				entry: entries[index],
				setup: setupName.get(item.owner ?? '') ?? item.owner ?? '',
			}))),
		);
	}
	const readers = byLayer('emulator');
	if (readers.length) {
		const list = document.createElement('ul');
		for (const item of readers) {
			const row = document.createElement('li');
			row.append(link(item.url, item.owner ?? item.name ?? ''));
			if (item.name && item.name !== held[0]?.name) row.append(' as ', code(item.name));
			list.append(row);
		}
		blocks.push(paragraph(strong(`${LAYER_LABELS.emulator} (${readers.length})`)), list);
	}
	result.replaceChildren(...blocks);
};

// files/?sha1=<sha1> is a file's content address and opens its card
// wherever it is now; files/?hash= also takes an MD5, CRC32 or SHA256 and
// lists every page that names the value.
const resolveFileAddress = () => {
	const status = document.getElementById('rb-file-lookup-status');
	if (!status) return;
	const result = document.createElement('div');
	result.id = 'rb-file-lookup-result';
	status.after(result);
	const params = new URLSearchParams(location.search);
	const permalink = params.has('sha1');
	const value = normalizeHash(params.get('sha1') || params.get('hash') || '');
	if (!value) return;
	const input = document.getElementById('rb-hash') as HTMLInputElement | null;
	if (input) input.value = value;
	const prefix = permalink && /^[0-9a-f]{12}$/.test(value);
	if (!prefix && !isHash(value)) {
		status.textContent = 'A hash is 8 (CRC32), 32 (MD5), 40 (SHA1) or 64 (SHA256) hexadecimal characters.';
		return;
	}
	status.textContent = 'Looking it up...';
	lookupShard(value)
		.then((items) => {
			const matches = items.filter((item) =>
				prefix ? item.algorithm === 'sha1' && item.value.startsWith(value) : item.value === value);
			const cards = matches.filter((item) => item.layer === 'collection' || item.layer === 'data-directory');
			if (permalink && new Set(cards.map((item) => item.url)).size === 1) {
				location.replace(new URL(cards[0].url, siteRoot()));
			} else if (matches.length) {
				return renderMatches(status, result, prefix ? matches[0].value : value, prefix ? cards : matches);
			} else {
				status.textContent = 'No file the site knows has this hash.';
			}
		})
		.catch(() => {
			status.textContent = 'The file index could not be read.';
		});
};

// A file card moved to another page keeps its address: an id of the form
// sha1-<12 hex> that this page does not hold is looked up by content, and
// an emulator card listed in the table of a split page opens its page.
const followMovedCard = () => {
	const id = decodeURIComponent(location.hash.slice(1));
	if (!id) return;
	const target = document.getElementById(id);
	const link = target?.closest('.rb-card-index tr')?.querySelector<HTMLAnchorElement>('a[href]');
	if (link) {
		location.replace(link.href);
		return;
	}
	const match = /^sha1-([0-9a-f]{12})$/.exec(id);
	if (match && !target) location.replace(new URL(`files/?sha1=${match[1]}`, siteRoot()));
};

// The site search holds no hashes. A hash typed in it gets "Look up this
// hash" as its first result, and Enter opens the lookup.
const hashLookupRow = (drawer: Element, value: string) => {
	let row = drawer.querySelector<HTMLElement>(':scope > .rb-search-hash');
	if (!isHash(value)) {
		row?.remove();
		return;
	}
	if (!row) {
		row = document.createElement('p');
		row.className = 'rb-search-hash';
		drawer.prepend(row);
	}
	const target = new URL(`files/?hash=${value}`, siteRoot()).href;
	if (row.querySelector('a')?.href === target) return;
	const anchor = document.createElement('a');
	anchor.href = target;
	anchor.append('Look up this hash');
	row.replaceChildren(anchor, ` ${HASH_LENGTHS[value.length]} `, code(value));
};

// Pagefind lists a page's title twice: once for the page and once for the
// anchor of its h1 (#_top). The second title is hidden, its excerpt kept.
// The `files` field of a system page weighs its file names in the ranking
// and is never shown: a result is not a list of 120 names. Pagefind reuses
// its nodes from one query to the next, so both are worked out again on
// every change.
const tidyResults = (root: Element) => {
	for (const title of root.querySelectorAll<HTMLElement>('.pagefind-ui__result-nested > .pagefind-ui__result-title')) {
		title.hidden = !!title.querySelector('a')?.getAttribute('href')?.endsWith('#_top');
	}
	for (const tags of root.querySelectorAll<HTMLElement>('.pagefind-ui__result-tags')) {
		let shown = 0;
		for (const tag of tags.querySelectorAll<HTMLElement>('.pagefind-ui__result-tag')) {
			tag.hidden = tag.dataset.pagefindUiMeta === 'files';
			if (!tag.hidden) shown += 1;
		}
		tags.hidden = shown === 0;
	}
};

const hashSearch = () => {
	const root = document.getElementById('starlight__search');
	if (root) {
		const refresh = () => {
			tidyResults(root);
			const input = root.querySelector<HTMLInputElement>('input');
			const drawer = root.querySelector('.pagefind-ui__drawer');
			if (input && drawer) hashLookupRow(drawer, normalizeHash(input.value));
		};
		new MutationObserver(refresh).observe(root, {
			childList: true,
			subtree: true,
			characterData: true,
			attributes: true,
			attributeFilter: ['href', 'data-pagefind-ui-meta'],
		});
		root.addEventListener('input', refresh);
	}
	document.addEventListener(
		'keydown',
		(event) => {
			if (event.key !== 'Enter') return;
			const input = event.target as HTMLInputElement;
			if (!input.matches?.('site-search dialog input')) return;
			const value = normalizeHash(input.value);
			if (!isHash(value)) return;
			event.preventDefault();
			event.stopPropagation();
			location.assign(new URL(`files/?hash=${value}`, siteRoot()));
		},
		true,
	);
};

// A glossary term shows its definition on hover; a tap or Enter shows it
// on a phone or to a keyboard, and Escape or a tap elsewhere closes it.
const glossaryTerms = () => {
	const terms = document.querySelectorAll<HTMLElement>('abbr.rb-term');
	if (!terms.length) return;
	const close = () => document.querySelectorAll('abbr.rb-term[data-open]').forEach((term) => term.removeAttribute('data-open'));
	for (const term of terms) {
		term.tabIndex = 0;
		term.setAttribute('role', 'button');
		term.setAttribute('aria-label', `${term.textContent}: ${term.title}`);
		term.dataset.definition = term.title;
		const toggle = (event: Event) => {
			event.stopPropagation();
			const open = term.hasAttribute('data-open');
			close();
			if (!open) term.setAttribute('data-open', '');
		};
		term.addEventListener('click', toggle);
		term.addEventListener('keydown', (event) => {
			if (event.key === 'Enter' || event.key === ' ') {
				event.preventDefault();
				toggle(event);
			} else if (event.key === 'Escape') close();
		});
	}
	document.addEventListener('click', close);
};

// The 404 page: a search button, and the list a moved page belongs to.
const notFound = () => {
	document.querySelector('[data-rb-open-search]')?.addEventListener('click', () => {
		document.querySelector<HTMLButtonElement>('site-search button[data-open-modal]')?.click();
	});
	const hint = document.getElementById('rb-notfound-hint');
	const match = location.pathname.match(/\/(emulators|systems|platforms)\/([^/]+)\/?$/);
	if (!hint || !match) return;
	const link = document.createElement('a');
	link.href = new URL(`${match[1]}/`, siteRoot()).href;
	link.textContent = `the list of ${match[1]}`;
	hint.append(`No page is named ${decodeURIComponent(match[2])} here. It may be listed under another name in `, link, '.');
	hint.hidden = false;
};

resolveFileAddress();
followMovedCard();
enhanceTables();
hashSearch();
glossaryTerms();
notFound();
