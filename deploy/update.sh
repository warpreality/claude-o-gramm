#!/usr/bin/env bash
# Автообновление tg-claude: если в origin/main есть новые коммиты — подтягиваем,
# ставим зависимости и перезапускаем бота. Сессии Claude живут в tmux и рестарт переживают.
set -euo pipefail

cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"
BRANCH="${TGC_BRANCH:-main}"

git fetch -q origin "$BRANCH"
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")
if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0
fi

echo "обновление ${LOCAL:0:7} -> ${REMOTE:0:7}"
git log --oneline "$LOCAL..$REMOTE"
# .env и прочие неотслеживаемые файлы не трогаются
git reset -q --hard "origin/$BRANCH"
uv sync --frozen -q

# если поменялись юниты systemd — переустанавливаем
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
for f in deploy/*.service deploy/*.timer; do
    cmp -s "$f" "$UNIT_DIR/$(basename "$f")" || cp "$f" "$UNIT_DIR/"
done
systemctl --user daemon-reload
systemctl --user restart tg-claude
echo "готово"
