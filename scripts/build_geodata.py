"""Сборка геоданных городов-миллионников из OpenStreetMap (Overpass API).

Запускать там, где есть интернет (один раз, потом раз в месяц):

  .venv/bin/python -m scripts.build_geodata                     # все города
  .venv/bin/python -m scripts.build_geodata --cities Казань Уфа # часть
  .venv/bin/python -m scripts.build_geodata --force             # пересобрать готовые

Для каждого города пишет в backend/geodata/cities/<slug>.json.gz:
  districts — административные районы: границы (прореженные), центр, площадь, признаки для меток;
  places    — неофициальные районы и микрорайоны (Купчино, Уралмаш, Азино…) с привязкой к району;
  metro     — станции метро / метротрама;
  poi       — что рядом (школы, детсады, парки…) в формате backend.geo (для проверки без 2ГИС и Overpass);
  roads     — крупные дороги и железная дорога (для «тихого» дома).

Данные © участники OpenStreetMap, лицензия ODbL. Скрипт бережёт сервер: паузы между запросами,
запасное зеркало, повтор при 429/504. На все города уходит 20–40 минут.
"""
from __future__ import annotations

import argparse
import gzip
import os
import json
import math
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import geo  # noqa: E402  (формат POI и запросы те же, что у живой проверки)
from backend.geodata import CITIES, OUT_DIR, slug  # noqa: E402

MIRRORS = ["https://maps.mail.ru/osm/tools/overpass/api/interpreter",   # зеркало VK — обычно доступно из России
           "https://overpass-api.de/api/interpreter",
           "https://overpass.private.coffee/api/interpreter",
           "https://overpass.kumi.systems/api/interpreter"]
if os.getenv("OVERPASS_URL"):
    MIRRORS.insert(0, os.environ["OVERPASS_URL"])
HEADERS = {"User-Agent": "domclick-agent-geodata/1.0 (hackathon; build once per month)"}
PAUSE_S = 3.0
RING_STEP = 0.0009   # ~100 м между точками границы района
ROAD_STEP = 0.0006   # ~60 м между точками дороги
OKRUG_ABBR = {  # Москва: административные округа
    "Центральный": "ЦАО", "Северный": "САО", "Северо-Восточный": "СВАО", "Восточный": "ВАО", "Юго-Восточный": "ЮВАО",
    "Южный": "ЮАО", "Юго-Западный": "ЮЗАО", "Западный": "ЗАО", "Северо-Западный": "СЗАО", "Зеленоградский": "ЗелАО",
    "Новомосковский": "НАО", "Троицкий": "ТАО",
}
# Районы, у которых центр не попадает в контур своего округа (эксклавы, вытянутые формы), и Зеленоград,
# чей округ Overpass не всегда отдаёт среди admin_level=5. Последний шаг после голосования по точкам контура.
MOSCOW_OKRUG_FALLBACK = {
    "Кунцево": "ЗАО", "Некрасовка": "ЮВАО", "Восточный": "ВАО",
    "Крюково": "ЗелАО", "Старое Крюково": "ЗелАО", "Силино": "ЗелАО", "Матушкино": "ЗелАО", "Савёлки": "ЗелАО",
}
NAME_NOISE = re.compile(r"\b(район|муниципальный округ|внутригородской|административный округ|городской округ|"
                        r"поселение|округ|микрорайон|территория)\b", re.I)


# ------------------------------------------------------------------ Overpass

_mirror_lock = threading.Lock()
_mirror_penalty: dict[str, float] = {}  # чем больше, тем позже пробуем зеркало


def _order() -> list[str]:
    """Зеркала: сначала то, что отвечало последним; зависавшие — в конец."""
    with _mirror_lock:
        return sorted(MIRRORS, key=lambda u: _mirror_penalty.get(u, 0.0))


def _mark(url: str, ok: bool, hung: bool = False) -> None:
    with _mirror_lock:
        if ok:
            _mirror_penalty[url] = min(_mirror_penalty.get(url, 0.0), 0.0) - 1.0  # ответивший — первым
        else:
            _mirror_penalty[url] = max(_mirror_penalty.get(url, 0.0), 0.0) + (10.0 if hung else 1.0)


def partial(remark: str) -> bool:
    """Overpass при нехватке времени или памяти отвечает 200 с частью данных и пишет об этом в remark."""
    r = remark.lower()
    return "runtime error" in r or "timed out" in r or "out of memory" in r


def overpass(query: str, timeout: int = 180, fetch=None) -> list[dict]:
    """Запрос к Overpass: зеркала по очереди, начиная с ответившего последним; зависшие — в конец списка."""
    if fetch is not None:  # тесты подставляют ответы
        return fetch(query)
    last = None
    for attempt in range(5):
        for url in _order():
            try:
                # соединение — 15 с; ответ — чуть дольше лимита самого запроса, но не минуты сверх него
                r = httpx.post(url, data={"data": query}, headers=HEADERS,
                               timeout=httpx.Timeout(timeout + 15, connect=15))
                if r.status_code == 200:
                    body = r.json()
                    remark = body.get("remark") or ""
                    if not partial(remark):
                        _mark(url, True)
                        time.sleep(PAUSE_S)
                        return body.get("elements", [])
                    last = f"{url}: неполный ответ ({remark[:80]})"  # 200, но запрос оборван по времени/памяти
                else:
                    last = f"{url}: HTTP {r.status_code}"
                _mark(url, False)
            except (httpx.TimeoutException,) as exc:
                last = f"{url}: {type(exc).__name__}"
                _mark(url, False, hung=True)
            except (httpx.HTTPError, ValueError) as exc:
                last = f"{url}: {type(exc).__name__}"
                _mark(url, False)
            print("   …", last, file=sys.stderr)
        time.sleep(PAUSE_S * (attempt + 2))  # пауза между кругами, а не после каждого зеркала
    raise RuntimeError(f"Overpass недоступен: {last}. Если серверы не отвечают из вашей сети — включите VPN "
                       f"или укажите другой сервер: --mirror https://…/api/interpreter")


# ------------------------------------------------------------------ геометрия

def okrug_of(c: tuple[float, float], rings: list[list[float]], okrugs: list[dict]) -> str | None:
    """Округ района: по центру, а если центр вне всех округов — по большинству точек контура района."""
    for o in okrugs:
        if o["rings"] and inside(c[0], c[1], o["rings"]):
            return o["abbr"] or o["name"]
    votes: dict[str, int] = {}
    for line in rings:
        for i in range(0, len(line) - 1, 2):
            for o in okrugs:
                if o["rings"] and inside(line[i], line[i + 1], o["rings"]):
                    key = o["abbr"] or o["name"]
                    votes[key] = votes.get(key, 0) + 1
                    break
    return max(votes, key=votes.get) if votes else None


def clean_name(name: str) -> str:
    """«район Хамовники» -> «Хамовники», «Вахитовский район» -> «Вахитовский» (как в выдаче Циана)."""
    text = NAME_NOISE.sub(" ", name or "")
    return re.sub(r"\s+", " ", text).strip(" -,") or (name or "").strip()


def thin(points: list[tuple[float, float]], step: float) -> list[float]:
    """Прореживание линии: оставляем точки не чаще step градусов; концы — всегда."""
    kept: list[float] = []
    for i, (lat, lng) in enumerate(points):
        if not kept or i == len(points) - 1 or abs(lat - kept[-2]) > step or abs(lng - kept[-1]) > step * 1.8:
            kept += [round(lat, 5), round(lng, 5)]
    return kept


def rings_of(rel: dict) -> list[list[float]]:
    """Контуры отношения как набор линий (outer и inner). Для «точка внутри» хватает чётности пересечений
    по всем отрезкам — собирать кольца из кусков не нужно."""
    out = []
    for m in rel.get("members") or []:
        if m.get("type") == "way" and m.get("geometry") and m.get("role") in ("outer", "inner", ""):
            line = thin([(p["lat"], p["lon"]) for p in m["geometry"] if p], RING_STEP)
            if len(line) >= 4:
                out.append(line)
    return out


def inside(lat: float, lng: float, rings: list[list[float]]) -> bool:
    """Точка внутри многоугольника (чётность пересечений луча по всем отрезкам контура)."""
    hit = False
    for line in rings:
        for i in range(0, len(line) - 2, 2):
            y1, x1, y2, x2 = line[i], line[i + 1], line[i + 2], line[i + 3]
            if (y1 > lat) != (y2 > lat) and lng < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
                hit = not hit
    return hit


def area_km2(rings: list[list[float]]) -> float:
    """Площадь (формула шнурка в локальной проекции): outer и inner дают разные знаки — берём модуль суммы
    по кольцам, которые сами замкнуты; для незамкнутых кусков это оценка, её хватает для плотности."""
    total = 0.0
    for line in rings:
        if len(line) < 6:
            continue
        lat0 = line[0]
        kx, ky = 111.32 * math.cos(math.radians(lat0)), 110.54
        pts = [((line[i + 1]) * kx, line[i] * ky) for i in range(0, len(line), 2)]
        s = sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1] for i in range(len(pts)))
        total += s / 2
    return round(abs(total), 2)


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    return geo.distance_m(a[0], a[1], b[0], b[1]) / 1000


def center_of(el: dict) -> tuple[float, float] | None:
    if "lat" in el:
        return el["lat"], el["lon"]
    c = el.get("center")
    return (c["lat"], c["lon"]) if c else None


# ------------------------------------------------------------------ сборка города

def city_area(cfg: dict, fetch=None) -> tuple[int, tuple[float, float]]:
    """Граница города -> (id области Overpass, центр города). Ищем по точному имени в индексе областей —
    это быстро; relation в CITIES — если находится не та граница."""
    name = cfg["name"]
    if cfg.get("relation"):
        area_id = 3600000000 + cfg["relation"]
    else:
        els = overpass(f'[out:json][timeout:60];(area["boundary"="administrative"]["name"="{name}"];'
                       f'area["boundary"="administrative"]["name"="городской округ {name}"];'
                       f'area["boundary"="administrative"]["name"="город {name}"];);out ids tags;', 60, fetch)
        rank = {"4": 0, "6": 1, "5": 2, "8": 3}
        els = [e for e in els if (e.get("tags") or {}).get("admin_level") in rank]
        if not els:
            # граница названа иначе («Челябинский городской округ», «Пермский городской округ»): берём
            # административные области, в которых лежит точка города, и выбираем ту, где есть основа названия
            near = overpass(f'[out:json][timeout:60];node["place"="city"]["name"="{name}"];is_in;'
                            f'area._["boundary"="administrative"]["admin_level"~"^(4|5|6|8)$"];out ids tags;', 60, fetch)
            stem = name.casefold().replace("ё", "е")[:max(4, len(name) - 2)]
            # уровень 4 — край/область/республика: городу федерального значения хватает поиска по точному имени
            els = [e for e in near if (e.get("tags") or {}).get("admin_level") in ("6", "8", "5")
                   and stem in (e["tags"].get("name") or "").casefold().replace("ё", "е")]
        els.sort(key=lambda e: rank[e["tags"]["admin_level"]])
        if not els:
            raise RuntimeError(f"{name}: граница города в OSM не найдена — задайте relation в backend.geodata.CITIES")
        area_id = els[0]["id"]
    nodes = overpass(f'[out:json][timeout:60];area({area_id})->.c;node(area.c)["place"="city"]["name"="{name}"];out;', 60, fetch)
    if cfg.get("center"):
        center = tuple(cfg["center"])
    elif nodes:
        center = (nodes[0]["lat"], nodes[0]["lon"])
    else:
        raise RuntimeError(f"{name}: точка центра (place=city) не найдена — задайте center в backend.geodata.CITIES")
    return area_id, center


def fetch_roads(name: str, area: str, fetch=None, log=print) -> dict:
    """Крупные дороги и ж/д города (для «тихо»)."""
    roads = {"major": [], "rail": []}
    for e in overpass(f'[out:json][timeout:240];{area}way(area.c)["highway"~"^(motorway|trunk|primary)$"];out geom qt;', 240, fetch):
        if e.get("geometry"):
            roads["major"].append(thin([(p["lat"], p["lon"]) for p in e["geometry"] if p], ROAD_STEP))
    for e in overpass(f'[out:json][timeout:240];{area}way(area.c)["railway"="rail"]["service"!~"."];out geom qt;', 240, fetch):
        if e.get("geometry"):
            roads["rail"].append(thin([(p["lat"], p["lon"]) for p in e["geometry"] if p], ROAD_STEP))
    log(f"  {name}: крупных дорог {len(roads['major'])}, ж/д {len(roads['rail'])}")
    if not roads["rail"] or len(roads["major"]) < 300:  # Уфа: 396 при полном ответе — так в OSM
        log(f"  {name}: подозрительно мало дорог или нет ж/д — проверьте: --cities {name} --roads")
    return roads


def refresh_roads(cfg: dict, fetch=None, log=print) -> dict:
    """Пересобрать только дороги и ж/д у готового города; остальное не трогаем."""
    path = OUT_DIR / f"{slug(cfg['name'])}.poi.json.gz"
    with gzip.open(path, "rt", encoding="utf-8") as f:
        extra = json.load(f)
    area_id, _ = city_area(cfg, fetch)
    extra["roads"] = fetch_roads(cfg["name"], f"area({area_id})->.c;", fetch, log)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(extra, f, ensure_ascii=False, separators=(",", ":"))
    return extra


def build_city(cfg: dict, fetch=None, log=print) -> tuple[dict, dict]:
    """-> (справочник города, данные POI/дорог). fetch — подмена Overpass в тестах."""
    name = cfg["name"]
    area_id, center = city_area(cfg, fetch)
    area = f"area({area_id})->.c;"
    log(f"  {name}: граница найдена, центр {center[0]:.4f},{center[1]:.4f}")

    # районы: уровень выбираем по городу (Москва — 8 и округа 5, СПб — 5, остальные — 9)
    levels = cfg.get("district_levels") or ["9", "8", "5"]
    heads = overpass(f'[out:json][timeout:120];{area}rel(area.c)["boundary"="administrative"]'
                     f'["admin_level"~"^({"|".join(sorted(set(levels + ["5"])))})$"]["name"];out ids tags center;', 120, fetch)
    by_level: dict[str, list[dict]] = {}
    for h in heads:
        by_level.setdefault(h["tags"].get("admin_level"), []).append(h)
    level = next((lv for lv in levels if len(by_level.get(lv, [])) >= 3), None)
    if level is None:
        raise RuntimeError(f"{name}: районы не найдены (уровни {levels})")
    chosen = by_level[level]
    ids = ",".join(str(h["id"]) for h in chosen)
    shapes = {e["id"]: e for e in overpass(f"[out:json][timeout:300];rel(id:{ids});out geom;", 300, fetch)}
    okrugs = []
    if cfg.get("okrugs") and level != "5":
        heads5 = by_level.get("5", [])
        geo5 = {e["id"]: e for e in overpass(f"[out:json][timeout:300];rel(id:{','.join(str(h['id']) for h in heads5)});out geom;", 300, fetch)} if heads5 else {}
        for h in heads5:
            label = clean_name(h["tags"]["name"])
            okrugs.append({"name": label, "abbr": OKRUG_ABBR.get(label), "rings": rings_of(geo5.get(h["id"], {}))})
    districts = []
    for h in chosen:
        rings = rings_of(shapes.get(h["id"], {}))
        c = center_of(h) or center
        d = {"name": clean_name(h["tags"]["name"]), "osm_name": h["tags"]["name"], "level": level,
             "centroid": [round(c[0], 5), round(c[1], 5)], "rings": rings, "area_km2": area_km2(rings),
             "dist_km": round(km(c, center), 1)}
        lats = [v for line in rings for v in line[0::2]] or [c[0]]
        lngs = [v for line in rings for v in line[1::2]] or [c[1]]
        d["bbox"] = [min(lats), min(lngs), max(lats), max(lngs)]
        if okrugs:
            d["okrug"] = okrug_of(c, rings, okrugs) or MOSCOW_OKRUG_FALLBACK.get(d["name"])
        if rings and inside(center[0], center[1], rings):
            d["has_center"] = True
        districts.append(d)
    log(f"  {name}: районов {len(districts)} (уровень {level})" + (f", округов {len(okrugs)}" if okrugs else ""))

    def district_of(lat, lng):
        for d in districts:
            b = d["bbox"]
            if b[0] <= lat <= b[2] and b[1] <= lng <= b[3] and inside(lat, lng, d["rings"]):
                return d["name"]
        return None

    places = []
    for e in overpass(f'[out:json][timeout:120];{area}node(area.c)["place"~"^(suburb|quarter|neighbourhood|microdistrict)$"]["name"];out;', 120, fetch):
        places.append({"name": e["tags"]["name"], "kind": e["tags"]["place"], "lat": round(e["lat"], 5),
                       "lng": round(e["lon"], 5), "district": district_of(e["lat"], e["lon"])})
    metro = []
    seen = set()
    for e in overpass(f'[out:json][timeout:120];{area}(node(area.c)["station"~"^(subway|light_rail)$"]["name"];'
                      f'node(area.c)["railway"="station"]["subway"="yes"]["name"];);out;', 120, fetch):
        key = e["tags"]["name"].casefold()
        if key in seen:
            continue
        seen.add(key)
        metro.append({"name": e["tags"]["name"], "lat": round(e["lat"], 5), "lng": round(e["lon"], 5),
                      "district": district_of(e["lat"], e["lon"])})
    log(f"  {name}: неофициальных районов {len(places)}, станций метро {len(metro)}")

    # что рядом — те же виды и формат, что у живой проверки backend.geo
    poi: dict[str, list] = {}
    for kind in geo.POI:
        elements = overpass(geo._query([kind], ["(area.c)"], 240).replace("[out:json][timeout:240];",
                                                                           f"[out:json][timeout:240];{area}"), 240, fetch)
        poi[kind] = geo._rows(elements, [kind])[kind]
    log(f"  {name}: объектов «что рядом» {sum(len(v) for v in poi.values())}")

    # признаки районов: деловая активность (офисы, магазины) и «семейность» (школы, детсады), парки
    counts = {d["name"]: {"office": 0, "shop": 0, "family": 0, "park": 0} for d in districts}
    for tag, key in (('nwr(area.c)["office"]', "office"), ('nwr(area.c)["shop"]', "shop")):
        for e in overpass(f"[out:json][timeout:240];{area}{tag};out ids center qt;", 240, fetch):
            c = center_of(e)
            if c and (dn := district_of(*c)):
                counts[dn][key] += 1
    for kind, key in (("school", "family"), ("kindergarten", "family"), ("park", "park")):
        for row in poi.get(kind, []):
            if dn := district_of(row[0], row[1]):
                counts[dn][key] += 1
    for d in districts:
        a = max(d["area_km2"], 0.5)
        c = counts[d["name"]]
        d["features"] = {"business_per_km2": round((c["office"] + c["shop"]) / a, 1),
                         "family_per_km2": round(c["family"] / a, 2), "parks": c["park"]}

    roads = fetch_roads(name, area, fetch, log)

    radius = max([d["dist_km"] for d in districts] + [3.0])
    city = {"version": 1, "city": name, "slug": slug(name), "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "© участники OpenStreetMap, ODbL", "center": [round(center[0], 5), round(center[1], 5)],
            "radius_km": round(radius, 1), "districts": districts, "okrugs": [{k: v for k, v in o.items() if k != "rings"} for o in okrugs],
            "places": places, "metro": metro}
    return city, {"version": 1, "city": name, "poi": poi, "roads": roads}


def write(city: dict, extra: dict, out_dir: Path = OUT_DIR) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    main = out_dir / f"{city['slug']}.json.gz"
    data = out_dir / f"{city['slug']}.poi.json.gz"
    for path, obj in ((main, city), (data, extra)):
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    return main, data


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cities", nargs="*", help="какие города собрать (по умолчанию все из backend.geodata.CITIES)")
    ap.add_argument("--force", action="store_true", help="пересобрать уже готовые")
    ap.add_argument("--mirror", help="свой сервер Overpass (…/api/interpreter) — первым в списке")
    ap.add_argument("--jobs", type=int, default=3, help="сколько городов собирать одновременно (по умолчанию 3)")
    ap.add_argument("--roads", action="store_true", help="у готовых городов пересобрать только дороги и ж/д (несколько минут)")
    args = ap.parse_args()
    if args.mirror:
        MIRRORS.insert(0, args.mirror)
    todo = [c for c in CITIES if not args.cities or c["name"] in args.cities]
    unknown = set(args.cities or []) - {c["name"] for c in CITIES}
    if unknown:
        print("Нет в списке backend.geodata.CITIES:", ", ".join(sorted(unknown)))
        return 2
    failed = []
    if args.roads:
        for cfg in todo:
            if not (OUT_DIR / f"{slug(cfg['name'])}.poi.json.gz").exists():
                print(f"{cfg['name']}: город ещё не собран — сначала без --roads")
                failed.append(cfg["name"])
                continue
            try:
                refresh_roads(cfg, log=lambda msg: print(msg, flush=True))
            except Exception as exc:  # noqa: BLE001
                print(f"  {cfg['name']}: ОШИБКА: {exc}", flush=True)
                failed.append(cfg["name"])
        return 1 if failed else 0
    todo = [c for c in todo if args.force or not (OUT_DIR / f"{slug(c['name'])}.json.gz").exists() or print(
        f"{c['name']}: уже собран, пропускаю")]

    def one(cfg: dict) -> str | None:
        t0 = time.monotonic()
        try:
            city, extra = build_city(cfg, log=lambda msg: print(msg, flush=True))
        except Exception as exc:  # noqa: BLE001 — один город не должен останавливать остальные
            print(f"  {cfg['name']}: ОШИБКА: {exc}", flush=True)
            return cfg["name"]
        main_path, data_path = write(city, extra)
        print(f"  {cfg['name']}: готово за {time.monotonic() - t0:.0f} с: {main_path.name} ({main_path.stat().st_size // 1024} КБ), "
              f"{data_path.name} ({data_path.stat().st_size // 1024} КБ)  [{len(list(OUT_DIR.glob('*.poi.json.gz')))}/{len(CITIES)}]",
              flush=True)
        return None

    print(f"Собираю {len(todo)} городов, по {args.jobs} одновременно")
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for fut in as_completed([pool.submit(one, cfg) for cfg in todo]):
            if name := fut.result():
                failed.append(name)
    if failed:
        print("Не собраны:", ", ".join(failed), "— запустите ещё раз: готовые города пропускаются.")
        return 1
    print("Все города собраны. Проверка: .venv/bin/python -m backend.geodata")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
