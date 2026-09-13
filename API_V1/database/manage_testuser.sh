#!/bin/bash
# ============================================================
# Xtracting archive - a read-and-write login for testing
#
#     cd database
#     ./manage_testuser.sh
#
# Creates a role called `testuser` that may SELECT, INSERT and UPDATE in
# every schema this archive uses, prints the connection string once, and
# takes the whole thing away again when it is no longer wanted.
#
# WHAT IT IS FOR, AND WHAT IT IS NOT. It is for pointing a client at the
# archive while somebody is working on it - DBeaver, a notebook, a colleague
# writing a report, a load of test rows that will be thrown away. It is NOT
# the boundary between the services: that is init/04-roles.sh, which gives
# the dashboard and the crawler each the narrow set of rights their job
# needs and nothing else. This role is deliberately wider than either of
# them, which is exactly why it has a random password, why the password is
# shown once, and why revoking it is the second item on the menu rather
# than something to write yourself later.
#
# IT CAN READ AND WRITE ROWS, AND IT CANNOT CHANGE THE SHAPE OF ANYTHING.
# SELECT, INSERT, UPDATE and DELETE, plus the sequences behind the identity
# columns - so a test can add rows, correct them and clear them away again. It
# was SELECT, INSERT and UPDATE, which sounded careful and was not: an account
# that cannot delete cannot clean up after itself, so every row anybody tried
# out stayed in the archive for good.
#
# What it still cannot do is TRUNCATE (one statement, an exclusive lock, no way
# back) or CREATE anything - `CREATE TABLE` answers "permission denied for
# schema", which is the right answer for a login that is there to look at an
# archive rather than to change its shape. DROP OWNED BY on the way out then
# has nothing of its own to drop, which is how it should be.
#
# ANOTHER STACK: this repository is built to run several archives side by
# side (ARCHIVE_STACK in .env), so the container is found through Compose
# rather than named here. To work on a copy that uses another env file:
#
#     ENV_FILE=.env.test ./manage_testuser.sh
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SELF="$(basename "$0")"

fail() { echo "$SELF: $*" >&2; exit 1; }
log()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# A PASSWORD GOES TO THE TERMINAL THAT ASKED FOR IT, AND NOWHERE ELSE.
# Everything else prints through log(), which is what somebody redirects into
# a file when they want a record of what was done - and a password in a file
# is a password in a backup, in a diff, and in whatever reads the backup
# next. With no terminal to write to it falls back to stdout, because the
# alternative is creating a login nobody can read the password of.
#
# THE TEST OPENS THE DEVICE INSTEAD OF ASKING ABOUT THE FILE. `[ -w /dev/tty ]`
# answers yes to everybody: the node is world-writable, so it reports on the
# permissions of a file rather than on whether this process has a controlling
# terminal at all. Under cron, under a systemd unit or under `setsid` there is
# none, the redirect then fails with "No such device or address", and `set -e`
# would end the script on the line after the role was created - leaving a login
# whose password nobody has ever seen. Opening it for writing is the only test
# that answers the question actually being asked.
secret_out() {
    if { true > /dev/tty; } 2>/dev/null; then printf '%s\n' "$*" > /dev/tty
    else printf '%s\n' "$*"
    fi
}

#: The role this script owns. One name, in one place: everything below is
#: written so that changing it here is the whole change.
ROLE="testuser"

#: Every schema the archive uses. `processed_data` is the archive itself;
#: `scraper`, `scraper_config` and `monitoring` arrive with init/03-scraper.sql
#: and `dashboard` with the dashboard's own first start, so an archive without
#: the crawler tables simply has fewer of them - each is checked below rather
#: than assumed.
SCHEMAS=(processed_data scraper scraper_config monitoring dashboard)

# ── Which archive, and in which container ────────────────────
#
# TWO VALUES ARE READ OUT OF `.env`, AND NOTHING IS EXPORTED FROM IT.
#
# Not `set -a; . "$ENV_FILE"; set +a`, which is the obvious way and breaks
# Compose. Compose reads the same file, but a variable already in the
# ENVIRONMENT beats the one in the file - so a password the shell cannot
# parse the way dotenv does (a `#`, a quote, a `$` in it) arrives at Compose as
# an EMPTY value, and every command dies with
#
#     required variable POSTGRES_PASSWORD is missing a value: set it in .env
#
# on an archive whose .env sets it perfectly well. It cost an afternoon on one
# installation and worked on the next, which is the worst shape a bug can have.
#
# So: pull out the two values this script needs for psql, by name, and leave
# the file to Compose. Nothing else is read, and the password never passes
# through this script at all - it does not need to, because psql runs INSIDE
# the container over the local socket.
env_value() {
    [ -f "$ENV_FILE" ] || return 0
    # The last assignment wins, as dotenv does; surrounding quotes come off;
    # `export FOO=` is accepted because people write it.
    sed -n "s/^[[:space:]]*\\(export[[:space:]]\\{1,\\}\\)\\{0,1\\}$1=//p" "$ENV_FILE" \
        | tail -n1 \
        | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/.env}"
POSTGRES_USER="${POSTGRES_USER:-$(env_value POSTGRES_USER)}"
POSTGRES_USER="${POSTGRES_USER:-xtracting}"
POSTGRES_DB="${POSTGRES_DB:-$(env_value POSTGRES_DB)}"
POSTGRES_DB="${POSTGRES_DB:-xtracting_archive}"
POSTGRES_PORT="${POSTGRES_PORT:-$(env_value POSTGRES_PORT)}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"

[ "$ROLE" != "$POSTGRES_USER" ] || fail "$ROLE must not be POSTGRES_USER - the owner is never a test login"

compose() {
    if [ -f "$ENV_FILE" ]; then
        (cd "$SCRIPT_DIR" && docker compose --env-file "$ENV_FILE" "$@")
    else
        (cd "$SCRIPT_DIR" && docker compose "$@")
    fi
}

# THE CONTAINER IS FOUND, NOT NAMED. `xtracting_db` is the SERVICE, and the
# container it runs in is called after ARCHIVE_STACK - so a machine running
# the everyday archive and a test copy has two of them, and a name written
# into this script would sooner or later be the wrong one.
CONTAINER="$(compose ps -q xtracting_db 2>/dev/null | head -n1 || true)"
[ -n "$CONTAINER" ] || fail "no running xtracting_db container for $ENV_FILE - start the archive with 'docker compose up -d' in $SCRIPT_DIR, or point ENV_FILE at the copy you mean"

# SQL ON STDIN RATHER THAN IN -c, which is init/04-roles.sh's rule: psql does
# not substitute :'variables' inside -c, and a password that reached the
# server as SQL instead of as a literal would be a very bad afternoon.
#
# -q AS WELL AS -tA. -tA takes away the column header and the "(1 row)" footer,
# but NOT the command tag psql prints after a statement that changed something,
# so a CREATE ROLE or a GRANT still answers with a line of its own. Nothing
# here reads one back today - every captured value is a count - and that is
# exactly why it is silenced now, before a later `x="$(run_sql ...)"` around
# something other than a SELECT quietly picks the tag up as part of its answer.
run_sql()        { docker exec -i "$CONTAINER" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -qtA "$@"; }
run_sql_pretty() { docker exec -i "$CONTAINER" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 "$@"; }

role_exists() {
    local count
    count="$(run_sql -v r="$ROLE" <<'SQL'
SELECT count(*) FROM pg_roles WHERE rolname = :'r';
SQL
)" || fail "could not ask whether the role $ROLE exists"
    [ "$count" = "1" ]
}

# ── Create ───────────────────────────────────────────────────

create_testuser() {
    local password verb schema present list quoted
    # 24 characters out of base64, and `cut` rather than `head -c`: head
    # closes the pipe on the character it wants, and `set -o pipefail` would
    # then see the SIGPIPE'd `tr` as a failure and end the script without
    # saying why.
    password="$(openssl rand -base64 32 | tr -d '/+=\n' | cut -c1-24)"

    if role_exists; then
        verb="ALTER"
        log "$ROLE exists already - resetting its password and re-issuing the grants"
    else
        verb="CREATE"
    fi
    run_sql -v r="$ROLE" -v p="$password" <<SQL > /dev/null
${verb} ROLE :"r" LOGIN PASSWORD :'p';
SQL
    log "${verb} ROLE $ROLE"

    # Only the schemas that are actually there. An archive that never loaded
    # init/03-scraper.sql has no `scraper`, and a GRANT naming it would stop
    # this script half way through - with the role created and no rights.
    present=()
    for schema in "${SCHEMAS[@]}"; do
        if [ "$(run_sql -v s="$schema" <<'SQL'
SELECT count(*) FROM pg_namespace WHERE nspname = :'s';
SQL
)" = "1" ]; then
            present+=("$schema")
        else
            log "no schema '$schema' in this archive - skipped"
        fi
    done
    [ "${#present[@]}" -gt 0 ] || fail "none of the schemas this archive should have exist - is $POSTGRES_DB the right database?"
    list="$(IFS=,; echo "${present[*]}")"
    # The same names again, each in quotes, for the one place they are read as
    # values rather than as identifiers.
    quoted=""
    for schema in "${present[@]}"; do
        quoted="${quoted:+$quoted, }'$schema'"
    done

    # The heredoc is UNQUOTED so that $list expands into it, which is the one
    # place in this script where something reaches the server as SQL rather
    # than as a literal - and it is a comma-joined subset of the fixed array
    # at the top of this file, not anything a person typed.
    #
    # ALTER DEFAULT PRIVILEGES AS WELL AS GRANT ON ALL TABLES, because the two
    # answer different questions: the GRANT covers the tables that exist right
    # now, the default privileges cover the ones a later migration adds. With
    # only the first, a test login stops seeing half the archive the week
    # after the next schema change, and the error names a table rather than
    # the grant that is missing.
    run_sql -v r="$ROLE" -v owner="$POSTGRES_USER" -v dbname="$POSTGRES_DB" <<SQL > /dev/null
GRANT CONNECT, TEMPORARY ON DATABASE :"dbname" TO :"r";

GRANT USAGE ON SCHEMA $list TO :"r";
-- DELETE IS IN THE LIST, TRUNCATE IS NOT, AND NEITHER IS AN OVERSIGHT.
-- This account is asked for with "read and write", and an account that can
-- INSERT but not DELETE cannot clear up after itself: every row somebody tries
-- out stays in the archive for good, which is the opposite of what a test
-- login is for. TRUNCATE is a different thing - it empties a table in one
-- statement, takes an exclusive lock and cannot be undone row by row - and
-- nothing a person is trying out needs it.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA $list TO :"r";
-- Without USAGE on the sequences every INSERT fails with "permission denied
-- for sequence", a message that does not name the table - and one looks for
-- the fault in the table grants.
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA $list TO :"r";

ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA $list
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"r";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA $list
    GRANT USAGE, SELECT ON SEQUENCES TO :"r";
SQL
    log "granted SELECT, INSERT, UPDATE on $list"

    # The dashboard's tables are created by the dashboard ROLE where the
    # optional application roles are in use (init/04-roles.sh), so the default
    # privileges that will cover its next migration are that role's and not
    # the owner's. Only when that role is configured and actually exists.
    if [ -n "${DASHBOARD_DB_USER:-}" ] && [ "$(run_sql -v r="$DASHBOARD_DB_USER" <<'SQL'
SELECT count(*) FROM pg_roles WHERE rolname = :'r';
SQL
)" = "1" ]; then
        run_sql -v r="$ROLE" -v dash="$DASHBOARD_DB_USER" <<'SQL' > /dev/null
ALTER DEFAULT PRIVILEGES FOR ROLE :"dash" IN SCHEMA dashboard
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"r";
ALTER DEFAULT PRIVILEGES FOR ROLE :"dash" IN SCHEMA dashboard
    GRANT USAGE, SELECT ON SEQUENCES TO :"r";
SQL
        log "and for what $DASHBOARD_DB_USER creates in dashboard from now on"
    fi

    # ── The verification ──
    #
    # NOT A FORMALITY. It connects the way a person will: over TCP, which is
    # the connection the password is actually checked on - the unix socket
    # inside the container trusts everybody, so a "successful" psql there
    # would prove nothing about the string printed below. Then it counts, as
    # the new role, how many tables of each schema it may read and write.
    echo ""
    if ! docker exec -e PGPASSWORD="$password" -i "$CONTAINER" \
            psql -h 127.0.0.1 -U "$ROLE" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 <<SQL
SELECT current_user AS "Connected as", inet_server_port() AS "On port";
SELECT n.nspname                                                        AS "Schema",
       count(*)                                                         AS "Tables",
       count(*) FILTER (WHERE has_table_privilege(c.oid, 'SELECT'))     AS "Can read",
       count(*) FILTER (WHERE has_table_privilege(c.oid, 'INSERT')
                          AND has_table_privilege(c.oid, 'UPDATE'))     AS "Can write"
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p') AND n.nspname IN ($quoted)
GROUP BY 1 ORDER BY 1;
SQL
    then
        fail "the role was created but could not connect with its own password - nothing below would have worked, so fix that before handing it out"
    fi

    log "connected as $ROLE and read back its rights"
    echo ""
    echo "  The password is shown once. From this machine:"
    echo ""
    secret_out "      postgresql://$ROLE:$password@127.0.0.1:$POSTGRES_PORT/$POSTGRES_DB"
    echo ""
    echo "  or:"
    echo ""
    secret_out "      psql 'postgresql://$ROLE:$password@127.0.0.1:$POSTGRES_PORT/$POSTGRES_DB'"
    echo ""
    log "run this script again and choose 2 when the login is no longer wanted"
}

# ── Revoke ───────────────────────────────────────────────────

revoke_testuser() {
    local sure sessions
    if ! role_exists; then
        log "there is no role called $ROLE - nothing to revoke"
        return 0
    fi

    sessions="$(run_sql -v r="$ROLE" <<'SQL'
SELECT count(*) FROM pg_stat_activity WHERE usename = :'r';
SQL
)"
    log "$ROLE has $sessions open session(s)"
    read -rp "Drop the role $ROLE and everything granted to it? [y/N]: " sure
    case "$sure" in
        [Yy]*) ;;
        *) log "nothing was changed"; return 0 ;;
    esac

    # THE ORDER MATTERS, and each step exists because the next one fails
    # without it. An open session holds the role, so DROP ROLE would answer
    # "role cannot be dropped because some objects depend on it" while
    # somebody's DBeaver sits idle in another window. DROP OWNED BY then
    # takes away every privilege granted to it - including the default
    # privileges above, which live in the owner's catalogue and not in the
    # role, and which would otherwise be left behind pointing at a role that
    # no longer exists.
    run_sql -v r="$ROLE" <<'SQL' > /dev/null
SELECT pg_terminate_backend(pid) FROM pg_stat_activity
 WHERE usename = :'r' AND pid <> pg_backend_pid();
DROP OWNED BY :"r";
DROP ROLE :"r";
SQL
    log "$ROLE dropped - its sessions were closed, its rights and its default privileges are gone"
}

# ── The menu ─────────────────────────────────────────────────

echo ""
echo "A test login for the archive"
echo "  archive:   $POSTGRES_DB as $POSTGRES_USER"
echo "  container: $(compose ps --format '{{.Name}}' xtracting_db 2>/dev/null | head -n1)"
echo "  role:      $ROLE ($(role_exists && echo 'exists' || echo 'not there yet'))"
echo ""
echo "  1) Create it, or reset its password"
echo "  2) Revoke it"
echo ""
read -rp "Action [1/2]: " choice
echo ""

case "$choice" in
    1) create_testuser ;;
    2) revoke_testuser ;;
    *) fail "that is not one of the two - nothing was changed" ;;
esac
