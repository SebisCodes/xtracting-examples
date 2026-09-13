#!/bin/bash
# ============================================================
# Xtracting archive - application roles, OPT-IN
#
# Without DASHBOARD_DB_USER, DASHBOARD_DB_PASSWORD, SCRAPER_DB_USER and
# SCRAPER_DB_PASSWORD this script prints one line and does nothing. The
# archive then has a single user, the owner from POSTGRES_USER, and the
# dashboard and the crawler connect as that owner - which is fine for a
# machine you alone reach, and what every example in this repository
# assumes.
#
# Set the four variables and the boundary that 03-scraper.sql describes
# becomes one the database enforces:
#
#   DASHBOARD_DB_USER  SELECT on processed_data, monitoring and scraper;
#                      read and write in dashboard and scraper_config.
#                      Cannot touch a row of the archive, cannot touch the
#                      crawler's state.
#   SCRAPER_DB_USER    SELECT on processed_data, scraper_config and
#                      dashboard (for the pause switch in
#                      dashboard.settings); read and write in scraper;
#                      INSERT into monitoring.scraper_runs and nothing else
#                      in monitoring. Cannot change a configuration.
#
# Neither role can create objects anywhere, with one exception: the
# dashboard role OWNS the dashboard schema, because the dashboard seeds its
# own tables and refreshes its own materialised view on every start, and
# both need ownership. The schema is created here, empty, so the
# ownership is settled before the dashboard's first start; if it already
# exists - the roles are being added to an archive the dashboard has been
# using as the owner - its objects are handed over below.
#
# Neither role is the owner. POSTGRES_USER stays what init/ and psql use.
#
# APPLIES TO AN EXISTING ARCHIVE TOO, by hand:
#
#     docker compose exec -T \
#       -e DASHBOARD_DB_USER=... -e DASHBOARD_DB_PASSWORD=... \
#       -e SCRAPER_DB_USER=... -e SCRAPER_DB_PASSWORD=... \
#       xtracting_db bash /docker-entrypoint-initdb.d/04-roles.sh
#
# A second run finds the roles, resets their passwords and re-issues the
# grants. Nothing here is lost by running it twice.
#
# A shell script and not .sql because the passwords come from the
# environment. The image's entrypoint runs *.sh and *.sql in name order on
# an empty volume, so this runs after 01-03. It runs an executable script
# in its own shell and SOURCES a non-executable one into the entrypoint
# itself - where `exit` would end the entrypoint and `set -e` would change
# its behaviour. The guard right after the header re-executes this file
# whenever it finds itself sourced, so the file mode cannot matter.
#
# ON THE SPELLING: psql substitutes :'var' and :"var" everywhere EXCEPT
# inside dollar-quoted blocks ($$ ... $$), which its lexer treats as one
# token. The role names therefore go into the top-level statements as
# psql variables, and the one DO block that needs them reads them back
# through current_setting().
# ============================================================
if [ "${BASH_SOURCE[0]}" != "$0" ]; then
    bash "${BASH_SOURCE[0]}"
    return $?
fi
set -euo pipefail

fail() { echo "04-roles.sh: $*" >&2; exit 1; }

if [ -z "${DASHBOARD_DB_USER:-}" ] && [ -z "${DASHBOARD_DB_PASSWORD:-}" ] \
   && [ -z "${SCRAPER_DB_USER:-}" ] && [ -z "${SCRAPER_DB_PASSWORD:-}" ]; then
    echo "04-roles.sh: no application roles requested (DASHBOARD_DB_USER / SCRAPER_DB_USER not set) - skipped"
    exit 0
fi

# Half a configuration is a typo, not a choice. A database that comes up
# with one role and not the other would show up as an authentication error
# in the container that lost out, which looks like a wrong password.
[ -n "${DASHBOARD_DB_USER:-}" ]     || fail "DASHBOARD_DB_USER is not set"
[ -n "${DASHBOARD_DB_PASSWORD:-}" ] || fail "DASHBOARD_DB_PASSWORD is not set"
[ -n "${SCRAPER_DB_USER:-}" ]       || fail "SCRAPER_DB_USER is not set"
[ -n "${SCRAPER_DB_PASSWORD:-}" ]   || fail "SCRAPER_DB_PASSWORD is not set"
[ -n "${POSTGRES_USER:-}" ]         || fail "POSTGRES_USER is not set"
[ -n "${POSTGRES_DB:-}" ]           || fail "POSTGRES_DB is not set"

[ "$DASHBOARD_DB_USER" != "$POSTGRES_USER" ] \
    || fail "DASHBOARD_DB_USER must not be POSTGRES_USER - the owner is never an application role"
[ "$SCRAPER_DB_USER" != "$POSTGRES_USER" ] \
    || fail "SCRAPER_DB_USER must not be POSTGRES_USER - the owner is never an application role"
[ "$DASHBOARD_DB_USER" != "$SCRAPER_DB_USER" ] \
    || fail "DASHBOARD_DB_USER and SCRAPER_DB_USER must differ - one role on each side of the boundary"

PSQL=(psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB")

# The name travels as a LITERAL (:'r'), never as an identifier, so a name
# with odd characters is harmless here. Through stdin and not -c: psql does
# not interpolate variables in -c, the query would reach the server with
# a literal :'r' and fail - and inside an `if`, set -e would not notice.
role_exists() {
    local count
    count=$("${PSQL[@]}" -tA -v r="$1" <<'SQL'
SELECT count(*) FROM pg_roles WHERE rolname = :'r';
SQL
    ) || fail "could not check whether role '$1' exists"
    [ "$count" = "1" ]
}

create_or_update_role() {
    local user="$1" pw="$2" verb
    if role_exists "$user"; then verb=ALTER; else verb=CREATE; fi
    "${PSQL[@]}" -v u="$user" -v p="$pw" <<SQL
${verb} ROLE :"u" LOGIN PASSWORD :'p';
SQL
    echo "04-roles.sh: ${verb} ROLE ${user}"
}

create_or_update_role "$DASHBOARD_DB_USER" "$DASHBOARD_DB_PASSWORD"
create_or_update_role "$SCRAPER_DB_USER"   "$SCRAPER_DB_PASSWORD"

"${PSQL[@]}" \
    -v dashboard_user="$DASHBOARD_DB_USER" \
    -v crawler_user="$SCRAPER_DB_USER" \
    -v owner="$POSTGRES_USER" \
    -v dbname="$POSTGRES_DB" <<'SQL'

-- Nobody but the owner creates objects, and nobody connects without being
-- named here. TEMP is granted because a temporary table is not a write
-- into the archive, and refusing one buys nothing.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON DATABASE :"dbname" FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE :"dbname" TO :"dashboard_user";
GRANT CONNECT, TEMPORARY ON DATABASE :"dbname" TO :"crawler_user";

-- ---------- The dashboard schema, owned by the dashboard role ----------
--
-- Created empty if the dashboard has not run yet. If it has, the schema
-- and what it holds belong to the owner, and the dashboard role could
-- neither add its tables nor REFRESH its materialised view - so the
-- ownership is handed over, object by object. The DO block cannot see
-- psql variables; the role name reaches it as a session setting.
CREATE SCHEMA IF NOT EXISTS dashboard;
ALTER SCHEMA dashboard OWNER TO :"dashboard_user";
SELECT set_config('roles.dashboard_user', :'dashboard_user', false);
DO $$
DECLARE
    r RECORD;
    new_owner TEXT := current_setting('roles.dashboard_user');
BEGIN
    FOR r IN
        SELECT c.relkind, c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'dashboard'
          AND c.relkind IN ('r', 'S', 'v', 'm')
          AND pg_get_userbyid(c.relowner) <> new_owner
    LOOP
        EXECUTE format('ALTER %s dashboard.%I OWNER TO %I',
                       CASE r.relkind WHEN 'r' THEN 'TABLE'
                                      WHEN 'S' THEN 'SEQUENCE'
                                      WHEN 'v' THEN 'VIEW'
                                      ELSE 'MATERIALIZED VIEW' END,
                       r.relname, new_owner);
    END LOOP;
END
$$;

-- ---------- Dashboard: read the archive and the crawler's state ----------
GRANT USAGE ON SCHEMA processed_data, monitoring, scraper TO :"dashboard_user";
GRANT SELECT ON ALL TABLES IN SCHEMA processed_data TO :"dashboard_user";
GRANT SELECT ON ALL TABLES IN SCHEMA monitoring     TO :"dashboard_user";
GRANT SELECT ON ALL TABLES IN SCHEMA scraper        TO :"dashboard_user";
-- Also for tables a later migration adds. Without this every migration
-- needs a GRANT after it, which is forgotten exactly once.
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA processed_data
    GRANT SELECT ON TABLES TO :"dashboard_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA monitoring
    GRANT SELECT ON TABLES TO :"dashboard_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA scraper
    GRANT SELECT ON TABLES TO :"dashboard_user";

-- The one exception to "the dashboard only reads what the crawler writes":
-- deleting an old project is the customer's act, not the crawler's. Deleting
-- the project row takes its watchlists and rules with it through
-- scraper_config; the archive keeps every document, and the project stays
-- selectable everywhere, because that list is built from
-- processed_data.tasks.text_project and not from this table.
--
-- DELETE and no more. The dashboard still cannot write a project or a key: it
-- has no way to invent one, and a registry it could write into would stop being
-- a record of what the keys actually answered.
GRANT DELETE ON scraper.projects TO :"dashboard_user";

-- ---------- Dashboard: write the configuration ----------
--
-- Rows, not structure: SELECT, INSERT, UPDATE, DELETE and the sequences
-- behind the identity columns. Without USAGE on the sequences every
-- INSERT fails with "permission denied for sequence", a message that does
-- not name the table - one looks for the fault in the table grants.
GRANT USAGE ON SCHEMA scraper_config TO :"dashboard_user";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA scraper_config TO :"dashboard_user";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA scraper_config TO :"dashboard_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA scraper_config
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"dashboard_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA scraper_config
    GRANT USAGE, SELECT ON SEQUENCES TO :"dashboard_user";

-- ---------- Crawler: read the configuration and the archive ----------
GRANT USAGE ON SCHEMA processed_data, scraper_config, dashboard TO :"crawler_user";
GRANT SELECT ON ALL TABLES IN SCHEMA processed_data TO :"crawler_user";
GRANT SELECT ON ALL TABLES IN SCHEMA scraper_config TO :"crawler_user";
GRANT SELECT ON ALL TABLES IN SCHEMA dashboard      TO :"crawler_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA processed_data
    GRANT SELECT ON TABLES TO :"crawler_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA scraper_config
    GRANT SELECT ON TABLES TO :"crawler_user";
-- The one exception to "the crawler only reads the configuration": a manual
-- run is a job the dashboard writes and the crawler carries out, so the
-- crawler has to be able to say where it got to. It writes nothing else here.
GRANT UPDATE ON scraper_config.manual_runs TO :"crawler_user";
-- The dashboard's tables are created by the dashboard role, so the
-- default privileges that cover them are that role's, not the owner's.
ALTER DEFAULT PRIVILEGES FOR ROLE :"dashboard_user" IN SCHEMA dashboard
    GRANT SELECT ON TABLES TO :"crawler_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA dashboard
    GRANT SELECT ON TABLES TO :"crawler_user";

-- ---------- Crawler: its own state, and its run log ----------
GRANT USAGE ON SCHEMA scraper, monitoring TO :"crawler_user";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA scraper TO :"crawler_user";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA scraper TO :"crawler_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA scraper
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"crawler_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA scraper
    GRANT USAGE, SELECT ON SEQUENCES TO :"crawler_user";
-- One table in monitoring, and only that one: the collector's run log is
-- the collector's. TimescaleDB carries a grant on the hypertable over to
-- every chunk, present and future, so this is the whole grant.
GRANT SELECT, INSERT ON monitoring.scraper_runs TO :"crawler_user";
-- And the two tables every service writes: its own liveness, and - when the
-- switch in dashboard.settings is on - what it is doing step by step.
GRANT SELECT, INSERT, UPDATE ON monitoring.heartbeat   TO :"crawler_user";
GRANT SELECT, INSERT          ON monitoring.service_log TO :"crawler_user";
GRANT USAGE, SELECT ON SEQUENCE monitoring.service_log_bigint_id_seq TO :"crawler_user";

SQL

echo "04-roles.sh: roles ${DASHBOARD_DB_USER} (dashboard) and ${SCRAPER_DB_USER} (crawler) set up"
