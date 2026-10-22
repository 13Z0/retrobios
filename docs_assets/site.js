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
