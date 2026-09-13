#!/bin/bash
# ============================================================
# Xtracting archive - the tokens that open the dashboard gate
#
#     cd database
#     ./manage_tokens.sh
#
# List them, add one, or revoke one by its id. The rows live in
# dashboard.access_tokens, and dashboard/standard/app/gate.py reads them once every
# 30 seconds - so a token added here works within half a minute, and one
# revoked here stops working within half a minute. Nothing is restarted and
# nobody else is logged out, which is the whole reason this table exists
# beside DASHBOARD_GATE_PASSWORD: changing that variable means editing a
# file on the server, restarting the container, and ending every session.
#
# A TOKEN IS A PASSWORD. It is typed into the same box, it opens the same
# dashboard, and anybody holding it sees the whole archive. Give it a label
# that says whose it is, give it an expiry, and hand it over the way you
# would hand over a password - not in an e-mail thread that outlives it.
#
# IT NEEDS THE DASHBOARD SCHEMA. `dashboard.access_tokens` is created by
# dashboard/standard/sql/01-dashboard-schema.sql, which the dashboard
# applies on start. An archive that has never run one says so below rather
# than failing with a psql error.
#
# ANOTHER STACK: this repository is built to run several archives side by
# side (ARCHIVE_STACK in .env), so the container is found through Compose
# rather than named here. To work on a copy that uses another env file:
#
#     ENV_FILE=.env.test ./manage_tokens.sh
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SELF="$(basename "$0")"

# Prefixed with the file name, like init/04-roles.sh: these scripts are run
# from a terminal that has several things going on in it.
fail() { echo "$SELF: $*" >&2; exit 1; }
log()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# A TOKEN GOES TO THE TERMINAL THAT ASKED FOR IT, AND NOWHERE ELSE. Everything
# else here prints through log(), which is what somebody redirects into a file
# when they want a record of what was done - and a token in a file is a token
# in a backup, in a diff, and in whatever reads the backup next. With no
# terminal to write to (a script, a pipe) it falls back to stdout, because the
# alternative is issuing a token nobody can read.
#
# THE TEST OPENS THE DEVICE INSTEAD OF ASKING ABOUT THE FILE. `[ -w /dev/tty ]`
# answers yes to everybody: the node is world-writable, so it reports on the
# permissions of a file rather than on whether this process has a controlling
# terminal at all. Under cron, under a systemd unit or under `setsid` there is
# none, the redirect then fails with "No such device or address", and `set -e`
# would end the script on the line after the INSERT - leaving a token in the
# table that nobody has ever seen. Opening it for writing is the only test
# that answers the question actually being asked.
secret_out() {
    if { true > /dev/tty; } 2>/dev/null; then printf '%s\n' "$*" > /dev/tty
    else printf '%s\n' "$*"
    fi
}

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

# SQL ON STDIN RATHER THAN IN -c, which is init/04-roles.sh's rule and matters
# here: psql does not substitute :'variables' inside -c, so a label with a
# quote in it would reach the server as SQL instead of as text. Everything a
# person types travels as a psql variable and arrives as a literal.
#
# -q AS WELL AS -tA, and that one is not cosmetic. -tA takes away the column
# header and the "(1 row)" footer, but NOT the command tag psql prints after a
# statement that changed something - so the id read back out of an
# INSERT ... RETURNING arrives as the number followed by a second line saying
# "INSERT 0 1", and every sentence built from it repeats the tag in the middle
# of itself. Only -q silences that.
run_sql()        { docker exec -i "$CONTAINER" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -qtA "$@"; }
run_sql_pretty() { docker exec -i "$CONTAINER" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 "$@"; }

have_table="$(run_sql <<'SQL' || true
SELECT to_regclass('dashboard.access_tokens') IS NOT NULL;
SQL
)"
[ "$have_table" = "t" ] || fail "dashboard.access_tokens does not exist in $POSTGRES_DB - start the dashboard once (it applies dashboard/sql/01-dashboard-schema.sql), or run that file by hand"

# ── The three things a person comes here to do ───────────────

list_tokens() {
    local count
    count="$(run_sql <<'SQL'
SELECT count(*) FROM dashboard.access_tokens;
SQL
)"
    if [ "$count" = "0" ]; then
        log "no tokens yet - the gate runs on DASHBOARD_GATE_PASSWORD alone"
        return 0
    fi
    # The state column answers the question somebody actually has, which is
    # not "is the switch on" but "does this still work today": a token can be
    # enabled and expired, and the two columns beside it say which.
    run_sql_pretty <<'SQL'
SELECT bigint_id                                   AS "ID",
       text_label                                  AS "Label",
       text_token                                  AS "Token",
       CASE WHEN NOT bool_enabled THEN 'disabled'
            WHEN date_expires IS NOT NULL AND date_expires <= NOW() THEN 'expired'
            ELSE 'live' END                        AS "State",
       CASE WHEN bool_enabled THEN 'yes' ELSE 'no' END AS "Enabled",
       coalesce(to_char(date_expires,   'YYYY-MM-DD'),       'never') AS "Expires",
       coalesce(to_char(date_last_used, 'YYYY-MM-DD HH24:MI'), '-')   AS "Last used"
FROM dashboard.access_tokens
ORDER BY bigint_id;
SQL
}

add_token() {
    local label months token id
    read -rp "A label for this token (whose it is): " label
    [ -n "${label//[[:space:]]/}" ] || fail "a token needs a label - it is the only thing that says whose it is when you come back to revoke it"

    read -rp "Months until it expires [6, or 0 for never]: " months
    months="${months:-6}"
    case "$months" in
        ''|*[!0-9]*) fail "the number of months has to be a whole number, or 0 for a token that never expires" ;;
    esac

    # 24 characters out of base64, which is about 140 bits - and `cut` rather
    # than `head -c`, because head closes the pipe on the character it wants
    # and `set -o pipefail` would then see the SIGPIPE'd `tr` as a failure and
    # end the script without saying why.
    token="tok_$(openssl rand -base64 32 | tr -d '/+=\n' | cut -c1-24)"

    id="$(run_sql -v tok="$token" -v lbl="$label" -v months="$months" <<'SQL'
INSERT INTO dashboard.access_tokens (text_token, text_label, date_expires)
VALUES (:'tok', :'lbl',
        CASE WHEN :'months'::int = 0 THEN NULL
             ELSE NOW() + (:'months' || ' months')::interval END)
RETURNING bigint_id;
SQL
)"
    log "token $id added for '$label'"
    if [ "$months" = "0" ]; then
        log "it never expires - revoke it here when it is no longer wanted"
    else
        log "it expires in $months month(s); after that it stops working on its own"
    fi
    echo ""
    echo "  This is the only time it is shown. Hand it over the way you would a password:"
    echo ""
    secret_out "      $token"
    echo ""
    log "the dashboard reads this table every 30 seconds, so it works within half a minute"
}

revoke_token() {
    local id label answer sure
    read -rp "Which id? (the ID column above): " id
    case "$id" in
        ''|*[!0-9]*) fail "the id is the number in the ID column - a whole number, not the token itself" ;;
    esac

    # BY ID AND NOT BY THE TOKEN STRING. Revoking is done by somebody looking
    # at the list, and asking them to paste a 28-character secret to take it
    # away invites the one typo that revokes nothing and looks like success.
    label="$(run_sql -v id="$id" <<'SQL'
SELECT text_label FROM dashboard.access_tokens WHERE bigint_id = :'id'::bigint;
SQL
)"
    [ -n "$label" ] || fail "there is no token with id $id - run this again and choose 1 to see the list"

    run_sql_pretty -v id="$id" <<'SQL'
SELECT bigint_id AS "ID", text_label AS "Label", text_token AS "Token",
       CASE WHEN bool_enabled THEN 'yes' ELSE 'no' END AS "Enabled",
       coalesce(to_char(date_expires,   'YYYY-MM-DD'),       'never') AS "Expires",
       coalesce(to_char(date_last_used, 'YYYY-MM-DD HH24:MI'), '-')   AS "Last used"
FROM dashboard.access_tokens WHERE bigint_id = :'id'::bigint;
SQL

    echo ""
    echo "  1) Disable it - it stops working, and the row keeps its label and its last use"
    echo "  2) Delete it for good"
    echo ""
    read -rp "Action [1/2]: " answer
    case "$answer" in
        1)
            run_sql -v id="$id" <<'SQL' > /dev/null
UPDATE dashboard.access_tokens SET bool_enabled = false WHERE bigint_id = :'id'::bigint;
SQL
            log "token $id ('$label') disabled - it stops working within 30 seconds"
            ;;
        2)
            # The one destructive act in this script, so it is spelled out and
            # asked for. Disabling is the answer nine times out of ten: a
            # deleted row cannot answer "when was that last used?" next week.
            read -rp "Delete token $id ('$label') for good? The label and the last-used date go with it. [y/N]: " sure
            case "$sure" in
                [Yy]*) ;;
                *) log "nothing was changed"; return 0 ;;
            esac
            run_sql -v id="$id" <<'SQL' > /dev/null
DELETE FROM dashboard.access_tokens WHERE bigint_id = :'id'::bigint;
SQL
            log "token $id ('$label') deleted - it stops working within 30 seconds"
            ;;
        *)
            log "nothing was changed"
            ;;
    esac
}

# ── The menu ─────────────────────────────────────────────────

echo ""
echo "Access tokens for the dashboard gate"
echo "  archive:   $POSTGRES_DB as $POSTGRES_USER"
echo "  container: $(compose ps --format '{{.Name}}' xtracting_db 2>/dev/null | head -n1)"
echo ""
echo "  1) List the tokens"
echo "  2) Add a token"
echo "  3) Revoke a token, by id"
echo ""
read -rp "Action [1/2/3]: " choice
echo ""

case "$choice" in
    1) list_tokens ;;
    2) add_token ;;
    3) list_tokens; revoke_token ;;
    *) fail "that is not one of the three - nothing was changed" ;;
esac
