const settingsForm = document.querySelector('#signal-settings');
if (settingsForm) {
  function example() {
    const value = k => Number(settingsForm.elements[k].value.replace(',', '.')) / 100;
    const discount = value('min_discount');
    const fee = value('csfloat_fee');
    const base = settingsForm.elements.discount_basis.value === 'median' ? 100 : 100 * (1 - fee);
    const cost = base * (1 - discount);
    const net = 100 * (1 - fee);
    const profit = net - cost;
    const mode = settingsForm.elements.price_basis.value;
    const target = mode === 'either' ? 'по диапазону float ИЛИ по всему предмету' : mode === 'min' ? 'по диапазону float И по всему предмету' : mode === 'item' ? 'по всему предмету' : 'по диапазону float';
    document.querySelector('#signal-example').textContent = `Пример ${target}: медиана $100 → затраты на покупку до $${cost.toFixed(2)} при скидке ${(discount * 100).toFixed(0)}%. После комиссии CSFloat остаётся $${net.toFixed(2)}; выгода $${profit.toFixed(2)} (${cost ? (profit / cost * 100).toFixed(1) : '—'}% от вложений). Дополнительно применяются оба порога выгоды ниже.`;
  }
  settingsForm.addEventListener('input', example);
  settingsForm.addEventListener('change', example);
  example();
}
