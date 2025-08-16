from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, AnyHttpUrl
from typing import List, Optional, Dict, Union
from urllib.parse import urljoin, urlparse, quote_plus
from datetime import datetime
import httpx, json, re
from bs4 import BeautifulSoup

app = FastAPI(title="Shopify Insights (No API)")

# ---------- Simple models ----------
class Product(BaseModel):
    title: str
    url: Optional[str] = None
    price: Optional[float] = None
    image: Optional[str] = None

class Policy(BaseModel):
    type: str          # privacy/refund/shipping/terms
    url: Optional[str] = None
    text_excerpt: Optional[str] = None

class FAQItem(BaseModel):
    question: str
    answer: str
    url: Optional[str] = None

class BrandContext(BaseModel):
    store_url: AnyHttpUrl
    brand_name: Optional[str] = None
    hero_products: List[Product] = []
    catalog: List[Product] = []
    policies: List[Policy] = []
    faqs: List[FAQItem] = []
    social: Dict[str, Optional[str]] = {}
    contact: Dict[str, Optional[Union[List[str], str]]] = {}
    about_text: Optional[str] = None
    important_links: Dict[str, Optional[str]] = {}
    fetched_at: str

class CompetitorResult(BaseModel):
    brand: BrandContext
    competitors: List[BrandContext] = []

# ---------- Helpers ----------
SOCIAL_MAP = {
    "instagram.com": "instagram",
    "facebook.com": "facebook",
    "x.com": "twitter",
    "twitter.com": "twitter",
    "tiktok.com": "tiktok",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "pinterest.com": "pinterest",
    "linkedin.com": "linkedin",
}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\+?\d[\d\-\s()]{6,}\d")

def text_excerpt(s: str, n: int = 800) -> str:
    s = " ".join((s or "").split())
    return s[:n]

def classify_social(href: str) -> Optional[str]:
    host = urlparse(href).netloc.lower()
    for dom, key in SOCIAL_MAP.items():
        if dom in host:
            return key
    return None

def absolutize(base: str, href: Optional[str]) -> Optional[str]:
    return urljoin(base, href) if href else None

def normalize_root(url: str) -> str:
    """Return scheme+host root, always ending with a slash."""
    p = urlparse(url)
    root = f"{p.scheme}://{p.netloc}/"
    return root

# ---------- Scraper ----------
def fetch_html(client: httpx.Client, base: str, path: str) -> Optional[BeautifulSoup]:
    try:
        r = client.get(urljoin(base, path), follow_redirects=True)
        if r.status_code == 200:
            return BeautifulSoup(r.text, "lxml")
    except httpx.RequestError:
        pass
    return None

def fetch_json_ok(client: httpx.Client, url: str) -> Optional[dict]:
    try:
        r = client.get(url, follow_redirects=True)
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None

def scrape_brand_name(soup: Optional[BeautifulSoup]) -> Optional[str]:
    if not soup: return None
    if soup.title and soup.title.text:
        return soup.title.text.strip().split("|")[0].strip()
    og = soup.find("meta", property="og:site_name")
    return og.get("content").strip() if og and og.get("content") else None

def scrape_hero_products(base: str, soup: Optional[BeautifulSoup]) -> List[Product]:
    if not soup: return []
    seen, out = set(), []
    for a in soup.select('a[href*="/products/"]'):
        href = absolutize(base, a.get("href"))
        if not href or href in seen:
            continue
        title = (a.get("title") or a.get_text(" ", strip=True) or "").strip()
        if not title:
            img = a.find("img")
            if img and img.get("alt"):
                title = img["alt"].strip()
        if title:
            out.append(Product(title=title, url=href))
            seen.add(href)
        if len(out) >= 8:
            break
    return out

def scrape_catalog(client: httpx.Client, base: str) -> List[Product]:
    products: List[Product] = []
    page = 1
    while True:
        try:
            r = client.get(urljoin(base, f"/products.json?limit=250&page={page}"), follow_redirects=True)
            if r.status_code != 200:
                break
            data = r.json()
            items = data.get("products", [])
            if not items:
                break
            for it in items:
                handle = it.get("handle")
                url = absolutize(base, f"/products/{handle}") if handle else None
                image = None
                if it.get("image") and it["image"].get("src"):
                    image = absolutize(base, it["image"]["src"])
                price = None
                if it.get("variants"):
                    v0 = it["variants"][0]
                    if v0.get("price"):
                        try:
                            price = float(v0["price"])
                        except ValueError:
                            pass
                products.append(Product(title=(it.get("title") or "").strip(), url=url, price=price, image=image))
            page += 1
        except Exception:
            break
    return products

def scrape_policies(client: httpx.Client, base: str) -> List[Policy]:
    paths = [
        ("privacy", "/policies/privacy-policy"),
        ("refund", "/policies/refund-policy"),
        ("shipping", "/policies/shipping-policy"),
        ("terms", "/policies/terms-of-service"),
    ]
    out: List[Policy] = []
    for ptype, path in paths:
        soup = fetch_html(client, base, path)
        if soup:
            out.append(Policy(type=ptype, url=urljoin(base, path), text_excerpt=text_excerpt(soup.get_text(" ", strip=True))))
    return out

def scrape_faqs(client: httpx.Client, base: str) -> List[FAQItem]:
    for path in ["/pages/faq", "/pages/faqs", "/pages/help", "/pages/support"]:
        soup = fetch_html(client, base, path)
        if not soup:
            continue
        faqs: List[FAQItem] = []
        # JSON-LD
        for s in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(s.text)
                if isinstance(data, dict) and data.get("@type") == "FAQPage":
                    for ent in data.get("mainEntity", []):
                        q = (ent.get("name") or "").strip()
                        a = ""
                        aa = ent.get("acceptedAnswer") or {}
                        if isinstance(aa, dict):
                            a = (aa.get("text") or "").strip()
                        if q and a:
                            faqs.append(FAQItem(question=q, answer=a, url=urljoin(base, path)))
            except Exception:
                pass
        # HTML <details><summary>
        for det in soup.find_all("details"):
            summ = det.find("summary")
            q = (summ.get_text(" ", strip=True) if summ else "").strip()
            a = det.get_text(" ", strip=True)
            if q and a:
                faqs.append(FAQItem(question=q, answer=a, url=urljoin(base, path)))
        if faqs:
            return faqs
    return []

def scrape_social(soup: Optional[BeautifulSoup]) -> Dict[str, Optional[str]]:
    if not soup: return {}
    out: Dict[str, Optional[str]] = {}
    for a in soup.find_all("a", href=True):
        key = classify_social(a["href"])
        if key and key not in out:
            out[key] = a["href"]
    return out

def scrape_contact(client: httpx.Client, base: str) -> Dict[str, Optional[Union[List[str], str]]]:
    emails, phones, page_url = [], [], None
    for path in ["/pages/contact", "/pages/contact-us", "/contact"]:
        soup = fetch_html(client, base, path)
        if not soup:
            continue
        txt = soup.get_text(" ", strip=True)
        emails += EMAIL_RE.findall(txt)
        phones += PHONE_RE.findall(txt)
        for a in soup.select('a[href^="mailto:"], a[href^="tel:"]'):
            href = a["href"]
            if href.startswith("mailto:"):
                emails.append(href.replace("mailto:", "").strip())
            if href.startswith("tel:"):
                phones.append(href.replace("tel:", "").strip())
        page_url = urljoin(base, path)
        break
    return {
        "emails": sorted(set(emails)) or None,
        "phones": sorted(set(phones)) or None,
        "contact_page": page_url
    }

def scrape_about(client: httpx.Client, base: str) -> Optional[str]:
    for path in ["/pages/about", "/pages/our-story", "/pages/about-us"]:
        soup = fetch_html(client, base, path)
        if soup:
            return text_excerpt(soup.get_text(" ", strip=True), 1200)
    return None

def scrape_important_links(client: httpx.Client, base: str) -> Dict[str, Optional[str]]:
    out = {"order_tracking": None, "contact_us": None, "blogs": None}
    for path, key in [
        ("/pages/track", "order_tracking"),
        ("/pages/track-order", "order_tracking"),
        ("/pages/order-tracking", "order_tracking"),
        ("/pages/contact", "contact_us"),
        ("/blogs/news", "blogs"),
        ("/blogs", "blogs"),
    ]:
        soup = fetch_html(client, base, path)
        if soup:
            out[key] = urljoin(base, path)
    return out

def get_brand_context(client: httpx.Client, website_url: str) -> BrandContext:
    base = website_url if website_url.endswith("/") else website_url + "/"
    home = fetch_html(client, base, "/")
    brand_name = scrape_brand_name(home)
    hero_products = scrape_hero_products(base, home)
    catalog = scrape_catalog(client, base)
    policies = scrape_policies(client, base)
    faqs = scrape_faqs(client, base)
    social = scrape_social(home)
    contact = scrape_contact(client, base)
    about_text = scrape_about(client, base)
    important_links = scrape_important_links(client, base)

    ctx = BrandContext(
        store_url=base,
        brand_name=brand_name,
        hero_products=hero_products,
        catalog=catalog,
        policies=policies,
        faqs=faqs,
        social=social,
        contact=contact,
        about_text=about_text,
        important_links=important_links,
        fetched_at=datetime.utcnow().isoformat() + "Z",
    )
    return ctx

# ---------- Competitor finder (simple & safe) ----------
def looks_like_shopify(client: httpx.Client, url: str) -> bool:
    """
    Heuristic: a domain is "Shopify-like" if /products.json returns JSON with 'products' key
    or returns 200 with a JSON object.
    """
    root = normalize_root(url)
    test_url = urljoin(root, "/products.json?limit=1")
    data = fetch_json_ok(client, test_url)
    if isinstance(data, dict) and "products" in data:
        return True
    return False

def find_competitor_candidates(client: httpx.Client, website_url: str, brand_name: Optional[str], limit: int = 3) -> List[str]:
    """
    Very light-weight approach:
    - Query DuckDuckGo HTML for "<brand_name> shopify" and "<brand_name> competitors shopify"
    - Collect result links, normalize to roots, filter duplicates and self-domain
    - Check which look like Shopify stores
    """
    root = normalize_root(website_url)
    self_host = urlparse(root).netloc
    queries = []
    if brand_name:
        queries.extend([
            f"{brand_name} shopify",
            f"{brand_name} competitors shopify",
            f"{brand_name} similar brands shopify",
        ])
    else:
        # fallback: just search the host name
        host_without_www = self_host.replace("www.", "")
        queries.extend([
            f"{host_without_www} competitors shopify",
            f"{host_without_www} similar brands shopify",
        ])

    candidates: List[str] = []
    seen_hosts: set[str] = set()
    headers = {"User-Agent": "ShopifyInsightsDemo/1.0"}

    for q in queries:
        url = f"https://duckduckgo.com/html/?q={quote_plus(q)}"
        try:
            r = client.get(url, headers=headers, timeout=15)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "lxml")
            # Collect external links
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if not href.startswith("http"):
                    continue
                host = urlparse(href).netloc
                if not host or host == self_host:
                    continue
                # normalize to root
                root_cand = normalize_root(href)
                h = urlparse(root_cand).netloc
                if h in seen_hosts:
                    continue
                seen_hosts.add(h)
                candidates.append(root_cand)
                if len(candidates) >= limit * 4:  # collect a few extras before filtering
                    break
        except Exception:
            continue

    # Filter to Shopify-like domains and drop obvious non-shop domains
    filtered: List[str] = []
    for cand in candidates:
        if len(filtered) >= limit:
            break
        try:
            if looks_like_shopify(client, cand):
                filtered.append(cand)
        except Exception:
            continue

    return filtered[:limit]

# ---------- Routes ----------
@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/insights", response_model=BrandContext)
def insights(website_url: AnyHttpUrl = Query(..., description="Shopify store URL, e.g. https://memy.co.in")):
    base = str(website_url)
    client = httpx.Client(timeout=20, headers={"User-Agent": "ShopifyInsightsDemo/1.0"})
    try:
        ctx = get_brand_context(client, base)

        if not ctx.catalog and not ctx.hero_products:
            raise HTTPException(status_code=401, detail="Website not found or not a typical Shopify storefront.")
        return ctx

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")
    finally:
        client.close()

@app.get("/competitors", response_model=CompetitorResult)
def competitors(
    website_url: AnyHttpUrl = Query(..., description="Brand website (Shopify storefront)"),
    limit: int = Query(3, ge=1, le=5, description="Max competitors to fetch (1–5)")
):
    base = str(website_url)
    client = httpx.Client(timeout=20, headers={"User-Agent": "ShopifyInsightsDemo/1.0"})
    try:
        # Brand itself
        brand_ctx = get_brand_context(client, base)
        if not brand_ctx.catalog and not brand_ctx.hero_products:
            raise HTTPException(status_code=401, detail="Website not found or not a typical Shopify storefront.")

        # Find competitor URLs (simple search)
        competitor_urls = find_competitor_candidates(client, str(brand_ctx.store_url), brand_ctx.brand_name, limit=limit)

        # Fetch contexts for competitors
        competitor_contexts: List[BrandContext] = []
        for cu in competitor_urls:
            try:
                cctx = get_brand_context(client, cu)
                if cctx.catalog or cctx.hero_products:
                    competitor_contexts.append(cctx)
            except Exception:
                # ignore individual failures
                continue

        return CompetitorResult(brand=brand_ctx, competitors=competitor_contexts)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")
    finally:
        client.close()
