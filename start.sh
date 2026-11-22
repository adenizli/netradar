#!/usr/bin/env bash
# One command on Mac starts Pi RTL-SDR, FastAPI and the local NetRadar interface.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
RUNTIME="$ROOT/.netradar-run"
PI_STATE="$RUNTIME/pi"
BACKEND="http://127.0.0.1:8000"
RADAR="http://127.0.0.1:5173"

info() { printf '[NetRadar] %s\n' "$*"; }
die() { printf '[NetRadar] HATA: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "'$1' bulunamadı. $2"; }
ready() { curl --fail --silent --max-time 2 "$1" >/dev/null 2>&1; }

wait_for_exit() {
    local pid="$1" attempts="$2"
    while kill -0 "$pid" 2>/dev/null; do
        if (( attempts == 0 )); then return 1; fi
        attempts=$((attempts - 1))
        sleep 0.2
    done
}

stop_pi() {
    local requested="$1" actual="" pid="" args=""
    [[ -f "$PI_STATE/session" && -f "$PI_STATE/pid" ]] || return 0
    read -r actual < "$PI_STATE/session"
    [[ "$actual" == "$requested" ]] || return 0
    read -r pid < "$PI_STATE/pid"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 0
    args="$(ps -p "$pid" -o args= 2>/dev/null || true)"
    # Do not kill an unrelated process in case a PID was reused.
    if [[ "$args" == *"start.sh --pi"* ]] && kill -TERM "$pid" 2>/dev/null; then
        # Keep the SSH connection open while the remote launcher drains delivery.
        wait_for_exit "$pid" 120 || true
    fi
}

cleanup_pi() {
    trap - EXIT INT TERM HUP
    info "Pi servisleri kapatılıyor..."
    if [[ -n "${SDR_PID:-}" ]]; then
        kill -INT -- "-$SDR_PID" 2>/dev/null || kill -TERM "$SDR_PID" 2>/dev/null || true
        # Leave the API available through RTL cleanup and the producer's 10-second drain.
        if ! wait_for_exit "$SDR_PID" 100; then
            kill -TERM -- "-$SDR_PID" 2>/dev/null || true
            if ! wait_for_exit "$SDR_PID" 10; then
                kill -KILL -- "-$SDR_PID" 2>/dev/null || true
            fi
        fi
        wait "$SDR_PID" 2>/dev/null || true
    fi
    if [[ -n "${API_PID:-}" ]]; then
        kill -TERM "$API_PID" 2>/dev/null || true
        wait "$API_PID" 2>/dev/null || true
    fi
    if [[ -f "$PI_STATE/session" ]]; then
        local current=""
        read -r current < "$PI_STATE/session"
        [[ "$current" != "$NETRADAR_SESSION_ID" ]] || rm -rf "$PI_STATE"
    fi
}

run_pi() {
    [[ "$(uname -s)" == "Linux" ]] || die "--pi modu Linux/Raspberry Pi içindir."
    [[ "${NETRADAR_SESSION_ID:-}" =~ ^[0-9a-f]{32}$ ]] || die "Geçersiz başlatma oturumu."
    need rtl_sdr "Pi üzerinde: sudo apt install rtl-sdr"
    need curl "Pi üzerinde: sudo apt install curl"
    need setsid "util-linux paketi gerekli."
    mkdir -p "$RUNTIME"
    if ! mkdir "$PI_STATE" 2>/dev/null; then
        local pid=""
        [[ ! -r "$PI_STATE/pid" ]] || read -r pid < "$PI_STATE/pid"
        if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
            die "Pi üzerinde NetRadar zaten çalışıyor."
        fi
        rm -rf "$PI_STATE"
        mkdir "$PI_STATE" || die "Başka NetRadar oturumu başlatılıyor."
    fi
    printf '%s\n' "$$" > "$PI_STATE/pid"
    printf '%s\n' "$NETRADAR_SESSION_ID" > "$PI_STATE/session"
    trap cleanup_pi EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM HUP

    if [[ ! -x .venv/bin/python ]]; then
        need python3 "Python 3.10+ kur."
        python3 -m venv .venv
    fi
    if ! .venv/bin/python -c 'import fastapi, uvicorn, numpy, httpx' >/dev/null 2>&1; then
        info "Python bağımlılıkları kuruluyor..."
        .venv/bin/python -m pip install -r requirements-dev.txt
    fi
    curl --fail --silent "$BACKEND/healthz" >/dev/null 2>&1 &&
        die "8000 portunda bir backend zaten çalışıyor."
    info "FastAPI başlatılıyor (Pi localhost; erişim SSH tüneliyle)..."
    NETRADAR_EXPECT_RECEIVER=1 .venv/bin/python -m uvicorn netradar.backend.server:app \
        --host 127.0.0.1 --port 8000 --workers 1 --no-proxy-headers &
    API_PID=$!
    local attempt
    for attempt in $(seq 1 50); do
        ready "$BACKEND/healthz" && break
        kill -0 "$API_PID" 2>/dev/null || die "Backend başlayamadı."
        sleep 0.3
    done
    ready "$BACKEND/healthz" || die "Backend sağlık kontrolü başarısız."
    info "1090 MHz RTL-SDR alıcısı başlatılıyor..."
    # Isolate the subprocess group so rtl_sdr also stops at shutdown.
    setsid .venv/bin/python -m netradar.pipeline --rtl-sdr \
        --gain-db "${NETRADAR_GAIN_DB:-35}" --backend-url "$BACKEND" --diagnostics &
    SDR_PID=$!
    while :; do
        kill -0 "$API_PID" 2>/dev/null || die "Backend beklenmedik şekilde kapandı."
        if [[ -n "$SDR_PID" ]] && ! kill -0 "$SDR_PID" 2>/dev/null; then
            wait "$SDR_PID" 2>/dev/null || true
            SDR_PID=""
            info "SDR pipeline kapandı. Backend tanılama için açık kalacak."
        fi
        sleep 1
    done
}

cleanup_mac() {
    trap - EXIT INT TERM
    if [[ -n "${WEB_PID:-}" ]]; then
        kill -TERM "$WEB_PID" 2>/dev/null || true
        wait "$WEB_PID" 2>/dev/null || true
    fi
    if [[ -n "${SSH_SOCKET:-}" && -n "${TARGET:-}" && -n "${REMOTE_STOP:-}" ]]; then
        ssh -S "$SSH_SOCKET" -o ControlMaster=auto -o BatchMode=yes -T "$TARGET" \
            "$REMOTE_STOP" </dev/null >/dev/null 2>&1 || true
    fi
    if [[ -n "${REMOTE_PID:-}" ]]; then
        kill -TERM "$REMOTE_PID" 2>/dev/null || true
        wait "$REMOTE_PID" 2>/dev/null || true
    fi
    if [[ -n "${SSH_SOCKET:-}" && -n "${TARGET:-}" ]]; then
        ssh -S "$SSH_SOCKET" -O exit "$TARGET" >/dev/null 2>&1 || true
    fi
    [[ -z "${SSH_TMP:-}" ]] || rm -rf "$SSH_TMP"
    info "Kapatıldı."
}

install_frontend() {
    need npm "Node.js 22.12+ veya 24+ kur."
    need node "Node.js 22.12+ veya 24+ kur."
    local version
    version="$(node -p 'const [a,b]=process.versions.node.split(".").map(Number); (a===22&&b>=12)||a>=24 ? "ok":"bad"')"
    [[ "$version" == "ok" ]] || die "Node.js 22.12+ veya 24+ gerekli."
    if [[ ! -x frontend/node_modules/.bin/vite ||
          frontend/package-lock.json -nt frontend/node_modules/.package-lock.json ]]; then
        info "Frontend bağımlılıkları hazırlanıyor (npm ci)..."
        (cd frontend && npm ci)
    fi
}

run_mac() {
    need curl "curl gerekli."
    [[ "$(uname -s)" == "Darwin" ]] ||
        die "Varsayılan başlatma Mac içindir. Pi yalnızca --pi ile başlatılır."
    install_frontend
    mkdir -p "$RUNTIME"
    TARGET="${NETRADAR_PI_TARGET:-netradar@netradar.local}"
    trap cleanup_mac EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    need ssh "Pi için SSH gerekli."
    need python3 "Oturum kimliği oluşturmak için Python 3 gerekli."
    SSH_TMP="$(mktemp -d /tmp/netradar.XXXXXX)"
    SSH_SOCKET="$SSH_TMP/ssh.sock"
    local session directory quoted_dir remote_start
    session="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
    directory="${NETRADAR_PI_DIR:-netradar}"
    [[ -n "$directory" && "$directory" != "." && "$directory" != ".." ]] ||
        die "NETRADAR_PI_DIR geçersiz."
    printf -v quoted_dir '%q' "$directory"
    [[ "$directory" == /* ]] || quoted_dir="\$HOME/$quoted_dir"
    info "Pi SSH tüneli: $TARGET (gerekirse bir defa şifre sorulur)."
    ssh -M -S "$SSH_SOCKET" -o ControlMaster=yes -o ControlPersist=no \
        -o ExitOnForwardFailure=yes -o ConnectTimeout=12 -f -N \
        -L 127.0.0.1:8000:127.0.0.1:8000 "$TARGET" ||
        die "Pi'ye SSH bağlantısı kurulamadı."
    remote_start="if [ ! -d $quoted_dir/.git ]; then git clone https://github.com/adenizli/netradar.git $quoted_dir || exit 1; fi; git -C $quoted_dir pull --ff-only && cd $quoted_dir && exec env NETRADAR_SESSION_ID=$session NETRADAR_GAIN_DB=${NETRADAR_GAIN_DB:-35} bash start.sh --pi"
    REMOTE_STOP="cd $quoted_dir && bash start.sh --stop-pi $session"
    info "Pi'de backend ve RTL-SDR başlatılıyor..."
    ssh -S "$SSH_SOCKET" -o ControlMaster=auto -o BatchMode=yes \
        -T "$TARGET" "$remote_start" &
    REMOTE_PID=$!
    local i
    for i in $(seq 1 180); do
        ready "$BACKEND/healthz" && break
        kill -0 "$REMOTE_PID" 2>/dev/null || die "Pi başlatılamadı; loglara bak."
        sleep 1
    done
    ready "$BACKEND/healthz" || die "Backend hazır değil; Pi loglarını kontrol et."
    info "Radar arayüzü başlatılıyor..."
    (cd frontend; export VITE_BACKEND_URL="$BACKEND"; \
        export VITE_LAUNCHER=1; \
        exec ./node_modules/.bin/vite --host 127.0.0.1 --port 5173 --strictPort) &
    WEB_PID=$!
    for i in $(seq 1 45); do
        ready "$RADAR" && break
        kill -0 "$WEB_PID" 2>/dev/null || die "Frontend açılamadı."
        sleep 0.3
    done
    ready "$RADAR" || die "Radar arayüzü hazır değil."
    info "RADAR HAZIR: $RADAR (Ctrl+C ile tüm süreçleri kapat)"
    open "$RADAR" >/dev/null 2>&1 || true
    while :; do
        kill -0 "$WEB_PID" 2>/dev/null || die "Frontend kapandı."
        kill -0 "$REMOTE_PID" 2>/dev/null || die "Pi bağlantısı/alıcı kapandı."
        sleep 1
    done
}

case "${1:-}" in
    --pi) run_pi ;;
    --stop-pi) [[ -n "${2:-}" ]] && stop_pi "$2" ;;
    --help|-h)
        cat <<'HELP'
NetRadar tek komut (Mac -> Raspberry Pi + frontend)
  ./start.sh          Canlı RTL-SDR + backend + radar arayüzü
Değişkenler: NETRADAR_PI_TARGET, NETRADAR_PI_DIR, NETRADAR_GAIN_DB
Kapat: Ctrl+C
HELP
        ;;
    "") run_mac ;;
    *) die "Bilinmeyen argüman: $1 (./start.sh --help)" ;;
esac
