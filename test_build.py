"""Run with `python test_build.py`. Builds from the fixture into a temp dir and checks the output."""
import json, re, tempfile
from pathlib import Path

import build

ROOT = Path(__file__).parent
build.load_config(ROOT / "fixtures" / "site.toml")
items = json.loads((ROOT / "fixtures" / "items.json").read_text())
out = Path(tempfile.mkdtemp())
build.build(items, out)
site = build.C["site_url"]


def read(path):
    return (out / path.strip("/") / "index.html").read_text()


def jsonld(html):
    return [json.loads(m) for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]


item_dirs = sorted(p.name for p in (out / "item").iterdir())
assert len(item_dirs) == len(items)
assert all(re.fullmatch(r"\d+-[a-z0-9]+(-[a-z0-9]+)*", d) for d in item_dirs), item_dirs

for d in item_dirs:
    lid = d.split("-")[0]
    html = read(f"/item/{d}/")
    canonical = f"https://{build.C['domain']}/itm/{lid}"
    assert f'<link rel="canonical" href="{canonical}">' in html
    types = {j["@type"] for j in jsonld(html)}
    assert types == {"Product", "BreadcrumbList"}, types
    assert jsonld(html)[0]["offers"]["url"] == canonical

sitemap = (out / "sitemap.xml").read_text()
urls = re.findall(r"<loc>(.*?)</loc>", sitemap)
assert urls and not any("/item/" in u for u in urls)
for u in urls:
    assert (out / u.removeprefix(site).strip("/") / "index.html").exists(), u

assert '<meta name="robots" content="noindex,follow">' in read("/brand/lego/")  # 1 item
assert '<meta name="robots" content="noindex,follow">' not in read("/brand/casio/")  # 3 items
assert f"{site}/brand/lego/" not in urls and f"{site}/brand/casio/" in urls
assert 'name="robots" content="noindex' in read("/search/")
assert "<p>Example-seller-a" not in read("/seller/example-seller-a/") and "Every listing links straight to the eBay page" in read("/seller/example-seller-a/")

# untrusted description: no script, style, attributes or javascript: links survive
bad = read("/item/" + next(d for d in item_dirs if d.startswith("110000000005")))
assert "<script>alert" not in bad and "alert('xss')" not in bad and "display:none" not in bad
assert "onclick" not in bad and "javascript:" not in bad and "Dishwasher safe" in bad

# crafted text can't close the ld+json script tag
crafted = build.ld({"name": "</script><script>alert(1)</script><!--"})
assert "</" not in crafted and "<!--" not in crafted and json.loads(crafted)["name"].startswith("</script>")

for f in ("feed.xml", "robots.txt", "404.html", "search.json", "static/style.css"):
    assert (out / f).exists(), f
assert not (out / "CNAME").exists()
print("ok")
