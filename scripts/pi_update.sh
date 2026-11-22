#!/usr/bin/env bash
# NetRadar Pi auto-update: run periodically by netradar-update.timer (systemd user unit).
#
# 1. Fetch origin/main. If nothing new, exit.
# 2. Update only with a fast-forward on a clean checkout of the deploy branch;
#    never delete or overwrite local changes on the Pi.
# 3. Install Python dependencies if requirements*.txt changed.
# 4. Restart netradar.service if runtime files changed (docs/frontend/tests do not
#    require it), then wait for /healthz.
# 5. If the system was healthy before the update but is not after it, roll back
#    to the previous commit and mark the bad commit so it is not tried again.
#    The next commit pushed to GitHub is tried as usual.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

REMOTE="${NETRADAR_UPDATE_REMOTE:-origin}"
BRANCH="${NETRADAR_UPDATE_BRANCH:-main}"
SERVICE="${NETRADAR_SERVICE:-netradar.service}"
HEALTH_URL="${NETRADAR_HEALTH_URL:-http://127.0.0.1:8000/healthz}"
HEALTH_TIMEOUT="${NETRADAR_HEALTH_TIMEOUT:-120}"
# After /healthz answers, the service must stay up this long (catches SDR crashes).
HEALTH_SETTLE="${NETRADAR_HEALTH_SETTLE:-20}"
STATE_DIR="$ROOT/.netradar-run"
BAD_COMMIT_FILE="$STATE_DIR/bad-commit"

log() { printf '[netradar-update] %s\n' "$*"; }

svc() { systemctl --user "$@"; }

service_running() {
    local state
    state="$(svc is-active "$SERVICE" 2>/dev/null || true)"
    [[ "$state" == "active" ]]
}

service_wanted() {
    # Restart if it is enabled (incl. a crash loop) or currently running.
    # A service stopped by hand (disabled and inactive) is left stopped.
    svc is-enabled --quiet "$SERVICE" 2>/dev/null || service_running
}

healthy_now() {
    service_running && curl --fail --silent --max-time 3 "$HEALTH_URL" >/dev/null 2>&1
}

wait_healthy() {
    local waited=0
    while (( waited < HEALTH_TIMEOUT )); do
        if healthy_now; then
            sleep "$HEALTH_SETTLE"
            healthy_now && return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done
    healthy_now
}

needs_restart() {
    # Runtime files on the Pi: anything except docs, the frontend (served from
    # the Mac by Vite), tests, fixtures, and Markdown files.
    local file
    while IFS= read -r file; do
        [[ -n "$file" ]] || continue
        case "$file" in
            docs/*|frontend/*|tests/*|fixtures/*|*.md) ;;
            *) return 0 ;;
        esac
    done
    return 1
}

install_requirements() {
    if [[ ! -x .venv/bin/python ]]; then
        log ".venv yok; bağımlılıklar servis ilk açılışta kurulacak."
        return 0
    fi
    log "requirements değişti; Python bağımlılıkları kuruluyor..."
    .venv/bin/python -m pip install --quiet -r requirements-dev.txt
}

mkdir -p "$STATE_DIR"

current_branch="$(git symbolic-ref --quiet --short HEAD || true)"
if [[ "$current_branch" != "$BRANCH" ]]; then
    log "Pi '$current_branch' branch'inde, '$BRANCH' değil; güncelleme atlandı."
    exit 0
fi

if ! git fetch --quiet "$REMOTE" "$BRANCH"; then
    log "GitHub'a ulaşılamadı; sonraki denemede tekrar bakılacak."
    exit 0
fi

old="$(git rev-parse HEAD)"
new="$(git rev-parse "refs/remotes/$REMOTE/$BRANCH")"
[[ "$old" != "$new" ]] || exit 0

if [[ -f "$BAD_COMMIT_FILE" && "$(cat "$BAD_COMMIT_FILE")" == "$new" ]]; then
    exit 0   # This commit already failed and was rolled back; wait for a newer one.
fi
if ! git merge-base --is-ancestor "$old" "$new"; then
    log "Yerel $BRANCH, $REMOTE/$BRANCH'den ayrışmış; güncelleme atlandı (elle çöz)."
    exit 0
fi
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    log "Pi'de commit edilmemiş değişiklik var; güncelleme atlandı (silinmez)."
    exit 0
fi

changed="$(git diff --name-only "$old" "$new")"
was_healthy=0
healthy_now && was_healthy=1

log "Güncelleniyor: ${old:0:7} -> ${new:0:7}"
git merge --ff-only --quiet "$new"

requirements_changed=0
if grep -q '^requirements' <<<"$changed"; then
    requirements_changed=1
fi

rollback() {
    local reason="$1"
    log "HATA: $reason. ${old:0:7} sürümüne geri dönülüyor."
    git reset --quiet --keep "$old"
    if (( requirements_changed )); then
        install_requirements || log "Eski bağımlılıklar geri kurulamadı."
    fi
    printf '%s\n' "$new" > "$BAD_COMMIT_FILE"
    if service_wanted; then
        svc restart "$SERVICE" || true
        wait_healthy || log "Eski sürüm de sağlıklı açılmadı; alıcıyı kontrol et."
    fi
    exit 1
}

if (( requirements_changed )); then
    install_requirements || rollback "bağımlılık kurulumu başarısız"
fi

if ! needs_restart <<<"$changed"; then
    log "Sadece doküman/frontend/test değişti; servis yeniden başlatılmadı."
    rm -f "$BAD_COMMIT_FILE"
    exit 0
fi

if ! service_wanted; then
    log "$SERVICE elle durdurulmuş; kod güncellendi ama servis başlatılmadı."
    exit 0
fi

log "$SERVICE yeniden başlatılıyor..."
svc restart "$SERVICE" || true
if wait_healthy; then
    rm -f "$BAD_COMMIT_FILE"
    log "Güncelleme tamam: ${new:0:7} çalışıyor."
    exit 0
fi

if (( was_healthy )); then
    rollback "yeni sürüm ${HEALTH_TIMEOUT}s içinde sağlıklı açılmadı"
fi
log "UYARI: servis sağlıklı değil; güncellemeden önce de değildi (dongle bağlı mı?)."
log "Güncelleme geri alınmadı: ${new:0:7} çalışmaya çalışıyor."
exit 0
