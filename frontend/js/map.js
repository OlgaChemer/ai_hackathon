// Карта подборки: квартиры (метки с ценой) и то, что рядом — школы, поликлиники, парки.
// Leaflet хранится в проекте и загружается только когда нужна карта.
import { html } from "./format.js";

const LEAFLET = "/vendor/leaflet";
const instances = new WeakMap();

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
    const css = Object.assign(document.createElement("link"), { rel: "stylesheet", href: `${LEAFLET}/leaflet.css` });
    const js = Object.assign(document.createElement("script"), { src: `${LEAFLET}/leaflet.js` });
    js.onload = () => resolve(window.L);
    js.onerror = () => { loading = null; reject(new Error("Leaflet не загрузился")); };
    document.head.append(css, js);
  });
  return loading;
}

const mln = (v) => (v / 1e6).toFixed(1).replace(".", ",").replace(",0", "");

/** Рисует карту в node по карточкам подборки. */
export async function mountMap(node, cards) {
  const flats = cards.filter((c) => Number.isFinite(c.lat) && Number.isFinite(c.lng));
  if (!flats.length) { node.closest("[data-map-wrap]")?.remove(); return; }
  let L, config;
  try {
    [L, config] = await Promise.all([loadLeaflet(), fetch('/api/map').then(r => r.ok ? r.json() : {}).catch(() => ({}))]);
  } catch {
    node.innerHTML = html`<p class="map-fallback">Карта не загрузилась — проверьте интернет.</p>`;
    return;
  }
  if (!node.isConnected) return;
  const map = L.map(node, { scrollWheelZoom: false, zoomControl: true });
  map.attributionControl.setPrefix('<a href="https://leafletjs.com" title="Библиотека карты">Leaflet</a>');
  instances.set(node, map);
  const use2gis = config.provider === '2gis';
  const note = (text, external = false) => {
    node.querySelector('.map-error')?.remove();
    const center = map.getCenter();
    const url = `https://2gis.ru/geo/${center.lng},${center.lat}`;
    const message = document.createElement('p');
    message.className = 'map-error';
    message.innerHTML = html`${text}${external ? html.raw(html` <a href="${url}" target="_blank" rel="noopener noreferrer">Открыть 2ГИС</a>`) : ''}`;
    node.append(message);
  };
  let activeLayer;
  const setTiles = (provider) => {
    const is2gis = provider === '2gis';
    let loaded = 0;
    const layer = L.tileLayer(is2gis ? '/api/map/tiles/{z}/{x}/{y}.png' : 'https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: is2gis ? '© <a href="https://2gis.ru">2ГИС</a>' : '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      maxZoom: 19,
    });
    activeLayer = layer;
    layer.on('tileload', () => {
      if (activeLayer !== layer) return;
      loaded++;
      if (!use2gis || is2gis) node.querySelector('.map-error')?.remove();
    });
    layer.on('tileerror', () => {
      if (activeLayer !== layer) return;
      if (is2gis) {
        layer.remove();
        note('Карта 2ГИС временно недоступна — показываем OpenStreetMap.');
        setTiles('osm');
      } else if (!loaded) {
        note('Подложка карты не загрузилась. Метки квартир сохранены.', true);
      }
    });
    layer.addTo(map);
  };
  setTiles(use2gis ? '2gis' : 'osm');
  const observer = new ResizeObserver(() => { if (node.isConnected && node.clientWidth) map.invalidateSize(); });
  observer.observe(node);
  map.on('unload', () => observer.disconnect());


  const bounds = [];
  const seen = new Set();
  for (const c of flats) {
    for (const p of c.nearby || []) {
      if (!p.lat || seen.has(`${p.kind}${p.lat}${p.lng}`)) continue;
      seen.add(`${p.kind}${p.lat}${p.lng}`);
      L.circleMarker([p.lat, p.lng], {
        radius: 6, weight: 2, color: "#fff", fillColor: POI_COLORS[p.kind] || "#6b7280", fillOpacity: 1,
      }).addTo(map).bindTooltip(html`${p.label}: ${p.name}`);
      bounds.push([p.lat, p.lng]);
    }
  }
  for (const c of flats) {
    const icon = L.divIcon({ className: "flat-pin", html: `<span>${mln(c.price)}</span>`, iconSize: null });
    const marker = L.marker([c.lat, c.lng], { icon, riseOnHover: true }).addTo(map);
    marker.bindTooltip(html`${c.rooms === 0 ? "Студия" : `${c.rooms}-комн.`}, ${c.area} м² · ${c.address}`);
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

export function unmountMap(node) {
  if (!node) return;
  instances.get(node)?.remove();
  instances.delete(node);
}
