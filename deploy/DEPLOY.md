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

И автотесты (на своём компьютере, на временной базе — настоящие данные не трогаются):

```
.venv\Scripts\python -m pip install -r requirements-dev.txt   # один раз
.venv\Scripts\python -m pytest
```

Должно быть «… passed», без «failed».

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
git -c core.autocrlf=false archive --format=tar HEAD | ssh root@185.168.208.134 "mkdir -p /root/pizza-crm && tar -x -C /root/pizza-crm"
```

`-c core.autocrlf=false` обязателен на Windows: иначе Git отдаст файлы с переводами
строк Windows (CRLF), и служба systemd на сервере может не запуститься.

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

## Обновление уже работающей панели

1. Резервная копия базы (шаг 1).
2. Код (шаг 2, на компьютере).
3. На сервере — то, что поменялось:
   ```
   cd /root/pizza-crm
   .venv/bin/pip install -r requirements.txt                        # если менялся requirements.txt
   cp deploy/pizza-crm.service deploy/pizza-crm-backup.* /etc/systemd/system/ && systemctl daemon-reload
   cp deploy/Caddyfile /etc/caddy/Caddyfile && caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy
   systemctl restart pizza-crm
   ```
4. Проверка (шаг 8). Бот при обновлении панели не перезапускается.

## 10. Ночная резервная копия (на сервере)

Каждый день в 04:00 по Москве `backup.py` копирует `orders.db` в
`/root/backup/orders-auto-ГГГГ-ММ-ДД_ЧЧММСС.db` безопасным для SQLite способом,
проверяет копию и удаляет свои копии старше 14 дней. Ручные копии
(`orders-ГГГГ-…db` без `auto`) не удаляются.

Установка (один раз):

```
cp /root/pizza-crm/deploy/pizza-crm-backup.service /root/pizza-crm/deploy/pizza-crm-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now pizza-crm-backup.timer
```

Проверка, что копии делаются:

```
systemctl list-timers pizza-crm-backup.timer     # когда следующий запуск
systemctl start pizza-crm-backup                 # сделать копию прямо сейчас
journalctl -u pizza-crm-backup -n 20 --no-pager  # «Копия готова: … целостность ok, заказов: N»
ls -l /root/backup/
```

Проверка, что из копии можно восстановиться — **боевую базу не трогает**:

```
python3 /root/pizza-crm/backup.py --verify /root/backup/orders-auto-<дата>.db
cp /root/backup/orders-auto-<дата>.db /tmp/restore-test.db
python3 -c "import sqlite3; c=sqlite3.connect('/tmp/restore-test.db'); print(c.execute('SELECT id, created_at, total, status FROM orders ORDER BY id DESC LIMIT 5').fetchall())"
rm /tmp/restore-test.db
```

Должны быть видны последние заказы на момент копии.

### Восстановление из копии (только при аварии)

Бот и панель на время восстановления останавливаются. Повреждённая база
не удаляется, а откладывается рядом:

```
systemctl stop pizza-bot pizza-crm
cd /root/pizza-bot
STAMP=$(date +%Y%m%d-%H%M%S)
for f in orders.db orders.db-wal orders.db-shm; do [ -e "$f" ] && mv "$f" "$f.broken-$STAMP"; done
cp /root/backup/orders-auto-<дата>.db orders.db
python3 /root/pizza-crm/backup.py --verify orders.db
systemctl start pizza-bot pizza-crm
```

Заказы, пришедшие после времени копии, в восстановленной базе не будет.

## 11. SSH: вход только по ключу

Пока в SSH можно войти по паролю, сервер круглосуточно пытаются подобрать
пароль root (тысячи попыток в сутки). Вход только по ключу это закрывает.

**Порядок строго такой** — иначе можно потерять доступ к серверу:

1. Убедиться, что вход по ключу работает (в новом окне):
   `ssh -o PreferredAuthentications=publickey root@185.168.208.134 true` — без вопроса о пароле.
2. **Не закрывать** текущую SSH-сессию до конца проверки.
3. В текущей сессии:
   ```
   cat > /etc/ssh/sshd_config.d/00-key-only.conf <<'EOF'
   PasswordAuthentication no
   KbdInteractiveAuthentication no
   PermitRootLogin prohibit-password
   EOF
   sshd -t && systemctl reload-or-restart ssh
   sshd -T | grep -iE "^(passwordauthentication|permitrootlogin) "
   ```
4. В **новом** окне: вход по ключу работает, а по паролю — отказ
   `Permission denied (publickey)`:
   ```
   ssh root@185.168.208.134 true
   ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password root@185.168.208.134 true
   ```
5. Только после этого закрыть старую сессию.

Если на шаге 4 по ключу не пускает — в старой сессии откатить:
`mv /etc/ssh/sshd_config.d/00-key-only.conf /root/00-key-only.conf.off && systemctl reload-or-restart ssh`.

Если ключ на компьютере потерян — войти можно только через консоль
в панели управления хостинга.

## Если Let's Encrypt не выдал сертификат

Для общего домена sslip.io бывают лимиты. Тогда покупается свой домен
(например, `crm.<домен>.ru`), его A-запись указывается на `185.168.208.134`,
в `/etc/caddy/Caddyfile` меняется первая строка на этот домен и выполняется
`systemctl reload caddy`. Код панели не меняется.
