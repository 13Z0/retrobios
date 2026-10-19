/* Progressive enhancement for generated RetroBIOS reference tables. */
(() => {
  "use strict";

  const textFromHeading = (element) => {
    let cursor = element;
    while (cursor) {
      cursor = cursor.previousElementSibling;
      if (!cursor) break;
      if (/^H[1-6]$/.test(cursor.tagName)) {
        return cursor.textContent.trim();
      }
    }
    return document.querySelector("h1")?.textContent.trim() || "Data table";
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

      const container = table.parentElement;
      const heading = textFromHeading(container);
      if (!table.querySelector("caption")) {
        const caption = document.createElement("caption");
        caption.className = "rb-visually-hidden";
        caption.textContent = heading;
        table.prepend(caption);
      }
      if (container && container.scrollWidth > container.clientWidth) {
        container.tabIndex = 0;
        container.setAttribute("role", "region");
        container.setAttribute("aria-label", `${heading}, scrollable table`);
      }

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
    document.querySelectorAll('[role="progressbar"]').forEach((progress) => {
      if (!progress.hasAttribute("aria-label") && !progress.hasAttribute("aria-labelledby")) {
        progress.setAttribute("aria-label", "Loading page");
      }
    });
  };

  // files/?sha1=<sha1> is a file's content address: the card it names may
  // sit on another manufacturer's page today than when the link was cited.
  const resolveFileAddress = () => {
    const status = document.getElementById("rb-file-lookup-status");
    if (!status) return;
    const sha1 = (new URLSearchParams(window.location.search).get("sha1") || "")
      .trim().toLowerCase();
    if (!sha1) return;
    if (!/^[0-9a-f]{40}$/.test(sha1)) {
      status.textContent = "A SHA1 is 40 hexadecimal characters.";
      return;
    }
    status.textContent = "Looking it up...";
    const index = new URL("../api/v1/file-locations.json", window.location.href);
    fetch(index)
      .then((response) => response.json())
      .then((document_) => {
        const item = (document_.items || []).find((entry) => entry.sha1 === sha1);
        if (item) {
          window.location.replace(new URL("../" + item.url, window.location.href));
        } else {
          status.textContent = "The collection holds no file with this SHA1.";
        }
      })
      .catch(() => {
        status.textContent = "The file index could not be read.";
      });
  };

  const enhance = () => {
    enhanceTables();
    nameMaterialWidgets();
    resolveFileAddress();
  };

  if (window.document$?.subscribe) {
    window.document$.subscribe(enhance);
  } else if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", enhance, { once: true });
  } else {
    enhance();
  }
})();
