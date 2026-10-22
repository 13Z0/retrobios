// Static server shaped like GitHub Pages for measurements: gzip, no brotli,
// max-age=600, the site under /retrobios/, real 404 page.
//   node serve.mjs <site dir> <port>
import { createServer } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';
import { gzipSync } from 'node:zlib';

const [root = 'dist', port = '4321'] = process.argv.slice(2);
const BASE = '/retrobios/';
const TYPES = {
	'.html': 'text/html; charset=utf-8', '.css': 'text/css', '.js': 'text/javascript',
	'.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png',
	'.xml': 'application/xml', '.txt': 'text/plain', '.wasm': 'application/wasm',
};
const COMPRESS = new Set(['.html', '.css', '.js', '.json', '.svg', '.xml', '.txt']);

const resolveFile = async (path) => {
	let file = join(root, normalize(path).replace(/^(\.\.[/\\])+/, ''));
	try {
		if ((await stat(file)).isDirectory()) file = join(file, 'index.html');
		await stat(file);
		return file;
	} catch {
		return null;
	}
};

createServer(async (req, res) => {
	const url = new URL(req.url, 'http://x');
	if (!url.pathname.startsWith(BASE)) {
		res.writeHead(302, { location: BASE }).end();
		return;
	}
	const rel = decodeURIComponent(url.pathname.slice(BASE.length));
	let file = await resolveFile(rel);
	let status = 200;
	if (!file) {
		status = 404;
		file = await resolveFile('404.html');
	}
	const ext = extname(file);
	let body = await readFile(file);
	const headers = { 'content-type': TYPES[ext] ?? 'application/octet-stream', 'cache-control': 'max-age=600' };
	if (COMPRESS.has(ext) && /\bgzip\b/.test(req.headers['accept-encoding'] ?? '')) {
		body = gzipSync(body);
		headers['content-encoding'] = 'gzip';
	}
	headers['content-length'] = body.length;
	res.writeHead(status, headers).end(body);
}).listen(Number(port), '127.0.0.1');
