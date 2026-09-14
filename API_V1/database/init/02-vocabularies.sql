-- ============================================================
-- Fixed vocabularies
--
-- The closed sets the extraction chooses from, seeded so a dashboard can join
-- against them and order by float_value instead of by alphabet. Open
-- vocabularies - entity types, units, perspectives - are not seeded: the
-- collector inserts those as they first appear, because they are yours.
--
-- float_value exists to make "worse than" a comparison rather than a lookup
-- table in application code. Negative is bad, positive is good, zero is
-- neutral, and Unset is deliberately absent from the scales rather than zero -
-- "the source says nothing" is not the same as "the source says neutral".
-- ============================================================

INSERT INTO processed_data.rating_values (text_name, float_value) VALUES
    ('Egregious', -3.0),
    ('Very Bad',  -2.0),
    ('Bad',       -1.0),
    ('Neutral',    0.0),
    ('Good',       1.0),
    ('Very Good',  2.0),
    ('Excellent',  3.0)
ON CONFLICT DO NOTHING;

INSERT INTO processed_data.importance_types (text_name, float_value, text_description) VALUES
    ('Not Important',       0.0, 'The source does not matter for this perspective'),
    ('Low Importance',      1.0, 'Marginally relevant'),
    ('Medium Importance',   2.0, 'Relevant'),
    ('High Importance',     3.0, 'Clearly relevant'),
    ('Critical Importance', 4.0, 'Decisive for this perspective')
ON CONFLICT DO NOTHING;

-- Which way a market insight points, over each horizon.
INSERT INTO processed_data.outlook_types (text_name, text_description) VALUES
    ('Rising',    'Pointing upward'),
    ('Declining', 'Pointing downward'),
    ('Neutral',   'No direction indicated'),
    ('Unset',     'The source says nothing either way')
ON CONFLICT DO NOTHING;

-- How strongly a source read, over each horizon. Note that this describes the
-- reporting, not a market - and never an instruction to do anything.
INSERT INTO processed_data.sentiment_types (text_name, text_description) VALUES
    ('Very Negative',     'Strongly negative reporting'),
    ('Negative',          'Negative reporting'),
    ('Slightly Negative', 'Mildly negative reporting'),
    ('Neutral',           'Balanced reporting'),
    ('Slightly Positive', 'Mildly positive reporting'),
    ('Positive',          'Positive reporting'),
    ('Very Positive',     'Strongly positive reporting'),
    ('Unset',             'The source says nothing either way')
ON CONFLICT DO NOTHING;

-- How directly a source supports the structured extraction built from it.
INSERT INTO processed_data.relation_types (text_name, text_description) VALUES
    ('Direct',    'The source states this'),
    ('Indirect',  'The source implies this'),
    ('Unrelated', 'Carried along, not supported by this source')
ON CONFLICT DO NOTHING;

INSERT INTO processed_data.trustlist_types (text_name) VALUES
    ('Trusted'), ('Unknown'), ('Untrusted')
ON CONFLICT DO NOTHING;
