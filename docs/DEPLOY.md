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

```bash
cd /root/buff_notifier && git pull && .venv/bin/pip install -r requirements.txt
```
```bash
systemctl restart buff-notifier buff-web
```
