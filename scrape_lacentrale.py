"""
Scrape annonces voitures sur lacentrale.fr selon des criteres precis.

Strategie:
  1. Essai avec requests + BeautifulSoup (rapide).
  2. Si DataDome / 403, bascule sur Playwright (avec stealth) en mode non-headless
     pour passer la protection anti-bot.

Donnees extraites:
  modele, annee, kilometrage, prix, vendeur, departement, carburant, places, url

Filtres:
  - place de >= 4 (on ecarte les 2 places)
  - pas de diesel anterieur a 2017
  - kilometrage <= 160 000 km
  - departement IDF uniquement: 75, 77, 78, 91, 92, 93, 94, 95

Tri: meilleur -> moins bon
  cle = (-annee, kilometrage, prix)  # annee recente, km bas, prix bas
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, asdict
from typing import Iterable, Optional
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup


URL = (
    "https://www.lacentrale.fr/listing?"
    "location=idf&yearMin=2016&mileageMax=160000&priceMax=5700"
    "&criteria=veryGoodDeal%2CgoodDeal&sellerType=pro&nbDoors=5"
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

IDF_DEPTS = {"75", "77", "78", "91", "92", "93", "94", "95"}

# Modeles 2 places a exclure (quadricycles, citadines biplaces)
MODELES_2_PLACES = (
    "ami",          # Citroen AMI (quadricycle)
    "fortwo",       # Smart Fortwo
    "twizy",        # Renault Twizy
    "mia",          # Mia electric
)

# Bornes de coherence (URL: yearMin=2016)
ANNEE_MIN = 2016


# ---------------------------------------------------------------------------
# Modele
# ---------------------------------------------------------------------------
@dataclass
class Listing:
    modele: str
    annee: Optional[int]
    kilometrage: Optional[int]
    prix: Optional[int]
    vendeur: str
    departement: str
    carburant: str
    places: Optional[int]
    url: str

    def passe_filtres(self) -> bool:
        if self.departement not in IDF_DEPTS:
            return False
        if self.kilometrage is not None and self.kilometrage > 160_000:
            return False
        if self.places is not None and self.places < 4:
            return False
        if self.annee is not None and self.annee < ANNEE_MIN:
            return False
        modele_lc = self.modele.lower()
        if any(m in modele_lc for m in MODELES_2_PLACES):
            return False
        if (
            self.carburant
            and "diesel" in self.carburant.lower()
            and self.annee is not None
            and self.annee < 2017
        ):
            return False
        return True

    def sort_key(self) -> tuple:
        return (
            -(self.annee or 0),                # annee recente d'abord
            self.kilometrage or 10**9,         # km bas d'abord
            self.prix or 10**9,                # prix bas d'abord
        )


# ---------------------------------------------------------------------------
# Parsing du HTML lacentrale (Next.js => __NEXT_DATA__ JSON)
# ---------------------------------------------------------------------------
def _to_int(v) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = re.sub(r"[^\d]", "", str(v))
    return int(s) if s else None


def _dept_from(*candidates: str) -> str:
    """Extrait un departement IDF a partir d'un code postal ou d'un libelle."""
    for c in candidates:
        if not c:
            continue
        m = re.search(r"\b(75|77|78|91|92|93|94|95)\d{3}\b", c)
        if m:
            return m.group(1)
        m = re.search(r"\((75|77|78|91|92|93|94|95)\)", c)
        if m:
            return m.group(1)
        m = re.search(r"\b(75|77|78|91|92|93|94|95)\b", c)
        if m:
            return m.group(1)
    return ""


def _walk(obj):
    """Yield tous les dicts d'un JSON imbrique."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _looks_like_listing(d: dict) -> bool:
    keys = set(d.keys())
    # heuristique: les annonces lacentrale ont customerPrice + brand/model + classifiedURL
    return (
        ("customerPrice" in keys or "price" in keys)
        and ("classifiedURL" in keys or "url" in keys or "vehicleURL" in keys)
        and ("brand" in keys or "make" in keys or "makeName" in keys)
    )


def parse_next_data(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "lxml")
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        return []
    try:
        data = json.loads(tag.string)
    except json.JSONDecodeError:
        return []

    out: list[Listing] = []
    seen: set[str] = set()
    for d in _walk(data):
        if not _looks_like_listing(d):
            continue

        url = d.get("classifiedURL") or d.get("url") or d.get("vehicleURL") or ""
        if url and url.startswith("/"):
            url = "https://www.lacentrale.fr" + url
        if url in seen:
            continue
        seen.add(url)

        brand = d.get("brand") or d.get("make") or d.get("makeName") or ""
        model = d.get("model") or d.get("modelName") or ""
        version = d.get("version") or d.get("trim") or ""
        modele = " ".join(s for s in [str(brand), str(model), str(version)] if s).strip()

        # priorite: champs explicites annee/immatriculation, en validant la plage
        annee = None
        for k in ("vehicleYear", "registrationYear", "year", "modelYear", "firstRegistrationYear"):
            v = _to_int(d.get(k))
            if v and 1990 <= v <= 2030:
                annee = v
                break
        # date d'immatriculation type "2018-05" ou "05/2018"
        if annee is None:
            for k in ("registrationDate", "firstRegistrationDate", "vehicleRegistrationDate"):
                s = d.get(k)
                if not s:
                    continue
                m = re.search(r"(19|20)\d{2}", str(s))
                if m:
                    annee = int(m.group(0))
                    break
        km = _to_int(d.get("mileage") or d.get("vehicleMileage"))
        prix = _to_int(d.get("customerPrice") or d.get("price"))
        carburant = str(d.get("energy") or d.get("fuel") or d.get("fuelType") or "")
        places = _to_int(d.get("nbSeats") or d.get("seats"))

        seller = (
            d.get("sellerName")
            or d.get("customerName")
            or (d.get("seller") or {}).get("name")
            or d.get("contactName")
            or ""
        )
        zipcode = (
            d.get("zipCode")
            or d.get("postalCode")
            or (d.get("seller") or {}).get("zipCode")
            or ""
        )
        city = d.get("city") or (d.get("seller") or {}).get("city") or ""
        departement = _dept_from(str(zipcode), str(city))

        out.append(
            Listing(
                modele=modele or "?",
                annee=annee,
                kilometrage=km,
                prix=prix,
                vendeur=str(seller),
                departement=departement,
                carburant=carburant,
                places=places,
                url=url,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Fallback: parsing DOM (cartes d'annonces) si __NEXT_DATA__ absent
# ---------------------------------------------------------------------------
def parse_dom(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "lxml")
    out: list[Listing] = []
    for card in soup.select('a[href*="/auto-occasion-annonce-"]'):
        href = card.get("href", "")
        if href.startswith("/"):
            href = "https://www.lacentrale.fr" + href
        text = card.get_text(" ", strip=True)
        m_year = re.search(r"\b(20\d{2})\b", text)
        m_km = re.search(r"([\d\s]{2,7})\s*km", text, re.I)
        m_price = re.search(r"([\d\s]{3,7})\s*€", text)
        out.append(
            Listing(
                modele=text[:60],
                annee=int(m_year.group(1)) if m_year else None,
                kilometrage=_to_int(m_km.group(1)) if m_km else None,
                prix=_to_int(m_price.group(1)) if m_price else None,
                vendeur="",
                departement=_dept_from(text),
                carburant="",
                places=None,
                url=href,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------
def fetch_with_requests(url: str) -> Optional[str]:
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }
    r = requests.get(url, headers=headers, timeout=20)
    if r.status_code == 200 and "captcha" not in r.text.lower():
        return r.text
    print(
        f"[requests] bloque (status={r.status_code}, captcha={'captcha' in r.text.lower()})",
        file=sys.stderr,
    )
    return None


def fetch_with_playwright(url: str, headless: bool = False) -> Optional[str]:
    """Fallback Playwright, idealement en mode non-headless contre DataDome."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Installer playwright: pip install playwright && playwright install chromium",
              file=sys.stderr)
        return None

    try:
        from playwright_stealth import stealth_sync  # type: ignore
    except ImportError:
        stealth_sync = None  # facultatif mais recommande

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        ctx = browser.new_context(
            user_agent=UA,
            locale="fr-FR",
            timezone_id="Europe/Paris",
            viewport={"width": 1366, "height": 800},
        )
        page = ctx.new_page()
        if stealth_sync:
            try:
                stealth_sync(page)
            except Exception:
                pass
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            # laisse passer l'eventuel challenge DataDome
            try:
                page.wait_for_selector("script#__NEXT_DATA__", timeout=20_000)
            except Exception:
                page.wait_for_timeout(8_000)
            html = page.content()
        finally:
            browser.close()

    if "captcha" in html.lower() and "__NEXT_DATA__" not in html:
        print("[playwright] toujours bloque par DataDome (essayer headless=False)",
              file=sys.stderr)
        return None
    return html


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def scrape(url: str = URL) -> list[Listing]:
    html = fetch_with_requests(url)
    if html is None:
        print("[fallback] bascule sur Playwright...", file=sys.stderr)
        html = fetch_with_playwright(url, headless=False)
    if html is None:
        print("Echec: impossible de recuperer la page.", file=sys.stderr)
        return []

    listings = parse_next_data(html)
    if not listings:
        listings = parse_dom(html)
    return listings


def afficher(listings: Iterable[Listing]) -> None:
    listings = [l for l in listings if l.passe_filtres()]
    listings.sort(key=lambda l: l.sort_key())

    if not listings:
        print("Aucune annonce ne correspond aux criteres.")
        return

    print(f"{len(listings)} annonces correspondantes (meilleure -> moins bonne):\n")
    fmt = "{:<4} {:<35} {:>4} {:>9} {:>7} {:>3} {:<10} {:<18} {}"
    print(fmt.format("#", "Modele", "An", "Km", "Prix", "Dpt", "Carbu", "Vendeur", "URL"))
    print("-" * 160)
    for i, l in enumerate(listings, 1):
        print(
            fmt.format(
                i,
                (l.modele[:33] + "..") if len(l.modele) > 35 else l.modele,
                l.annee or "?",
                f"{l.kilometrage:,}".replace(",", " ") if l.kilometrage else "?",
                f"{l.prix} €" if l.prix else "?",
                l.departement or "?",
                (l.carburant[:10]) if l.carburant else "?",
                (l.vendeur[:16] + "..") if len(l.vendeur) > 18 else l.vendeur,
                l.url,
            )
        )


if __name__ == "__main__":
    rows = scrape()
    afficher(rows)
