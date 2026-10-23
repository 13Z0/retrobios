import { defineCollection } from 'astro:content';
import { z } from 'astro/zod';
import { docsLoader } from '@astrojs/starlight/loaders';
import { docsSchema } from '@astrojs/starlight/schema';

// The generator's own front matter, typed: a page with a misspelt level fails the build.
export const collections = {
	docs: defineCollection({
		loader: docsLoader(),
		schema: docsSchema({
			extend: z.object({
				// Who the page is written for, from someone who knows nothing
				// (start) to someone adding to the project (contribute).
				level: z.enum(['start', 'setup', 'explore', 'reference', 'contribute']).optional(),
				// How much the site search weighs the page's words (1 by default).
				searchWeight: z.number().min(0.1).max(10).optional(),
				// The date the page's content carries, for the sitemap.
				lastmod: z.string().optional(),
				// A mark drawn beside the title (a platform's logo), a path under
				// the site root such as assets/logos/batocera.svg.
				logo: z.string().optional(),
				// The page a part page belongs to (a console of a setup's list),
				// read by routeData.ts: the last crumb, marked in the sidebar.
				parent: z.object({ label: z.string(), link: z.string() }).optional(),
			}),
		}),
	}),
};
