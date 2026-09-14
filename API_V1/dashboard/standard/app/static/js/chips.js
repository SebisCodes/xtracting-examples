/* ==========================================================================
 *  Chip input - several search terms in one field.
 *
 *  Typing a term and pressing `,` or Enter turns it into a chip; Backspace
 *  in an empty field removes the last chip; pasting "battery, antitrust"
 *  splits into two. A hidden input carries the terms as a JSON array so a
 *  form (or state.js) reads one field, not a variable number of them.
 *
 *  Why chips and not a plain text field with commas: the query view
 *  searches "battery" OR "antitrust", and people must SEE that they have
 *  two terms, not one long one. Each chip has its own remove button large
 *  enough to hit (WCAG 2.5.8) - older users do not drag or hover.
 *
 *  Markup (the Jinja macro `chipinput` emits it):
 *
 *    <div class="chips" data-chips>
 *      <input type="hidden" name="terms" value='["battery"]'>
 *      <ul class="chips-list" aria-label="Search terms"></ul>
 *      <input type="text" class="chips-input" ...>
 *    </div>
 *
 *  Events: the root dispatches `chips:change` (bubbles) with detail
 *  {values}. Enter on an empty field submits the form as usual.
 * ========================================================================== */

const SPLIT = /[,\n;]+/;

function parseValues(raw) {
  if (!raw) return [];
  try {
    const v = JSON.parse(raw);
    if (Array.isArray(v)) return v.map((s) => String(s).trim()).filter(Boolean);
  } catch (e) {
    // A plain comma list is accepted too, so a hand-written URL works.
  }
  return String(raw).split(SPLIT).map((s) => s.trim()).filter(Boolean);
}

export function initChips(root) {
  if (root._chips) return root._chips;
  const hidden = root.querySelector('input[type="hidden"]');
  const list = root.querySelector(".chips-list") || root.appendChild(Object.assign(document.createElement("ul"), { className: "chips-list" }));
  const field = root.querySelector('input[type="text"]');
  if (!field) throw new Error("chips: text input missing");
  if (!list.getAttribute("aria-label")) list.setAttribute("aria-label", "Terms");

  let values = parseValues(hidden ? hidden.value : root.dataset.values);

  function write() {
    if (hidden) hidden.value = JSON.stringify(values);
    list.textContent = "";
    values.forEach((v, i) => {
      const li = document.createElement("li");
      li.className = "chip";
      const text = document.createElement("span");
      text.className = "chip-text";
      text.textContent = v;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "chip-remove";
      btn.setAttribute("aria-label", `Remove ${v}`);
      btn.textContent = "×";
      btn.addEventListener("click", () => {
        remove(i);
        field.focus();
      });
      li.appendChild(text);
      li.appendChild(btn);
      list.appendChild(li);
    });
    root.classList.toggle("has-chips", values.length > 0);
  }

  function emit() {
    root.dispatchEvent(new CustomEvent("chips:change", { bubbles: true, detail: { values: values.slice() } }));
  }

  function add(text) {
    const parts = String(text).split(SPLIT).map((s) => s.trim()).filter(Boolean);
    let changed = false;
    parts.forEach((p) => {
      // Case-insensitive duplicate check: "Battery" and "battery" are one term.
      if (!values.some((v) => v.toLowerCase() === p.toLowerCase())) {
        values.push(p);
        changed = true;
      }
    });
    if (changed) { write(); emit(); }
    return changed;
  }

  function remove(index) {
    if (index < 0 || index >= values.length) return;
    values.splice(index, 1);
    write();
    emit();
  }

  function commitField() {
    const t = field.value.trim();
    field.value = "";
    if (t) add(t);
  }

  field.addEventListener("keydown", (e) => {
    if (e.key === "," ) {
      e.preventDefault();
      commitField();
    } else if (e.key === "Enter") {
      if (field.value.trim()) {
        // Enter with text makes a chip and does NOT submit; the user sees
        // the chip and presses Enter again (or Search) to go.
        e.preventDefault();
        commitField();
      }
    } else if (e.key === "Backspace" && !field.value) {
      if (values.length) {
        e.preventDefault();
        remove(values.length - 1);
      }
    }
  });

  field.addEventListener("paste", (e) => {
    const text = (e.clipboardData || window.clipboardData).getData("text");
    if (text && SPLIT.test(text)) {
      e.preventDefault();
      add(text);
    }
  });

  // Leaving the field with text in it keeps the text as a chip - a term
  // one typed but never "confirmed" must not silently disappear.
  field.addEventListener("blur", commitField);

  // Clicking the empty area of the box focuses the field, like a real input.
  root.addEventListener("click", (e) => {
    if (e.target === root || e.target === list) field.focus();
  });

  write();

  const api = {
    root, field, hidden, add, remove,
    get values() { return values.slice(); },
    set values(v) { values = parseValues(JSON.stringify(v)); write(); emit(); },
    clear() { values = []; write(); emit(); },
  };
  root._chips = api;
  return api;
}

export function initAll(scope) {
  (scope || document).querySelectorAll("[data-chips]").forEach(initChips);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => initAll(document));
} else {
  initAll(document);
}
