#!/usr/bin/env bash
# Install (or remove) NetRadar on the Pi so it comes back by itself after a power cut:
#   - netradar.service (user): receiver, starts at boot, restarts on failure
#   - netradar-update.timer (user): GitHub auto-update every 2 minutes
#   - netradar-monitor.service (system): power/shutdown/internet/receiver event log
#   - persistent, size-capped journal and the hardware watchdog (system drop-ins)
#
#   bash scripts/pi_install_service.sh             install + start (asks for sudo once)
#   bash scripts/pi_install_service.sh --uninstall stop + remove (event logs are kept)
#   bash scripts/pi_install_service.sh --status    services, timer, recent events
#   bash scripts/pi_install_service.sh --events    the event report (power cuts etc.)
#
# NETRADAR_NO_WATCHDOG=1 skips the hardware watchdog.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNITS=(netradar.service netradar-update.service netradar-update.timer)
LOG_DIR="${NETRADAR_LOG_DIR:-$HOME/netradar-logs}"
SYSTEM_UNIT=/etc/systemd/system/netradar-monitor.service
JOURNALD_DROPIN=/etc/systemd/journald.conf.d/netradar.conf
WATCHDOG_DROPIN=/etc/systemd/system.conf.d/netradar-watchdog.conf
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

info() { printf '[NetRadar] %s\n' "$*"; }
warn() { printf '[NetRadar] UYARI: %s\n' "$*"; }
die() { printf '[NetRadar] HATA: %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Linux" ]] || die "Bu script Raspberry Pi (Linux) içindir."
command -v systemctl >/dev/null 2>&1 || die "systemd bulunamadı."

monitor_groups_line() {
    # Journal access (who shut the Pi down) and vcgencmd (under-voltage) need these groups.
    local group groups=()
    for group in adm systemd-journal video; do
        getent group "$group" >/dev/null 2>&1 && groups+=("$group")
    done
    if ((${#groups[@]})); then
        printf 'SupplementaryGroups=%s\n' "${groups[*]}"
    else
        printf '# SupplementaryGroups: none of adm/systemd-journal/video exist\n'
    fi
}

install_user_units() {
    mkdir -p "$UNIT_DIR"
    local unit
    for unit in "${UNITS[@]}"; do
        sed "s|@ROOT@|$ROOT|g" "$ROOT/deploy/systemd/$unit" > "$UNIT_DIR/$unit"
    done
    # Linger starts the user services at boot without anyone logging in.
    if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || true)" != "yes" ]]; then
        info "Açılışta kendiliğinden başlaması için linger açılıyor..."
        sudo loginctl enable-linger "$USER"
    fi
    systemctl --user daemon-reload
    systemctl --user enable --now netradar.service netradar-update.timer
}

install_system_parts() {
    command -v python3 >/dev/null 2>&1 || die "python3 bulunamadı (izleme servisi için gerekli)."
    mkdir -p "$LOG_DIR"
    local groups_line
    groups_line="$(monitor_groups_line)"
    sed -e "s|@ROOT@|$ROOT|g" -e "s|@USER@|$USER|g" -e "s|@LOGDIR@|$LOG_DIR|g" \
        -e "s|@GROUPS_LINE@|$groups_line|" "$ROOT/deploy/systemd/netradar-monitor.service" |
        sudo tee "$SYSTEM_UNIT" >/dev/null

    info "Journal kalıcı yapılıyor (elektrik kesilse de loglar kalsın, en fazla 100 MB)..."
    sudo install -D -m 0644 "$ROOT/deploy/systemd/journald-netradar.conf" "$JOURNALD_DROPIN"
    sudo mkdir -p /var/log/journal
    sudo systemd-tmpfiles --create --prefix /var/log/journal >/dev/null 2>&1 || true
    sudo systemctl restart systemd-journald

    if [[ -n "${NETRADAR_NO_WATCHDOG:-}" ]]; then
        info "Watchdog atlandı (NETRADAR_NO_WATCHDOG)."
    elif [[ -e /dev/watchdog || -e /dev/watchdog0 ]]; then
        info "Donanım watchdog'u açılıyor (sistem donarsa Pi kendiliğinden yeniden başlar)..."
        sudo install -D -m 0644 "$ROOT/deploy/systemd/watchdog-netradar.conf" "$WATCHDOG_DROPIN"
        sudo systemctl daemon-reexec
    else
        warn "/dev/watchdog yok; donma durumunda otomatik yeniden başlatma kurulamadı."
    fi

    sudo systemctl daemon-reload
    sudo systemctl enable netradar-monitor.service >/dev/null
    sudo systemctl restart netradar-monitor.service
}

check_boot_resilience() {
    # Report only; these are not changed automatically because a mistake can stop the Pi booting.
    if grep -qw 'fsck.repair=yes' /proc/cmdline 2>/dev/null; then
        info "OK: elektrik kesintisinden sonra açılışta dosya sistemi otomatik onarılıyor (fsck.repair=yes)."
    else
        warn "fsck.repair=yes açılış parametrelerinde yok. /boot/firmware/cmdline.txt satırının sonuna ekleyin."
    fi
    if systemctl is-active --quiet systemd-timesyncd 2>/dev/null || systemctl is-active --quiet chrony 2>/dev/null; then
        info "OK: saat internetten senkronize ediliyor (Pi'de pil/RTC yok)."
    else
        warn "Saat senkronizasyonu (systemd-timesyncd/chrony) çalışmıyor; olay saatleri yanlış olabilir."
    fi
    if command -v fake-hwclock >/dev/null 2>&1; then
        info "OK: fake-hwclock kurulu (açılışta saat son kayıtlı zamandan başlar)."
    fi
    if command -v nmcli >/dev/null 2>&1; then
        local wifi
        wifi="$(nmcli -t -f NAME,TYPE,AUTOCONNECT connection show 2>/dev/null | grep -E ':(802-11-wireless|802-3-ethernet):' || true)"
        if grep -q ':no$' <<<"$wifi"; then
            warn "Otomatik bağlanmayan ağ profili var: $(grep ':no$' <<<"$wifi" | cut -d: -f1 | tr '\n' ' ')"
        else
            info "OK: ağ profilleri açılışta otomatik bağlanıyor."
        fi
    fi
}

install_all() {
    [[ -d "$ROOT/.git" ]] || die "$ROOT bir git klonu değil."
    local branch
    branch="$(git -C "$ROOT" symbolic-ref --quiet --short HEAD || true)"
    [[ "$branch" == "main" ]] ||
        warn "Pi '$branch' branch'inde; otomatik güncelleme sadece main'de çalışır."
    info "Kurulum için sudo şifresi bir kez sorulabilir."
    install_user_units
    install_system_parts
    check_boot_resilience
    info "Kuruldu. Elektrik gidip gelince Pi açılır, alıcı ve izleme kendiliğinden başlar."
    info "Olay kaydı: $LOG_DIR/olaylar.log   ·   özet: $LOG_DIR/rapor.md"
    info "Rapor: bash scripts/pi_install_service.sh --events   (Mac'ten: ./start.sh --events)"
}

uninstall_all() {
    systemctl --user disable --now netradar-update.timer netradar.service 2>/dev/null || true
    local unit
    for unit in "${UNITS[@]}"; do rm -f "$UNIT_DIR/$unit"; done
    systemctl --user daemon-reload
    if [[ -e "$SYSTEM_UNIT" || -e "$WATCHDOG_DROPIN" || -e "$JOURNALD_DROPIN" ]]; then
        sudo systemctl disable --now netradar-monitor.service 2>/dev/null || true
        sudo rm -f "$SYSTEM_UNIT" "$WATCHDOG_DROPIN" "$JOURNALD_DROPIN"
        sudo systemctl daemon-reexec
        sudo systemctl restart systemd-journald
    fi
    info "Servisler, izleme ve watchdog kaldırıldı. Olay kayıtları $LOG_DIR içinde duruyor."
}

case "${1:-}" in
    "") install_all ;;
    --uninstall) uninstall_all ;;
    --status)
        systemctl --user --no-pager status netradar.service || true
        systemctl --user --no-pager list-timers netradar-update.timer || true
        systemctl --no-pager status netradar-monitor.service || true
        journalctl --user --no-pager -n 10 -u netradar-update.service || true
        if [[ -f "$LOG_DIR/olaylar.log" ]]; then
            info "Son olaylar ($LOG_DIR/olaylar.log):"
            tail -n 15 "$LOG_DIR/olaylar.log"
        fi
        ;;
    --events) python3 "$ROOT/tools/station_monitor.py" --log-dir "$LOG_DIR" --report ;;
    *) die "Bilinmeyen argüman: $1" ;;
esac
