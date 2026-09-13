/* ==========================================================================
 *  WHAT THE TERM TURNED INTO, SAID TRUTHFULLY - AND THE WAY OUT OF IT.
 *
 *  Four views ask app/scope.py the same question - "what does this term
 *  mean?" - and each answering it in its own words is how «“Apple Inc.” is
 *  a bucket: Apple (Company), Apple Inc. (Company)» ends up on a page: no
 *  bucket is called Apple Inc. The
 *  bucket is called Apple, Apple Inc. is one of its two members, and the
 *  sentence has to be built from the BUCKET'S name, not from what was
 *  typed.
 *
 *  A sentence is not enough either. The reader who typed "Apple Inc." meant
 *  Apple Inc., and until the type travelled with the search there was
 *  nowhere in the product to see it alone - every spelling of it resolved
 *  back into the bucket. So this renders a CONTROL beside the sentence:
 *  "Show only Apple Inc." from inside a bucket, and "Show the bucket
 *  “Apple”" from inside a single member. Two clicks, both directions.
 *
 *  Everything the sentence needs is in the API answer (app/scope.py, "A
 *  term that names ONE entity"):
 *
 *    resolved.bucket     the bucket a bare name resolved to, with members
 *    resolved.member     which member the typed term is, and the term that
 *                        shows that member alone ("Apple Inc. (Company)")
 *    resolved.type       the type a "Name (Type)" term named
 *    resolved.in_bucket  the bucket that single entity is a member of
 *
 *  WHEN IT SAYS NOTHING. Every entity chosen from a suggestion list now
 *  carries its type, so a notice on every ordinary search would be a
 *  warning-coloured box above every result. It speaks in the two cases
 *  where the answer is not what the term says on its face: when the search
 *  was WIDENED to a bucket, and when it was deliberately NOT widened
 *  although a bucket exists. Nothing else.
 * ========================================================================== */

/* "Apple (Company)" - the same spelling the server parses back into a name
 * and a type (app/scope.py: split_entity_term). */
export function entityTerm(name, type) {
  const n = String(name || "").trim();
  const t = String(type || "").trim();
  return n && t ? `${n} (${t})` : "";
}

export function memberList(members) {
  return (members || []).map((m) => (m.type ? `${m.name} (${m.type})` : m.name));
}

/* "both members" reads better than "all 2 members", and a bucket of one is
 * not a plural at all. */
function membersWord(n) {
  if (n === 1) return "its one member";
  return n === 2 ? "both members" : `all ${n} members`;
}

/* {text, action} - the sentence and, where there is one, the control that
 * leaves the answer for the other one. `q` is the term the view searched
 * for, used when the answer does not repeat it. */
export function resolvedNotice(resolved, q) {
  const r = resolved || {};
  const term = String(r.q || q || "").trim();
  const bucket = r.bucket;
  if (bucket && bucket.name) {
    const members = memberList(bucket.members);
    const named = term.toLowerCase() === String(bucket.name).toLowerCase();
    // WHAT THE GROUPING IS CALLED IS WHAT IT IS. Connection types are
    // grouped by the Connection colours page rather than by a bucket, and a
    // sentence calling that group a bucket would send the reader to a page
    // where they will not find it. app/scope.py says which it was.
    const what = bucket.source === "colour group" ? "colour group" : "bucket";
    const text = named
      ? `“${bucket.name}” is a ${what}: ${members.join(", ")}.`
      : `“${term}” belongs to the ${what} “${bucket.name}”, so this shows `
        + `${membersWord(members.length)}: ${members.join(", ")}.`;
    // A member recorded without a type, whose name the archive gives two
    // types, has no term of its own (app/scope.py: _member_of) - then the
    // sentence stands alone rather than offering a control that would come
    // straight back to the bucket.
    const member = r.member && r.member.q
      ? { label: `Show only ${r.member.name}`, term: r.member.q,
          title: `${r.member.q} on its own, without the rest of the bucket` }
      : null;
    return { text, action: member };
  }
  if (r.type && r.in_bucket && r.in_bucket.name) {
    const shown = r.label || entityTerm(term, r.type) || term;
    return {
      text: `“${shown}” alone - it is a member of the bucket “${r.in_bucket.name}”, `
        + `which is not shown.`,
      action: { label: `Show the bucket “${r.in_bucket.name}”`, term: r.in_bucket.name,
                title: `Every member of ${r.in_bucket.name} at once` },
    };
  }
  return { text: "", action: null };
}

/* Put the sentence and its control into a box. The box is emptied and
 * hidden when there is nothing to say, so a stale notice can never survive
 * the next search and describe a term that has already been replaced. */
export function renderResolved(box, notice, onPick) {
  if (!box) return false;
  box.textContent = "";
  const text = notice && notice.text;
  if (!text) {
    box.hidden = true;
    return false;
  }
  box.appendChild(document.createTextNode(text));
  if (notice.action && typeof onPick === "function") {
    const button = document.createElement("button");
    button.type = "button";
    // no-print: on paper the sentence is the whole answer and a button is
    // an offer nobody can take up (static/css/print.css).
    button.className = "button button--secondary button--inline no-print";
    button.textContent = notice.action.label;
    if (notice.action.title) button.title = notice.action.title;
    button.addEventListener("click", () => onPick(notice.action.term));
    box.appendChild(button);
  }
  box.hidden = false;
  return true;
}

/* Search for a term as if it had been chosen from the list.
 *
 * Through the typeahead's own set(), never by writing the hidden input:
 * set() fills the value, shows the term as the placeholder AND fires
 * `typeahead:choose`, which is the event all four of these views already
 * search on. Writing the input alone would leave the field saying one thing
 * and the URL another - the one failure this dashboard's search fields were
 * built to make impossible. */
export function searchFor(input, term) {
  if (!input || !input._typeahead || !term) return false;
  input._typeahead.set(term, term);
  return true;
}
