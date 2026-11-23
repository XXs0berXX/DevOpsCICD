// Shared helpers for all pages.

async function api(path, options = {}) {
  const res = await fetch(window.API_BASE + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail ? JSON.stringify(data.detail) : res.statusText);
  return data;
}

const param = (name) => new URLSearchParams(location.search).get(name);
const money = (n) => Number(n).toLocaleString();
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function header() {
  document.body.insertAdjacentHTML("afterbegin", `
    <header>
      <a class="logo" href="index.html">NestMarket</a>
      <nav>
        <a href="index.html">Browse</a>
        <a href="sell.html">Sell</a>
        <a href="dashboard.html">Dashboard</a>
      </nav>
    </header>`);
}

function photo(url) {
  return url ? `<img src="${esc(url)}" alt="">` : `<div class="no-photo">No photo</div>`;
}

function footer() {
  document.body.insertAdjacentHTML("beforeend", `
    <footer>NestMarket &middot; build v${esc(window.APP_VERSION || "dev")}</footer>`);
}

header();
footer();
