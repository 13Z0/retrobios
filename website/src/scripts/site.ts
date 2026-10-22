// Progressive enhancement of the generated pages: table captions, filters
// and phone layout, the hash lookup on files/, moved cards, a hash typed in
// the search, glossary terms on a tap, and the 404 page's hints.

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

const headingText = (element: Element): string => {
	let cursor: Element | null = element;
	while (cursor) {
		cursor = cursor.previousElementSibling;
		if (!cursor) break;
		const heading = /^H[1-6]$/.test(cursor.tagName) ? cursor : cursor.querySelector('h1, h2, h3, h4, h5, h6');
		if (heading && cursor.classList.contains('sl-heading-wrapper')) return heading.textContent!.trim();
		if (/^H[1-6]$/.test(cursor.tagName)) return cursor.textContent!.trim();
	}
	return document.querySelector('h1')?.textContent?.trim() || 'Data table';
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
	const heading = table.dataset.rbHeading ?? 'Data table';
	if (overflows) {
		table.tabIndex = 0;
		table.setAttribute('role', 'region');
		table.setAttribute('aria-label', `${heading}, scrollable table`);
	} else if (table.getAttribute('role') === 'region') {
		table.removeAttribute('tabindex');
		table.removeAttribute('role');
		table.removeAttribute('aria-label');
	}
};

// A table whose rows end in an action (a button, a pack to download) is laid
// out one block per row on a phone, each cell under its column name, so the
// action never sits behind a horizontal scroll.
const stackActionTable = (table: HTMLTableElement) => {
	if (!table.querySelector('tbody :is(.rb-button, a[href*="/releases/download/"], a[href$="/releases/latest"])')) return;
	const labels = [...table.querySelectorAll('thead th')].map((cell) => cell.textContent!.trim());
	if (!labels.length) return;
	table.dataset.rbStack = 'true';
	for (const row of table.querySelectorAll('tbody tr')) {
		[...row.children].forEach((cell, column) => {
			if (labels[column] && !cell.hasAttribute('data-label')) cell.setAttribute('data-label', labels[column]);
		});
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
		const heading = headingText(table);
		table.dataset.rbHeading = heading;
		if (!table.querySelector('caption') && !table.closest('.rb-meta-card')) {
			const caption = document.createElement('caption');
			caption.className = 'rb-visually-hidden';
			caption.textContent = heading;
			table.prepend(caption);
		}
		stackActionTable(table);
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
	tables.forEach((table) => observer.observe(table));
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

const renderMatches = (status: HTMLElement, value: string, items: Item[]) => {
	status.textContent = '';
	const heading = document.createElement('p');
	heading.textContent = `${items.length} ${items.length === 1 ? 'match' : 'matches'} for ${HASH_LENGTHS[value.length]} ${value}:`;
	status.append(heading);
	for (const [layer, label] of Object.entries(LAYER_LABELS)) {
		const found = items.filter((item) => item.layer === layer);
		if (!found.length) continue;
		const title = document.createElement('p');
		const strong = document.createElement('strong');
		strong.textContent = label;
		title.append(strong);
		const list = document.createElement('ul');
		for (const item of found) {
			const row = document.createElement('li');
			const link = document.createElement('a');
			link.href = new URL(item.url, siteRoot()).href;
			const code = document.createElement('code');
			code.textContent = item.name || item.value;
			link.append(code);
			row.append(link);
			if (item.owner) row.append(` (${item.owner})`);
			list.append(row);
		}
		status.append(title, list);
	}
};

// files/?sha1=<sha1> is a file's content address and opens its card
// wherever it is now; files/?hash= also takes an MD5, CRC32 or SHA256 and
// lists every page that names the value.
const resolveFileAddress = () => {
	const status = document.getElementById('rb-file-lookup-status');
	if (!status) return;
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
				renderMatches(status, prefix ? matches[0].value : value, prefix ? cards : matches);
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

// The site search holds no hashes: Enter on one opens the lookup.
const hashSearch = () => {
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
