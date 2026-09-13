"""Which colour a connection type gets, and a helper that guesses a group.

Connection type names are free text from the extraction: "Supplier",
"supplier", "Former Supplier", "Lieferant", "manufactured-by" and thousands
more. A real archive holds tens of thousands of distinct ones, so the map and
the graph colour them by GROUP - a dozen categories with one colour each -
and the assignment of type to group is a table (dashboard.colour_group_types)
the customer edits on a settings page.

Two things live here, and they must not be confused:

  ColourResolver   the RUNTIME lookup. A dict built from the table, refreshed
                   every 60 seconds or when a settings page says so. It never
                   guesses: a type without a row gets the fallback group. The
                   same type must come out in the same colour on every view,
                   and a regex that is "improved" one day would break that.

  suggest_group()  the keyword helper. It runs ONCE on first seed over the types
                   already in the archive (rows marked 'suggested') and behind
                   the "Suggest" button on the settings page, where the result
                   is shown but not saved until the person says so.

Colours are checked for at least 3:1 contrast against white (WCAG 1.4.11):
edges and swatches sit on a white page, and a line that cannot be told from
the background is not a legend entry but a rumour.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

FALLBACK_KEY = "other"
# What an unassigned connection type is drawn in. Named here because four
# places need it - this module, the graph endpoint, and the two scripts that
# draw before the server has answered - and four literals drift.
#
# IT IS NOT GREY, and that is the point of its being a dark brown. Grey is
# --no-data in app.css and means "no reading at all"; a type nobody has
# grouped yet is a reading, so painting it grey put two meanings on one
# colour, side by side on the Connections tab (see tests/unit/
# test_categorical_palette.py).
FALLBACK_COLOUR = "#7D4B12"
MIN_CONTRAST_ON_WHITE = 3.0

# ── Suggestions ──────────────────────────────────────────────
#
# Order matters: the first matching rule wins, specific before generic, so
# "Competitive Partnership" is a rivalry and not a partnership, and
# "Investment Partner" is about capital. The patterns are the previous
# dashboard's CONN_SCALE, measured there against ~9'900 real edges (tests/unit/
# conn-type-corpus.json), with the group keys translated to English. They are
# matched against the lower-cased type name.

SUGGESTION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("competitor", re.compile(
        r"competit|rival|adversar|aggressor|attack|opponent|antagonist|threat|challeng|hostile"
        r"|dispute|conflict|sanction|tariff|boycott|\bwar\b|invasion|invader|invades|enemy|victim"
        r"|accused|accuser|critic|\btarget\b|bans?\b|blocks?\b|restrict|protest|controvers|seiz"
        r"|blockad|espionage|fraud|breach")),
    ("regulator", re.compile(
        r"regulat|government|authorit|agency|ministr|court|judicial|legal|lawsuit|litig|plaintiff"
        r"|defendant|prosecut|attorney|law enforcement|police|\btax\b|customs|compliance|oversight"
        r"|supervis|watchdog|journalist|media|press\b|reporter|news|publication|broadcaster"
        r"|auditor|inspector|president|prime minister|minister|governor|senator|politician"
        r"|head of state|parliament|legislat|governing body|state actor|governs|central bank"
        r"|issuer|political entity|federal|municipal|diplomat|embassy|treaty body")),
    ("investor", re.compile(
        r"invest|shareholder|stakeholder|stake in|financ|fund|capital|lender|creditor|debtor"
        r"|loan|underwrit|valuation|\bipo\b|\bbonds\b|bondholder|bond issue|bond financing|sponsor")),
    ("person", re.compile(
        r"\bceo\b|\bcto\b|\bcfo\b|founder|employee|employer|executive|director|chairman"
        r"|chairperson|manager|staff|athlete|player|artist|actor|musician|citizen|candidate"
        r"|ambassador|attendee|spokesperson|official|leader|\bhead\b|represent|appointe"
        r"|successor|predecessor|\bperson\b")),
    ("ownership", re.compile(
        r"parent|mutter|tochter|subsidiar|owner|owned|owns|acquir|acquisit|merge|holding|part of|division"
        r"|branch|affiliate|conglomerate|spin-?off|controls|controlled by|includes|comprises"
        r"|consists|belongs")),
    ("customer", re.compile(
        r"client|customer|consumer|buyer|purchas|\buser\b|utiliz|\buses\b|used by|recipient"
        r"|subscriber|tenant|patient|passenger|audience|licensee|borrower|adopt")),
    ("supplier", re.compile(
        r"supplier|suppl|manufactur|vendor|seller|\bsells\b|distribut|resell|retail|provider"
        r"|provides|operator|operate|publisher|publish|contractor|producer|produce|developer"
        r"|develop|builder|construct|installer|maintain|\bhost|carrier|logistics|shipper"
        r"|export|import|licensor|creator|designer|author|source of|research|innovat"
        r"|\bmakes?\b|built by|made by")),
    ("partner", re.compile(
        r"partner|\bally\b|allies|alliance|collab|cooperat|joint|consortium|member|participant"
        r"|organiz|affiliat|associat|supporter|supported by|contributor|works with|signator"
        r"|counterpart|network|agreement|treaty|\bpact\b|negotiat|proposer|propose|initiator"
        r"|initiate|influencer|influence|supports|support from|mediator|facilit|advis|mentor"
        r"|donor|beneficiar|neighbor|neighbour")),
    ("product", re.compile(
        r"product|brand|model|technolog|software|hardware|platform|service|system|application"
        r"|\bapp\b|tool|device|component|material|ingredient|feature|version|solution|offering"
        r"|portfolio|smartphone|vehicle|\bdrug\b|chemical|\bfood\b|energy|commodity|\bport\b"
        r"|infrastructure|asset")),
    ("location", re.compile(
        r"country|nation|region|city|town|\bstate\b|province|county|district|continent"
        r"|location|located|headquart|based in|\bsite\b|facility|plant|office|territor|union"
        r"|\barea\b|\bzone\b|\bmarket\b|domicile")),
)

SUGGESTION_KEYS = tuple(key for key, _ in SUGGESTION_RULES) + (FALLBACK_KEY,)


def suggest_group(type_name: str | None) -> str:
    """The group key the keyword rules propose for a type name; FALLBACK_KEY
    when nothing matches. Never None, so a caller can always look it up.

    >>> suggest_group("Supplier"), suggest_group("Competitive Partnership")
    ('supplier', 'competitor')
    >>> suggest_group("Zzz Qqq"), suggest_group(""), suggest_group(None)
    ('other', 'other', 'other')
    """
    text = (type_name or "").strip().lower()
    if text:
        for key, pattern in SUGGESTION_RULES:
            if pattern.search(text):
                return key
    return FALLBACK_KEY


# ── Contrast ─────────────────────────────────────────────────

def _channel(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    if len(h) != 6:
        raise ValueError(f"not a six-digit hex colour: {hex_colour!r}")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast_ratio(a: str, b: str) -> float:
    """WCAG contrast ratio between two hex colours, 1.0 to 21.0.

    >>> round(contrast_ratio("#000000", "#ffffff"), 1)
    21.0
    >>> contrast_ratio("#BB1B60", "#ffffff") >= 3.0
    True
    """
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def text_colour_for(background: str) -> str:
    """Dark or light text on a swatch of that colour - whichever contrasts
    more, so a label is readable on amber as well as on slate."""
    return "#212121" if contrast_ratio(background, "#212121") >= contrast_ratio(background, "#ffffff") else "#ffffff"


def is_valid_hex(value: str | None) -> bool:
    return bool(value) and re.fullmatch(r"#[0-9A-Fa-f]{6}", value) is not None


# ── The runtime lookup ───────────────────────────────────────

@dataclass(frozen=True)
class Group:
    id: int
    key: str
    name: str
    colour: str
    description: str
    sort: int
    fallback: bool

    @property
    def text_colour(self) -> str:
        return text_colour_for(self.colour)

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "key": self.key, "name": self.name, "colour": self.colour,
                "text_colour": self.text_colour, "description": self.description,
                "sort": self.sort, "fallback": self.fallback}


@dataclass(frozen=True)
class Colour:
    type_name: str
    group: Group
    # 'manual' | 'suggested' from the table; 'fallback' when no row exists.
    source: str

    @property
    def colour(self) -> str:
        return self.group.colour

    @property
    def group_key(self) -> str:
        return self.group.key

    def as_dict(self) -> dict[str, Any]:
        return {"type": self.type_name, "group": self.group.key, "group_name": self.group.name,
                "colour": self.group.colour, "text_colour": self.group.text_colour,
                "source": self.source}


# What a loader returns: the groups and the type assignments, as row dicts
# with the column names of the two tables.
Loader = Callable[[], tuple[list[dict[str, Any]], list[dict[str, Any]]]]

# When the table is unreachable or empty, this is what an edge is drawn in.
# Same colour as the seeded "Other" group, so the page does not change colour
# once the database answers.
_BUILTIN_FALLBACK = Group(id=0, key=FALLBACK_KEY, name="Other", colour=FALLBACK_COLOUR,
                          description="Everything not assigned to another group",
                          sort=110, fallback=True)


class ColourResolver:
    """Type name -> Colour, from a table, cached for `ttl` seconds.

    Lookups are exact first and case-insensitive second, so "supplier" follows
    "Supplier" unless somebody assigned the lower-cased spelling separately.
    """

    def __init__(self, loader: Loader, ttl: float = 60.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._loader = loader
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._loaded_at: float | None = None
        self._groups: list[Group] = []
        self._by_key: dict[str, Group] = {}
        self._exact: dict[str, tuple[Group, str]] = {}
        self._lower: dict[str, tuple[Group, str]] = {}
        self._fallback: Group = _BUILTIN_FALLBACK

    # -- loading --------------------------------------------------------

    def invalidate(self) -> None:
        """Forget the cache; the next lookup reloads. Called by the settings
        API after every write so a change shows on the next request."""
        with self._lock:
            self._loaded_at = None

    def _fresh(self) -> bool:
        return self._loaded_at is not None and self._clock() - self._loaded_at < self._ttl

    def _ensure(self) -> None:
        with self._lock:
            if self._fresh():
                return
            try:
                group_rows, type_rows = self._loader()
            except Exception:
                # Keep whatever was loaded before; a colour that is a minute
                # old beats a map with no colours.
                log.exception("loading colour groups failed; keeping the previous table")
                self._loaded_at = self._clock()
                return
            self._apply(group_rows, type_rows)
            self._loaded_at = self._clock()

    def _apply(self, group_rows: list[dict[str, Any]], type_rows: list[dict[str, Any]]) -> None:
        groups = [Group(id=int(r["bigint_id"]), key=r["text_key"], name=r["text_name"],
                        colour=r["text_colour"], description=r.get("text_description") or "",
                        sort=int(r.get("integer_sort") or 0), fallback=bool(r.get("bool_fallback")))
                  for r in group_rows]
        groups.sort(key=lambda g: (g.sort, g.name))
        by_id = {g.id: g for g in groups}
        fallback = next((g for g in groups if g.fallback), None) or _BUILTIN_FALLBACK
        exact: dict[str, tuple[Group, str]] = {}
        lower: dict[str, tuple[Group, str]] = {}
        for r in type_rows:
            g = by_id.get(int(r["bigint_fk_group"]))
            if g is None:
                continue
            name = r["text_type_name"]
            source = r.get("text_source") or "manual"
            exact[name] = (g, source)
            # First assignment wins for the case-insensitive spelling; an
            # exact row for the other spelling still takes precedence above.
            lower.setdefault(name.lower(), (g, source))
        self._groups = groups
        self._by_key = {g.key: g for g in groups}
        self._exact, self._lower, self._fallback = exact, lower, fallback

    # -- lookups --------------------------------------------------------

    def resolve(self, type_name: str | None) -> Colour:
        self._ensure()
        name = (type_name or "").strip()
        hit = self._exact.get(name) or self._lower.get(name.lower())
        if hit:
            group, source = hit
            return Colour(name, group, source)
        return Colour(name, self._fallback, "fallback")

    def colour_of(self, type_name: str | None) -> str:
        return self.resolve(type_name).colour

    def group_by_key(self, key: str) -> Group | None:
        self._ensure()
        return self._by_key.get(key)

    @property
    def fallback(self) -> Group:
        self._ensure()
        return self._fallback

    def groups(self) -> list[Group]:
        self._ensure()
        return list(self._groups)

    def legend(self) -> list[dict[str, Any]]:
        """Every group in display order - what a legend panel shows."""
        return [g.as_dict() for g in self.groups()]

    def assigned_types(self) -> dict[str, str]:
        """type name -> group key for every row in the table."""
        self._ensure()
        return {name: g.key for name, (g, _) in self._exact.items()}


# ── The database side ────────────────────────────────────────

GROUPS_SQL = ("SELECT bigint_id, text_key, text_name, text_colour, text_description, "
              "integer_sort, bool_fallback FROM dashboard.colour_groups ORDER BY integer_sort, text_name")
TYPES_SQL = "SELECT text_type_name, bigint_fk_group, text_source FROM dashboard.colour_group_types"


def loader_for(db) -> Loader:
    """A loader over the application's Database (app.db.Database)."""
    def load() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        with db.read() as conn:
            groups = [dict(r) for r in conn.execute(GROUPS_SQL).fetchall()]
            types = [dict(r) for r in conn.execute(TYPES_SQL).fetchall()]
        return groups, types
    return load


_resolver: ColourResolver | None = None
_resolver_lock = threading.Lock()


def get_resolver() -> ColourResolver:
    """The one resolver the views share, built over app.db.get_db() on first use."""
    global _resolver
    with _resolver_lock:
        if _resolver is None:
            from .db import get_db
            _resolver = ColourResolver(loader_for(get_db()))
        return _resolver


def set_resolver(resolver: ColourResolver | None) -> None:
    global _resolver
    with _resolver_lock:
        _resolver = resolver


# Every column a type name can come from: both directions of a connection,
# plus the vocabulary the collector fills as names first appear.
DISTINCT_TYPES_SQL = """
    SELECT DISTINCT name FROM (
        SELECT text_name AS name FROM processed_data.connection_types
        UNION
        SELECT text_type_parent_to_child FROM processed_data.connections
        UNION
        SELECT text_type_child_to_parent FROM processed_data.connections
    ) x
    WHERE name IS NOT NULL AND btrim(name) <> ''
"""


def suggest_for_names(names: list[str], existing: set[str] | None = None) -> list[tuple[str, str]]:
    """(type name, group key) for every name the rules place in a real group.
    Names already assigned and names that only reach the fallback are left
    out - a fallback needs no row, and a decision must not be overwritten
    by a guess."""
    existing = existing or set()
    out = []
    for name in names:
        if name in existing:
            continue
        key = suggest_group(name)
        if key != FALLBACK_KEY:
            out.append((name, key))
    return out


def seed_suggested_types(conn) -> int:
    """First-seed hook (app/schema.py): one 'suggested' row per connection
    type already in the archive. Runs inside the seeding transaction and
    returns how many rows it wrote."""
    groups = {r["text_key"]: r["bigint_id"] for r in conn.execute(
        "SELECT text_key, bigint_id FROM dashboard.colour_groups").fetchall()}
    existing = {r["text_type_name"] for r in conn.execute(
        "SELECT text_type_name FROM dashboard.colour_group_types").fetchall()}
    names = [r["name"] for r in conn.execute(DISTINCT_TYPES_SQL).fetchall()]
    rows = [(name, groups[key]) for name, key in suggest_for_names(names, existing) if key in groups]
    if rows:
        conn.cursor().executemany(
            "INSERT INTO dashboard.colour_group_types (text_type_name, bigint_fk_group, text_source) "
            "VALUES (%s, %s, 'suggested') ON CONFLICT (text_type_name) DO NOTHING", rows)
    return len(rows)
