/* Progressive enhancement for generated RetroBIOS reference tables. */
(() => {
  "use strict";

  // A heading's words without its permalink sign.
  const headingText = (heading) => {
    const copy = heading.cloneNode(true);
    copy.querySelectorAll(".headerlink").forEach((link) => link.remove());
    return copy.textContent.trim();
  };

  const textFromHeading = (element) => {
    let cursor = element;
    while (cursor) {
      cursor = cursor.previousElementSibling;
      if (!cursor) break;
      if (/^H[1-6]$/.test(cursor.tagName)) {
        return headingText(cursor);
      }
    }
    const title = document.querySelector("h1");
    return title ? headingText(title) : "Data table";
  };

  // A scroller is a stop in the tab order only while it overflows: a table
  // in a closed block or a hidden tab gains its overflow later.
  const markScrollable = (container, heading) => {
    if (!container) return;
    container.dataset.rbHeading = heading;
    if (container.scrollWidth > container.clientWidth + 1) {
      container.tabIndex = 0;
      container.setAttribute("role", "region");
      container.setAttribute("aria-label", `${heading}, scrollable table`);
    } else if (container.getAttribute("role") === "region") {
      container.removeAttribute("tabindex");
      container.removeAttribute("role");
      container.removeAttribute("aria-label");
    }
  };

  const recheckScrollable = () => {
    document.querySelectorAll(".md-typeset__scrollwrap[data-rb-heading]").forEach((container) => {
      markScrollable(container, container.dataset.rbHeading);
    });
  };

  // A table whose rows end in an action (a download button) is laid out one
  // block per row on a phone, each cell under its column name, so the action
  // never sits behind a horizontal scroll.
  const stackActionTable = (table) => {
    if (!table.querySelector("tbody .md-button")) return;
    const labels = Array.from(table.querySelectorAll("thead th")).map((cell) =>
      cell.textContent.trim()
    );
    if (!labels.length) return;
    table.dataset.rbStack = "true";
    table.querySelectorAll("tbody tr").forEach((row) => {
      Array.from(row.children).forEach((cell, column) => {
        if (labels[column] && !cell.hasAttribute("data-label")) {
          cell.setAttribute("data-label", labels[column]);
        }
      });
    });
  };

  const enhanceTables = () => {
    document.querySelectorAll(".md-typeset table").forEach((table, index) => {
      if (table.dataset.rbEnhanced === "true") return;
      table.dataset.rbEnhanced = "true";

      table.querySelectorAll("thead th").forEach((cell) => {
        if (!cell.hasAttribute("scope")) cell.setAttribute("scope", "col");
      });
      table.querySelectorAll("tbody tr").forEach((row) => {
        const first = row.querySelector(":scope > th");
        if (first && !first.hasAttribute("scope")) first.setAttribute("scope", "row");
      });

      // The scrolling element is Material's scrollwrap, one level above
      // the table's own wrapper.
      const container = table.closest(".md-typeset__scrollwrap") || table.parentElement;
      const heading = textFromHeading(container);
      if (!table.querySelector("caption")) {
        const caption = document.createElement("caption");
        caption.className = "rb-visually-hidden";
        caption.textContent = heading;
        table.prepend(caption);
      }
      stackActionTable(table);
      markScrollable(container, heading);

      const rows = Array.from(table.querySelectorAll("tbody tr"));
      if (rows.length < 20 || !container || container.dataset.rbFilter === "true") {
        return;
      }
      container.dataset.rbFilter = "true";

      const controls = document.createElement("div");
      controls.className = "rb-table-filter";
      const id = `rb-table-filter-${index}-${Math.random().toString(36).slice(2, 8)}`;
      const label = document.createElement("label");
      label.htmlFor = id;
      label.textContent = `Filter ${heading}`;
      const input = document.createElement("input");
      input.id = id;
      input.type = "search";
      input.autocomplete = "off";
      input.placeholder = "Name, platform, system, hash...";
      const count = document.createElement("span");
      count.className = "rb-table-filter-count";
      count.setAttribute("aria-live", "polite");
      count.textContent = `${rows.length} rows`;
      controls.append(label, input, count);
      container.before(controls);

      const searchable = rows.map((row) => ({
        row,
        text: row.textContent.normalize("NFKD").toLocaleLowerCase(),
      }));
      input.addEventListener("input", () => {
        const query = input.value.trim().normalize("NFKD").toLocaleLowerCase();
        let visible = 0;
        searchable.forEach((entry) => {
          const matches = !query || entry.text.includes(query);
          entry.row.hidden = !matches;
          if (matches) visible += 1;
        });
        count.textContent = `${visible} of ${rows.length} rows`;
      });
    });
  };

  const nameMaterialWidgets = () => {
    document.querySelectorAll('[role="dialog"]').forEach((dialog) => {
      if (dialog.hasAttribute("aria-label") || dialog.hasAttribute("aria-labelledby")) return;
      const heading = dialog.querySelector("h1, h2, h3");
      dialog.setAttribute("aria-label", heading?.textContent.trim() || "Site search");
    });
    // The page-load bar is drawn for the eye only: the new page announces
    // itself when it arrives.
    document.querySelectorAll(".md-progress").forEach((progress) => {
      progress.setAttribute("aria-hidden", "true");
    });
    // Material wraps each copy button in a nav element: one unnamed
    // navigation landmark per code block. The button stays, the landmark goes.
    document.querySelectorAll("nav.md-code__nav").forEach((nav) => {
      nav.setAttribute("role", "none");
    });
  };

  // files/?hash= reads one shard of the lookup, named by the first two hex
  // characters of the value: 20 KB where the whole index was 675 KB.
  const HASH_LENGTHS = { 8: "CRC32", 32: "MD5", 40: "SHA1", 64: "SHA256" };
  const LAYER_LABELS = {
    collection: "In the collection",
    "data-directory": "In a data directory the packs ship",
    platform: "Declared by a platform",
    emulator: "Read by an emulator",
  };
  const siteRoot = () => {
    const script = document.querySelector('script[src$="javascripts/site.js"]');
    return script ? new URL("../", script.src) : new URL("../", window.location.href);
  };
  const lookupShard = (value) =>
    fetch(new URL(`api/v1/lookup/${value.slice(0, 2)}.json`, siteRoot()))
      .then((response) => (response.ok ? response.json() : { items: [] }))
      .then((shard) => shard.items || []);
  const normalizeHash = (raw) => raw.trim().toLowerCase().replace(/^0x/, "").replace(/\s+/g, "");

  const renderMatches = (status, value, items) => {
    status.textContent = "";
    const kind = HASH_LENGTHS[value.length];
    const heading = document.createElement("p");
    heading.textContent = `${items.length} ${items.length === 1 ? "match" : "matches"} for ${kind} ${value}:`;
    status.append(heading);
    Object.entries(LAYER_LABELS).forEach(([layer, label]) => {
      const found = items.filter((item) => item.layer === layer);
      if (!found.length) return;
      const title = document.createElement("p");
      const strong = document.createElement("strong");
      strong.textContent = label;
      title.append(strong);
      const list = document.createElement("ul");
      found.forEach((item) => {
        const row = document.createElement("li");
        const link = document.createElement("a");
        link.href = new URL(item.url, siteRoot()).href;
        const code = document.createElement("code");
        code.textContent = item.name || item.value;
        link.append(code);
        row.append(link);
        if (item.owner) row.append(` (${item.owner})`);
        list.append(row);
      });
      status.append(title, list);
    });
  };

  // files/?sha1=<sha1> is a file's content address and opens its card
  // wherever it is now; files/?hash= also takes an MD5, CRC32 or SHA256 and
  // lists every page that names the value.
  const resolveFileAddress = () => {
    const status = document.getElementById("rb-file-lookup-status");
    if (!status) return;
    const params = new URLSearchParams(window.location.search);
    const permalink = params.has("sha1");
    const value = normalizeHash(params.get("sha1") || params.get("hash") || "");
    if (!value) return;
    const input = document.getElementById("rb-hash");
    if (input) input.value = value;
    const prefix = permalink && /^[0-9a-f]{12}$/.test(value);
    if (!prefix && !(/^[0-9a-f]+$/.test(value) && HASH_LENGTHS[value.length])) {
      status.textContent =
        "A hash is 8 (CRC32), 32 (MD5), 40 (SHA1) or 64 (SHA256) hexadecimal characters.";
      return;
    }
    status.textContent = "Looking it up...";
    lookupShard(value)
      .then((items) => {
        const matches = items.filter((item) =>
          prefix
            ? item.algorithm === "sha1" && item.value.startsWith(value)
            : item.value === value
        );
        const cards = matches.filter((item) => item.layer === "collection" || item.layer === "data-directory");
        const targets = new Set(cards.map((item) => item.url));
        if (permalink && targets.size === 1) {
          window.location.replace(new URL(cards[0].url, siteRoot()));
        } else if (matches.length) {
          renderMatches(status, prefix ? matches[0].value : value, prefix ? cards : matches);
        } else {
          status.textContent = "No file the site knows has this hash.";
        }
      })
      .catch(() => {
        status.textContent = "The file index could not be read.";
      });
  };

  // A file card moved to another page keeps its address: an id of the form
  // sha1-<12 hex> that this page does not hold is looked up by content.
  const followMovedCard = () => {
    const match = /^#sha1-([0-9a-f]{12})$/.exec(window.location.hash);
    if (!match || document.getElementById(`sha1-${match[1]}`)) return;
    window.location.replace(new URL(`files/?sha1=${match[1]}`, siteRoot()));
  };

  // The site search no longer holds hashes: Enter on a hash opens the lookup.
  const hashSearch = () => {
    const query = document.querySelector('input[data-md-component="search-query"]');
    if (!query || query.dataset.rbHash === "true") return;
    query.dataset.rbHash = "true";
    query.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      const value = normalizeHash(query.value);
      if (/^[0-9a-f]+$/.test(value) && HASH_LENGTHS[value.length]) {
        event.preventDefault();
        window.location.assign(new URL(`files/?hash=${value}`, siteRoot()));
      }
    });
  };

  const enhance = () => {
    enhanceTables();
    nameMaterialWidgets();
    resolveFileAddress();
    followMovedCard();
    hashSearch();
  };

  let resizeTimer = 0;
  window.addEventListener("resize", () => {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(recheckScrollable, 150);
  });
  document.addEventListener("toggle", recheckScrollable, true);
  document.addEventListener("change", (event) => {
    if (event.target.matches?.(".tabbed-set > input")) recheckScrollable();
  });

  if (window.document$?.subscribe) {
    window.document$.subscribe(enhance);
  } else if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", enhance, { once: true });
  } else {
    enhance();
  }
})();
