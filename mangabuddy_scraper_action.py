#!/usr/bin/env python3
"""
MangaBuddy Scraper — versión GitHub Actions (sin Flask).
Lee los sitemaps XML del propio repo y scrapea según los inputs del workflow.

Inputs via variables de entorno (seteadas por el .yml):
  SCRAPE_MODE    = "pages" | "url"
  SCRAPE_PAGES   = "1-3" | "1,3,5" | "2"      (si SCRAPE_MODE=pages)
  SCRAPE_URL     = "https://..."               (si SCRAPE_MODE=url)
  PAGE_SIZE      = "20"                        (opcional, default 20)
  OUTPUT_DIR     = "output"                    (opcional, default output)
  SCRAPER_PROXY  = "http://user:pass@host:port" (opcional, proxy residencial)
"""

import json
import os
import re
import sys
import time
import random
import logging
from pathlib import Path
from urllib.parse import urlparse, urljoin
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Pool de User-Agents reales (Chrome, Firefox, Edge — Windows/Mac/Linux) ─────
USER_AGENTS = [
    # Chrome Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    # Chrome Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Firefox Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    # Firefox Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:125.0) Gecko/20100101 Firefox/125.0",
    # Edge Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    # Chrome Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

ACCEPT_LANGUAGES = [
    "en-US,en;q=0.9",
    "en-GB,en;q=0.9",
    "en-US,en;q=0.8,es;q=0.6",
    "en-US,en;q=0.9,fr;q=0.7",
]

# ── Sesión HTTP ────────────────────────────────────────────────────────────────
SESSION = requests.Session()

# Proxy opcional (variable de entorno SCRAPER_PROXY)
_proxy = os.environ.get("SCRAPER_PROXY", "").strip()
if _proxy:
    SESSION.proxies.update({"http": _proxy, "https": _proxy})
    log.info(f"Proxy activo: {_proxy.split('@')[-1]}")  # oculta credenciales en log

def _new_headers() -> dict:
    """Genera un conjunto de headers realistas con UA y Accept-Language aleatorios."""
    ua = random.choice(USER_AGENTS)
    is_firefox = "Firefox" in ua
    return {
        "User-Agent": ua,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
            if is_firefox else
            "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Language": random.choice(ACCEPT_LANGUAGES),
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
        **({"DNT": "1"} if random.random() > 0.5 else {}),
    }

# Delay base en segundos — el real será aleatorio entre DELAY_MIN y DELAY_MAX
DELAY_MIN = 2.0
DELAY_MAX = 5.0

def _delay(extra: float = 0.0):
    """Pausa aleatoria entre requests para no parecer un bot."""
    t = random.uniform(DELAY_MIN, DELAY_MAX) + extra
    time.sleep(t)

# Mantener compatibilidad con el resto del código que usa DELAY directamente
DELAY = DELAY_MIN

# ── Config desde entorno ───────────────────────────────────────────────────────
SCRAPE_MODE = os.environ.get("SCRAPE_MODE", "pages").lower()
SCRAPE_PAGES = os.environ.get("SCRAPE_PAGES", "1")
SCRAPE_URL = os.environ.get("SCRAPE_URL", "").strip()
PAGE_SIZE = int(os.environ.get("PAGE_SIZE", "20"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "output"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Sitemaps: busca todos los .xml en el directorio actual del repo ────────────
SCRIPT_DIR = Path(__file__).parent


def find_sitemap_files() -> list[Path]:
    """
    Encuentra todos los sitemap XML en el mismo directorio que el .py.
    Acepta cualquier nombre que coincida con sitemap-comic*.xml
    """
    found = sorted(SCRIPT_DIR.glob("sitemap-comic*.xml"))
    if not found:
        # fallback: cualquier XML en el directorio
        found = sorted(SCRIPT_DIR.glob("*.xml"))
    log.info(f"Sitemaps encontrados ({len(found)}): {[f.name for f in found]}")
    return found


def load_sitemap_urls() -> list[str]:
    """Lee todos los sitemaps XML y extrae URLs de /series/."""
    urls = []
    for fpath in find_sitemap_files():
        try:
            tree = ET.parse(fpath)
            root = tree.getroot()
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            locs = root.findall(".//sm:loc", ns)
            if not locs:
                locs = root.findall(".//loc")
            for loc in locs:
                url = (loc.text or "").strip()
                if "/series/" in url:
                    urls.append(url)
            log.info(f"  {fpath.name}: {len(locs)} locs → {sum(1 for u in urls if u)} con /series/")
        except Exception as e:
            log.error(f"Error leyendo {fpath.name}: {e}")
    log.info(f"Total URLs en sitemaps: {len(urls)}")
    return urls


# ── Parseo de páginas ──────────────────────────────────────────────────────────
def parse_pages_input(raw: str) -> list[int]:
    """
    Acepta:
      "1"        → [1]
      "1-5"      → [1, 2, 3, 4, 5]
      "1,3,5"    → [1, 3, 5]
      "1-3,7,9"  → [1, 2, 3, 7, 9]
    """
    pages = set()
    for part in raw.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            pages.update(range(int(a), int(b) + 1))
        elif part.isdigit():
            pages.add(int(part))
    return sorted(pages)


# ── HTTP helpers ───────────────────────────────────────────────────────────────
MAX_RETRIES = 4
RETRY_BACKOFF = [10, 30, 60, 120]  # segundos de espera entre reintentos

def get_soup(url: str) -> BeautifulSoup:
    """
    GET con rotación de headers + retry con backoff exponencial.
    En cada intento usa un User-Agent distinto.
    Si recibe 429 o 503 espera más antes de reintentar.
    """
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            SESSION.headers.update(_new_headers())
            resp = SESSION.get(url, timeout=25)

            # Rate-limit explícito: esperar y reintentar
            if resp.status_code in (429, 503):
                wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                log.warning(f"  [{resp.status_code}] Rate-limit en {url} — esperando {wait}s (intento {attempt+1}/{MAX_RETRIES})")
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")

        except requests.HTTPError as e:
            last_exc = e
            if e.response is not None and e.response.status_code == 403:
                wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                log.warning(f"  [403] Bloqueado en {url} — esperando {wait}s (intento {attempt+1}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            raise  # otros 4xx no se reintentan

        except requests.RequestException as e:
            last_exc = e
            wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
            log.warning(f"  [Error red] {e} — esperando {wait}s (intento {attempt+1}/{MAX_RETRIES})")
            time.sleep(wait)

    raise last_exc or RuntimeError(f"Falló tras {MAX_RETRIES} intentos: {url}")


# ── Scraping de serie ──────────────────────────────────────────────────────────
def scrape_manga_meta(url: str):
    soup = get_soup(url)
    slug = urlparse(url).path.rstrip("/").split("/")[-1].split(".")[0]

    data = {
        "manga": slug, "url": url,
        "title": "", "cover": "", "genres": [],
        "status": "", "authors": [], "chapters_count": 0, "synopsis": "",
    }

    for sel in ["h1[itemprop='name']", "h1.title", "h1"]:
        el = soup.select_one(sel)
        if el:
            data["title"] = el.get_text(strip=True)
            break

    for img in soup.find_all("img"):
        src = img.get("data-src") or img.get("src", "")
        alt = (img.get("alt") or "").lower()
        if src and ("thumb" in src or "cover" in alt or "cover" in src.lower()):
            if re.search(r"\.(webp|jpg|jpeg|png)", src, re.I):
                data["cover"] = src
                break

    for sel in ["[itemprop='description']", ".synopsis", ".summary", ".description"]:
        el = soup.select_one(sel)
        if el:
            data["synopsis"] = el.get_text(separator="\n", strip=True)
            break

    for row in soup.find_all("div", class_=re.compile(r"justify-between")):
        label_el = row.find(["h1", "h2", "h3", "span", "b"])
        if not label_el:
            continue
        label = label_el.get_text(strip=True).lower()
        all_vals = [v.get_text(strip=True) for v in row.find_all(["a", "span", "p"]) if v.get_text(strip=True)]
        if "status" in label:
            data["status"] = all_vals[-1] if all_vals else ""
        elif "author" in label:
            data["authors"] = [v for v in all_vals if v.lower() not in ("updating", "author", "authors")]
        elif "chapter" in label:
            nums = [v for v in all_vals if v.isdigit()]
            if nums:
                data["chapters_count"] = int(nums[0])

    genre_links = soup.find_all("a", itemprop="genre") or soup.find_all("a", href=re.compile(r"/genre/"))
    data["genres"] = list(dict.fromkeys(a.get_text(strip=True) for a in genre_links if a.get_text(strip=True)))

    type_a = soup.find("a", href=re.compile(r"/type/"))
    if type_a:
        type_val = type_a.get_text(strip=True)
        if type_val and type_val.lower() not in [g.lower() for g in data["genres"]]:
            data["genres"].append(type_val)

    return data, soup


def scrape_chapter_list(soup, base_url: str) -> list[dict]:
    container = soup.find(id="chapter-list") or soup
    latest_a = container.find("a", href=re.compile(r"/chapter-[\d]"))
    if not latest_a:
        return []

    latest_url = urljoin(base_url, latest_a.get("href", ""))
    log.info(f"  [→] Leyendo dropdown: {latest_url}")
    _delay()
    latest_soup = get_soup(latest_url)

    buttons = latest_soup.select("[data-chapter-menu] button[data-chapter]")
    if not buttons:
        return []

    serie_base = re.sub(r"/chapter-[\d.]+.*$", "", base_url.rstrip("/"))
    chapters = []
    for btn in buttons:
        raw = btn.get("data-chapter", "").strip()
        if not raw:
            continue
        try:
            num = float(raw)
        except ValueError:
            continue
        chapters.append({
            "name": btn.get_text(strip=True) or f"Chapter {raw}",
            "url": f"{serie_base}/chapter-{raw}",
            "num": num,
        })

    seen = set()
    unique = []
    for ch in chapters:
        if ch["url"] not in seen:
            seen.add(ch["url"])
            unique.append(ch)
    unique.sort(key=lambda x: x["num"])
    return unique


def scrape_chapter_images(chapter_url: str) -> list[str]:
    soup = get_soup(chapter_url)
    images = []

    numbered = soup.find_all("img", attrs={"data-number": True})
    if numbered:
        numbered.sort(key=lambda img: int(img["data-number"]))
        for img in numbered:
            src = img.get("data-src") or img.get("src", "")
            if src and _is_chapter_img(src):
                images.append(src)
        if images:
            return list(dict.fromkeys(images))

    for img in soup.select(".image-container img, .page-break img, .chapter-content img"):
        src = img.get("data-src") or img.get("src", "")
        if src and _is_chapter_img(src):
            images.append(src)
    if images:
        return list(dict.fromkeys(images))

    for img in soup.find_all("img"):
        src = img.get("data-src") or img.get("src", "")
        if src and _is_chapter_img(src):
            images.append(src)

    return list(dict.fromkeys(images))


def _is_chapter_img(src: str) -> bool:
    sl = src.lower()
    if not re.search(r"\.(webp|jpg|jpeg|png)(\?|$)", sl):
        return False
    skip = ["thumb", "logo", "banner", "avatar", "default", "ads", "icon"]
    return not any(p in sl for p in skip)


# ── Scraping de un manga completo ──────────────────────────────────────────────
def scrape_single_manga(manga_url: str) -> dict | None:
    manga_url = manga_url.rstrip("/")
    slug = manga_url.split("/")[-1]
    log.info(f"➡ Scrapeando: {manga_url}")

    try:
        manga_meta, soup = scrape_manga_meta(manga_url)
        _delay()
        chapters_meta = scrape_chapter_list(soup, manga_url)

        if not chapters_meta:
            log.warning(f"  ⚠ Sin capítulos, saltando: {manga_url}")
            return None

        result = {
            "manga":          slug,
            "url":            manga_url,
            "title":          manga_meta.get("title", slug),
            "cover":          manga_meta.get("cover", ""),
            "genres":         manga_meta.get("genres", []),
            "status":         manga_meta.get("status", ""),
            "authors":        manga_meta.get("authors", []),
            "chapters_count": manga_meta.get("chapters_count", 0),
            "synopsis":       manga_meta.get("synopsis", ""),
            "chapters":       [],
        }

        total = len(chapters_meta)
        for i, ch in enumerate(chapters_meta, 1):
            log.info(f"  [{i}/{total}] {ch['name']}")
            _delay()
            try:
                images = scrape_chapter_images(ch["url"])
            except Exception as e:
                log.warning(f"  ⚠ Error capítulo {ch['name']}: {e}")
                images = []

            result["chapters"].append({
                "name":         ch["name"],
                "url":          ch["url"],
                "num":          ch["num"],
                "total_images": len(images),
                "images":       images,
            })

        log.info(f"  ✅ {result['title']} — {len(result['chapters'])} capítulos")
        return result

    except Exception as e:
        log.error(f"  ✗ Error scrapeando {manga_url}: {e}")
        return None


# ── Modo páginas ───────────────────────────────────────────────────────────────
def run_pages_mode():
    all_urls = load_sitemap_urls()
    if not all_urls:
        log.error("❌ No se encontraron URLs en los sitemaps. ¿Están los XML en el mismo directorio?")
        sys.exit(1)

    total_pages = (len(all_urls) + PAGE_SIZE - 1) // PAGE_SIZE
    pages = parse_pages_input(SCRAPE_PAGES)

    invalid = [p for p in pages if p < 1 or p > total_pages]
    if invalid:
        log.error(f"❌ Páginas fuera de rango: {invalid}. Máximo: {total_pages} (con {len(all_urls)} URLs, {PAGE_SIZE} por página)")
        sys.exit(1)

    log.info(f"🚀 Modo páginas: {pages} — {total_pages} páginas totales, {len(all_urls)} URLs")

    results_summary = []
    for page in pages:
        start = (page - 1) * PAGE_SIZE
        batch = all_urls[start:start + PAGE_SIZE]
        log.info(f"\n📄 Página {page} ({len(batch)} mangas)")

        page_results = []
        for url in batch:
            manga_data = scrape_single_manga(url)
            if manga_data:
                page_results.append(manga_data)

        out_file = OUTPUT_DIR / f"page_{page}.json"
        out_file.write_text(json.dumps(page_results, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info(f"💾 Guardado: {out_file} ({len(page_results)} mangas)")
        results_summary.append({"page": page, "file": str(out_file), "mangas": len(page_results)})

    # Resumen final
    summary_file = OUTPUT_DIR / "scrape_summary.json"
    summary_file.write_text(json.dumps({
        "mode": "pages",
        "pages_scraped": pages,
        "total_mangas": sum(r["mangas"] for r in results_summary),
        "files": results_summary,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    log.info(f"\n✅ Completado. {sum(r['mangas'] for r in results_summary)} mangas en {len(pages)} página(s).")


# ── Modo URL individual ────────────────────────────────────────────────────────
def run_url_mode():
    if not SCRAPE_URL:
        log.error("❌ SCRAPE_URL está vacío.")
        sys.exit(1)

    log.info(f"🚀 Modo URL individual: {SCRAPE_URL}")
    result = scrape_single_manga(SCRAPE_URL)

    if not result:
        log.error("❌ No se pudo scrapear la URL.")
        sys.exit(1)

    slug = result["manga"]
    out_file = OUTPUT_DIR / f"{slug}.json"
    out_file.write_text(json.dumps([result], ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"💾 Guardado: {out_file}")
    log.info(f"✅ {result['title']} — {len(result['chapters'])} capítulos")


# ── Main ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info(f"=== MangaBuddy Scraper — GitHub Actions ===")
    log.info(f"Modo       : {SCRAPE_MODE}")
    log.info(f"Sitemaps   : {SCRIPT_DIR}")
    log.info(f"Output dir : {OUTPUT_DIR}")

    if SCRAPE_MODE == "url":
        run_url_mode()
    else:
        run_pages_mode()
