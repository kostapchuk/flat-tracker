#!/bin/bash
# Страхующий запуск на своей машине.
#
# Работает с тем же состоянием, что и GitHub Actions: сначала подтягивает
# репозиторий, потом проверяет — и только если GitHub давно не отчитывался.
# Поэтому дублей уведомлений не будет: кто первый нашёл квартиру, тот её и
# записал в state.json, второй увидит её уже известной.
set -euo pipefail

cd "$(dirname "$0")"

# порог простоя в минутах: меньше — уступаем основному расписанию
STALE_AFTER="${STALE_AFTER:-25}"

git pull --rebase --autostash -q || {
    echo "не смог обновить репозиторий, пропускаю запуск" >&2
    exit 0
}

python3 check.py --if-stale "$STALE_AFTER" --verbose

git add state.json
if git diff --staged --quiet; then
    exit 0
fi

# как и в воркфлоу: подряд идущие коммиты состояния схлопываем в один
if git log -1 --pretty=%s | grep -q '^state: ' && [ "$(git log -1 --pretty=%an)" = "flat-tracker" ]; then
    git -c user.name=flat-tracker -c user.email=flat-tracker@users.noreply.github.com \
        commit --amend -q --date=now -m "state: $(date -u +%FT%TZ)"
    git push --force-with-lease -q
else
    git -c user.name=flat-tracker -c user.email=flat-tracker@users.noreply.github.com \
        commit -q -m "state: $(date -u +%FT%TZ)"
    git push -q
fi
