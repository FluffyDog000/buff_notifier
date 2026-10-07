# Запуск на сервере и веб-панель через DuckDNS

Команды вводятся **по одной**: скопировать строку, вставить, Enter. Строки,
начинающиеся с `#`, — пояснения, их вводить не нужно.

Что получится:

```
buff-notifier   служба опроса: BuffMarket → оценка → Telegram
buff-web        веб-панель на 127.0.0.1:5050 (снаружи не видна)
Caddy           https://ВАШ-ПОДДОМЕН.duckdns.org → 127.0.0.1:5050, сертификат сам
```

---

## 1. Обновить код и зависимости

```bash
cd /root/buff_notifier
```
```bash
git pull
```
```bash
.venv/bin/pip install -r requirements.txt
```
```bash
.venv/bin/python -m pytest -q
```

Последняя команда должна закончиться словом `passed`.

## 2. Пароль веб-панели

Панель будет открыта в интернет, без пароля она не работает. Не короче 10
символов; в `.env` попадает только хэш.

```bash
.venv/bin/python -m tools.set_password
```

## 3. Службы

```bash
cp deploy/buff-notifier.service deploy/buff-web.service /etc/systemd/system/
```
```bash
systemctl daemon-reload
```
```bash
systemctl enable --now buff-notifier buff-web
```
```bash
systemctl status buff-notifier buff-web --no-pager
```

Обе должны быть `active (running)`. Логи: `journalctl -u buff-notifier -f`
(выход — Ctrl+C).

## 4. Поддомен DuckDNS

1. Откройте https://www.duckdns.org и войдите (через Google, GitHub или Reddit).
2. В поле **sub domain** впишите имя, например `mybuff`, нажмите **add domain**.
3. В строке нового домена в поле **current ip** впишите IP сервера и нажмите
   **update ip**. IP сервера покажет команда на сервере:
   ```bash
   curl -4 -s https://api.ipify.org; echo
   ```
4. Проверьте на сервере, что имя указывает на сервер (подставьте своё):
   ```bash
   getent hosts mybuff.duckdns.org
   ```
   Должен напечататься тот же IP. Если пусто — подождите пару минут.

У VPS адрес постоянный, поэтому автообновление IP в DuckDNS не нужно.

## 5. HTTPS через Caddy

Сначала узнайте, стоит ли Caddy (его мог поставить CSFloat-бот для своего
дашборда):

```bash
systemctl status caddy --no-pager
```

**Если Caddy нет** (`could not be found`) — поставить:

```bash
apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
```
```bash
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
```
```bash
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
```
```bash
apt update && apt install -y caddy
```

**Дальше — в обоих случаях.** Откройте настройки Caddy:

```bash
nano /etc/caddy/Caddyfile
```

Добавьте **в конец** файла блок (имя — ваше). Если там уже есть блок
CSFloat-бота — его не трогайте, только допишите новый ниже. Если файл свежий и
в нём пример с `:80 { ... }` — пример можно удалить.

```
mybuff.duckdns.org {
    reverse_proxy 127.0.0.1:5050
}
```

Сохранить: Ctrl+O, Enter, Ctrl+X. Проверить и применить:

```bash
caddy validate --config /etc/caddy/Caddyfile
```
```bash
systemctl reload caddy
```

## 6. Порты

Если включён файрвол `ufw`, Caddy нужны 80 и 443 (80 — для получения
сертификата):

```bash
ufw status
```

Если написано `Status: active`:

```bash
ufw allow 80/tcp
```
```bash
ufw allow 443/tcp
```

Порт 5050 открывать **не нужно**: панель слушает только 127.0.0.1.

У некоторых провайдеров VPS есть ещё свой файрвол в личном кабинете — там
тоже должны быть открыты 80 и 443.

## 7. Открыть

`https://mybuff.duckdns.org` — форма входа, замок в адресной строке. Первый
раз сертификат получается до минуты.

В панели:

1. **Настройки → Telegram**: токен бота (от @BotFather), напишите боту
   `/start`, «Найти chat id», «Сохранить», «Тестовое сообщение».
2. **Настройки → Сессия BuffMarket**: «Copy as cURL» запроса `sell_order` с
   компьютера, где вы вошли на buff.market → «Сохранить сессию» → «Проверить».
3. **Настройки → База CSFloat-бота**: путь к `csfloat_sales.db`, если он не
   `/root/csfloatpricesparcing/data/csfloat_sales.db`.
4. **Предметы**: goods_id по строке.

Всё, что меняется в панели, служба опроса подхватывает сама, перезапуск не
нужен.

## База с другого сервера

Если CSFloat-бот работает на **другом** сервере, база приходит оттуда копией
раз в час: предметы и продажи за 60 дней, несколько мегабайт. Ключ SSH для
этого умеет только одно — выдать копию: зайти по нему на сервер бота нельзя.

Ниже **«сервер бота»** — где работает CSFloat-бот, **«этот сервер»** — где
buff_notifier.

**1. На сервере бота — найти базу:**

```bash
find / -name "csfloat_sales*.db" -not -path "/proc/*" -not -path "/tmp/*" 2>/dev/null
```

Запомните путь (дальше — `ПУТЬ_К_БАЗЕ`).

**2. На сервере бота — положить скрипт копии.** Он только читает базу.

```bash
cd /root
```
```bash
git clone https://github.com/fluffydog000/buff_notifier.git buff_export
```
```bash
cd /root/buff_export && git checkout claude/new-session-gco59s
```

Проверка — должно напечататься число больше нуля:

```bash
python3 /root/buff_export/tools/csfloat_export.py ПУТЬ_К_БАЗЕ | wc -c
```

**3. На этом сервере — сделать ключ и показать его:**

```bash
ssh-keygen -t ed25519 -N "" -f /root/.ssh/csfloat_pull
```
```bash
cat /root/.ssh/csfloat_pull.pub
```

Скопируйте напечатанную строку целиком (начинается с `ssh-ed25519`). Это
открытая часть ключа, её не страшно показывать.

**4. На сервере бота — разрешить этому ключу только копию.** В команде
замените `ПУТЬ_К_БАЗЕ` и `СТРОКА_КЛЮЧА` (одинарные кавычки оставить):

```bash
mkdir -p /root/.ssh && chmod 700 /root/.ssh
```
```bash
echo 'command="python3 /root/buff_export/tools/csfloat_export.py ПУТЬ_К_БАЗЕ",restrict СТРОКА_КЛЮЧА' >> /root/.ssh/authorized_keys
```

**5. На этом сервере — указать сервер бота и проверить.** В панели:
**Настройки → Опрос → Сервер CSFloat-бота** — `root@IP-сервера-бота`,
«Сохранить настройки». Затем:

```bash
cd /root/buff_notifier && git pull
```
```bash
.venv/bin/python -m tools.pull_csfloat
```

Должно быть `Готово: N предметов, M продаж…`. Путь к базе в настройках
встанет сам (`data/csfloat_snapshot.db`).

**6. На этом сервере — копия каждый час:**

```bash
cp deploy/buff-pull.service deploy/buff-pull.timer /etc/systemd/system/
```
```bash
systemctl daemon-reload
```
```bash
systemctl enable --now buff-pull.timer
```

На «Обзоре» под «База CSFloat» видно, когда пришла последняя копия.

Если на шаге 5 `Permission denied` — на сервере бота вход под root по ключу
запрещён (`PermitRootLogin no`). Тогда шаги 2 и 4 делаются под тем
пользователем, от которого работает бот (`/home/ИМЯ/...` вместо `/root/...`),
а в панели — `ИМЯ@IP-сервера-бота`.

## Если не открывается

```bash
systemctl status buff-web --no-pager
```
```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5050/login
```

`200` или `503` — панель работает, дело в Caddy или DNS:

```bash
journalctl -u caddy -n 50 --no-pager
```

Частые причины: имя в Caddyfile не совпадает с DuckDNS, в DuckDNS не тот IP,
закрыт порт 80 (Let's Encrypt не может проверить домен).

## Обновление кода

Для функции входа в Steam нужен Chromium. После установки requirements:

```bash
.venv/bin/python -m playwright install --with-deps chromium
```

Сессии дополнительных аккаунтов хранятся в `data/accounts.json` (600),
авторизация браузеров — в `data/browser_profiles` (700). При переносе
сервера сохраните эти приватные файлы вместе с `.env`; в Git они не входят.
Панель запускает один браузер входа одновременно и закрывает его по
таймауту/отмене/перезапуску службы. При обновлении Playwright установите
соответствующий Chromium указанной командой.

```bash
cd /root/buff_notifier && git pull && .venv/bin/pip install -r requirements.txt
```
```bash
systemctl restart buff-notifier buff-web
```
