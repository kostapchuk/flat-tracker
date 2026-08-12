#!/bin/bash
# Ставит/переустанавливает фоновую проверку через launchd (macOS).
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="by.newbor.flat-tracker"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ ! -f "$DIR/.env" ]; then
    echo "Нет $DIR/.env — сначала: cp .env.example .env и заполни токен/chat_id" >&2
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
cp "$DIR/$LABEL.plist" "$TARGET"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$TARGET"

echo "Готово. Проверка запускается каждые 15 минут."
echo "  статус:   launchctl print gui/$(id -u)/$LABEL | head -20"
echo "  логи:     tail -f $DIR/tracker.log"
echo "  запустить сейчас: launchctl kickstart gui/$(id -u)/$LABEL"
echo "  выключить: launchctl bootout gui/$(id -u)/$LABEL"
