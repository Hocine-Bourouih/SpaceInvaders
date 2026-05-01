"""
Scrape annonces voitures sur leboncoin.fr selon des criteres precis.

Strategie identique au script lacentrale:
  1. requests + BeautifulSoup
  2. Fallback Playwright (stealth, non-headless) si DataDome bloque

Filtres:
  - departement IDF: 75, 77, 78, 91, 92, 93, 94, 95
  - annee >= 2016
  - kilometrage <= 160 000 km
  - prix <= 5700 EUR
  - 5 portes
  - vendeur pro
  - exclure 2 places (AMI, Fortwo, Twizy, Mia)
  - exclure diesel < 2017

Tri: (-annee, kilometrage, prix)  # recent / km bas / prix bas
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from typing import Iterable, Optional

import requests
from bs4 import BeautifulSoup


# Recherche voitures, IDF, 2016+, <=160000km, <=5700 EUR, 5 portes, pro
URL = (
    "https://www.leboncoin.fr/recherche?"
    "category=2"
    "&locations=r_12"                  # region 12 = Ile-de-France
    "&regdate=2016-2026"
    "&mileage=0-160000"
    "&price=0-5700"
    "&doors=5"
    "&owner_type=pro"
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

IDF_DEPTS = {"75", "77", "78", "91", "92", "93", "94", "95"}

MODELES_2_PLACES = ("ami", "fortwo", "twizy", "mia")

ANNEE_MIN = 2016
ANNEE_MAX = 2025          # >2025 = vehicule neuf / annonce LOA-LLD a exclure
PRIX_MIN = 1000           # < 1000 EUR = mensualite leasing, pas un prix d'achat
PRIX_MAX = 5700


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
        if self.annee is not None and not (ANNEE_MIN <= self.annee <= ANNEE_MAX):
            return False
        if self.prix is not None and not (PRIX_MIN <= self.prix <= PRIX_MAX):
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
            -(self.annee or 0),
            self.kilometrage or 10**9,
            self.prix or 10**9,
        )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _to_int(v) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = re.sub(r"[^\d]", "", str(v))
    return int(s) if s else None


def _attrs_to_dict(ad: dict) -> dict:
    """leboncoin stocke les caracteristiques voitures dans `attributes` (liste).

    On privilegie `value_label` (lisible: "Diesel", "120 000 km", "Essence") plutot
    que `value` qui est souvent un ID numerique pour les enums (fuel, gearbox...).
    """
    out: dict = {}
    for a in ad.get("attributes") or []:
        k = a.get("key")
        if not k:
            continue
        v = a.get("value_label")
        if v is None or v == "":
            v = a.get("value")
        out[k] = v
    return out


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _looks_like_ad(d: dict) -> bool:
    """Une annonce leboncoin contient au moins list_id, subject, price, location."""
    keys = set(d.keys())
    return "list_id" in keys and "subject" in keys and "price" in keys


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
    seen: set[int] = set()

    for ad in _walk(data):
        if not _looks_like_ad(ad):
            continue
        list_id = ad.get("list_id")
        if list_id in seen:
            continue
        seen.add(list_id)

        attrs = _attrs_to_dict(ad)

        # marque + modele
        brand = attrs.get("u_car_brand") or attrs.get("brand") or ""
        model = attrs.get("u_car_model") or attrs.get("model") or ""
        subject = ad.get("subject", "")
        modele = " ".join(s for s in [str(brand), str(model)] if s).strip() or subject

        # annee
        annee = _to_int(attrs.get("regdate")) or _to_int(attrs.get("vehicule_year"))
        if annee is None:
            # parfois mm/aaaa
            for k in ("regdate", "first_circulation_date"):
                s = attrs.get(k)
                if s:
                    m = re.search(r"(19|20)\d{2}", str(s))
                    if m:
                        annee = int(m.group(0))
                        break

        km = _to_int(attrs.get("mileage"))

        # prix: peut etre dans `price` (liste de centimes ou euros) ou price_cents
        prix = None
        p = ad.get("price")
        if isinstance(p, list) and p:
            prix = _to_int(p[0])
        elif p is not None:
            prix = _to_int(p)
        if prix is None:
            prix = _to_int(ad.get("price_cents"))
            if prix and prix > 100_000:  # heuristique cents -> euros
                prix //= 100

        carburant = str(attrs.get("fuel") or attrs.get("vehicule_fuel") or "")
        places = _to_int(attrs.get("seats") or attrs.get("vehicule_seats"))

        loc = ad.get("location") or {}
        zipcode = loc.get("zipcode") or ""
        city = loc.get("city") or ""
        dept = ""
        m = re.match(r"(\d{2})\d{3}", str(zipcode))
        if m:
            dept = m.group(1)
        elif loc.get("department_id"):
            dept = str(loc["department_id"]).zfill(2)

        owner = ad.get("owner") or {}
        otype = (owner.get("type") or "").lower()
        vendeur = owner.get("name") or owner.get("user_id") or ""
        # filtre pro cote python (en plus du parametre URL)
        if otype and otype not in ("pro", "professional", "professionnel"):
            continue

        url = ad.get("url") or f"https://www.leboncoin.fr/ad/voitures/{list_id}"

        out.append(
            Listing(
                modele=modele or "?",
                annee=annee,
                kilometrage=km,
                prix=prix,
                vendeur=str(vendeur),
                departement=dept,
                carburant=carburant,
                places=places,
                url=url,
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
    if r.status_code == 200 and "captcha" not in r.text.lower() and "datadome" not in r.text.lower():
        return r.text
    print(
        f"[requests] bloque (status={r.status_code})",
        file=sys.stderr,
    )
    return None


def fetch_with_playwright(url: str, headless: bool = False) -> Optional[str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Installer playwright: pip install playwright && playwright install chromium",
              file=sys.stderr)
        return None

    try:
        from playwright_stealth import stealth_sync  # type: ignore
    except ImportError:
        stealth_sync = None

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
            try:
                page.wait_for_selector("script#__NEXT_DATA__", timeout=20_000)
            except Exception:
                page.wait_for_timeout(8_000)
            # leboncoin a parfois une consent modal qui peut bloquer, on tente de fermer
            for sel in [
                'button:has-text("Accepter")',
                'button:has-text("Tout accepter")',
                "#didomi-notice-agree-button",
            ]:
                try:
                    page.locator(sel).first.click(timeout=1500)
                    break
                except Exception:
                    pass
            html = page.content()
        finally:
            browser.close()

    if ("captcha" in html.lower() or "datadome" in html.lower()) and "__NEXT_DATA__" not in html:
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

    return parse_next_data(html)


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
