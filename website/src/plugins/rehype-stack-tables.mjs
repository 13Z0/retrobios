// Tables a phone reads one row at a time, marked when the page is built so
// the stacked layout is there on first paint (the CSS is retrobios.css,
// table[data-rb-stack]). The rule reads the table, never the page that
// wrote it:
// - a row that ends in an action (a release download, a button, the link to
//   a setup's install command) or a header that names a ZIP column: one
//   block per row, each cell under its column name;
// - two columns: a list of label and value, data-rb-stack="kv";
// - on a level 0 or 1 page (`level: start` or `setup`), a table short enough
//   to read as records (under 20 rows, the size from which site.ts adds a
//   filter): one block per row. A longer one is a list scanned by column and
//   keeps its scroll.
// Satteri runs hast plugins (the rehype stage of its pipeline): this one is
// registered in astro.config.mjs through satteri({ hastPlugins }).

const ACTION_HREF = /\/releases\/(download\/|latest\/?$)|#install$/;
const ACTION_CLASS = /\bclass=["'][^"']*\brb-button\b/;
const DOWNLOAD_HEADER = /^(zip|download)$/i;
const READER_LEVELS = new Set(['start', 'setup']);
export const RECORD_ROWS = 20;

const elements = (node, tag, found = []) => {
	for (const child of node.children ?? []) {
		if (child.type !== 'element') continue;
		if (child.tagName === tag) found.push(child);
		// A table inside a cell is its own table.
		if (child.tagName !== 'table') elements(child, tag, found);
	}
	return found;
};

const holdsAction = (node) =>
	(node.children ?? []).some(
		(child) =>
			(child.type === 'element' &&
				((child.tagName === 'a' && ACTION_HREF.test(String(child.properties?.href ?? ''))) ||
					String(child.properties?.className ?? '').split(/[\s,]+/).includes('rb-button') ||
					holdsAction(child))) ||
			(child.type === 'raw' && ACTION_CLASS.test(child.value ?? '')),
	);

export const stackKind = ({ labels, rows, action, level }) => {
	if (labels.length === 2) return 'kv';
	if (action || DOWNLOAD_HEADER.test(labels.at(-1) ?? '')) return 'true';
	if (READER_LEVELS.has(level) && labels.length > 2 && rows < RECORD_ROWS) return 'true';
	return null;
};

export default function stackTables() {
	return {
		name: 'rb-stack-tables',
		element: {
			filter: ['table'],
			visit(table, ctx) {
				const head = elements(table, 'thead')[0];
				const body = elements(table, 'tbody')[0];
				if (!head || !body) return;
				const labels = elements(head, 'th').map((cell) => ctx.textContent(cell).trim());
				const rows = elements(body, 'tr');
				const level = ctx.data.astro?.frontmatter?.level;
				const kind = stackKind({ labels, rows: rows.length, action: holdsAction(body), level });
				if (!kind) return;
				ctx.setProperty(table, 'dataRbStack', kind);
				if (kind === 'kv') return;
				for (const row of rows) {
					elements(row, 'td').forEach((cell, column) => {
						if (labels[column] && cell.properties?.dataLabel === undefined) ctx.setProperty(cell, 'dataLabel', labels[column]);
					});
				}
			},
		},
	};
}
