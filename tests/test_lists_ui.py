"""Real DOM selection and form submission against the Flask test client."""
import os
from pathlib import Path
import threading
from contextlib import ExitStack

import pytest
from werkzeug.serving import make_server

from tests.test_web import app, logged_in
from notifier.accounts import Accounts, profile_key
from notifier.store import Store


@pytest.mark.skipif(not os.environ.get('BUFF_TEST_BROWSER'), reason='opt-in Chromium check')
def test_compact_lists_select_only_visible_rows_and_submit_actions(app):
    from playwright.sync_api import sync_playwright
    st = Store(app.tmp / 'buff.db')
    for gid in range(60):
        st.add_watch(gid, f'Item {gid:03}')
    reg = Accounts(app.tmp / 'accounts.json', app.tmp / '.env')
    for n in range(3):
        aid = reg.save(None, f'Ready {n:02}', {'BUFF_COOKIE': f'session=PRIVATE{n}'}, 'http://u:SECRET@proxy:80', 5)
        a = next(p for p in reg.list() if p['id'] == aid)
        reg.tested(aid, profile_key(a), f'1.2.3.{n+1}')
        reg.set_enabled([aid], True)
    c = logged_in(app)
    app.config['SESSION_COOKIE_SECURE'] = False
    server = make_server('127.0.0.1', 0, app)
    origin = f'http://127.0.0.1:{server.server_port}'
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Testing only: use the signed session of the logged-in Flask client.
    with ExitStack() as cleanup, sync_playwright() as pw:
        cleanup.callback(lambda: (server.shutdown(), server.server_close(), thread.join(timeout=5)))
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={'width': 1280, 'height': 900}, color_scheme='dark')
        ctx.add_cookies([dict(name='session', value=c.get_cookie('session').value, url=origin)])
        page = ctx.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(origin + '/items?per_page=25&page=2')
        assert page.locator('input[name="ids"]').count() == 25
        page.locator('[data-select-all]').check()
        assert page.locator('[data-selected-count]').inner_text() == 'Выбрано: 25'
        page.locator('[data-select-all]').uncheck()
        page.locator('input[name="ids"][value="26"]').check()
        page.locator('input[name="ids"][value="27"]').check()
        page.locator('#items-selection button[value="disable"]').click()
        page.wait_for_url('**/items?**')
        assert {w['goods_id'] for w in st.watch_list() if not w['active']} == {26, 27}
        page.goto(origin + '/items?status=disabled')
        page.locator('[data-select-all]').check()
        page.on('dialog', lambda dialog: dialog.accept())
        page.locator('#items-selection button[value="delete"]').click()
        page.wait_for_url('**/items?**')
        assert len(st.watch_list()) == 58
        page.goto(origin + '/accounts?q=Ready+01')
        assert page.locator('input[name="ids"]').count() == 1
        assert 'SECRET' not in page.content() and 'session=PRIVATE' not in page.content()
        page.locator('[data-select-all]').check()
        page.locator('#accounts-selection button[value="disable"]').click()
        page.wait_for_url('**/accounts?**')
        assert not next(a for a in reg.list() if a['label'] == 'Ready 01')['enabled']
        page.goto(origin + '/accounts')
        page.get_by_text('Управление', exact=True).first.click()
        page.get_by_text('Изменить название, сессию, прокси или паузу', exact=True).first.click()
        assert page.locator('input[name="label"]').first.is_visible()
        shots = Path(__file__).resolve().parent.parent / 'data' / 'ui-preview'
        shots.mkdir(parents=True, exist_ok=True)
        page.goto(origin + '/accounts')
        page.screenshot(path=str(shots / 'accounts-desktop.png'), full_page=True)
        page.goto(origin + '/settings')
        page.locator('#price_basis').select_option('either')
        page.locator('#discount_basis').select_option('median')
        page.locator('#min_discount').fill('20')
        assert '$80.00' in page.locator('#signal-example').inner_text()
        page.screenshot(path=str(shots / 'settings-desktop.png'), full_page=True)
        page.goto(origin + '/items?per_page=25')
        page.screenshot(path=str(shots / 'items-desktop.png'), full_page=True)
        page.set_viewport_size({'width': 390, 'height': 844})
        page.goto(origin + '/accounts')
        assert page.locator('[data-select-all]').is_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.screenshot(path=str(shots / 'accounts-mobile.png'), full_page=True)
        assert not errors
        browser.close()
