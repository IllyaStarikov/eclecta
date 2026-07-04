#!/bin/sh
set -a
[ -f "$HOME/.config/signal/worker.env" ] && . "$HOME/.config/signal/worker.env"
set +a
exec /usr/bin/caffeinate -i /Users/starikov/.pyenv/versions/3.9.22/bin/python3 -m signalpipe worker
