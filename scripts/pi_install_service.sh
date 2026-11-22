#!/usr/bin/env bash
# Install (or remove) the NetRadar always-on service and GitHub auto-update on the Pi.
#   bash scripts/pi_install_service.sh             install + start
#   bash scripts/pi_install_service.sh --uninstall stop + remove
#   bash scripts/pi_install_service.sh --status    service, timer and recent update logs
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNITS=(netradar.service netradar-update.service netradar-update.timer)
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

info() { printf '[NetRadar] %s\n' "$*"; }
die() { printf '[NetRadar] HATA: %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Linux" ]] || die "Bu script Raspberry Pi (Linux) içindir."
command -v systemctl >/dev/null 2>&1 || die "systemd bulunamadı."

install_units() {
    [[ -d "$ROOT/.git" ]] || die "$ROOT bir git klonu değil."
    local branch
    branch="$(git -C "$ROOT" symbolic-ref --quiet --short HEAD || true)"
    [[ "$branch" == "main" ]] ||
        info "UYARI: Pi '$branch' branch'inde; otomatik güncelleme sadece main'de çalışır."
    mkdir -p "$UNIT_DIR"
    local unit
    for unit in "${UNITS[@]}"; do
        sed "s|@ROOT@|$ROOT|g" "$ROOT/deploy/systemd/$unit" > "$UNIT_DIR/$unit"
    done
    # Linger keeps user services running after logout and starts them at boot.
    if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || true)" != "yes" ]]; then
        info "Açılışta başlaması için linger açılıyor (sudo şifresi sorulabilir)..."
        sudo loginctl enable-linger "$USER"
    fi
    systemctl --user daemon-reload
    systemctl --user enable --now netradar.service netradar-update.timer
    info "Kuruldu. Alıcı sürekli çalışacak; GitHub'daki main her 2 dakikada kontrol edilecek."
    info "Loglar: journalctl --user -u netradar -f   |   journalctl --user -u netradar-update"
}

uninstall_units() {
    systemctl --user disable --now netradar-update.timer netradar.service 2>/dev/null || true
    local unit
    for unit in "${UNITS[@]}"; do rm -f "$UNIT_DIR/$unit"; done
    systemctl --user daemon-reload
    info "Servis ve otomatik güncelleme kaldırıldı."
}

case "${1:-}" in
    "") install_units ;;
    --uninstall) uninstall_units ;;
    --status)
        systemctl --user --no-pager status netradar.service || true
        systemctl --user --no-pager list-timers netradar-update.timer || true
        journalctl --user --no-pager -n 20 -u netradar-update.service || true
        ;;
    *) die "Bilinmeyen argüman: $1" ;;
esac
