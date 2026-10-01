#!/usr/bin/env python3
"""Fetch eBay listings (or load a fixture) and render the static site into _site/."""
import argparse, base64, collections, gzip, hashlib, html, json, os, re, shutil, sys, time, tomllib, unicodedata
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

ROOT = Path(__file__).parent
CACHE = ROOT / ".cache" / "items"  # sold items live next to it in .cache/sold
SOLD_DAYS = 30
RELATED = 6
ITEM_CANONICAL = "ebay"  # "self" if Search Console shows our item pages would rank better
MIN_HUB_ITEMS = 3
PAGE_SIZE = 48
MARKETPLACES = {  # marketplace id -> (eBay web domain, currency)
    "EBAY_GB": ("www.ebay.co.uk", "GBP"),
    "EBAY_US": ("www.ebay.com", "USD"),
    "EBAY_DE": ("www.ebay.de", "EUR"),
    "EBAY_AU": ("www.ebay.com.au", "AUD"),
}
EBAY_PRIVACY = {  # marketplace id -> eBay privacy notice; others fall back to the domain's /help home
    "EBAY_GB": "https://www.ebay.co.uk/help/policies/member-behaviour-policies/user-privacy-notice-privacy-policy?id=4260",
}
SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€", "AUD": "A$"}
API = "https://api.ebay.com"
SCOPE = "https://api.ebay.com/oauth/api_scope"
ALLOWED_TAGS = {"p", "br", "ul", "ol", "li", "b", "strong", "i", "em", "h2", "h3", "h4", "table", "tr", "td", "th"}
DROP_TAGS = {"script", "style"}  # content dropped too, not just the tags
NO_BRAND = {"unbranded", "does not apply", "n/a"}

C = {}  # site config, filled by load_config()
STYLE = Markup((ROOT / "static" / "style.css").read_text())
env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html"]))


def legacy_id(item_id):
    """eBay item ids end up in file paths, so refuse anything that isn't plain digits."""
    if not item_id.isdigit():
        raise ValueError(f"unexpected eBay item id {item_id!r}")
    return item_id


def load_config(path):
    C.clear()
    C.update(tomllib.loads(Path(path).read_text()))
    C["site_url"] = C["site_url"].rstrip("/")
    C["domain"], C["currency"] = MARKETPLACES[C["marketplace"]]


# ---------- text helpers ----------

def slugify(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "x"


def ld(obj):
    """JSON for a ld+json script tag. Escapes '</' and '<!--' so untrusted text can't end the tag."""
    return Markup(json.dumps(obj, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "\\u003c!--"))


class _Clean(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out, self.stack, self.skip = [], [], 0

    def handle_starttag(self, tag, attrs):  # attrs are dropped on purpose
        if tag in DROP_TAGS:
            self.skip += 1
        elif tag in ALLOWED_TAGS and not self.skip:
            self.out.append(f"<{tag}>")
            if tag != "br":
                self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in DROP_TAGS:
            self.skip = max(0, self.skip - 1)
        elif tag in self.stack:
            while self.stack:  # close anything left open inside it
                t = self.stack.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data))


def clean_html(s):
    p = _Clean()
    p.feed(s)
    p.close()
    return "".join(p.out) + "".join(f"</{t}>" for t in reversed(p.stack))


def img(url, width):
    """Route through the site's image transform when configured, so blockers see a first-party URL."""
    if t := C.get("image_transform"):
        return t.format(width=width) + url
    return re.sub(r"s-l\d+\.\w+", f"s-l{width}.webp", url)  # eBay serves sized variants by URL


def srcset(url, widths):
    return ", ".join(f"{img(url, w)} {w}w" for w in widths)


def money(value, currency):
    return f"{SYMBOLS.get(currency, currency + ' ')}{value}"


# ---------- fetching ----------

def http(url, headers, data=None, tries=3):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": f"tinylistings (+{C['site_url']})", "Accept-Encoding": "gzip", **headers})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
                return json.loads(gzip.decompress(body) if r.headers.get("Content-Encoding") == "gzip" else body)
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500 or attempt == tries - 1:
                raise
        except (urllib.error.URLError, ConnectionError, TimeoutError):  # eBay resets connections now and then; one retry usually clears it
            if attempt == tries - 1:
                raise
        time.sleep(2 ** attempt * 5)


def top_categories(headers):
    """Search needs a category, so walk every top-level one of the marketplace's live tree."""
    if "top_category_ids" in C:
        return C["top_category_ids"]
    tax = f"{API}/commerce/taxonomy/v1"
    tree = http(f"{tax}/get_default_category_tree_id?marketplace_id={C['marketplace']}", headers)["categoryTreeId"]
    root = http(f"{tax}/category_tree/{tree}", headers)["rootCategoryNode"]
    return [n["category"]["categoryId"] for n in root["childCategoryTreeNodes"]]


def search_seller(headers, seller, cats):
    found = {}  # dedup by itemId; a listing sits in one category but paging can overlap
    for cat in cats:
        offset = 0
        while True:
            q = urllib.parse.urlencode({"category_ids": cat, "filter": "sellers:{%s}" % seller, "limit": 200, "offset": offset})
            page = http(f"{API}/buy/browse/v1/item_summary/search?{q}", headers)
            for s in page.get("itemSummaries", []):
                found[s["itemId"]] = s
            offset += 200
            if offset >= page.get("total", 0):
                break
    return found


def fetch_live():
    for var in ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET"):
        if var not in os.environ:
            sys.exit(f"{var} is not set (use --fixture for an offline build)")
    creds = base64.b64encode(f"{os.environ['EBAY_CLIENT_ID']}:{os.environ['EBAY_CLIENT_SECRET']}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": SCOPE}).encode()
    token = http(f"{API}/identity/v1/oauth2/token", {"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"}, body)["access_token"]
    headers = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": C["marketplace"]}
    cats = top_categories(headers)
    summaries = {}
    for seller in C["sellers"]:
        found = search_seller(headers, seller, cats)
        if not found:
            sys.exit(f"no items returned for seller {seller}")
        print(f"{seller}: {len(found)} items")
        summaries.update(found)
    # ponytail: description and aspects only refresh on a cache miss, so edits made on eBay show up late.
    # Upgrade path: compare itemEndDate/lastModified against the cached copy if that becomes a problem.
    CACHE.mkdir(parents=True, exist_ok=True)
    sold = retire_cache({legacy_id(item_id.split("|")[1]) for item_id in summaries}, datetime.now(timezone.utc))
    items = []
    for item_id, s in summaries.items():
        f = CACHE / f"{legacy_id(item_id.split('|')[1])}.json"
        if f.exists():
            detail = json.loads(f.read_text())
        else:
            detail = http(f"{API}/buy/browse/v1/item/{urllib.parse.quote(item_id, safe='')}", headers)
            f.write_text(json.dumps(detail))
            time.sleep(0.2)
        fresh = {k: s[k] for k in ("title", "price", "condition", "itemWebUrl", "marketingPrice") if k in s}
        detail = {k: v for k, v in detail.items() if k != "marketingPrice"}  # a markdown that has ended must not linger in the cache
        # Search only returns items that are for sale, so the cached availability would be stale by definition.
        items.append({**detail, **fresh, "estimatedAvailabilities": [{"estimatedAvailabilityStatus": "IN_STOCK"}]})
    return items, sold


def sold_stub(wrapper):
    """The minimal sold wrapper. eBay content must go once it is no longer public, so only what routes the page is kept.
    Old {"ended", "item"} wrappers are converted."""
    if "item" not in wrapper:
        return wrapper
    i = prepare(wrapper["item"])
    return {"ended": wrapper["ended"], "id": i["id"], "url": i["url"], "seller": i["seller"], "cat_slug": i["cat_slug"]}


def retire_cache(listed, now):
    """Turn cached getItem files of unlisted items into minimal .cache/sold wrappers so shared links keep working for SOLD_DAYS.
    Relisted ids lose their sold file. Returns the live sold wrappers."""
    sold_dir = CACHE.parent / "sold"
    sold_dir.mkdir(parents=True, exist_ok=True)
    for f in CACHE.glob("*.json"):
        dest = sold_dir / f.name
        if f.stem not in listed and not dest.exists():
            dest.write_text(json.dumps(sold_stub({"ended": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "item": json.loads(f.read_text())})))
        if f.stem not in listed:
            f.unlink()
    sold = []
    for f in sold_dir.glob("*.json"):
        old = json.loads(f.read_text())
        wrapper = sold_stub(old)
        if f.stem in listed or datetime.fromisoformat(wrapper["ended"]) < now - timedelta(days=SOLD_DAYS):
            f.unlink()
        else:
            if wrapper is not old:
                f.write_text(json.dumps(wrapper))  # migrate in place, so no getItem copy stays on disk
            sold.append(wrapper)
    return sold


# ---------- model ----------

def is_lowered(raw):
    """True only when eBay's own markdown field (marketingPrice) shows an original price above the current one."""
    try:
        return float(raw["marketingPrice"]["originalPrice"]["value"]) > float(raw["price"]["value"])
    except (KeyError, TypeError, ValueError):
        return False


def prepare(raw):
    """Turn a getItem response into the flat dict the templates use."""
    item_id = legacy_id(raw["legacyItemId"])
    aspects = {a["name"]: a["value"] for a in raw.get("localizedAspects", [])}
    brand = aspects.get("Brand")
    if brand and brand.lower() in NO_BRAND:
        brand = None
    cat = (raw.get("categoryPath") or "Other").split("|")[-1]
    price = raw.get("price", {})
    currency = price.get("currency", C["currency"])
    images = [raw["image"]["imageUrl"]] if raw.get("image") else []
    images += [i["imageUrl"] for i in raw.get("additionalImages", [])]
    status = (raw.get("estimatedAvailabilities") or [{}])[0].get("estimatedAvailabilityStatus")
    desc = clean_html(raw.get("description", ""))
    return {
        "id": item_id,
        "title": raw["title"],
        "url": f"/item/{item_id}-{slugify(raw['title'])[:60].strip('-')}/",
        "price": price.get("value", ""),
        "currency": currency,
        "price_text": money(price.get("value", ""), currency), "lowered": is_lowered(raw),
        "condition": raw.get("condition", ""),
        "condition_id": int(raw.get("conditionId") or 0),
        "cat": cat, "cat_slug": slugify(cat), "cat_parent": "|".join((raw.get("categoryPath") or "").split("|")[:2]),
        "brand": brand, "brand_slug": slugify(brand) if brand else None,
        "seller": raw["seller"]["username"],
        "images": images, "image": images[0] if images else "",
        "thumb": img(images[0], 500) if images else "",
        "thumb_srcset": srcset(images[0], (300, 500)) if images else "",
        "gallery": [{"src": img(u, 1600), "srcset": srcset(u, (500, 960, 1600))} for u in images],
        "specs": [(n, v) for n, v in aspects.items()],
        "desc": Markup(desc),
        "text": re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", desc))).strip(),
        "created": raw.get("itemCreationDate", ""),
        "ebay_url": raw.get("itemWebUrl", ""),
        "ebay_canonical": f"https://{C['domain']}/itm/{item_id}",
        "shipping": (raw.get("shippingOptions") or [{}])[0].get("shippingCost"),
        "returns": raw.get("returnTerms"),
        "gtin": raw.get("gtin"), "mpn": raw.get("mpn"),
        "area": (raw.get("itemLocation") or {}).get("country"),
        "availability": {"IN_STOCK": "InStock", "OUT_OF_STOCK": "OutOfStock"}.get(status),
    }


# ---------- rendering ----------

def write(out, path, text):
    f = out / path.lstrip("/")
    if path.endswith("/"):
        f = f / "index.html"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")


def page(out, path, template, **ctx):
    ctx.setdefault("canonical", C["site_url"] + path)
    write(out, path, env.get_template(template).render(site=C, ld=ld, **ctx))


def read_intro(kind, slug):
    f = ROOT / "content" / kind / f"{slug}.md"
    if not f.exists():
        return []
    return [p.strip() for p in re.split(r"\n\s*\n", f.read_text()) if p.strip()]


def item_list(items, start=1):
    return {"@context": "https://schema.org", "@type": "ItemList", "itemListElement": [
        {"@type": "ListItem", "position": n, "name": i["title"], "url": C["site_url"] + i["url"]}
        for n, i in enumerate(items, start)]}


def short_title(t, limit=40):
    if len(t) <= limit:
        return t
    cut = t[:limit - 1]
    return (cut.rsplit(" ", 1)[0] if " " in cut and t[limit - 1] != " " else cut).rstrip(" ,-|:;/–") + "…"


def hub_description(items, name, n=1):
    """Generated meta description, at most 160 chars. Drops the second title, then the 'including' clause, to fit."""
    sellers = {i["seller"] for i in items}
    who = next(iter(sellers)) if len(sellers) == 1 else f"{len(sellers)} eBay sellers"
    prices = [i for i in items if i["price"]]
    price = f" Prices from {min(prices, key=lambda i: float(i['price']))['price_text']}." if prices else ""
    tail = " Updated every 6 hours." + (f" Page {n}." if n > 1 else "")
    titles = [short_title(i["title"]) for i in items[:2]]
    count = f"{len(items)} listing{'' if len(items) == 1 else 's'}"
    base = f"{name + ' for sale: ' if name else ''}{count} from {who}"
    options = [f", including {titles[0]}, {titles[1]} and more"] if len(titles) > 1 else []
    options += [f", including {titles[0]} and more"] if titles else []
    options.append("")
    for inc in options:
        d = f"{base}{inc}.{price}{tail}"
        if len(d) <= 160:
            return d
    return d[:160]


def hub(out, sitemap, path, heading, items, kind=None, slug=None, link_groups=(), extra_ld=(), title=None):
    """A listing page: paginated, self-canonical, noindex while it has too few items."""
    sellers = len({i["seller"] for i in items})
    summary = f"{len(items)} listings from {sellers} seller{'' if sellers == 1 else 's'}, updated every 6 hours"
    intro = read_intro(kind, slug) if kind else []
    name = heading if kind in ("category", "brand") else ""
    thin = len(items) < MIN_HUB_ITEMS
    chunks = [items[i:i + PAGE_SIZE] for i in range(0, len(items), PAGE_SIZE)] or [[]]
    urls = [path if n == 1 else f"{path}page/{n}/" for n in range(1, len(chunks) + 1)]
    crumbs = [("Home", "/"), (heading, path)] if path != "/" else []
    for n, chunk in enumerate(chunks, 1):
        page(out, urls[n - 1], "hub.html",
             title=f"{title or heading}{f' - page {n}' if n > 1 else ''} | {C['site_name']}",
             description=intro[0][:160] if intro else hub_description(items, name, n), heading=heading, intro=intro, summary=summary, items=chunk,
             robots="noindex,follow" if thin else None, link_groups=link_groups,
             pager=list(enumerate(urls, 1)) if len(urls) > 1 else [], current=n,
             crumbs=crumbs, card_url=f"https://{C['domain']}/usr/{slug}" if kind == "seller" else None,
             jsonld=[item_list(chunk, (n - 1) * PAGE_SIZE + 1), *extra_ld, *([crumb_ld(crumbs)] if crumbs else [])],
             og_image=chunk[0]["image"] if chunk else "")
    if not thin:
        sitemap.append((C["site_url"] + path, max(i["created"] for i in items)[:10]))




def condition_type(condition_id):
    """eBay condition ids: below 2000 new, 2000-2500 refurbished, 2750 and up used (e.g. 4000 "Very Good")."""
    if not condition_id:
        return None
    return "NewCondition" if condition_id < 2000 else "RefurbishedCondition" if condition_id <= 2500 else "UsedCondition"


def crumb_ld(crumbs):
    return {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": n, "name": name, "item": C["site_url"] + p} for n, (name, p) in enumerate(crumbs, 1)]}


STOPWORDS = {"and", "the", "with", "for", "new", "set", "from", "all", "bundle", "x2", "x3", "x4", "x5", "x6", "x7", "x8", "x9"}


def title_words(t):
    return {w for w in re.findall(r"[a-z0-9]+", t.lower()) if len(w) > 2 and w not in STOPWORDS}


def related(items, i):
    """Listings that share something real with this one: brand, category, or title words. The seller alone doesn't count."""
    words = title_words(i["title"])
    def score(o):
        overlap = len(words & title_words(o["title"])) / (len(words | title_words(o["title"])) or 1)
        return (3 * (bool(i["brand"]) and o["brand"] == i["brand"]) + 2 * (o["cat_slug"] == i["cat_slug"])
                + (bool(i["cat_parent"]) and o["cat_parent"] == i["cat_parent"]) + 4 * overlap)
    scored = [(score(o), o) for o in items if o["id"] != i["id"]]
    # ponytail: 2 means at least a shared category or brand, or strong title overlap. Tune if pages look sparse.
    return [o for sc, o in sorted((t for t in scored if t[0] >= 2), key=lambda t: -t[0])][:RELATED]


def sold_page(out, w, more):
    crumbs = [("Home", "/"), ("Listing ended", w["url"])]
    page(out, w["url"], "sold.html", item=w, related=more, canonical=None, robots="noindex,follow", crumbs=crumbs,
         jsonld=[crumb_ld(crumbs)], title=f"Listing ended | {C['site_name']}", description="This listing has ended.")


def privacy_page(out):
    sellers = C["sellers"]
    who = ", ".join(sellers[:-1]) + " and " + sellers[-1] if len(sellers) > 1 else sellers[0]
    page(out, "/privacy/", "privacy.html", title=f"Privacy | {C['site_name']}", description="How this site handles your data.",
         robots="noindex,follow", crumbs=[], sellers=who,
         ebay_privacy=EBAY_PRIVACY.get(C["marketplace"], f"https://{C['domain']}/help"))


def item_page(out, i, more):
    canonical = i["ebay_canonical"] if ITEM_CANONICAL == "ebay" else C["site_url"] + i["url"]
    crumbs = [("Home", "/"), (i["cat"], f"/category/{i['cat_slug']}/"), (i["title"], i["url"])]
    offer = {"@type": "Offer", "url": i["ebay_canonical"], "price": i["price"], "priceCurrency": i["currency"],
             "seller": {"@type": "Person", "name": i["seller"]}}
    if i["availability"]:
        offer["availability"] = "https://schema.org/" + i["availability"]
    cond = condition_type(i["condition_id"])
    if cond:
        offer["itemCondition"] = "https://schema.org/" + cond
    country = C["marketplace"][5:]  # EBAY_GB -> GB
    if i["shipping"]:
        offer["shippingDetails"] = {"@type": "OfferShippingDetails",
            "shippingRate": {"@type": "MonetaryAmount", "value": i["shipping"]["value"], "currency": i["shipping"]["currency"]},
            "shippingDestination": {"@type": "DefinedRegion", "addressCountry": country}}
    if r := i["returns"]:
        if r.get("returnsAccepted"):
            policy = {"returnPolicyCategory": "https://schema.org/MerchantReturnFiniteReturnWindow", "applicableCountry": country,
                      "returnFees": "https://schema.org/" + ("FreeReturn" if r.get("returnShippingCostPayer") == "SELLER" else "ReturnShippingFees")}
            if days := (r.get("returnPeriod") or {}).get("value"):
                policy["merchantReturnDays"] = days
        else:
            policy = {"returnPolicyCategory": "https://schema.org/MerchantReturnNotPermitted"}
        offer["hasMerchantReturnPolicy"] = {"@type": "MerchantReturnPolicy", **policy}
    if i["area"]:
        offer["areaServed"] = i["area"]
    product = {"@context": "https://schema.org", "@type": "Product", "name": i["title"], "sku": i["id"],
               "description": (i["text"] or i["title"])[:500], "offers": offer}
    if i["images"]:
        product["image"] = i["images"]
    if i["brand"]:
        product["brand"] = {"@type": "Brand", "name": i["brand"]}
    for k in ("gtin", "mpn"):
        if i[k]:
            product[k] = i[k]
    page(out, i["url"], "item.html", item=i, related=more, canonical=canonical, crumbs=crumbs, jsonld=[product, crumb_ld(crumbs)],
         og_product={"amount": i["price"], "currency": i["currency"], "condition": (cond or "")[:-9].lower()},
         title=f"{i['title']} | {C['site_name']}", og_image=i["image"],
         description=f"{i['title']} - {i['price_text']}{', ' + i['condition'] if i['condition'] else ''}. Sold by {i['seller']} on eBay."[:160])


def headers():
    """Cloudflare Pages _headers. The CSP allows the inlined stylesheet by hash, so style-src needs no 'unsafe-inline'."""
    style_hash = base64.b64encode(hashlib.sha256(str(STYLE).encode()).digest()).decode()
    csp = ("default-src 'self'; img-src 'self' data: https://i.ebayimg.com; "
           f"style-src 'sha256-{style_hash}'; script-src 'self' https://static.cloudflareinsights.com; connect-src 'self' https://cloudflareinsights.com; "
           "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
    noindex = "  X-Robots-Tag: noindex\n"
    return ("/*\n"
            f"  Content-Security-Policy: {csp}\n"
            "  Strict-Transport-Security: max-age=31536000\n"
            "  Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()\n"
            "  Referrer-Policy: strict-origin-when-cross-origin\n"
            "  X-Content-Type-Options: nosniff\n"
            # Files under /static/ are referenced with ?v=<content hash>, so a change gets a new URL.
            "\n/static/*\n  Cache-Control: public, max-age=31536000, immutable\n"
            f"\nhttps://:project.pages.dev/*\n{noindex}"
            f"\nhttps://:version.:project.pages.dev/*\n{noindex}")


def group(items, key, name):
    groups = {}
    for i in items:
        if i[key]:
            groups.setdefault(i[key], []).append(i)
    return sorted(((g[0][name], slug, g) for slug, g in groups.items()), key=lambda t: -len(t[2]))


def merge_brands(items):
    """'p louise', 'PLouise' and 'P. Louise' are one brand: show the most common spelling."""
    spellings = {}
    for i in items:
        if i["brand"]:
            spellings.setdefault(re.sub(r"[^a-z0-9]", "", slugify(i["brand"])), collections.Counter())[i["brand"]] += 1
    for i in items:
        if i["brand"]:
            name = spellings[re.sub(r"[^a-z0-9]", "", slugify(i["brand"]))].most_common(1)[0][0]
            i["brand"] = name.title() if name.islower() else name  # "p louise" reads as a typo in a heading
            i["brand_slug"] = slugify(i["brand"])


def build(items, out, sold=()):
    env.globals["style"] = STYLE
    env.globals["js_version"] = hashlib.sha256((ROOT / "static" / "search.js").read_bytes()).hexdigest()[:10]
    env.globals["built"] = datetime.now(timezone.utc)
    # Set by GitHub Actions, so each fork links to its own repo without extra config.
    if repo := os.environ.get("GITHUB_REPOSITORY"):
        env.globals["repo_url"] = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}"
    out = Path(out)
    shutil.rmtree(out, ignore_errors=True)
    items = sorted((prepare(r) for r in items), key=lambda i: i["created"], reverse=True)
    sitemap = []
    merge_brands(items)
    cats, brands = group(items, "cat_slug", "cat"), group(items, "brand_slug", "brand")
    groups = [("Categories", [(n, f"/category/{s}/", len(g)) for n, s, g in cats]),
              ("Brands", [(n, f"/brand/{s}/", len(g)) for n, s, g in brands])]
    tagline = C.get("tagline")
    hub(out, sitemap, "/", tagline or "Listings from " + " and ".join(C["sellers"]), items, link_groups=groups)
    for seller in C["sellers"]:
        org = {"@context": "https://schema.org", "@type": "Person", "name": seller,
               "url": f"{C['site_url']}/seller/{seller}/", "sameAs": [f"https://{C['domain']}/usr/{seller}"]}
        hub(out, sitemap, f"/seller/{seller}/", seller, [i for i in items if i["seller"] == seller], "seller", seller, extra_ld=[org], title=f"{seller} on eBay")
    for name, slug, g in cats:
        hub(out, sitemap, f"/category/{slug}/", name, g, "category", slug, title=f"{name} for sale")
    for name, slug, g in brands:
        hub(out, sitemap, f"/brand/{slug}/", name, g, "brand", slug, title=f"{name} for sale")
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    hub(out, sitemap, "/new/", "New this week", [i for i in items if i["created"] >= week_ago], title="New listings this week")
    for i in items:
        item_page(out, i, related(items, i))
    live_ids = {i["id"] for i in items}
    for w in sold:
        if w["id"] not in live_ids:  # live wins if the item came back
            # Category only: no title, brand or parent, so the seller and title words never count as relevance.
            stub = {"id": w["id"], "cat_slug": w["cat_slug"], "seller": w["seller"], "title": "", "brand": None, "cat_parent": ""}
            sold_page(out, w, related(items, stub))

    privacy_page(out)
    page(out, "/search/", "search.html", title=f"Search | {C['site_name']}", description="Search listings.", robots="noindex,follow", crumbs=[])
    page(out, "/404.html", "notfound.html", title=f"Not found | {C['site_name']}", description="Page not found.", robots="noindex,follow", canonical=None)
    write(out, "/search.json", json.dumps([{"id": i["id"], "title": i["title"], "price": i["price_text"], "url": i["url"],
                                            "image": i["thumb"], "srcset": i["thumb_srcset"], "category": i["cat"]} for i in items]))
    write(out, "/sitemap.xml", '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
          + "".join(f"<url><loc>{xml_escape(u)}</loc><lastmod>{d}</lastmod></url>\n" for u, d in sitemap) + "</urlset>\n")
    updated = items[0]["created"] if items else ""
    write(out, "/feed.xml", '<?xml version="1.0" encoding="UTF-8"?>\n<feed xmlns="http://www.w3.org/2005/Atom">\n'
          f'<title>{xml_escape(C["site_name"])}</title>\n<id>{C["site_url"]}/</id>\n<updated>{updated}</updated>\n'
          f'<link rel="self" href="{C["site_url"]}/feed.xml"/>\n<link href="{C["site_url"]}/"/>\n'
          + "".join(f'<entry><title>{xml_escape(i["title"])}</title><id>{C["site_url"]}{i["url"]}</id><updated>{i["created"]}</updated>'
                    f'<link href="{C["site_url"]}{i["url"]}"/><summary>{xml_escape(i["text"][:300] or i["title"])}</summary></entry>\n'
                    for i in items[:30]) + "</feed>\n")
    write(out, "/_headers", headers())
    write(out, "/robots.txt", f"User-agent: *\nAllow: /\n\nSitemap: {C['site_url']}/sitemap.xml\n")
    shutil.copytree(ROOT / "static", out / "static", ignore=shutil.ignore_patterns("style.css"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "site.toml"))
    ap.add_argument("--fixture", help="JSON list of getItem responses; skips the eBay API")
    ap.add_argument("--sold", help="JSON list of {ended, id, url, seller, cat_slug} wrappers to render as sold pages (with --fixture)")
    ap.add_argument("--out", default=str(ROOT / "_site"))
    args = ap.parse_args()
    load_config(args.config)
    try:
        if args.fixture:
            items, sold = json.loads(Path(args.fixture).read_text()), [sold_stub(w) for w in json.loads(Path(args.sold).read_text())] if args.sold else []
        else:
            # Price history held old eBay prices, which are eBay content that is no longer public. Don't bring it back.
            (CACHE.parent / "prices.json").unlink(missing_ok=True)
            items, sold = fetch_live()
    except OSError as e:  # URLError and HTTPError are OSErrors
        sys.exit(f"fetch failed: {e}")
    build(items, args.out, sold=sold)
    print(f"built {len(items)} items and {len(sold)} sold pages into {args.out}")


if __name__ == "__main__":
    main()
