# buff_notifier

Уведомления в Telegram о выгодных лотах на BuffMarket: лот дешевле рынка или
дорогой float по обычной цене. «Рынок» — история продаж CSFloat из базы
CSFloat-бота. Вводная — `csfloatpricesparcing/docs/BUFF_NOTIFIER.md`.

## Рамки

- Один аккаунт, один постоянный адрес, обычный темп. Ограничивает сайт —
  сокращаем список или частоту, а не размножаем аккаунты.
- База CSFloat-бота только читается (`mode=ro`). Сам бот не меняется.

## На сервере

```
/root/csfloatpricesparcing           CSFloat-бот, база в data/csfloat_sales.db
/root/buff_notifier                  этот сервис
```

```bash
cd /root/buff_notifier
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env
.venv/bin/python -m pytest -q
```

## Что откуда

- `notifier/estimate.py`, `notifier/recency.py`, `notifier/phases.py` —
  перенесены из `csfloatpricesparcing/src` (коммит в шапке файла) без изменений
  в логике: медиана по сотой float, пересчёт к сегодняшним ценам, фазы
  Doppler. Перенос, а не импорт по пути: перестановка внутри бота не должна
  молча ломать уведомления. Связь с ботом — только схема базы.
- `notifier/sales.py` — чтение `items` и `sales` на чтение, с `age_days`.
- `notifier/buff.py` — запрос страницы предмета
  `api.buff.market/api/market/goods/sell_order`, по одному, с паузой
  `BUFF_MIN_INTERVAL`. Без входа сайт отвечает `Login Required`, поэтому
  идёт сессия одного аккаунта из `.env`: `BUFF_COOKIE`, `BUFF_CSRF`,
  `BUFF_USER_AGENT`.

## Сессия аккаунта

В браузере, где вы вошли на buff.market: F12 → Network → страница предмета →
запрос `sell_order` → правой кнопкой → Copy → Copy as cURL (bash). Затем на
сервере:

```bash
.venv/bin/python -m tools.set_session      # вставить curl, Enter, Ctrl+D
```

Куки, токен и User-Agent запишутся в `.env` (права 600), значения не
печатаются.

## Пробник

Один запрос, форма ответа и первые лоты:

```bash
.venv/bin/python -m tools.probe 5777 --save probe.json
```
