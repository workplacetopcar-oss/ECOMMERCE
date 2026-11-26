(() => {
  const menu = document.querySelector('.admin-menu');
  if (!menu) return;
  const desktop = matchMedia('(min-width:821px)');
  const sync = () => { menu.open = desktop.matches; };
  desktop.addEventListener('change', sync);
  sync();
})();
