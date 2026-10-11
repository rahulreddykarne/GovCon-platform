/* Refresh stored telemetry without dropping input or open inspectors. */
document.addEventListener('DOMContentLoaded', function () {
  const controls = document.querySelector('[data-console-refresh]');
  if (!controls) return;
  let dirty = false;
  let busy = false;
  document.addEventListener('input', function (event) {
    if (!controls.contains(event.target) && event.target.closest('main')) dirty = true;
  });
  document.addEventListener('change', function (event) {
    if (!controls.contains(event.target) && event.target.closest('main')) dirty = true;
  });
  async function refresh() {
    if (busy || document.hidden) return;
    if (dirty) { const message = document.querySelector('[data-refresh-status]'); if (message) message.textContent = 'Refresh paused: finish or save your form first.'; return; }
    busy = true;
    try {
      const response = await fetch(window.location.href, {cache: 'no-store', signal: AbortSignal.timeout(10000)});
      if (response.redirected && new URL(response.url).pathname === '/login') { window.location.assign('/login'); return; }
      if (!response.ok) throw new Error('refresh unavailable');
      const parsed = new DOMParser().parseFromString(await response.text(), 'text/html');
      const fresh = parsed.querySelector('.mission');
      const current = document.querySelector('.mission');
      if (!fresh || !current) throw new Error('refresh unavailable');
      const openDetails = Array.from(current.querySelectorAll('details')).map((item, index) => item.open ? index : -1);
      current.innerHTML = fresh.innerHTML;
      current.querySelectorAll('details').forEach((item, index) => { item.open = openDetails.includes(index); });
      // Script tags inserted by innerHTML do not execute; rebind the controls explicitly.
      bindControls(current.querySelector('[data-console-refresh]'));
    } catch (_) {
      const status = document.querySelector('[data-refresh-status]');
      if (status) status.textContent = 'Refresh failed. Displayed data is from the previous snapshot.';
    } finally { busy = false; }
  }
  let enabled = false;
  function bindControls(element) {
    if (!element) return;
    const input = element.querySelector('[data-auto-refresh]');
    input.checked = enabled;
    input.addEventListener('change', function () { enabled = input.checked; });
    element.querySelector('[data-refresh-now]').addEventListener('click', function () { refresh(); });
  }
  bindControls(controls);
  window.setInterval(function () { if (enabled) refresh(); }, 15000);
});
