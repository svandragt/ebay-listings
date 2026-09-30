# AGENTS.md

This file gives guidance to coding agents (Claude Code and others) working on tinylistings, static listing pages for people selling on eBay. `site.toml` holds one instance's settings; everything else is shared code.

## Commands

```sh
python3 test_build.py                                                    # the whole test suite: plain asserts, prints "ok"
uv run --with jinja2 python build.py --config fixtures/site.toml --fixture fixtures/items.json   # offline build into _site/
python3 -m http.server -d _site                                          # preview
EBAY_CLIENT_ID=… EBAY_CLIENT_SECRET=… uv run --with jinja2 python build.py   # live build with site.toml
```

There is no linter and no test framework. `test_build.py` builds the fixture into a temp dir and asserts on the HTML. Add asserts there, not a new suite. No Node locally: use `devbox run -- npx …` (for example Lighthouse or wrangler).

## Architecture

All the logic lives in `build.py`: fetch → `prepare()` → render → write `_site/`. Templates are Jinja2 (`base.html`, `macros.html` with the shared grid `card` and "More like this", one `hub.html` for every list page, `item.html`, `sold.html`, `search.html`, `notfound.html`). `static/style.css` is inlined into every page through the `style` global, not linked.

- **Config vs code.** Anything site-specific belongs in `site.toml`, because the repo is public and meant to be forked. Tests use fake sellers in `fixtures/site.toml`. Marketplace → eBay domain and currency is the `MARKETPLACES` dict. The GitHub repo link in the footer comes from `GITHUB_REPOSITORY`.
- **Fetching.** The Browse `item_summary/search` endpoint needs a category, so `top_categories()` reads the marketplace's top-level categories from the Taxonomy API, and each seller is searched per category. `getItem` responses are cached in `.cache/items/{legacyItemId}.json`, and the workflow persists that cache between runs. Price, title and condition are always refreshed from the search summary. The call budget (5,000 a day, shared with another app) is why the cache exists.
- **Sold pages.** `retire_cache()` turns the cached `getItem` file of an unlisted item into `.cache/sold/{id}.json` as `{"ended", "id", "url", "seller", "cat_slug"}` and deletes the file. It keeps the first `ended`, deletes the file if the item is listed again and purges it after 30 days. `sold_stub()` also migrates old `{"ended", "item"}` wrappers in place. `build(items, out, sold)` renders each one at its old URL with `sold.html`: no eBay content, "This listing has ended", "More like this" by category only, `noindex`, no canonical, BreadcrumbList only. Sold items stay out of hubs, counts, sitemap, `search.json` and the feed. `--sold file.json` does the same in fixture mode.
- **Price lowered.** The item page shows a muted "Price lowered" only when eBay's own `marketingPrice.originalPrice` (a markdown sale) is above the current price, through `is_lowered()`. No old price or percentage, nothing on cards or in JSON-LD. The live run takes `marketingPrice` fresh from the search summary and drops any cached copy.
- **Privacy page.** `/privacy/` is self-canonical, `noindex,follow`, linked from the footer and left out of the sitemap. Its eBay link comes from `EBAY_PRIVACY`, with the marketplace's `/help` as fallback.
- **Fail closed.** Any HTTP error, or a seller returning 0 items, exits non-zero so the last good deploy stays live.
- **Canonical split.** Item pages set their canonical to `https://{domain}/itm/{legacyItemId}` (`ITEM_CANONICAL = "ebay"`) and are left out of the sitemap. Hub pages (home, seller, category, brand, `/new/`) are self-canonical and in the sitemap unless they have fewer than `MIN_HUB_ITEMS`, in which case they are `noindex,follow`. `/search/` is always `noindex`.
- **Structured data.**
  - JSON-LD goes through `ld()`, which escapes `</`.
  - Items get Product, Offer and BreadcrumbList. The Offer includes shipping and return policy.
  - Hubs get ItemList and BreadcrumbList, and seller pages get Person (the sellers are individuals, not traders).
  - Condition maps from eBay's numeric `conditionId` in `condition_type()`, not the text.
  - Item pages also carry `og:type=product` tags, and pages carry microformats2 (`h-product`, `h-feed`, `h-card`).
  - Never emit ratings or reviews. eBay's `primaryProductReviewRating` is third-party data.
- **Untrusted HTML.** Seller descriptions pass through `clean_html()` (tag allowlist, all attributes stripped). search.js builds DOM with `textContent` only.
- **Images.** `img(url, width)` routes through Cloudflare's `/cdn-cgi/image/` when `image_transform` is set in `site.toml`. Firefox tracking protection blocks `i.ebayimg.com`, so first-party URLs are required. Without the setting, it uses eBay's `s-l{width}.webp` variants. Only use widths eBay serves: 300, 500, 960 and 1600. `og:image` and JSON-LD keep the direct eBay URL.

## eBay licence

The API Licence Agreement (section 8.1) shaped these rules:
- Listing data is at most 6 hours old, hence the 6-hourly build.
- eBay content that is no longer public must be deleted. That is why sold pages keep only an id, URL, seller and category, and why `.cache/prices.json` is deleted on every live run. Don't keep price history.
- The price-lowered indicator comes only from eBay's own markdown field, because keeping old prices ourselves breaks that rule.
- Our own text stays visually separate from eBay content, so hub intros sit in `<section class="intro">`.
- A public privacy page is required.
- No framing of eBay pages.
- No price modelling.

## Deploy

`.github/workflows/build.yml` runs on push to `main`, every 6 hours (the eBay licence caps listing data at 6 hours old) and on manual dispatch. It has three jobs:
- `build` uploads `_site` as an artifact;
- `deploy` publishes it to Cloudflare Pages with wrangler, using `vars.CF_PAGES_PROJECT` for the project name;
- `keepalive` re-enables the workflow through the API, so GitHub doesn't disable the schedule after 60 days without activity.

Actions are pinned to commit SHAs with a `#vX.Y.Z` comment. GitHub Pages is not an option: its terms bar sites aimed at commercial transactions.

## Conventions

- Keep `build.py` as one file of plain functions. Stdlib and jinja2 only: HTTP uses urllib and the sanitiser uses html.parser.
- Deliberate shortcuts carry a `ponytail:` comment naming the ceiling.
- Stage files explicitly when committing. `.wrangler/`, `.devbox/`, `devbox.json` and `.cache/` are local.
