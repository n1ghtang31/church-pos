#!/usr/bin/env bash
# Church POS installer for Proxmox VE.
#
# Creates a Debian LXC container, installs Docker in it, and starts Church POS.
# Run it in the Proxmox host's shell as root:
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/n1ghtang31/church-pos/self_hosted/proxmox/install.sh)"
#
# Any prompt can be answered ahead of time with an environment variable of the same
# name (CTID, CT_HOSTNAME, ADMIN_CODE, ...). CONFIRM=yes skips the final question.

set -Eeuo pipefail

REPO_URL="${REPO_URL:-https://github.com/n1ghtang31/church-pos.git}"
BRANCH="${BRANCH:-self_hosted}"
APP_DIR="/opt/church-pos"
BACKUP_DIR="/opt/church-pos-backups"
LOG="/tmp/church-pos-install-$(date +%Y%m%d-%H%M%S).log"
CREATED_CTID=""

if [[ -t 1 ]]; then
  BOLD=$'\e[1m' GREEN=$'\e[32m' YELLOW=$'\e[33m' RED=$'\e[31m' RESET=$'\e[0m'
else
  BOLD="" GREEN="" YELLOW="" RED="" RESET=""
fi

msg_info() { printf ' %s...%s %s\n' "$YELLOW" "$RESET" "$1"; }
msg_ok() { printf ' %s✓%s %s\n' "$GREEN" "$RESET" "$1"; }
msg_warn() { printf ' %s!%s %s\n' "$YELLOW" "$RESET" "$1"; }

die() {
  trap - ERR
  printf '\n %s✗ %s%s\n' "$RED" "$1" "$RESET" >&2
  if [[ -n $CREATED_CTID ]]; then
    printf '\n   Container %s was left in place for troubleshooting. To remove it:\n' "$CREATED_CTID" >&2
    printf '     pct stop %s; pct destroy %s\n' "$CREATED_CTID" "$CREATED_CTID" >&2
  fi
  exit 1
}
trap 'die "Unexpected error on line $LINENO: $BASH_COMMAND (log: $LOG)"' ERR

# Run a command quietly, showing the end of its output only if it fails.
# Functions run by step must return non-zero themselves on failure, because
# errexit is off while a function runs as an if condition.
step() {
  local description=$1
  shift
  msg_info "$description"
  if "$@" >>"$LOG" 2>&1; then
    msg_ok "$description"
  else
    printf '\n' >&2
    tail -n 25 "$LOG" >&2
    die "$description failed (full log: $LOG)"
  fi
}

in_ct() { pct exec "$CTID" -- bash -c "$1"; }

has_terminal() { { : </dev/tty; } 2>/dev/null; }

# ask VAR "Question" default: skipped when VAR is already set
ask() {
  local var=$1 question=$2 default=$3 reply
  if [[ -n ${!var:-} ]]; then
    return
  fi
  has_terminal || die "No terminal to ask for $var. Set $var=... to run without prompts."
  read -r -p "   $question [$default]: " reply </dev/tty
  printf -v "$var" '%s' "${reply:-$default}"
}

ask_admin_code() {
  local again
  if [[ -z ${ADMIN_CODE:-} ]] && ! has_terminal; then
    die "No terminal to ask for ADMIN_CODE. Set ADMIN_CODE=... to run without prompts."
  fi
  while [[ -z ${ADMIN_CODE:-} ]]; do
    read -r -s -p "   Admin code, used to manage cashiers (8-32 letters or digits): " ADMIN_CODE </dev/tty
    printf '\n'
    if [[ ! $ADMIN_CODE =~ ^[A-Za-z0-9]{8,32}$ ]]; then
      printf '   It must be 8 to 32 letters or digits.\n'
      ADMIN_CODE=""
      continue
    fi
    read -r -s -p "   Enter it again: " again </dev/tty
    printf '\n'
    if [[ $ADMIN_CODE != "$again" ]]; then
      printf "   The codes didn't match.\n"
      ADMIN_CODE=""
    fi
  done
  [[ $ADMIN_CODE =~ ^[A-Za-z0-9]{8,32}$ ]] || die "ADMIN_CODE must be 8 to 32 letters or digits"
}

# First active storage that holds the given content type, preferring $2
default_storage() {
  local content=$1 preferred=$2 list
  list=$(pvesm status -content "$content" 2>/dev/null | awk 'NR > 1 && $3 == "active" {print $1}') || true
  if grep -qx "$preferred" <<<"$list"; then
    echo "$preferred"
  else
    head -n 1 <<<"$list"
  fi
}

host_timezone() {
  timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo "America/Chicago"
}

# Newest Debian standard template, preferring Debian 13 over 12
find_template() {
  pveam available --section system |
    awk '{print $2}' |
    grep -E '^debian-1[23]-standard_.*_amd64\.tar\.(zst|gz|xz)$' |
    sort -V |
    tail -n 1 || true
}

validate_settings() {
  [[ $CTID =~ ^[0-9]+$ && $CTID -ge 100 ]] || die "Container ID must be a number of at least 100"
  if pct config "$CTID" &>/dev/null || qm config "$CTID" &>/dev/null; then
    die "ID $CTID is already used by a container or VM"
  fi
  [[ $CT_HOSTNAME =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$ ]] || die "Hostname may only use letters, digits, and hyphens"
  [[ -n $ROOT_STORAGE ]] || die "No storage for container disks was found"
  [[ -n $TEMPLATE_STORAGE ]] || die "No storage for container templates was found"
  [[ $DISK_GB =~ ^[0-9]+$ && $DISK_GB -ge 4 ]] || die "Disk size must be at least 4 GB"
  [[ $CORES =~ ^[0-9]+$ && $CORES -ge 1 ]] || die "CPU cores must be at least 1"
  [[ $MEMORY_MB =~ ^[0-9]+$ && $MEMORY_MB -ge 512 ]] || die "Memory must be at least 512 MB"
  [[ -d /sys/class/net/$BRIDGE ]] || die "Network bridge $BRIDGE was not found"
  if [[ $IP_ADDRESS != dhcp ]]; then
    [[ $IP_ADDRESS =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}$ ]] || die "Static IP must look like 192.168.1.50/24"
    [[ $GATEWAY =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || die "Gateway must look like 192.168.1.1"
  fi
  [[ -f /usr/share/zoneinfo/$POS_TIMEZONE ]] || die "Unknown timezone: $POS_TIMEZONE"
  [[ $POS_PORT =~ ^[0-9]+$ && $POS_PORT -ge 1 && $POS_PORT -le 65535 ]] || die "Port must be between 1 and 65535"
  [[ $USE_PROXY =~ ^(yes|no)$ ]] || die "USE_PROXY must be yes or no"
  if [[ $USE_PROXY == yes ]]; then
    [[ $PROXY_IP =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}(/[0-9]{1,2})?$ ]] || die "Reverse proxy IP must look like 192.168.1.10"
  fi
}

write_env() {
  local env_file
  env_file=$(mktemp)
  chmod 600 "$env_file"
  cat >"$env_file" <<EOF
# Church POS settings. Keep a copy of this file with your backups:
# every cashier PIN depends on SECRET_KEY.

# Code typed on the login screen to add, edit, and remove cashiers
ADMIN_CODE=$ADMIN_CODE

# Signs login cookies and protects cashier PINs. Changing it means every PIN
# has to be set again.
SECRET_KEY=$SECRET_KEY

# Timezone that decides which day a sale counts toward in reports
POS_TIMEZONE=$POS_TIMEZONE

# Port on the server
POS_PORT=$POS_PORT

# True when served over HTTPS through a reverse proxy. Logins then only work
# through the HTTPS address.
COOKIE_SECURE=$COOKIE_SECURE

# Reverse proxy whose X-Forwarded-For header is trusted
TRUSTED_PROXY_IPS=$TRUSTED_PROXY_IPS
EOF
  pct push "$CTID" "$env_file" "$APP_DIR/.env" --perms 600 || { rm -f "$env_file"; return 1; }
  rm -f "$env_file"
}

install_helpers() {
  local backup update unban cron
  backup=$(mktemp)
  update=$(mktemp)
  unban=$(mktemp)
  cron=$(mktemp)

  cat >"$backup" <<EOF
#!/usr/bin/env bash
# Copies the Church POS database and settings to $BACKUP_DIR, keeping 30 days.
set -euo pipefail
cd $APP_DIR
mkdir -p $BACKUP_DIR
chmod 700 $BACKUP_DIR
name="church_pos_\$(date +%F_%H%M).db"
docker compose exec -T web python -c "import sqlite3; sqlite3.connect('/data/church_pos.db').backup(sqlite3.connect('/data/backup.db'))"
docker compose cp web:/data/backup.db "$BACKUP_DIR/\$name"
docker compose exec -T web rm -f /data/backup.db
install -m 600 $APP_DIR/.env $BACKUP_DIR/env.backup
find $BACKUP_DIR -name 'church_pos_*.db' -mtime +30 -delete
echo "Saved $BACKUP_DIR/\$name"
EOF

  cat >"$update" <<EOF
#!/usr/bin/env bash
# Backs up, then installs the latest Church POS from GitHub. Sales data is kept.
set -euo pipefail
/usr/local/bin/church-pos-backup
cd $APP_DIR
git pull --ff-only
docker compose up -d --build
docker image prune -f
echo "Church POS is updated"
EOF

  cat >"$unban" <<EOF
#!/usr/bin/env bash
# Lists blocked addresses, or unblocks one: church-pos-unban <address>, or --all for every address.
set -euo pipefail
cd $APP_DIR
if [[ \$# -eq 0 ]]; then
  docker compose exec -T web flask --app app bans
else
  docker compose exec -T web flask --app app unban "\$@"
fi
EOF

  cat >"$cron" <<EOF
# Nightly Church POS backup
30 23 * * * root /usr/local/bin/church-pos-backup >>/var/log/church-pos-backup.log 2>&1
EOF

  local status=0
  pct push "$CTID" "$backup" /usr/local/bin/church-pos-backup --perms 755 &&
    pct push "$CTID" "$update" /usr/local/bin/church-pos-update --perms 755 &&
    pct push "$CTID" "$unban" /usr/local/bin/church-pos-unban --perms 755 &&
    pct push "$CTID" "$cron" /etc/cron.d/church-pos-backup --perms 644 ||
    status=1
  rm -f "$backup" "$update" "$unban" "$cron"
  return "$status"
}

wait_for() {
  local description=$1 command=$2 attempts=$3
  msg_info "$description"
  for _ in $(seq 1 "$attempts"); do
    if in_ct "$command" >>"$LOG" 2>&1; then
      msg_ok "$description"
      return
    fi
    sleep 2
  done
  tail -n 25 "$LOG" >&2
  die "$description timed out (full log: $LOG)"
}

main() {
  printf '\n%s  Church POS installer for Proxmox VE%s\n\n' "$BOLD" "$RESET"

  [[ $EUID -eq 0 ]] || die "Run this as root on the Proxmox host"
  local cmd
  for cmd in pct pveam pvesh pvesm qm openssl; do
    command -v "$cmd" >/dev/null || die "This must run on a Proxmox VE host ($cmd was not found)"
  done

  printf '%sContainer%s (press Enter to accept the default)\n' "$BOLD" "$RESET"
  ask CTID "Container ID" "$(pvesh get /cluster/nextid)"
  ask CT_HOSTNAME "Hostname" "church-pos"
  ask ROOT_STORAGE "Storage for the container disk" "$(default_storage rootdir local-lvm)"
  ask TEMPLATE_STORAGE "Storage for the Debian template" "$(default_storage vztmpl local)"
  ask DISK_GB "Disk size in GB" 8
  ask CORES "CPU cores" 2
  ask MEMORY_MB "Memory in MB" 1024
  ask BRIDGE "Network bridge" "vmbr0"
  ask IP_ADDRESS "IP address: dhcp, or static like 192.168.1.50/24" "dhcp"
  if [[ $IP_ADDRESS != dhcp ]]; then
    local ip=${IP_ADDRESS%/*}
    ask GATEWAY "Gateway" "${ip%.*}.1"
  fi

  printf '\n%sChurch POS%s\n' "$BOLD" "$RESET"
  ask POS_TIMEZONE "Timezone for daily reports" "$(host_timezone)"
  ask POS_PORT "Web port" 5000
  ask USE_PROXY "Reached through an HTTPS reverse proxy such as nginx? (yes/no)" "yes"
  USE_PROXY=$(tr '[:upper:]' '[:lower:]' <<<"$USE_PROXY")
  [[ $USE_PROXY == y ]] && USE_PROXY=yes
  [[ $USE_PROXY == n ]] && USE_PROXY=no
  if [[ $USE_PROXY == yes ]]; then
    ask PROXY_IP "IP address of the reverse proxy" ""
  fi
  ask_admin_code

  validate_settings
  if [[ $USE_PROXY == yes ]]; then
    COOKIE_SECURE=true
    TRUSTED_PROXY_IPS=$PROXY_IP
  else
    COOKIE_SECURE=false
    TRUSTED_PROXY_IPS=""
  fi

  printf '\n%sReady to create:%s\n' "$BOLD" "$RESET"
  printf '   Container %s (%s): %s cores, %s MB memory, %s GB on %s\n' \
    "$CTID" "$CT_HOSTNAME" "$CORES" "$MEMORY_MB" "$DISK_GB" "$ROOT_STORAGE"
  printf '   Network: %s on %s\n' "$IP_ADDRESS" "$BRIDGE"
  printf '   Church POS from the %s branch, port %s, timezone %s\n' "$BRANCH" "$POS_PORT" "$POS_TIMEZONE"
  if [[ $USE_PROXY == yes ]]; then
    printf '   HTTPS through the reverse proxy at %s\n\n' "$PROXY_IP"
  else
    printf '   Local network only, over plain HTTP\n\n'
  fi
  ask CONFIRM "Continue? (yes/no)" "no"
  [[ $CONFIRM =~ ^[Yy]([Ee][Ss])?$ ]] || die "Cancelled. Nothing was changed."

  SECRET_KEY=$(openssl rand -hex 32)

  step "Updating the template list" pveam update
  local template
  template=$(find_template)
  [[ -n $template ]] || die "No Debian 12 or 13 template is available from pveam"
  if ! grep -qF "$template" <<<"$(pveam list "$TEMPLATE_STORAGE")"; then
    step "Downloading $template" pveam download "$TEMPLATE_STORAGE" "$template"
  fi

  local net0="name=eth0,bridge=$BRIDGE,ip=$IP_ADDRESS"
  if [[ $IP_ADDRESS != dhcp ]]; then
    net0+=",gw=$GATEWAY"
  fi

  # Docker needs nesting and keyctl to run inside an unprivileged container
  step "Creating container $CTID" pct create "$CTID" "$TEMPLATE_STORAGE:vztmpl/$template" \
    --hostname "$CT_HOSTNAME" \
    --cores "$CORES" \
    --memory "$MEMORY_MB" \
    --swap 512 \
    --rootfs "$ROOT_STORAGE:$DISK_GB" \
    --net0 "$net0" \
    --timezone "$POS_TIMEZONE" \
    --unprivileged 1 \
    --features nesting=1,keyctl=1 \
    --onboot 1 \
    --tags church-pos \
    --description "Church POS. Update with church-pos-update, back up with church-pos-backup."
  CREATED_CTID=$CTID

  step "Starting container" pct start "$CTID"
  wait_for "Waiting for the network" "getent hosts deb.debian.org" 30

  step "Updating Debian" in_ct "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get -y -o Dpkg::Options::=--force-confold full-upgrade"
  step "Installing tools" in_ct "DEBIAN_FRONTEND=noninteractive apt-get install -y curl git ca-certificates cron unattended-upgrades"
  step "Turning on automatic security updates" in_ct "printf 'APT::Periodic::Update-Package-Lists \"1\";\nAPT::Periodic::Unattended-Upgrade \"1\";\n' >/etc/apt/apt.conf.d/20auto-upgrades"
  step "Installing Docker" in_ct "curl -fsSL https://get.docker.com | sh"

  msg_info "Checking that Docker can run here"
  if ! in_ct "docker run --rm hello-world" >>"$LOG" 2>&1; then
    tail -n 25 "$LOG" >&2
    die "Docker can't run containers inside this LXC. Update the Proxmox host (apt update && apt full-upgrade), reboot it, then run this again. Log: $LOG"
  fi
  msg_ok "Checking that Docker can run here"

  step "Downloading Church POS" in_ct "git clone --branch $(printf '%q' "$BRANCH") $(printf '%q' "$REPO_URL") $APP_DIR"
  step "Saving settings" write_env
  step "Building and starting Church POS (this takes a few minutes)" in_ct "cd $APP_DIR && docker compose up -d --build"
  wait_for "Waiting for Church POS to respond" "curl -fsS http://localhost:$POS_PORT/api/health" 45
  step "Installing nightly backups and the update command" install_helpers
  # The shell from pct enter leaves /usr/local/bin out of PATH
  step "Adding the commands to root's PATH" in_ct "grep -qs '/usr/local/bin' /root/.bashrc || echo 'export PATH=\"/usr/local/sbin:/usr/local/bin:\$PATH\"' >> /root/.bashrc"

  local address
  if [[ $IP_ADDRESS == dhcp ]]; then
    address=$(pct exec "$CTID" -- hostname -I | awk '{print $1}')
  else
    address=${IP_ADDRESS%/*}
  fi

  if [[ $USE_PROXY == yes ]]; then
    cat <<EOF

${GREEN}${BOLD}Church POS is running.${RESET}

  Point your nginx site at:  ${BOLD}http://$address:$POS_PORT${RESET}
  Then open your HTTPS address and log in with your admin code to add cashiers.
  Logins only work through HTTPS. The README's "Hosting publicly" section has
  an example nginx config and a check that visitors' addresses come through.
EOF
  else
    cat <<EOF

${GREEN}${BOLD}Church POS is running.${RESET}

  Open:  ${BOLD}http://$address:$POS_PORT${RESET}
  Log in with your admin code to add cashiers.
EOF
  fi
  cat <<EOF

  Container $CTID starts automatically when Proxmox boots.
  Open a shell in it with:  pct enter $CTID

  Inside the container:
    church-pos-backup    back up now (also runs nightly at 11:30 PM)
    church-pos-update    back up, then install the latest version
    church-pos-unban     list blocked devices, or unblock one

  Backups are kept in $BACKUP_DIR for 30 days, along with a copy
  of the settings file. They live on the same disk as the app, so also set
  up a Proxmox backup job for container $CTID (Datacenter > Backup).
EOF
  if [[ $IP_ADDRESS == dhcp ]]; then
    printf '\n  %s!%s The address came from DHCP. Reserve it in your router so it stays the same.\n' "$YELLOW" "$RESET"
  fi
  printf '\n'
}

main "$@"
