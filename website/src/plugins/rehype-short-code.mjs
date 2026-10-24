// Code that must read in one piece, marked when the page is built (the CSS
// is retrobios.css, code.rb-short and table.rb-options).
//
// - Inline code of SHORT_CODE characters or fewer gets `rb-short` and never
//   breaks: `--restore` split into `--` and `restore` on the home page at
//   1440 px, and `--list-` / `systems` in the installer's options table.
//   Longer inline code (a path, a full command) still wraps, and gets a
//   `<wbr>` after each `/` and `\` so a path breaks at a separator:
//   `~/.var/app/org.libretro.RetroArch/config/r` then `etroarch/system/` on a
//   390 px phone. The break adds no character to a copy. Each `wbr` is an
//   element of the page, and a page of file cards holds thousands of paths
//   (bluemsx: 3,097 breaks on 9,369 elements, against a budget of 10,000).
//   So the build writes the first BREAK_BUDGET of a page, those of its first
//   screens, and site.ts writes the others as their block nears the screen.
// - A table whose first column is "Option" gets `rb-options`, so its flags
//   keep their column.
//
// Satteri runs hast plugins (the rehype stage of its pipeline): this one is
// registered in astro.config.mjs through satteri({ hastPlugins }).

export const SHORT_CODE = 30;
const OPTION_HEADER = /^options?$/i;

const classes = (node) => {
	const value = node.properties?.className;
	if (Array.isArray(value)) return value.map(String);
	return value ? String(value).split(/\s+/) : [];
};

const addClass = (ctx, node, name) => {
	const present = classes(node);
	if (!present.includes(name)) ctx.setProperty(node, 'className', [...present, name]);
};

const firstHeader = (ctx, table) => {
	for (const part of table.children ?? []) {
		if (part.type !== 'element' || part.tagName !== 'thead') continue;
		for (const row of part.children ?? []) {
			if (row.type !== 'element' || row.tagName !== 'tr') continue;
			const cell = (row.children ?? []).find((child) => child.type === 'element' && child.tagName === 'th');
			return cell ? ctx.textContent(cell).trim() : '';
		}
	}
	return '';
};

// A code element is inline unless a `pre` holds it.
export const isShort = (text, inPre) => !inPre && text.length > 0 && text.length <= SHORT_CODE;

export const BREAK_BUDGET = 200;
const SEPARATOR = /[/\\]/;
const SEPARATORS = /([/\\])(?!$)/g;

// Inline code long enough to wrap, with a separator to wrap at.
export const breaksAtSeparators = (text, inPre) => !inPre && text.length > SHORT_CODE && SEPARATOR.test(text);

const escapeText = (text) => text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

// The text as HTML with a break after each separator but the last character.
export const withBreaks = (html) => html.replace(SEPARATORS, '$1<wbr>');

// How many breaks withBreaks() writes in the text.
export const breakCount = (text) => text.match(SEPARATORS)?.length ?? 0;

// Takes *count* breaks from the page's budget, when it still holds them.
const spend = (room, count) => {
	if (count > room.left) return false;
	room.left -= count;
	return true;
};

// The cards and tables the generator writes as HTML reach this step as raw
// text: their code elements are marked by rewriting the tag. A block that
// holds a `pre` is left as written.
const RAW_CODE = /<code(\s[^>]*)?>([^<]*)<\/code>/g;
const ENTITY = /&(?:#\d+|#x[0-9a-f]+|[a-z]+);/gi;

export const markRawCode = (html, room = { left: Infinity }) => {
	if (html.includes('<pre')) return html;
	return html.replace(RAW_CODE, (whole, attributes = '', text) => {
		const plain = text.replace(ENTITY, '_');
		if (breaksAtSeparators(plain, false)) {
			return spend(room, breakCount(text)) ? `<code${attributes}>${withBreaks(text)}</code>` : whole;
		}
		if (/\bclass=/.test(attributes) || !isShort(plain, false)) return whole;
		return `<code class="rb-short"${attributes}>${text}</code>`;
	});
};

// A code element whose children are all text gets them back split, a `wbr`
// element after each separator. One already split is left alone.
const breakAtSeparators = (ctx, code, room) => {
	const children = code.children ?? [];
	if (!children.length || children.some((child) => child.type !== 'text')) return;
	if (!spend(room, breakCount(children.map((child) => child.value).join('')))) return;
	// split() with a capturing group puts each separator at an odd index.
	const parts = children.map((child) => child.value).join('').split(SEPARATORS).flatMap((piece, index) => [
		...(piece ? [{ type: 'text', value: piece }] : []),
		...(index % 2 ? [{ type: 'element', tagName: 'wbr', properties: {}, children: [] }] : []),
	]);
	ctx.replaceNode(code, { type: 'element', tagName: 'code', properties: { ...(code.properties ?? {}) }, children: parts });
};

// The page's budget of breaks, kept with the page being built.
const breakRoom = (ctx) => {
	ctx.data.rbBreaks ??= { left: BREAK_BUDGET };
	return ctx.data.rbBreaks;
};

export default function shortCode() {
	return {
		name: 'rb-short-code',
		raw(node, ctx) {
			const room = breakRoom(ctx);
			// Inline HTML in Markdown (a hash in a table cell) arrives in three
			// nodes: the opening tag, the text, the closing tag.
			const open = /^<code(\s[^>]*)?>$/.exec(node.value ?? '');
			if (open) {
				const siblings = ctx.parent(node)?.children ?? [];
				const index = ctx.indexOf(node) ?? -1;
				const [text, close] = [siblings[index + 1], siblings[index + 2]];
				if (text?.type !== 'text' || close?.type !== 'raw' || close.value !== '</code>') return;
				if (breaksAtSeparators(text.value, false)) {
					if (spend(room, breakCount(text.value))) {
						ctx.replaceNode(text, { type: 'raw', value: withBreaks(escapeText(text.value)) });
					}
				} else if (!/\bclass=/.test(open[1] ?? '') && isShort(text.value, false)) {
					ctx.replaceNode(node, { type: 'raw', value: `<code class="rb-short"${open[1] ?? ''}>` });
				}
				return;
			}
			const value = markRawCode(node.value ?? '', room);
			if (value !== node.value) ctx.replaceNode(node, { type: 'raw', value });
		},
		element: [
			{
				filter: ['code'],
				visit(code, ctx) {
					const parent = ctx.parent(code);
					const inPre = parent?.type === 'element' && parent.tagName === 'pre';
					const text = ctx.textContent(code);
					if (isShort(text, inPre)) addClass(ctx, code, 'rb-short');
					else if (breaksAtSeparators(text, inPre)) breakAtSeparators(ctx, code, breakRoom(ctx));
				},
			},
			{
				filter: ['table'],
				visit(table, ctx) {
					if (OPTION_HEADER.test(firstHeader(ctx, table))) addClass(ctx, table, 'rb-options');
				},
			},
		],
		// In an MDX page (one with tabs) inline HTML is JSX: a `<code title>`
		// hash in a table cell reaches neither raw() nor element. Long JSX code
		// gets its breaks from site.ts.
		mdxJsxTextElement: {
			filter: ['code'],
			visit(code, ctx) {
				const named = (code.attributes ?? []).some((attribute) => ['class', 'className'].includes(attribute.name));
				if (!named && isShort(ctx.textContent(code), false)) ctx.setProperty(code, 'class', 'rb-short');
			},
		},
	};
}
