# Выкладка панели на сервер

Сервер: `root@185.168.208.134`, Ubuntu 24.04. Бот уже работает там как служба
`pizza-bot` в папке `/root/pizza-bot`. **Бота не останавливаем и не меняем.**

Адрес панели: `https://185-168-208-134.sslip.io` (записан только в `Caddyfile`).

Команды из шагов 1–2 выполняются **на своём компьютере** в папке `pizza-crm`
(PowerShell или Git Bash), остальные — **на сервере** после `ssh root@185.168.208.134`.

---

## 0. Перед началом: всё закоммичено

На сервер попадают только файлы из git (последний коммит). Проверьте на компьютере:

```
git status
```

Должно быть «nothing to commit». Иначе — сначала коммит.

## 1. Резервная копия базы (на сервере)

Без копии дальше не идём. Копия делается встроенным в Python механизмом SQLite
`backup` — он безопасен, даже если бот в этот момент пишет в базу (обычный `cp`
мог бы поймать файл наполовину записанным).

```
mkdir -p /root/backup
BACKUP=/root/backup/orders-$(TZ=Europe/Moscow date +%Y-%m-%d_%H%M%S).db
python3 -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close()" /root/pizza-bot/orders.db "$BACKUP"
python3 -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(c.execute('PRAGMA integrity_check').fetchone()[0], c.execute('SELECT COUNT(*) FROM orders').fetchone()[0], 'заказов')" "$BACKUP"
```

Должно напечатать `ok N заказов`, где N — столько же, сколько в боевой базе.

## 2. Код панели на сервер (на компьютере)

Копируются только файлы из git — без `.venv`, `dev/`, `.env`:

```
git archive --format=tar HEAD | ssh root@185.168.208.134 "mkdir -p /root/pizza-crm && tar -x -C /root/pizza-crm"
```

При обновлении панели — та же команда, затем `systemctl restart pizza-crm`.

## 3. Python-окружение (на сервере)

```
cd /root/pizza-crm
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Если `python3 -m venv` ругается на `ensurepip` — `apt install -y python3-venv`.

## 4. Настройки `/root/pizza-crm/.env` (на сервере)

`SECRET_KEY` создаётся прямо на сервере и на экран не выводится. Токен бота
в `.env` панели **не копируется** — панель читает его из `.env` бота.
`set -o noclobber` не даст случайно перезаписать уже существующий `.env`.

```
cd /root/pizza-crm
set -o noclobber
umask 077
{
  echo "SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  echo "ORDERS_DB=/root/pizza-bot/orders.db"
  echo "BOT_ENV_FILE=/root/pizza-bot/.env"
} > .env
set +o noclobber
cut -d= -f1 .env      # проверка: видны только названия настроек, без значений
```

## 5. Caddy (на сервере)

Ставим из официального репозитория Caddy:

```
apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
apt update
apt install -y caddy
```

Свой `Caddyfile` вместо стандартного (стандартный сохраняем рядом):

```
cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.orig
cp /root/pizza-crm/deploy/Caddyfile /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy
```

## 6. Порты 80 и 443

Порт 80 нужен Let's Encrypt для проверки и для перенаправления на HTTPS, 443 — сама панель.

Проверьте файрвол: `ufw status`.

- **`Status: inactive`** — ничего делать не нужно. **Не включайте ufw**: без
  разрешённого порта 22 вы потеряете доступ к серверу по SSH.
- **`Status: active`** — сначала убедитесь, что в списке есть `22` (или `OpenSSH`),
  и только потом:
  ```
  ufw allow 80/tcp
  ufw allow 443/tcp
  ```

Если у хостинга есть свой файрвол в панели управления — там тоже должны быть открыты 80 и 443.

## 7. Служба панели (на сервере)

```
cp /root/pizza-crm/deploy/pizza-crm.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now pizza-crm
```

## 8. Проверка

```
systemctl status pizza-crm --no-pager      # active (running)
systemctl status pizza-bot --no-pager      # бот по-прежнему active (running)
journalctl -u caddy --no-pager | grep -iE "certificate obtained|error"
curl -I https://185-168-208-134.sslip.io   # HTTP/2 302 → /login
free -h
```

Если панель не запустилась — причина в `journalctl -u pizza-crm -n 50 --no-pager`
(например, «в .env не задано …»).

## 9. Первый админ (на сервере)

```
cd /root/pizza-crm && .venv/bin/flask --app app create-admin
```

Команда спросит логин, имя и пароль (не короче 8 символов). Оператора можно
добавить так же: `.venv/bin/flask --app app create-admin --role operator`.

## Если Let's Encrypt не выдал сертификат

Для общего домена sslip.io бывают лимиты. Тогда покупается свой домен
(например, `crm.<домен>.ru`), его A-запись указывается на `185.168.208.134`,
в `/etc/caddy/Caddyfile` меняется первая строка на этот домен и выполняется
`systemctl reload caddy`. Код панели не меняется.
