/// <reference path="../.astro/types.d.ts" />

declare namespace App {
	interface Locals {
		rbBreadcrumbs?: import('./routeData').Crumb[];
	}
}
