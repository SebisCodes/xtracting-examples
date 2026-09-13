-- ============================================================
-- Dashboard - default colour groups
--
-- Eleven groups, measured against ~9'900 real connection edges, where ten
-- semantic groups covered about 90% and the rest went to an explicit
-- "Other". "Other" is the fallback: a type nobody has assigned takes its
-- colour, so there is never an edge without one.
--
-- WHY NONE OF THEM IS RED, GREEN, VIOLET, BLUE OR GREY.
--
-- app.css reserves four hue families, one meaning each: red-to-green is
-- valence (a rating, a sentiment, an outlook), violet is relevance, blue is
-- a count, and grey is --no-data, "no reading at all". A group is none of
-- those things - it is a category somebody chose - so it may not wear their
-- colours. A red Competitor 1.28:1 from --valence-neg-1, a green Partner
-- 1.14:1 from --valence-pos-1, a violet Investor, a blue Supplier, and an
-- Ownership and an Other in two greys that meet --no-data on one screen
-- (Diagrams -> Connections draws "by colour group" one card above another
-- chart) would all break that. Two greys 1.72:1 apart carrying two
-- meanings side by side is exactly what the rule forbids.
--
-- The groups therefore live in the hues nothing else claims - teal, amber
-- and brown, olive, magenta and rose - and are told apart from each other
-- by lightness within a hue. tests/unit/test_categorical_palette.py
-- measures all of it and explains why "far enough from every step of every
-- scale" cannot be a contrast number alone.
--
-- Every colour still clears 3:1 against white (WCAG 1.4.11, non-text
-- contrast) - lines and swatches sit on a white page - and every one of
-- them takes a text colour at 4.5:1, which app/colours.py works out.
--
-- ON CONFLICT DO NOTHING on the key, so a customer's renamed or recoloured
-- group survives every restart. This file only ever adds what is missing.
-- ============================================================

INSERT INTO dashboard.colour_groups
    (text_key, text_name, text_colour, text_description, integer_sort, bool_fallback)
VALUES
    ('competitor', 'Competitor',        '#BB1B60', 'Rivalry, dispute, sanction, attack',              10, false),
    ('regulator',  'Regulator / Media', '#144143', 'Authorities, courts, government, the press',      20, false),
    ('investor',   'Investor',          '#BB1BAB', 'Capital: shareholders, lenders, funding',         30, false),
    ('person',     'Person / Role',     '#74116A', 'People and the roles they have',                  40, false),
    ('ownership',  'Ownership / Group', '#43280A', 'Parent, subsidiary, owner, acquisition',          50, false),
    ('customer',   'Customer',          '#620E32', 'Receives a product or service',                   60, false),
    ('supplier',   'Supplier',          '#30A0A6', 'Provides a product or service',                   70, false),
    ('partner',    'Partner',           '#7E9816', 'Cooperation, alliance, membership',               80, false),
    ('product',    'Product',           '#206B6F', 'Products, brands, technology',                    90, false),
    ('location',   'Location',          '#455214', 'Geographic belonging: country, city, site',      100, false),
    ('other',      'Other',             '#7D4B12', 'Everything not assigned to another group',       110, true)
ON CONFLICT (text_key) DO NOTHING;

-- ── The crawler starts switched off ──────────────────────────
--
-- ON CONFLICT DO NOTHING, so this decides the DEFAULT and never overrides a
-- choice somebody has made: once the switch has been touched the row exists
-- and this line does nothing for the life of the archive.
--
-- Off, because a freshly installed archive knows nothing about the sites it
-- is pointed at - whether they answer, what their list pages look like, how
-- much a page yields. A crawler that starts fetching and submitting the
-- moment it is deployed spends money on all of that before anybody has
-- looked at one page. The way in is the Test button on one watchlist row at
-- a time, and then the switch.
--
INSERT INTO dashboard.settings (text_key, text_value)
VALUES ('scraper.paused', 'true')
ON CONFLICT (text_key) DO NOTHING;


-- ── Step logging, off ────────────────────────────────────────────────────
--
-- Each service writes one row per step into monitoring.service_log while its
-- switch is on, and nothing at all while it is off. Off is the default for the
-- same reason the pause switch is on: a setting that costs disk while nobody is
-- watching should have to be asked for.
--
-- A change takes effect on the service's NEXT pass - the crawler reads this on
-- the tick where it already reads the pause, the collector at the start of a
-- round - because a service that re-read it mid-round would write half a round
-- and leave the reader wondering about the other half.
INSERT INTO dashboard.settings (text_key, text_value)
VALUES ('crawler.debug', 'false'),
       ('collector.debug', 'false')
ON CONFLICT (text_key) DO NOTHING;
