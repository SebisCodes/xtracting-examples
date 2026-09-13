/* ==========================================================================
 * Projects: the Xtracting projects the crawler's keys open.
 *
 * A reading of the crawler's own registry, plus one act that belongs to the
 * customer: deleting a configuration nobody uses. The dashboard cannot make a
 * project and does not try - only a key can be asked which project it opens,
 * and only the crawler holds the keys.
 *
 * OLD MEANS UNUSED, NOT UNIMPORTANT. A project is marked old when no key of it
 * has authenticated for seven days, which usually means its keys were taken out
 * of the crawler's configuration. The badge is an offer, not a verdict: the
 * delete button appears for every project, and the badge only says which one a
 * person probably came here for.
 * ========================================================================== */

import { api, isAbort, fmtAgo, plural, sentence } from "./api.js";
import { announce } from "./a11y.js";
import { outward, projectUrl, apiKeysUrl } from "./icons.js";

/* `plural(n, one, many)` RETURNS THE NUMBER WITH THE WORD - "3 keys", not
 * "keys". Every count on this page printed it twice ("3 3 keys that may
 * extract") because the number was written in front of it as well. It is
 * the only file that did; api.js is where the helper lives and says so. */

const list = document.getElementById("project-list");
if (list) start(list);

function start(list) {
  /* The sentence that says there is no project yet points at the page that
   * issues keys, not at the name of the variable one goes into. */
  const emptyKeys = document.getElementById("projects-empty-keys");
  if (emptyKeys) {
    emptyKeys.href = apiKeysUrl();
    outward(emptyKeys, "Create a key in Xtracting");
  }

  const template = document.getElementById("project-item-template");
  const keyTemplate = document.getElementById("project-key-template");
  const emptyNote = document.getElementById("projects-empty");
  const errorNote = document.getElementById("projects-error");
  const errorLine = document.getElementById("projects-error-text");
  const retry = document.getElementById("projects-retry");

  function drawKeys(node, project) {
    const target = node.querySelector("[data-key-list]");
    target.textContent = "";
    project.keys.forEach((key) => {
      const row = keyTemplate.content.firstElementChild.cloneNode(true);
      row.querySelector("[data-key-name]").textContent = key.name;
      row.querySelector("[data-key-prefix]").textContent = key.prefix;

      /* The effort of a key is only known when some key of this project could
       * read the project. Without one there is nothing to show and nothing to
       * compare, so the field stays hidden rather than saying "unknown" on
       * every row. */
      const effort = row.querySelector("[data-key-effort]");
      if (project.detailed && key.effort) {
        effort.hidden = false;
        effort.textContent = key.effort_name;
      }
      const translations = row.querySelector("[data-key-translations]");
      if (key.translations) {
        translations.hidden = false;
        translations.textContent = key.translations;
      }
      target.appendChild(row);
    });
  }

  function render(items) {
    list.textContent = "";
    list.setAttribute("aria-busy", "false");
    emptyNote.hidden = items.length > 0;

    items.forEach((project) => {
      const node = template.content.firstElementChild.cloneNode(true);
      node.querySelector("[data-name]").textContent = project.name;
      node.querySelector("[data-id]").textContent = project.project_id;

      const age = node.querySelector("[data-age]");
      if (project.old) {
        age.hidden = false;
        age.className = "pill pill--off";
        age.textContent = "not used for over a week";
      }

      node.querySelector("[data-keys]").textContent =
        project.keys.length
          ? `${plural(project.keys.length, "key", "keys")} that may extract`
          : "no key that may extract";
      node.querySelector("[data-watchlists]").textContent =
        plural(project.watchlists, "watchlist", "watchlists");
      node.querySelector("[data-seen]").textContent =
        project.last_seen ? `last used ${fmtAgo(project.last_seen)}` : "never used";

      /* THE PROJECT'S OWN PAGE ON XTRACTING. This dashboard can say what the
       * archive holds; the project's defaults, its perspectives and the keys
       * that may extract are set over there. Outward, like every link out of
       * this dashboard: a new tab, the arrow that says so, and the promise
       * in words for anybody who cannot see the arrow. */
      const open = node.querySelector("[data-open]");
      if (open) {
        open.href = projectUrl(project.project_id);
        open.hidden = false;
        outward(open, `Open ${project.name} in Xtracting`);
      }

      drawKeys(node, project);

      const remove = node.querySelector("[data-delete]");
      const note = node.querySelector("[data-delete-note]");
      remove.hidden = false;
      remove.addEventListener("click", () => confirmDelete(project, remove, note));

      list.appendChild(node);
    });
  }

  /* Two presses, and the second one says what it will take with it by number.
   * A confirm() dialog would say the same thing in a box the browser draws and
   * nobody reads; a second press on a button that has changed its own words is
   * the same protection with the consequence in the place the eye already is. */
  function confirmDelete(project, button, note) {
    if (button.dataset.armed !== "true") {
      button.dataset.armed = "true";
      button.textContent = project.watchlists
        ? `Press again to delete ${plural(project.watchlists, "watchlist", "watchlists")}`
        : "Press again to delete this project";
      note.hidden = false;
      note.textContent = sentence(
        "The archive is not touched: everything already collected stays, and " +
        "this project keeps appearing in the selector at the top of every view"
      );
      window.setTimeout(() => {
        if (button.dataset.armed !== "true") return;
        button.dataset.armed = "false";
        button.textContent = "Delete this project and its watchlists";
        note.hidden = true;
      }, 8000);
      return;
    }
    remove(project, button, note);
  }

  async function remove(project, button, note) {
    button.disabled = true;
    button.textContent = "Deleting…";
    try {
      const answer = await api(`/api/projects/crawler/${encodeURIComponent(project.project_id)}`, {
        method: "DELETE", context: false, quiet: true,
      });
      announce(sentence(
        `${project.name} is gone, with ${answer.watchlists} ` +
        `${plural(answer.watchlists, "watchlist", "watchlists")}. The archive is unchanged`
      ), { assertive: true });
      load();
    } catch (error) {
      if (isAbort(error)) return;
      button.disabled = false;
      button.dataset.armed = "false";
      button.textContent = "Delete this project and its watchlists";
      note.hidden = false;
      note.textContent = sentence(error.message || "It could not be deleted");
    }
  }

  async function load() {
    list.setAttribute("aria-busy", "true");
    try {
      const data = await api("/api/projects/crawler", { context: false, quiet: true });
      errorNote.hidden = true;
      render(data.items || []);
    } catch (error) {
      if (isAbort(error)) return;
      list.textContent = "";
      list.setAttribute("aria-busy", "false");
      emptyNote.hidden = true;
      errorNote.hidden = false;
      errorLine.textContent = sentence(
        (error.message || "The projects could not be read") +
        ". Nothing is lost - this page reads them again on the next attempt"
      );
    }
  }

  if (retry) retry.addEventListener("click", load);
  load();
}
