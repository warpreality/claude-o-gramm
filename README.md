# tg-claude

Telegram-бот, который запускает **Claude Code** в твоих проектах и транслирует диалог в Telegram.

- Каждый **тред** в личке с ботом — отдельная сессия Claude в выбранном проекте.
- Сессия живёт в **tmux** — переживает перезапуск бота, к ней можно подключиться с компьютера и продолжить руками.
- Разрешения: по умолчанию **автомод** Claude, кнопки «Разрешить / Запретить» — только для реально опасного.
- Вопросы Claude (AskUserQuestion) приходят **кнопками**.
- **«💬 Без проекта»** — чат с веб-поиском, без доступа к файлам и командам.
- Реакции: 👀 — сообщение прочитано, 👍 — Claude ответил.

## Настройка бота

1. В [@BotFather](https://t.me/BotFather) создай бота → **Bot Settings → Threaded Mode → Enable** (нужны треды в личке).
2. Узнай свой Telegram id (например, у [@userinfobot](https://t.me/userinfobot)).

## Запуск

Нужны: `tmux`, залогиненный `claude`, Python 3.12+ и [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env   # впиши BOT_TOKEN, ALLOWED_USER_IDS, REPOS_DIR
uv run tg-claude --repos ~/projects
```

## Как пользоваться

1. Создай новый тред в чате с ботом (или отправь `/new`) и напиши задачу.
2. Выбери проект кнопкой — Claude стартует и сразу получит твоё сообщение.
3. Дальше просто переписывайся. Можно присылать файлы и картинки.

Команды: `/status` (проект и команда для подключения), `/esc` (прервать ответ), `/stop` (закрыть сессию), `/new`.
Прочие команды со слешем (`/compact`, `/model` …) уходят прямо в Claude.

Подключиться к сессии с компьютера (команда есть в `/status`):

```bash
tmux -L tg-claude attach -t tgc-<chat>-<thread>
```

Выйти, не закрывая сессию: `Ctrl+B`, затем `D`.

## Как устроено

```
Telegram ⇄ aiogram-бот ──tmux send-keys──▶ claude (TUI в tmux, отдельный сервер tmux -L tg-claude)
              ▲                                   │
              │ читаем ~/.claude/projects/*/<id>.jsonl (ответы, инструменты)
              └── unix-сокет ◀── хуки Claude: PermissionRequest, AskUserQuestion, Stop
```

Состояние — в `~/.tg-claude/state.json`. Если tmux-сессия умерла, следующее сообщение поднимет её через `claude --resume`.

## Автозапуск (systemd, Linux)

Проект клонируется в `~/tg-claude`, рядом кладётся `.env`, затем:

```bash
cp deploy/*.service deploy/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tg-claude tg-claude-update.timer
loginctl enable-linger $USER   # чтобы сервисы жили без входа в систему
```

**Автообновление:** таймер `tg-claude-update.timer` каждые 5 минут запускает `deploy/update.sh` —
если в `origin/main` появились коммиты, он подтягивает их, ставит зависимости и перезапускает бота
(сессии Claude в tmux при этом не теряются). Лог: `journalctl --user -u tg-claude-update`.

## Тесты

```bash
uv run pytest
```
