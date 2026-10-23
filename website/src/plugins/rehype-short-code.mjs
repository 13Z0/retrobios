// Code that must read in one piece, marked when the page is built (the CSS
// is retrobios.css, code.rb-short and table.rb-options).
//
// - Inline code of SHORT_CODE characters or fewer gets `rb-short` and never
//   breaks: `--restore` split into `--` and `restore` on the home page at
//   1440 px, and `--list-` / `systems` in the installer's options table.
//   Longer inline code (a path, a full command) still wraps.
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

// The cards and tables the generator writes as HTML reach this step as raw
// text: their code elements are marked by rewriting the tag. A block that
// holds a `pre` is left as written.
const RAW_CODE = /<code(\s[^>]*)?>([^<]*)<\/code>/g;
const ENTITY = /&(?:#\d+|#x[0-9a-f]+|[a-z]+);/gi;

export const markRawCode = (html) => {
	if (html.includes('<pre')) return html;
	return html.replace(RAW_CODE, (whole, attributes = '', text) => {
		if (/\bclass=/.test(attributes) || !isShort(text.replace(ENTITY, '_'), false)) return whole;
		return `<code class="rb-short"${attributes}>${text}</code>`;
	});
};

export default function shortCode() {
	return {
		name: 'rb-short-code',
		raw(node, ctx) {
			// Inline HTML in Markdown (a hash in a table cell) arrives in three
			// nodes: the opening tag, the text, the closing tag.
			const open = /^<code(\s[^>]*)?>$/.exec(node.value ?? '');
			if (open) {
				const siblings = ctx.parent(node)?.children ?? [];
				const index = ctx.indexOf(node) ?? -1;
				const [text, close] = [siblings[index + 1], siblings[index + 2]];
				if (text?.type === 'text' && close?.type === 'raw' && close.value === '</code>'
					&& !/\bclass=/.test(open[1] ?? '') && isShort(text.value, false)) {
					ctx.replaceNode(node, { type: 'raw', value: `<code class="rb-short"${open[1] ?? ''}>` });
				}
				return;
			}
			const value = markRawCode(node.value ?? '');
			if (value !== node.value) ctx.replaceNode(node, { type: 'raw', value });
		},
		element: [
			{
				filter: ['code'],
				visit(code, ctx) {
					const parent = ctx.parent(code);
					const inPre = parent?.type === 'element' && parent.tagName === 'pre';
					if (isShort(ctx.textContent(code), inPre)) addClass(ctx, code, 'rb-short');
				},
			},
			{
				filter: ['table'],
				visit(table, ctx) {
					if (OPTION_HEADER.test(firstHeader(ctx, table))) addClass(ctx, table, 'rb-options');
				},
			},
		],
	};
}
