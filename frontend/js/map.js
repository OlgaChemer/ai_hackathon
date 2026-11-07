// Карта подборки: квартиры (метки с ценой) и то, что рядом — школы, поликлиники, парки.
// Leaflet хранится в проекте и загружается только когда нужна карта.
import { html } from "./format.js";

const LEAFLET = "/vendor/leaflet";
const instances = new WeakMap();
const pending = new WeakMap();

export const hasCoordinates = c => Number.isFinite(c.lat) && Math.abs(c.lat) <= 90
  && Number.isFinite(c.lng) && Math.abs(c.lng) <= 180;

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
export function mountMap(node, cards) {
  const ready = renderMap(node, cards);
  pending.set(node, ready);
  return ready;
}

async function renderMap(node, cards) {
  const flats = cards.filter(hasCoordinates);
  if (!flats.length) { node.closest("[data-map-wrap]")?.remove(); return; }
  let L, config;
  try {
    [L, config] = await Promise.all([loadLeaflet(), fetch('/api/map').then(r => r.ok ? r.json() : {}).catch(() => ({}))]);
  } catch {
    node.innerHTML = html`<p class="map-fallback">Карта не загрузилась — проверьте интернет.</p>`;
    return;
  }
  if (!node.isConnected || !pending.has(node)) return;
  const map = L.map(node, { scrollWheelZoom: false, zoomControl: true });
  map.attributionControl.setPrefix('<a href="https://leafletjs.com" title="Библиотека карты">Leaflet</a>');
  const markers = new Map();
  instances.set(node, { map, markers });
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
    markers.set(c.id, marker);
    marker.bindTooltip(html`${c.rooms === 0 ? "Студия" : `${c.rooms}-комн.`}, ${c.area} м² · ${c.address}`,
      {direction:'top', offset:[0,-8], className:'flat-tooltip'});
    marker.on("click", () => {
      const cardNode = node.closest('.panel')?.querySelector(`.offer[data-id="${CSS.escape(c.id)}"]`);
      cardNode?.scrollIntoView({ behavior: "smooth", block: "center" });
      cardNode?.classList.add("is-flash");
      setTimeout(() => cardNode?.classList.remove("is-flash"), 1200);
    });
    bounds.push([c.lat, c.lng]);
  }
  map.fitBounds(bounds, { padding: [28, 28], maxZoom: 15 });
}

export async function focusMap(node, listingId) {
  if (!node) return false;
  await pending.get(node);
  const instance = instances.get(node);
  const marker = instance?.markers.get(listingId);
  if (!node.isConnected || !marker) return false;
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  instance.map.setView(marker.getLatLng(), 16, { animate: !reduced });
  instance.markers.forEach(m => m.getElement()?.classList.remove('is-selected'));
  marker.getElement()?.classList.add('is-selected');
  marker.openTooltip();
  node.scrollIntoView({ behavior: reduced ? 'instant' : 'smooth', block:'center' });
  node.focus({preventScroll:true});
  return true;
}

export function unmountMap(node) {
  if (!node) return;
  pending.delete(node);
  instances.get(node)?.map.remove();
  instances.delete(node);
}
