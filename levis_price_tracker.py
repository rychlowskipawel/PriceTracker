#!/usr/bin/env python3
"""
Codzienny tracker ceny: Levi's XX Chino Standard Taper (171960001)

Działanie:
  1. Pobiera stronę produktu (requests; fallback: Playwright).
  2. Wyciąga cenę (JSON-LD -> regex "Sale price is" -> regex "zł").
  3. Dopisuje wiersz do CSV (data, cena).
  4. Jeśli cena się zmieniła / spadła poniżej progu -> powiadomienie (ntfy.sh, opcjonalnie).

Zależności:
  pip install requests
  pip install playwright && playwright install chromium   # tylko dla fallbacku

Cron (codziennie 8:00):
  0 8 * * * /usr/bin/python3 /sciezka/levis_price_tracker.py >> /sciezka/tracker.log 2>&1
"""

import csv
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import requests

URL = "https://www.levi.com/PL/pl_PL/odziez/mezczyzni/spodnie-xx-chino-standard-taper/p/171960001"
CSV_PATH = Path(__file__).with_name("levis_price_history.csv")

# Opcjonalne powiadomienia: ustaw zmienną NTFY_TOPIC (https://ntfy.sh/<topic>)
NTFY_TOPIC = os.getenv("NTFY_TOPIC")
# Opcjonalny próg alertu w zł, np. TARGET_PRICE=300
TARGET_PRICE = float(os.getenv("TARGET_PRICE", "0") or 0)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.5",
    "Accept": "text/html,application/xhtml+xml",
}


def to_float(raw: str) -> float:
    """'1 379,90' / '379.90' -> float"""
    s = raw.replace("\xa0", "").replace(" ", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    return float(s)


def parse_price(html: str) -> float | None:
    # 1) JSON-LD (schema.org Product/Offer)
    for block in re.findall(
        r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I
    ):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                offers = node.get("offers")
                if offers:
                    stack.extend(offers if isinstance(offers, list) else [offers])
                for key in ("price", "lowPrice"):
                    if key in node:
                        try:
                            return to_float(str(node[key]))
                        except ValueError:
                            pass
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
            elif isinstance(node, list):
                stack.extend(node)

    # 2) Tekst widoczny na stronie: "Sale price is379,90 zł"
    m = re.search(r"Sale price is\s*([\d\s\xa0.,]+?)\s*(?:zł|PLN)", html, re.I)
    if m:
        return to_float(m.group(1))

    # 3) Pierwsza cena w zł przy tytule produktu (ostateczność)
    m = re.search(r"([\d]{2,4}[,.]\d{2})\s*(?:zł|PLN)", html)
    if m:
        return to_float(m.group(1))
    return None


def fetch_requests() -> str:
    r = requests.get(URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def fetch_playwright() -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(locale="pl-PL", user_agent=HEADERS["User-Agent"])
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(4000)
        # Cookie banner nie powinien blokować odczytu, ale gdyby co:
        html = page.content() + "\n" + page.inner_text("body")
        browser.close()
        return html


def get_price() -> float:
    try:
        price = parse_price(fetch_requests())
        if price:
            return price
        print("requests: brak ceny w HTML, próbuję Playwright...", file=sys.stderr)
    except Exception as e:
        print(f"requests: {e}; próbuję Playwright...", file=sys.stderr)

    price = parse_price(fetch_playwright())
    if not price:
        raise RuntimeError("Nie udało się wyciągnąć ceny (zmieniła się struktura strony?)")
    return price


def last_price() -> float | None:
    if not CSV_PATH.exists():
        return None
    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return float(rows[-1]["price_pln"]) if rows else None


def append_csv(price: float) -> None:
    new = not CSV_PATH.exists()
    with CSV_PATH.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "price_pln"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), f"{price:.2f}"])


def notify(title: str, msg: str) -> None:
    print(f"[ALERT] {title}: {msg}")
    if NTFY_TOPIC:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=msg.encode("utf-8"),
            headers={"Title": title.encode("utf-8"), "Click": URL},
            timeout=15,
        )


def main() -> int:
    price = get_price()
    prev = last_price()
    append_csv(price)
    print(f"{datetime.now():%Y-%m-%d %H:%M} cena: {price:.2f} zł (poprzednio: {prev})")

    if prev is not None and price != prev:
        kierunek = "spadła" if price < prev else "wzrosła"
        notify("Levi's XX Chino", f"Cena {kierunek}: {prev:.2f} -> {price:.2f} zł")
    if TARGET_PRICE and price <= TARGET_PRICE:
        notify("Levi's XX Chino", f"Cena {price:.2f} zł <= próg {TARGET_PRICE:.2f} zł")
    return 0


if __name__ == "__main__":
    sys.exit(main())
