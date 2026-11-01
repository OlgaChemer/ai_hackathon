// Карта подборки: квартиры (метки с ценой) и то, что рядом — школы, поликлиники, парки.
// Leaflet грузится с CDN только когда нужна карта; если не загрузился — подборка работает без карты.
import { html } from "./format.js";

const LEAFLET = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4";

// цвета видов объектов (у квартир — фирменный зелёный)
export const POI_COLORS = {
  school: "#1b8fd8",
  kindergarten: "#e08a00",
  clinic: "#8a5cf5",
  hospital: "#c0396b",
  park: "#2f9e5b",
  water: "#2563eb",
  shop: "#6b7280",
  mall: "#a16207",
  pharmacy: "#0f9fa3",
  fitness: "#d9480f",
};

let loading = null;

function loadLeaflet() {
  if (window.L) return Promise.resolve(window.L);
  loading ??= new Promise((resolve, reject) => {
    const css = Object.assign(document.createElement("link"), { rel: "stylesheet", href: `${LEAFLET}/leaflet.min.css` });
    const js = Object.assign(document.createElement("script"), { src: `${LEAFLET}/leaflet.min.js` });
    js.onload = () => resolve(window.L);
    js.onerror = () => { loading = null; reject(new Error("Leaflet не загрузился")); };
    document.head.append(css, js);
  });
  return loading;
}

const mln = (v) => (v / 1e6).toFixed(1).replace(".", ",").replace(",0", "");

/** Рисует карту в node по карточкам подборки. */
export async function mountMap(node, cards) {
  const flats = cards.filter((c) => c.lat && c.lng);
  if (!flats.length) { node.closest("[data-map-wrap]")?.remove(); return; }
  let L;
  try {
    L = await loadLeaflet();
  } catch {
    node.innerHTML = html`<p class="map-fallback">Карта не загрузилась — проверьте интернет.</p>`;
    return;
  }
  if (!node.isConnected) return;
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  const map = L.map(node, { scrollWheelZoom: false, zoomControl: true });
  L.tileLayer(`https://{s}.basemaps.cartocdn.com/${dark ? "dark_all" : "light_all"}/{z}/{x}/{y}{r}.png`, {
    attribution: "© OpenStreetMap, © CARTO", subdomains: "abcd", maxZoom: 19,
  }).addTo(map);

  const bounds = [];
  const seen = new Set();
  for (const c of flats) {
    for (const p of c.nearby || []) {
      if (!p.lat || seen.has(`${p.kind}${p.lat}${p.lng}`)) continue;
      seen.add(`${p.kind}${p.lat}${p.lng}`);
      L.circleMarker([p.lat, p.lng], {
        radius: 6, weight: 2, color: "#fff", fillColor: POI_COLORS[p.kind] || "#6b7280", fillOpacity: 1,
      }).addTo(map).bindTooltip(`${p.label}: ${p.name}`);
      bounds.push([p.lat, p.lng]);
    }
  }
  for (const c of flats) {
    const icon = L.divIcon({ className: "flat-pin", html: `<span>${mln(c.price)}</span>`, iconSize: null });
    const marker = L.marker([c.lat, c.lng], { icon, riseOnHover: true }).addTo(map);
    marker.bindTooltip(`${c.rooms === 0 ? "Студия" : `${c.rooms}-комн.`}, ${c.area} м² · ${c.address}`);
    marker.on("click", () => {
      const cardNode = document.querySelector(`.offer[data-id="${CSS.escape(c.id)}"]`);
      cardNode?.scrollIntoView({ behavior: "smooth", block: "center" });
      cardNode?.classList.add("is-flash");
      setTimeout(() => cardNode?.classList.remove("is-flash"), 1200);
    });
    bounds.push([c.lat, c.lng]);
  }
  map.fitBounds(bounds, { padding: [28, 28], maxZoom: 15 });
}
