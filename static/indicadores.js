/* Revalidate result regions; filters, open forms and scroll positions stay in the live document. */
(() => {
  const revision = document.querySelector('meta[name="metrics-revision"]');
  let reading = false;
  async function update() {
    if (reading || document.hidden) return;
    reading = true;
    try {
      const res = await fetch('/api/indicadores/revision', {cache:'no-store'});
      if (!res.ok || (await res.json()).revision === revision.content) return;
      const current = Array.from(document.querySelectorAll('[data-live]'));
      if (current.some(region => region.contains(document.activeElement))) return;
      if (!window.getSelection().isCollapsed) return;
      const response = await fetch(location.pathname + location.search, {cache:'no-store'});
      if (!response.ok) return;
      const next = new DOMParser().parseFromString(await response.text(), 'text/html');
      const stamp = next.querySelector('meta[name="metrics-revision"]');
      const updated = next.querySelectorAll('[data-live]');
      if (!stamp || updated.length !== current.length) return;
      current.forEach((region, index) => {
        const positions = Array.from(region.querySelectorAll('.scroll'), e => e.scrollLeft);
        region.replaceChildren(...updated[index].childNodes);
        region.querySelectorAll('.scroll').forEach((e,i) => { e.scrollLeft = positions[i] || 0; });
      });
      revision.content = stamp.content;
    } catch (_) {
      // A transient network failure is retried on the next tick.
    } finally {
      reading = false;
    }
  }
  setInterval(update, 5000);
  document.addEventListener('visibilitychange', update);
})();
