#!/usr/bin/env bash
# social board installer for Ubuntu 24.04 servers.
#
#   sudo ./install.sh                         walk through every choice
#   sudo ./install.sh --config board.conf     take answers from a preset file (asks for anything missing)
#   sudo ./install.sh --config board.conf --yes   fully unattended: missing answers use defaults
#   sudo ./install.sh --dry-run ...           show what would happen, change nothing
#
# Safe to re-run: it updates the code and settings and keeps the database and accounts.
# See install/example.conf for every preset key.
set -Eeuo pipefail

# --- fixed layout -----------------------------------------------------------------------
APP_DIR=/opt/social-board
DATA_ROOT=/var/lib/social-board
DATA_DIR=$DATA_ROOT/data
ADS_DIR=$DATA_ROOT/ads
AVATARS_DIR=$DATA_ROOT/avatars
SAVED_CONF=$DATA_ROOT/install.conf
SVC_USER=socialboard
SERVICE=social-board
DB_NAME=social_board
DB_CONTAINER=social-board-db
DB_VOLUME=social-board-pgdata
DB_IMAGE=docker.io/library/postgres:17-alpine
DB_CONTAINER_PORT=5433            # host port for the podman/docker database (127.0.0.1 only)
DEFAULT_REPO=https://github.com/ReedGraf/social-board.git
RESERVED_PREFIXES=" p img static comment settings admin live ads "   # keep in step with sb/views.py RESERVED
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

KEYS=(SITE_NAME DB_MODE DB_URL PUBLIC_DOMAIN LISTEN PORT OPEN_FIREWALL
      USER_PREFIX COMMUNITY_PREFIX CUSTOM_USER_PREFIX CUSTOM_COMMUNITY_PREFIX OPEN_REGISTRATION COMMUNITY_CREATION
      ADMIN_USER ADMIN_PASSWORD IMPORT_SNAPSHOT CODE_SOURCE REPO_URL GIT_REF)
declare -A PRESET=()    # values read from --config (or a previous run's saved answers)
declare -A ANSWER=()    # the final answers
CONFIG_FILE="" YES=0 DRY_RUN=0 STEP="starting"
GENERATED_ADMIN_PASSWORD="" RECOVERY_PHRASE="" ADMIN_NOTE=""

# --- output -----------------------------------------------------------------------------
if [[ -t 1 ]]; then B=$'\e[1m' DIM=$'\e[2m' RED=$'\e[31m' GRN=$'\e[32m' YEL=$'\e[33m' RST=$'\e[0m'; else B="" DIM="" RED="" GRN="" YEL="" RST=""; fi
say()  { printf '%s\n' "$*"; }
info() { printf '%s==>%s %s\n' "$B" "$RST" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '%swarning:%s %s\n' "$YEL" "$RST" "$*" >&2; }
die()  { printf '%serror:%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }
step() { STEP=$1; info "$1"; }
on_error() {
    local code=$? line=$1
    printf '\n%sThe install stopped during: %s%s (line %s, exit %s).\n' "$RED" "$STEP" "$RST" "$line" "$code" >&2
    printf 'Fix the problem above and run the same command again: finished steps are skipped or safely redone.\n' >&2
    exit "$code"
}
trap 'on_error $LINENO' ERR

usage() { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

# --- arguments --------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case $1 in
        --config) [[ $# -ge 2 ]] || die "--config needs a file"; CONFIG_FILE=$2; shift 2 ;;
        --config=*) CONFIG_FILE=${1#*=}; shift ;;
        -y|--yes) YES=1; shift ;;
        -n|--dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage 0 ;;
        *) printf 'unknown option: %s\n\n' "$1" >&2; usage 1 ;;
    esac
done

# --- preset files: parsed, never executed ------------------------------------------------
# KEY=VALUE per line; # starts a comment (inside quotes it's literal); unknown keys are errors.
parse_config() {
    local file=$1 n=0 line key val
    [[ -r $file ]] || die "can't read config file $file"
    while IFS= read -r line || [[ -n $line ]]; do
        n=$((n + 1)); line=${line%$'\r'}
        [[ $line =~ ^[[:space:]]*(#|$) ]] && continue
        [[ $line =~ ^[[:space:]]*([A-Z_]+)[[:space:]]*=(.*)$ ]] || die "$file line $n: expected KEY=VALUE, got: $line"
        key=${BASH_REMATCH[1]} val=${BASH_REMATCH[2]}
        val=${val#"${val%%[![:space:]]*}"}                       # trim leading space
        if [[ $val =~ ^\"([^\"]*)\" ]] || [[ $val =~ ^\'([^\']*)\' ]]; then
            val=${BASH_REMATCH[1]}                                # quoted: take it literally
        else
            val=${val%%[[:space:]]#*}; val=${val%"${val##*[![:space:]]}"}   # strip " # comment", trailing space
        fi
        [[ " ${KEYS[*]} " == *" $key "* ]] || die "$file line $n: unknown key $key (known: ${KEYS[*]})"
        PRESET[$key]=$val
    done < "$file"
}

# --- validators: set ERR and return 1 when a value is bad ---------------------------------
ERR=""
v_any()      { return 0; }
v_sitename() { [[ ${#1} -ge 1 && ${#1} -le 40 ]] || { ERR="1-40 characters"; return 1; }; }
v_yesno()    { [[ $1 == yes || $1 == no ]] || { ERR="yes or no"; return 1; }; }
v_dbmode()   { [[ $1 =~ ^(native|podman|docker|external)$ ]] || { ERR="native, podman, docker or external"; return 1; }; }
v_listen()   { [[ $1 == lan || $1 == local ]] || { ERR="lan or local"; return 1; }; }
v_creation() { [[ $1 == anyone || $1 == admins ]] || { ERR="anyone or admins"; return 1; }; }
v_source()   { [[ $1 =~ ^(auto|local|github)$ ]] || { ERR="auto, local or github"; return 1; }; }
v_ref()      { [[ $1 =~ ^[A-Za-z0-9._/-]+$ ]] || { ERR="a branch, tag or commit"; return 1; }; }
v_dburl()    { [[ $1 =~ ^postgres(ql)?://.+ ]] || { ERR="a URL like postgresql://user:password@host:5432/dbname"; return 1; }; }
v_domain()   { [[ -z $1 || $1 =~ ^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z0-9]([a-z0-9-]*[a-z0-9])?$ || $1 == localhost ]] || { ERR="empty, or a lower-case host name like board.example.com"; return 1; }; }
v_username() { [[ $1 =~ ^[A-Za-z0-9_-]{3,20}$ ]] || { ERR="3-20 letters, numbers, - or _"; return 1; }; }
v_password() { [[ -z $1 || ( ${#1} -ge 8 && ${#1} -le 256 ) ]] || { ERR="8-256 characters (or empty to generate one)"; return 1; }; }
v_prefix() {
    [[ $1 =~ ^[A-Za-z0-9_]{1,8}$ ]] || { ERR="1-8 letters, numbers or _"; return 1; }
    [[ $RESERVED_PREFIXES != *" ${1,,} "* ]] || { ERR="\"$1\" is reserved (used by the board's own pages)"; return 1; }
}
v_snapshot() {
    [[ -z $1 ]] && return 0
    [[ -f $1 ]] || { ERR="no such file"; return 1; }
    [[ $(head -c 15 "$1") == "SQLite format 3" ]] || { ERR="not a board snapshot (made with: board.py export FILE)"; return 1; }
}
v_port() {
    [[ $1 =~ ^[0-9]+$ ]] && (( $1 >= 1 && $1 <= 65535 )) || { ERR="a number from 1 to 65535"; return 1; }
    port_free_or_ours "$1" || { ERR="port $1 is already in use by another program"; return 1; }
}
port_free_or_ours() {   # free, or held by this board's own service (a re-run)
    local listeners pid unit=/etc/systemd/system/$SERVICE.service
    listeners=$(ss -Hltnp "sport = :$1" 2>/dev/null || true)
    [[ -z $listeners ]] && return 0
    pid=$(systemctl show -p MainPID --value "$SERVICE" 2>/dev/null || echo 0)
    [[ $pid != 0 && $listeners == *"pid=$pid,"* ]] && return 0
    # ss can't always name the process (containers, hardened kernels): the board's service is
    # running and configured for this port, so the listener is the board itself.
    systemctl is-active -q "$SERVICE" 2>/dev/null && [[ -f $unit ]] && grep -q -- "--port $1 " "$unit"
}
caddy_has() { systemctl is-active -q caddy 2>/dev/null || ss -Hltnp "sport = :$1" 2>/dev/null | grep -q caddy; }

# --- asking -----------------------------------------------------------------------------
# ask KEY "question" DEFAULT VALIDATOR [secret]: preset value > (--yes: default) > prompt.
explain() {   # explain KEY... "text": shown only if at least one of those keys will be asked
    local text=${*: -1} k
    (( YES )) && return 0
    for k in "${@:1:$#-1}"; do [[ -z ${PRESET[$k]+set} ]] && { say "$text"; return 0; }; done
}
ask() {
    local key=$1 question=$2 default=$3 check=$4 secret=${5:-} value again shown
    if [[ -n ${PRESET[$key]+set} ]]; then
        value=${PRESET[$key]}
        if [[ -n $secret ]]; then shown="(hidden)"; else shown=$value; fi
        $check "$value" || die "config: $key=$shown is not valid: $ERR"
    elif (( YES )); then
        value=$default
        $check "$value" || die "$key has no valid default; set it in the config file ($ERR)"
    else
        while true; do
            if [[ -n $secret ]]; then
                # never echo a secret's saved value, not even as the default
                read -r -s -p "$question${default:+ [Enter keeps the saved one]}: " value < /dev/tty; printf '\n' > /dev/tty
                if [[ -n $value ]]; then
                    read -r -s -p "  again: " again < /dev/tty; printf '\n' > /dev/tty
                    [[ $value == "$again" ]] || { warn "they didn't match, try again"; continue; }
                fi
            else
                read -r -e -p "$question${default:+ [$default]}: " value < /dev/tty
            fi
            value=${value:-$default}
            $check "$value" && break
            warn "$ERR"
        done
    fi
    ANSWER[$key]=$value
}

# --- checks before asking anything --------------------------------------------------------
(( EUID == 0 )) || die "run this as root: sudo $0 $*"
# shellcheck source=/dev/null
. /etc/os-release
if [[ ${ID:-} != ubuntu || ${VERSION_ID:-} != 24.04 ]]; then
    warn "this installer is written for Ubuntu 24.04; this is ${PRETTY_NAME:-unknown}."
    (( YES )) && die "refusing to continue unattended on an unsupported system"
    read -r -p "Continue anyway? [y/N] " a < /dev/tty; [[ ${a,,} == y* ]] || exit 1
fi
for tool in ss systemctl runuser; do command -v $tool >/dev/null || die "missing $tool (is this a full Ubuntu server?)"; done

# Where earlier answers come from: --config FILE (used as answers), or the last run's saved
# answers (used as answers with --yes; otherwise offered as the defaults for each question).
declare -A DEFAULTS=()
if [[ -n $CONFIG_FILE ]]; then
    parse_config "$CONFIG_FILE"
    if [[ -n ${PRESET[ADMIN_PASSWORD]:-} ]] && (( (8#$(stat -c %a "$CONFIG_FILE") & 044) != 0 )); then
        warn "$CONFIG_FILE contains a password and others can read it: chmod 600 $CONFIG_FILE"
    fi
elif [[ -f $SAVED_CONF ]]; then
    parse_config "$SAVED_CONF"
    if (( ! YES )); then
        for k in "${!PRESET[@]}"; do DEFAULTS[$k]=${PRESET[$k]}; done
        PRESET=()
    fi
fi
d() { printf '%s' "${DEFAULTS[$1]-${2:-}}"; }   # a question's default: the last run's answer, else $2

# --- the questions ----------------------------------------------------------------------
say ""
say "${B}social board installer${RST}  ${DIM}(press Enter to accept the [default])${RST}"
say ""
# advanced (preset file only, never asked): where the code comes from
quiet() { local v=${PRESET[$1]-$(d "$1" "$2")}; $3 "$v" || die "config: $1=$v is not valid: $ERR"; ANSWER[$1]=$v; }
quiet CODE_SOURCE auto v_source
quiet REPO_URL "$DEFAULT_REPO" v_any
quiet GIT_REF main v_ref
ask SITE_NAME "Site name" "$(d SITE_NAME "social board")" v_sitename
explain DB_MODE ""
explain DB_MODE "Database: ${B}native${RST} = Ubuntu's PostgreSQL package (recommended), ${B}podman${RST} or ${B}docker${RST} = PostgreSQL in a
container, ${B}external${RST} = a PostgreSQL server you already run."
ask DB_MODE "Database" "$(d DB_MODE native)" v_dbmode
if [[ ${ANSWER[DB_MODE]} == external ]]; then
    ask DB_URL "Connection URL" "$(d DB_URL)" v_dburl secret
else
    ANSWER[DB_URL]=""
fi
explain PUBLIC_DOMAIN ""
explain PUBLIC_DOMAIN "HTTPS: enter a domain name that points at this server and Caddy will get a certificate automatically
(names ending in .internal, .home.arpa, .lan, .local or .test get a local certificate instead). Leave empty for plain HTTP."
ask PUBLIC_DOMAIN "Domain" "$(d PUBLIC_DOMAIN)" v_domain
if [[ -n ${ANSWER[PUBLIC_DOMAIN]} ]]; then
    ANSWER[LISTEN]=local
    for p in 80 443; do port_free_or_ours $p || caddy_has $p || die "port $p is in use by another program; Caddy needs it"; done
else
    ask LISTEN "Who can reach the board: lan (any computer on the network) or local (this server only)" "$(d LISTEN lan)" v_listen
fi
ask PORT "Port for the board" "$(d PORT 8080)" v_port
ask OPEN_FIREWALL "Add firewall (ufw) rules to let visitors in" "$(d OPEN_FIREWALL yes)" v_yesno
explain USER_PREFIX COMMUNITY_PREFIX ""
explain USER_PREFIX COMMUNITY_PREFIX "Names: users show as ${B}<user prefix>/name${RST} and communities as ${B}<community prefix>/name${RST} (like u/ and r/)."
ask USER_PREFIX "User prefix" "$(d USER_PREFIX u)" v_prefix
while true; do
    ask COMMUNITY_PREFIX "Community prefix" "$(d COMMUNITY_PREFIX r)" v_prefix
    [[ ${ANSWER[USER_PREFIX],,} != "${ANSWER[COMMUNITY_PREFIX],,}" ]] && break
    [[ -n ${PRESET[COMMUNITY_PREFIX]+set} ]] || (( YES )) && die "USER_PREFIX and COMMUNITY_PREFIX must be different"
    warn "it must differ from the user prefix"
done
ask CUSTOM_USER_PREFIX "Let users choose their own prefix" "$(d CUSTOM_USER_PREFIX no)" v_yesno
ask CUSTOM_COMMUNITY_PREFIX "Let community creators choose their own prefix" "$(d CUSTOM_COMMUNITY_PREFIX no)" v_yesno
ask OPEN_REGISTRATION "Anyone can sign up" "$(d OPEN_REGISTRATION yes)" v_yesno
ask COMMUNITY_CREATION "Who can create communities: anyone or admins" "$(d COMMUNITY_CREATION anyone)" v_creation
explain ADMIN_USER ADMIN_PASSWORD ""
ask ADMIN_USER "Admin username" "$(d ADMIN_USER admin)" v_username
ask ADMIN_PASSWORD "Admin password (empty = generate a strong one; ignored if the account exists)" "" v_password secret
ask IMPORT_SNAPSHOT "Import a board snapshot file (empty = start fresh)" "" v_snapshot

# --- confirm -------------------------------------------------------------------------------
A() { printf '%s' "${ANSWER[$1]}"; }
access() {
    if [[ -n $(A PUBLIC_DOMAIN) ]]; then printf 'https://%s/ (through Caddy)' "$(A PUBLIC_DOMAIN)"
    elif [[ $(A LISTEN) == lan ]]; then printf 'http://<this server>:%s/ (whole network)' "$(A PORT)"
    else printf 'http://127.0.0.1:%s/ (this server only)' "$(A PORT)"; fi
}
say ""
say "${B}Summary${RST}"
printf '  %-22s %s\n' "site name" "$(A SITE_NAME)" "address" "$(access)" "database" "$(A DB_MODE)" \
    "firewall rules" "$(A OPEN_FIREWALL)" "prefixes" "$(A USER_PREFIX)/name, $(A COMMUNITY_PREFIX)/name" \
    "custom prefixes" "users: $(A CUSTOM_USER_PREFIX), communities: $(A CUSTOM_COMMUNITY_PREFIX)" \
    "sign-ups" "$( [[ $(A OPEN_REGISTRATION) == yes ]] && echo open || echo closed )" \
    "create communities" "$(A COMMUNITY_CREATION)" \
    "admin" "$(A ADMIN_USER) ($( [[ -n $(A ADMIN_PASSWORD) ]] && echo "password given" || echo "password generated if new" ))" \
    "import" "$( [[ -n $(A IMPORT_SNAPSHOT) ]] && A IMPORT_SNAPSHOT || echo none )" \
    "code" "$(A CODE_SOURCE) ($(A GIT_REF))" "installs to" "$APP_DIR (code), $DATA_ROOT (data)"
say ""
if (( DRY_RUN )); then
    say "${B}Dry run${RST}: these steps would run, nothing has been changed."
    steps=("install packages" "create the $SVC_USER user" "install the code + Python packages" "set up the $(A DB_MODE) database")
    [[ -n $(A IMPORT_SNAPSHOT) ]] && steps+=("import the snapshot")
    steps+=("apply settings" "create the admin account" "install + start the $SERVICE service")
    [[ -n $(A PUBLIC_DOMAIN) ]] && steps+=("configure Caddy for HTTPS")
    [[ $(A OPEN_FIREWALL) == yes ]] && steps+=("add firewall rules")
    for i in "${!steps[@]}"; do say "  $((i + 1)). ${steps[$i]}"; done
    exit 0
fi
if (( ! YES )); then
    read -r -p "Go ahead? [Y/n] " a < /dev/tty
    [[ -z $a || ${a,,} == y* ]] || { say "Nothing was changed."; exit 1; }
fi

as_board() { runuser -u "$SVC_USER" -- env PYTHONDONTWRITEBYTECODE=1 "$APP_DIR/.venv/bin/python" "$APP_DIR/board.py" --data "$DATA_DIR" "$@"; }
random_secret() { python3 -c 'import secrets; print(secrets.token_urlsafe(24))'; }
apt_install() { DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "$@" >/dev/null; }

# --- 1. packages ----------------------------------------------------------------------------
step "Installing packages"
pkgs=(python3 python3-venv python3-pip git ca-certificates curl iproute2)
case $(A DB_MODE) in
    native) pkgs+=(postgresql) ;;
    podman) pkgs+=(podman) ;;
    docker) command -v docker >/dev/null || pkgs+=(docker.io) ;;
esac
[[ -n $(A PUBLIC_DOMAIN) ]] && pkgs+=(caddy)
[[ $(A OPEN_FIREWALL) == yes ]] && pkgs+=(ufw)
DEBIAN_FRONTEND=noninteractive apt-get update -q >/dev/null
apt_install "${pkgs[@]}"
ok "${pkgs[*]}"

# --- 2. service user + folders ----------------------------------------------------------------
step "Creating the $SVC_USER user and folders"
id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --home-dir "$DATA_ROOT" --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
install -d -m 750 -o "$SVC_USER" -g "$SVC_USER" "$DATA_ROOT" "$DATA_DIR" "$ADS_DIR" "$AVATARS_DIR"
ok "$DATA_ROOT"

# --- 3. code + Python packages ---------------------------------------------------------------
step "Installing the code"
src=$(A CODE_SOURCE)
if [[ $src == auto ]]; then
    if [[ $(realpath "$SCRIPT_DIR") == "$APP_DIR" ]]; then
        [[ -d $APP_DIR/.git ]] && src=github || src=local          # re-run from the installed copy
    elif [[ -f $SCRIPT_DIR/board.py && -d $SCRIPT_DIR/sb ]]; then src=local
    else src=github; fi
fi
ANSWER[CODE_SOURCE]=$src
if [[ $src == local ]]; then
    [[ -f $SCRIPT_DIR/board.py && -d $SCRIPT_DIR/sb ]] || die "CODE_SOURCE=local, but $SCRIPT_DIR isn't the social board repo"
    install -d -m 755 "$APP_DIR"
    if [[ $(realpath "$SCRIPT_DIR") != "$(realpath "$APP_DIR")" ]]; then
        # copy the code only: no git metadata, local data, virtualenv or ad files
        tar -C "$SCRIPT_DIR" --exclude=./.git --exclude=./.venv --exclude=./data --exclude='./ads/*' \
            --exclude='*/__pycache__' -cf - . | tar -C "$APP_DIR" --no-same-owner -xf -
    fi
    ok "copied from $SCRIPT_DIR"
else
    if [[ -d $APP_DIR/.git ]]; then
        git -C "$APP_DIR" remote set-url origin "$(A REPO_URL)"
        git -C "$APP_DIR" fetch -q --depth 1 origin "$(A GIT_REF)"
        git -C "$APP_DIR" reset -q --hard FETCH_HEAD
    else
        [[ -e $APP_DIR && -n $(ls -A "$APP_DIR" 2>/dev/null) ]] && die "$APP_DIR exists and isn't a git checkout; move it aside first"
        git clone -q --depth 1 --branch "$(A GIT_REF)" "$(A REPO_URL)" "$APP_DIR"
    fi
    ok "$(A REPO_URL) @ $(git -C "$APP_DIR" rev-parse --short HEAD)"
fi
chown -R root:root "$APP_DIR"; chmod -R go-w "$APP_DIR"
if [[ -z $(ls -A "$AVATARS_DIR") ]]; then   # first run: the built-in default avatars
    install -m 644 -o "$SVC_USER" -g "$SVC_USER" "$APP_DIR"/avatars/* "$AVATARS_DIR"/
fi
step "Installing Python packages"
[[ -x $APP_DIR/.venv/bin/python ]] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --disable-pip-version-check -r "$APP_DIR/requirements.txt"
ok "$("$APP_DIR/.venv/bin/python" -c 'import psycopg, PIL; print("psycopg", psycopg.__version__, "/ Pillow", PIL.__version__)')"

# --- 4. database --------------------------------------------------------------------------------
step "Setting up the database ($(A DB_MODE))"
url_file=$DATA_DIR/database_url
saved_url=$( [[ -f $url_file ]] && cat "$url_file" || true )
saved_pw() { [[ $saved_url =~ ^postgresql://socialboard:([^@]+)@ ]] && printf '%s' "${BASH_REMATCH[1]}"; }
case $(A DB_MODE) in
    native)
        systemctl enable --now postgresql >/dev/null 2>&1
        pw=$(saved_pw || true); [[ -n $pw && $saved_url == *"@127.0.0.1:5432/"* ]] || pw=$(random_secret)
        runuser -u postgres -- psql -q -v ON_ERROR_STOP=1 -v pw="$pw" -v db="$DB_NAME" >/dev/null <<'SQL'
SELECT format('CREATE ROLE socialboard LOGIN PASSWORD %L', :'pw') WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'socialboard') \gexec
SELECT format('ALTER ROLE socialboard PASSWORD %L', :'pw') \gexec
SELECT format('CREATE DATABASE %I OWNER socialboard', :'db') WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'db') \gexec
SQL
        runuser -u postgres -- psql -q -v ON_ERROR_STOP=1 -d "$DB_NAME" -c "SET client_min_messages = warning" \
            -c "CREATE EXTENSION IF NOT EXISTS citext" >/dev/null
        url="postgresql://socialboard:$pw@127.0.0.1:5432/$DB_NAME"
        ;;
    podman|docker)
        engine=$(A DB_MODE)
        if [[ $engine == docker ]]; then systemctl enable --now docker >/dev/null 2>&1; fi
        exists=0
        if [[ $engine == podman ]]; then podman container exists "$DB_CONTAINER" && exists=1
        else [[ -n $(docker ps -aq -f "name=^$DB_CONTAINER$") ]] && exists=1; fi
        port_free_or_ours $DB_CONTAINER_PORT || (( exists )) || die "port $DB_CONTAINER_PORT (for the database container) is in use"
        pw=$(saved_pw || true); [[ -n $pw && $saved_url == *"@127.0.0.1:$DB_CONTAINER_PORT/"* ]] || pw=$(random_secret)
        if [[ $engine == podman ]]; then
            # Quadlet: podman's own way to run a container as a systemd service (starts at boot)
            install -d -m 755 /etc/containers/systemd
            umask 077
            cat > /etc/containers/systemd/$DB_CONTAINER.container <<EOF
# written by social board's install.sh
[Unit]
Description=social board database (PostgreSQL)

[Container]
ContainerName=$DB_CONTAINER
Image=$DB_IMAGE
Environment=POSTGRES_USER=socialboard POSTGRES_DB=$DB_NAME POSTGRES_PASSWORD=$pw
Volume=$DB_VOLUME:/var/lib/postgresql/data
PublishPort=127.0.0.1:$DB_CONTAINER_PORT:5432

[Service]
Restart=always
TimeoutStartSec=900

[Install]
WantedBy=multi-user.target
EOF
            umask 022
            systemctl daemon-reload
            systemctl reset-failed "$DB_CONTAINER.service" 2>/dev/null || true
            systemctl restart "$DB_CONTAINER.service" || {
                journalctl -u "$DB_CONTAINER" -n 15 --no-pager >&2 || true
                die "the database container didn't start (log above; more: journalctl -u $DB_CONTAINER)"; }
            DB_UNIT=$DB_CONTAINER.service
        else
            if [[ -z $(docker ps -aq -f "name=^$DB_CONTAINER$") ]]; then
                envf=$(mktemp); chmod 600 "$envf"
                printf 'POSTGRES_USER=socialboard\nPOSTGRES_DB=%s\nPOSTGRES_PASSWORD=%s\n' "$DB_NAME" "$pw" > "$envf"
                docker run -d -q --name "$DB_CONTAINER" --restart unless-stopped --env-file "$envf" \
                    -v "$DB_VOLUME:/var/lib/postgresql/data" -p "127.0.0.1:$DB_CONTAINER_PORT:5432" "$DB_IMAGE" >/dev/null
                rm -f "$envf"
            else
                docker start "$DB_CONTAINER" >/dev/null
            fi
            DB_UNIT=docker.service
        fi
        # wait for PostgreSQL inside the container, then make sure the password is the one we keep
        for _ in $(seq 1 120); do $engine exec "$DB_CONTAINER" pg_isready -q -U socialboard -d "$DB_NAME" 2>/dev/null && break; sleep 1; done
        $engine exec "$DB_CONTAINER" pg_isready -q -U socialboard -d "$DB_NAME" || die "the database container didn't start; see: $engine logs $DB_CONTAINER"
        $engine exec -i "$DB_CONTAINER" psql -q -v ON_ERROR_STOP=1 -U socialboard -d "$DB_NAME" -v pw="$pw" >/dev/null <<'SQL'
SELECT format('ALTER ROLE socialboard PASSWORD %L', :'pw') \gexec
SQL
        url="postgresql://socialboard:$pw@127.0.0.1:$DB_CONTAINER_PORT/$DB_NAME"
        ;;
    external)
        url=$(A DB_URL)
        ;;
esac
# Test the connection first and only then replace the saved URL, so a wrong one never
# reaches the running board. (The URL travels in a file, not on the command line.)
install -m 600 -o "$SVC_USER" -g "$SVC_USER" /dev/null "$url_file.new"
printf '%s\n' "$url" > "$url_file.new"
can_connect() { runuser -u "$SVC_USER" -- "$APP_DIR/.venv/bin/python" -c 'import sys, psycopg; psycopg.connect(open(sys.argv[1]).read().strip(), connect_timeout=5).close()' "$url_file.new" 2>/dev/null; }
for _ in $(seq 1 30); do can_connect && break; sleep 1; done
can_connect || { rm -f "$url_file.new"; die "can't connect to the database (check the URL and that PostgreSQL is running)"; }
mv -f "$url_file.new" "$url_file"
ok "connected; connection details in $url_file (readable only by $SVC_USER)"

# --- 5. settings (+ import) -------------------------------------------------------------------------
step "Applying settings"
b01() { [[ $1 == yes ]] && echo 1 || echo 0; }
if [[ -n $(A IMPORT_SNAPSHOT) ]]; then
    snap=$DATA_ROOT/import-$(date +%s).db
    install -m 600 -o "$SVC_USER" -g "$SVC_USER" "$(A IMPORT_SNAPSHOT)" "$snap"
    as_board merge "$snap" | sed 's/^/  /'
    rm -f "$snap"
fi
as_board settings set "site_name=$(A SITE_NAME)" "user_prefix=$(A USER_PREFIX)" "community_prefix=$(A COMMUNITY_PREFIX)" \
    "custom_user_prefix=$(b01 "$(A CUSTOM_USER_PREFIX)")" "custom_community_prefix=$(b01 "$(A CUSTOM_COMMUNITY_PREFIX)")" \
    "open_registration=$(b01 "$(A OPEN_REGISTRATION)")" "community_creation=$(A COMMUNITY_CREATION)" | sed 's/^/  /'

# --- 6. admin account ---------------------------------------------------------------------------------
step "Setting up the admin account"
pw=$(A ADMIN_PASSWORD)
if [[ -z $pw ]]; then pw=$(random_secret); GENERATED_ADMIN_PASSWORD=$pw; fi
# create-user exits 3 when the account exists; "|| exit/rc" keeps that expected code from
# tripping the error trap (which, with set -E, would otherwise also fire inside the $(...)).
rc=0
out=$(printf '%s\n' "$pw" | as_board create-user "$(A ADMIN_USER)" --admin --password-stdin --avatars "$AVATARS_DIR" 2>&1 || exit $?) || rc=$?
if (( rc == 0 )); then
    RECOVERY_PHRASE=$(sed -n 's/^recovery phrase: //p' <<<"$out")
    ok "created $(A ADMIN_USER)"
elif (( rc == 3 )); then
    as_board admin "$(A ADMIN_USER)" --on >/dev/null
    GENERATED_ADMIN_PASSWORD=""
    ADMIN_NOTE="(already existed: password unchanged, admin rights confirmed)"
    ok "$(A ADMIN_USER) $ADMIN_NOTE"
else
    die "couldn't create the admin account: $out"
fi

# --- 7. the service -------------------------------------------------------------------------------------
step "Installing the $SERVICE service"
host=$( [[ $(A LISTEN) == lan ]] && echo 0.0.0.0 || echo 127.0.0.1 )
extra=$( [[ -n $(A PUBLIC_DOMAIN) ]] && echo "--trust-proxy --secure-cookies" || true )
case $(A DB_MODE) in native) dep=postgresql.service ;; podman) dep=${DB_UNIT:-$DB_CONTAINER.service} ;; docker) dep=docker.service ;; *) dep="" ;; esac
cat > /etc/systemd/system/$SERVICE.service <<EOF
# written by social board's install.sh; re-run the installer rather than editing this
[Unit]
Description=social board
After=network-online.target ${dep}
Wants=network-online.target ${dep}
StartLimitIntervalSec=0

[Service]
User=$SVC_USER
Group=$SVC_USER
WorkingDirectory=$APP_DIR
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/board.py --data $DATA_DIR serve --host $host --port $(A PORT) --ads $ADS_DIR --avatars $AVATARS_DIR $extra
Restart=always
RestartSec=3
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=$DATA_ROOT
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"
for _ in $(seq 1 60); do curl -fsS -o /dev/null "http://127.0.0.1:$(A PORT)/" 2>/dev/null && break; sleep 1; done
if ! curl -fsS -o /dev/null "http://127.0.0.1:$(A PORT)/"; then
    journalctl -u "$SERVICE" -n 30 --no-pager >&2 || true
    die "the board didn't start answering on port $(A PORT) (log above)"
fi
ok "running, starts at boot"

# --- 8. Caddy (HTTPS) ---------------------------------------------------------------------------------------
if [[ -n $(A PUBLIC_DOMAIN) ]]; then
    step "Configuring Caddy for https://$(A PUBLIC_DOMAIN)/"
    cf=/etc/caddy/Caddyfile
    if [[ -f $cf ]] && ! grep -q "written by social board" "$cf"; then cp "$cf" "$cf.before-social-board"; fi
    # Names that can't get a public certificate get Caddy's own local one, stated explicitly:
    # Ubuntu's Caddy (2.6) would otherwise ask Let's Encrypt for them and fail.
    tls_line=""
    if [[ $(A PUBLIC_DOMAIN) =~ (^localhost|\.localhost|\.local|\.internal|\.home\.arpa|\.test|\.lan)$ ]]; then tls_line=$'\ttls internal'; fi
    cat > "$cf" <<EOF
# written by social board's install.sh (the previous file, if any, is Caddyfile.before-social-board)
$(A PUBLIC_DOMAIN) {
${tls_line}
	encode gzip
	reverse_proxy 127.0.0.1:$(A PORT)
}
EOF
    caddy validate --config "$cf" --adapter caddyfile >/dev/null 2>&1 || die "Caddy rejected the generated $cf"
    systemctl enable caddy >/dev/null 2>&1
    systemctl reload-or-restart caddy
    if [[ -n $tls_line ]]; then ok "Caddy serves https://$(A PUBLIC_DOMAIN)/ with its own local certificate (browsers will warn once)"
    else ok "Caddy serves https://$(A PUBLIC_DOMAIN)/ (it gets a Let's Encrypt certificate on its own; DNS must point here)"; fi
fi

# --- 9. firewall -------------------------------------------------------------------------------------------
if [[ $(A OPEN_FIREWALL) == yes ]]; then
    step "Firewall rules"
    if [[ -n $(A PUBLIC_DOMAIN) ]]; then rules=(80/tcp 443/tcp); elif [[ $(A LISTEN) == lan ]]; then rules=("$(A PORT)/tcp"); else rules=(); fi
    failed=()
    for r in "${rules[@]}"; do ufw allow "$r" >/dev/null 2>&1 || failed+=("$r"); done
    if (( ${#failed[@]} )); then
        warn "ufw couldn't add a rule for ${failed[*]} (is the firewall usable here?); allow it yourself: sudo ufw allow ${failed[0]}"
    elif ufw status 2>/dev/null | grep -q "Status: active"; then
        ok "allowed ${rules[*]:-nothing (board is local only)}"
    else
        ok "added rules for ${rules[*]:-nothing}; ufw itself is off, so nothing is blocked yet"
        say "  ${DIM}(not turning ufw on for you: that could lock you out of SSH. To use it: sudo ufw allow OpenSSH && sudo ufw enable)${RST}"
    fi
fi

# --- remember the answers (minus the password) for re-runs / cloning this setup -------------------------------
umask 077
{
    printf '# social board install answers (%s). Re-run with: sudo install.sh --config %s --yes\n' "$(date -Is)" "$SAVED_CONF"
    for k in "${KEYS[@]}"; do
        case $k in ADMIN_PASSWORD|IMPORT_SNAPSHOT) v="" ;; *) v=${ANSWER[$k]} ;; esac
        printf '%s="%s"\n' "$k" "$v"
    done
} > "$SAVED_CONF"
umask 022

# --- done ------------------------------------------------------------------------------------------------------
say ""
say "${GRN}${B}Done.${RST} social board is running."
say ""
if [[ -n $(A PUBLIC_DOMAIN) ]]; then say "  open:      https://$(A PUBLIC_DOMAIN)/"
elif [[ $(A LISTEN) == lan ]]; then
    for ip in $(hostname -I 2>/dev/null); do [[ $ip == *:* ]] || say "  open:      http://$ip:$(A PORT)/"; done
else say "  open:      http://127.0.0.1:$(A PORT)/  (this server only)"; fi
say "  admin:     $(A ADMIN_USER) ${ADMIN_NOTE}"
[[ -n $GENERATED_ADMIN_PASSWORD ]] && say "  password:  ${B}$GENERATED_ADMIN_PASSWORD${RST}   (generated; shown only now)"
[[ -n $RECOVERY_PHRASE ]] && say "  recovery:  ${B}$RECOVERY_PHRASE${RST}" && say "             write this down: it's the only way back in if the password is lost"
say ""
say "  logs:      journalctl -u $SERVICE -f"
say "  restart:   sudo systemctl restart $SERVICE"
if [[ $(A CODE_SOURCE) == github ]]; then say "  update:    sudo $APP_DIR/install.sh --config $SAVED_CONF --yes"
else say "  update:    get the new code into $SCRIPT_DIR, then: sudo $(realpath "$0") --config $SAVED_CONF --yes"; fi
say "  backup:    sudo -u $SVC_USER $APP_DIR/.venv/bin/python $APP_DIR/board.py --data $DATA_DIR export /var/lib/social-board/backup.db"
say "  answers:   $SAVED_CONF"
