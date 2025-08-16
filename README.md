# Shopify Insights Fetcher

A tiny FastAPI app that reads public Shopify storefronts and gives you structured brand intel: catalog, hero products, policies, FAQs, socials, contact, about, links—plus basic competitor discovery.

## What it does

- Catalog: pulls products via public `/products.json` pages (no official Shopify API).
- Hero products: grabs products linked on the home page.
- Policies: privacy, refund, shipping, terms (common Shopify paths).
- FAQs: parses JSON-LD (`FAQPage`) and simple `<details><summary>` blocks.
- Social and links: Instagram/Facebook/etc. plus important links (blogs, contact, order tracking).
- Contact: finds emails/phone numbers and contact page.
- About: short excerpt of the brand’s story.
- Competitors (simple): light web search + Shopify heuristic to find similar stores.

> Works only with public info on Shopify storefronts. Some stores may block or customize endpoints, so not all data is guaranteed.

---

## Quick start

Requirements: Python 3.10+

Install:

```bash
git clone <your-repo-url> shopify-insights-fetcher
cd shopify-insights-fetcher

# Windows PowerShell
python -m venv venv
venv\Scripts\Activate.ps1

# macOS/Linux (alternative)
# python3 -m venv venv
# source venv/bin/activate

pip install -r requirements.txt
# or:
# pip install fastapi "uvicorn[standard]" httpx beautifulsoup4 lxml pydantic
