// Command blocks wrap (astro.config.mjs, wrap: true) so the end of a
// one-liner stays visible. A browser may also break a line after a hyphen,
// which split `--platform` into `--` and `platform`, and `nintendo-64dd`
// into `nintendo-` and `64dd`: a reader who types the command reads two
// things. Each word that holds a hyphen is put in a span that does not
// break inside. Only spans are added: the text, the copy button's
// data-code and a selection copied by hand are unchanged.
//
// A word longer than LONGEST is left breakable: an address that long would
// push the block wider than a phone.

export const LONGEST = 48;
export const UNBROKEN = new RegExp(`(?<!\\S)(?=\\S*-)\\S{1,${LONGEST}}(?!\\S)`, 'g');
const CLASS = 'rb-nb';

const textOf = (node) => (node.type === 'text' ? node.value : (node.children ?? []).map(textOf).join(''));

// A token span holds one text node: it can be cut in two, both halves
// keeping its colours.
const cuttable = (node) =>
	node.type === 'text' || (node.type === 'element' && node.children?.length === 1 && node.children[0].type === 'text');

const slice = (node, from, to) =>
	node.type === 'text'
		? { type: 'text', value: node.value.slice(from, to) }
		: { ...node, properties: { ...node.properties }, children: [{ type: 'text', value: node.children[0].value.slice(from, to) }] };

// The children of *container*, with every hyphenated word wrapped in one
// unbreakable span. A word that crosses a node this cannot cut is left as is.
export const unbreakWords = (container) => {
	const children = container.children ?? [];
	const text = children.map(textOf).join('');
	const ranges = [...text.matchAll(UNBROKEN)].map((match) => [match.index, match.index + match[0].length]);
	if (!ranges.length) return children;
	const cuts = [...new Set(ranges.flat())].sort((a, b) => a - b);
	const pieces = [];
	let position = 0;
	for (const child of children) {
		const end = position + textOf(child).length;
		const inner = cuts.filter((cut) => cut > position && cut < end);
		if (inner.length && cuttable(child)) {
			let start = position;
			for (const cut of [...inner, end]) {
				pieces.push({ node: slice(child, start - position, cut - position), from: start, to: cut });
				start = cut;
			}
		} else {
			pieces.push({ node: child, from: position, to: end });
		}
		position = end;
	}
	const ends = new Map(ranges);
	const out = [];
	for (let index = 0; index < pieces.length; ) {
		const end = ends.get(pieces[index].from);
		if (end !== undefined && pieces[index].to > pieces[index].from) {
			let last = index;
			while (last < pieces.length && pieces[last].to < end) last += 1;
			if (last < pieces.length && pieces[last].to === end) {
				out.push({
					type: 'element',
					tagName: 'span',
					properties: { className: [CLASS] },
					children: pieces.slice(index, last + 1).map((piece) => piece.node),
				});
				index = last + 1;
				continue;
			}
		}
		out.push(pieces[index].node);
		index += 1;
	}
	return out;
};

// The element of a rendered line that holds its tokens (div.code), else
// the line itself.
const tokensOf = (line) => {
	for (const child of line.children ?? []) {
		if (child.type === 'element' && String(child.properties?.className ?? '').split(/[\s,]+/).includes('code')) return child;
	}
	return line;
};

export default function unbrokenTokens() {
	return {
		name: 'rb-unbroken-tokens',
		baseStyles: `.${CLASS} { white-space: nowrap; }`,
		hooks: {
			postprocessRenderedLine({ renderData }) {
				const container = tokensOf(renderData.lineAst);
				container.children = unbreakWords(container);
			},
		},
	};
}
