#!/usr/bin/env bash
# Автообновление tg-claude: если в origin/main есть новые коммиты — подтягиваем, проверяем тестами,
# перезапускаем бота и убеждаемся, что он поднялся. Если что-то не так — откатываемся на прошлую версию.
# Сессии Claude живут в tmux и рестарт переживают.
set -euo pipefail

cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"
BRANCH="${TGC_BRANCH:-main}"
UNIT_DIR="$HOME/.config/systemd/user"
BAD=".update-bad"  # коммит, на котором обновление уже провалилось — не пробуем его снова

git fetch -q origin "$BRANCH"
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")
if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0
fi
if [ "$(cat "$BAD" 2>/dev/null)" = "$REMOTE" ]; then
    exit 0  # эта версия уже не прошла проверку; ждём следующего коммита
fi

install_units() {
    mkdir -p "$UNIT_DIR"
    for f in deploy/*.service deploy/*.timer; do
        cmp -s "$f" "$UNIT_DIR/$(basename "$f")" || cp "$f" "$UNIT_DIR/"
    done
    systemctl --user daemon-reload
}

restarts() {
    systemctl --user show tg-claude -p NRestarts --value
}

# откат: возвращаем прошлую версию и перезапускаем бота на ней
rollback() {
    echo "ОТКАТ на ${LOCAL:0:7}: $1" >&2
    echo "$REMOTE" > "$BAD"
    git reset -q --hard "$LOCAL"
    uv sync --frozen -q
    install_units
    systemctl --user restart tg-claude
    exit 1
}

echo "обновление ${LOCAL:0:7} -> ${REMOTE:0:7}"
git log --oneline "$LOCAL..$REMOTE"
# .env и прочие неотслеживаемые файлы не трогаются
git reset -q --hard "$REMOTE"
uv sync --frozen -q

# 1. проверка до перезапуска: код импортируется и тесты проходят
if ! uv run --frozen python -c "import tg_claude.bot, tg_claude.mattermost" >/dev/null; then
    git reset -q --hard "$LOCAL"; uv sync --frozen -q; echo "$REMOTE" > "$BAD"
    echo "ОТМЕНА: новая версия не импортируется, бот не перезапускался" >&2
    exit 1
fi
if ! uv run --frozen pytest -q -x >/tmp/tg-claude-update-tests.log 2>&1; then
    tail -20 /tmp/tg-claude-update-tests.log >&2
    git reset -q --hard "$LOCAL"; uv sync --frozen -q; echo "$REMOTE" > "$BAD"
    echo "ОТМЕНА: тесты не прошли, бот не перезапускался" >&2
    exit 1
fi

# 2. перезапуск и проверка, что бот поднялся и не падает
install_units
systemctl --user restart tg-claude
before=$(restarts)  # ручной restart обнуляет счётчик — читаем после него
sleep 20
if ! systemctl --user is-active -q tg-claude; then
    rollback "бот не запустился"
fi
if [ "$(restarts)" != "$before" ]; then
    rollback "бот падает и перезапускается"
fi
echo "готово"
