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
				// Search ranking of the first two levels.
				boost: z.number().min(1).max(10).optional(),
				// The date the page's content carries, for the sitemap.
				lastmod: z.string().optional(),
			}),
		}),
	}),
};
