import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from urllib.parse import urljoin, urlparse, urldefrag, urlencode

import requests
from bs4 import BeautifulSoup, NavigableString

HEADERS = {"User-Agent": "HomeWatch/2.0 (+competitor pricing research)"}
JAYMAN_BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
PRICE_RE = re.compile(r"\$\s*([0-9][0-9,]*(?:\.\d{2})?)")
SQFT_RE = re.compile(r"([0-9][0-9,]{2,})\s*(?:sq\.?\s*ft|sqft|square\s*feet|ft²|ft2)", re.I)
BED_RE = re.compile(r"(?:bedrooms?|beds?)\s*[:\-]?\s*([0-9]+(?:\.5)?)|([0-9]+(?:\.5)?)\s*(?:bedrooms?|beds?)\b", re.I)
BATH_RE = re.compile(r"(?:bathrooms?|baths?)\s*[:\-]?\s*([0-9]+(?:\.5)?)|([0-9]+(?:\.5)?)\s*(?:bathrooms?|baths?)\b", re.I)
GARAGE_RE = re.compile(r"((?:single|double|triple|[1-4])(?:\s*[- ]?car)?\s+(?:attached\s+|detached\s+)?garage|(?:attached|detached)\s+(?:single|double|triple|[1-4])(?:\s*[- ]?car)?\s+garage)", re.I)
POSSESSION_RE = re.compile(r"(?:possession\s+date|immediate\s+possession|move[ -]?in(?:\s+ready)?|available(?:\s+on)?)\s*[:\-]?\s*([^|•·\n]{3,70})", re.I)
ADDRESS_RE = re.compile(r"\b\d{1,6}\s+[A-Za-z0-9'’.-]+(?:\s+[A-Za-z0-9'’.-]+){0,5}\s+(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Drive|Dr\.?|Boulevard|Blvd\.?|Way|Lane|Ln\.?|Trail|Tr\.?|Court|Ct\.?|Close|Crescent|Cres\.?|Place|Pl\.?|Terrace|Terr\.?|Circle|Landing|Rise|View|Heights|Gate|Green|Park|Row|Mews|Common|Manor|Grove|Heath|Square|Gardens|Hollow|Port|Link|Bend)\b[^|•\n,]*", re.I)
CALL_FOR_PRICE_RE = re.compile(r"\b(?:call|contact|inquire|enquire)\s+(?:us\s+)?for\s+price\b|\bpricing\s+(?:available\s+)?(?:on request|upon request)\b", re.I)
STATUS_RE = re.compile(r"\b(?:move[ -]?in\s+ready|available\s+now|immediate\s+possession|quick\s+possession|conditionally\s+sold|sold)\b", re.I)
VIEW_HOME_RE = re.compile(r"\b(?:view|see|explore)\s+(?:this\s+)?home\b|\bhome\s+details?\b", re.I)
LOAD_MORE_TEXT = re.compile(r"(?:load more|show more|view more|see more|more homes|more listings)", re.I)
NEXT_TEXT = re.compile(r"^(?:next|next page|older|more homes|view more)$", re.I)
HOME_TYPE_LABELS = [
    ("Street Towns (No Condo Fees)", ["street towns (no condo fees)", "street towns", "street town"]),
    ("Bungalow Villas", ["bungalow villas", "bungalow villa"]),
    ("Duplex Homes", ["duplex homes", "duplex home", "duplex"]),
    ("Laned Home", ["laned home", "laned homes", "rear lane", "lane home"]),
    ("Front Drive", ["front drive", "front-drive", "front attached garage"]),
    ("Townhomes", ["townhomes", "townhome", "townhouse"]),
    ("Condos", ["condos", "condo", "apartment"]),
    ("Semi-detached", ["semi-detached", "semi detached", "paired home"]),
    ("Detached", ["single family", "single-family", "detached"]),
]
CARD_SELECTORS = ["article", ".home-card", ".listing-card", ".property-card", ".inventory-card", "[class*='home-card']", "[class*='listing-card']", "[class*='property-card']", "[class*='inventory-card']", "[data-testid*='home']", "[data-testid*='listing']", ".card", "[class*='home']", "[class*='listing']", "[class*='property']"]

API_NAME_KEYS = ("name", "title", "modelName", "model", "homeName", "planName", "unitName", "displayName")
API_PRICE_KEYS = ("webPrice", "price", "startingPrice", "salePrice", "listPrice", "basePrice", "priceFrom")
API_SQFT_KEYS = ("squareFeet", "sqft", "sqFt", "squareFootage", "size", "homeSize")
API_BED_KEYS = ("bedrooms", "beds", "bedroomCount")
API_BATH_KEYS = ("bathrooms", "baths", "bathroomCount")
API_URL_KEYS = ("url", "link", "detailUrl", "homeUrl", "pageUrl", "permalink")
API_COMMUNITY_KEYS = ("community", "communityName", "neighbourhood", "neighborhood", "development")
API_ADDRESS_KEYS = ("address", "streetAddress", "fullAddress")
API_STYLE_KEYS = ("homeStyle", "homeStyles", "style", "homeType", "productType", "type")
API_GARAGE_KEYS = ("garage", "garageType")
API_POSSESSION_KEYS = ("possessionDate", "moveInDate", "availability", "availableDate")
API_IMAGE_KEYS = ("image", "imageUrl", "primaryImage", "thumbnail")


AI_SEGMENTS = [
    "front_garage", "rear_lane", "duplex", "semi_detached", "townhome",
    "bungalow", "detached", "condo", "unclassified"
]
AI_PRICE_PER_MTOK = {
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.6-terra": (2.00, 12.00),
    "gpt-5.6-sol": (4.00, 20.00),
    "gpt-5.6": (4.00, 20.00),
}


def env_bool(name, default=False):
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def ai_configured():
    return env_bool("HOMEWATCH_AI_ENABLED", True) and bool(os.getenv("OPENAI_API_KEY", "").strip())


def ai_new_stats():
    return {
        "enabled": ai_configured(),
        "mode": os.getenv("HOMEWATCH_AI_MODE", "hybrid").strip().lower() or "hybrid",
        "model": os.getenv("HOMEWATCH_AI_MODEL", "gpt-5.6-luna").strip() or "gpt-5.6-luna",
        "fallback_model": os.getenv("HOMEWATCH_AI_FALLBACK_MODEL", "gpt-5.6-terra").strip() or "gpt-5.6-terra",
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost_usd": 0.0,
        "enriched_candidates": 0,
        "extracted_candidates": 0,
        "escalations": 0,
        "errors": 0,
    }


def _ai_output_text(payload):
    parts = []
    for entry in payload.get("output", []) if isinstance(payload, dict) else []:
        if not isinstance(entry, dict):
            continue
        for content in entry.get("content", []) or []:
            if isinstance(content, dict) and content.get("type") == "output_text" and content.get("text"):
                parts.append(str(content.get("text")))
    return "".join(parts).strip()


def _ai_add_usage(stats, model, payload):
    usage = payload.get("usage", {}) if isinstance(payload, dict) else {}
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    stats["input_tokens"] += inp
    stats["output_tokens"] += out
    in_rate, out_rate = AI_PRICE_PER_MTOK.get(model, (0.0, 0.0))
    stats["estimated_cost_usd"] += (inp / 1_000_000.0) * in_rate + (out / 1_000_000.0) * out_rate


def ai_response_json(model, instructions, input_text, schema_name, schema, stats):
    """Call the OpenAI Responses API with strict Structured Outputs.

    HomeWatch uses direct HTTPS so the Railway crawler does not need another
    Python dependency. The API key is read only from OPENAI_API_KEY.
    """
    if not stats.get("enabled"):
        return None
    max_calls = max(1, min(int(os.getenv("HOMEWATCH_AI_MAX_CALLS", "20")), 100))
    max_cost = max(0.01, float(os.getenv("HOMEWATCH_AI_MAX_COST_USD", "1.00")))
    if stats["calls"] >= max_calls or stats["estimated_cost_usd"] >= max_cost:
        print(f"AI: budget guard reached (calls={stats['calls']}, est=${stats['estimated_cost_usd']:.4f})", flush=True)
        return None

    key = os.getenv("OPENAI_API_KEY", "").strip()
    timeout = max(15, min(int(os.getenv("HOMEWATCH_AI_TIMEOUT_SECONDS", "75")), 180))
    body = {
        "model": model,
        "instructions": instructions,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": input_text}]}],
        "reasoning": {"effort": "none"},
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name[:64],
                "schema": schema,
                "strict": True,
            }
        },
    }
    try:
        stats["calls"] += 1
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=body,
            timeout=timeout,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"OpenAI API {response.status_code}: {response.text[:700]}")
        payload = response.json()
        _ai_add_usage(stats, model, payload)
        text = _ai_output_text(payload)
        if not text:
            raise RuntimeError("OpenAI response contained no output_text")
        return json.loads(text)
    except Exception as exc:
        stats["errors"] += 1
        print(f"AI ERROR ({model}): {type(exc).__name__}: {exc}", flush=True)
        return None


def _ai_candidate_schema():
    nullable_number = {"type": ["number", "null"]}
    nullable_integer = {"type": ["integer", "null"]}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["candidates"],
        "properties": {
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "id", "name", "price", "price_from", "price_to", "price_text", "sqft", "bedrooms", "bathrooms",
                        "community", "address", "home_type", "garage", "possession_date",
                        "product_segment", "confidence", "evidence"
                    ],
                    "properties": {
                        "id": {"type": "integer"},
                        "name": {"type": "string"},
                        "price": nullable_integer,
                        "price_from": nullable_integer,
                        "price_to": nullable_integer,
                        "price_text": {"type": "string"},
                        "sqft": nullable_integer,
                        "bedrooms": nullable_number,
                        "bathrooms": nullable_number,
                        "community": {"type": "string"},
                        "address": {"type": "string"},
                        "home_type": {"type": "string"},
                        "garage": {"type": "string"},
                        "possession_date": {"type": "string"},
                        "product_segment": {"type": "string", "enum": AI_SEGMENTS},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "evidence": {"type": "string"},
                    },
                },
            }
        },
    }


def _ai_page_schema():
    schema = _ai_candidate_schema()
    props = schema["properties"]["candidates"]["items"]["properties"]
    req = schema["properties"]["candidates"]["items"]["required"]
    props["url"] = {"type": "string"}
    props["listing_type"] = {"type": "string", "enum": ["quick_possession", "presale", "model", "unknown"]}
    req.extend(["url", "listing_type"])
    return schema


def _ai_candidate_payload(item, idx, context_chars):
    return {
        "id": idx,
        "current": {
            "name": item.name,
            "price": item.price,
            "price_from": item.price_from,
            "price_to": item.price_to,
            "price_text": item.price_text,
            "sqft": item.sqft,
            "bedrooms": item.bedrooms,
            "bathrooms": item.bathrooms,
            "community": item.community,
            "address": item.address,
            "home_type": item.home_type,
            "garage": item.garage,
            "possession_date": item.possession_date,
            "url": item.url,
            "product_segment": classify_segment(item),
        },
        # Detail-page evidence comes first. On builder listing cards the raw text can
        # be long enough to truncate the detail page before AI ever sees explicit
        # fields such as Sterling's "Style:" label.
        "evidence": clean_text(f"{item.detail_text} {item.raw_text}")[:context_chars],
    }


def _apply_ai_candidate(item, row, threshold):
    """AI may fill blanks, but never overwrite explicit crawler evidence.

    Product segment is accepted only when deterministic classification is still
    unclassified. This keeps user/source rules and strong deterministic parsing
    authoritative while allowing AI to solve ambiguous builder markup.
    """
    try:
        conf = float(row.get("confidence") or 0)
    except Exception:
        conf = 0.0
    item._ai_confidence = conf
    item._ai_evidence = clean_text(row.get("evidence"))[:500]
    if conf < threshold:
        return False

    changed = False
    fill = (
        ("name", str, 300), ("price_text", str, 300), ("community", str, 200),
        ("address", str, 300), ("home_type", str, 120), ("garage", str, 120),
        ("possession_date", str, 160),
    )
    for field, caster, limit in fill:
        if getattr(item, field, None) in (None, ""):
            value = clean_text(row.get(field))
            if value:
                setattr(item, field, value[:limit]); changed = True
    for field, caster in (("price", int), ("price_from", int), ("price_to", int), ("sqft", int), ("bedrooms", float), ("bathrooms", float)):
        if getattr(item, field, None) is None and row.get(field) is not None:
            try:
                setattr(item, field, caster(row[field])); changed = True
            except Exception:
                pass

    deterministic = classify_segment(item)
    segment = clean_text(row.get("product_segment"))
    if deterministic == "unclassified" and segment in AI_SEGMENTS and segment != "unclassified":
        item._ai_product_segment = segment
        changed = True
    if changed:
        item.origin = (item.origin + "+ai")[:80]
        if item._ai_evidence:
            item.evidence = (item.evidence + "; AI: " + item._ai_evidence).strip("; ")[:1000]
    return changed


def ai_enrich_items(source, items, stats, model=None, only_indexes=None, escalation=False):
    if not stats.get("enabled") or not items:
        return set()
    model = model or stats["model"]
    threshold = max(0.0, min(float(os.getenv("HOMEWATCH_AI_CONFIDENCE_THRESHOLD", "0.80")), 1.0))
    batch_size = max(5, min(int(os.getenv("HOMEWATCH_AI_BATCH_SIZE", "20")), 40))
    context_chars = max(500, min(int(os.getenv("HOMEWATCH_AI_CANDIDATE_CONTEXT_CHARS", "5000")), 6000))
    indexes = list(range(len(items))) if only_indexes is None else sorted(set(only_indexes))
    changed_ids = set()

    instructions = """You are HomeWatch's evidence-bound extraction engine for public homebuilder inventory.
For each supplied candidate, normalize and fill fields ONLY from that candidate's evidence. Never invent a value.
An empty string or null is correct when evidence is absent. Keep an explicit current value unless the evidence clearly supports the same value.
Classify product_segment ONLY for the specific listing: front_garage, rear_lane, duplex, semi_detached, townhome, bungalow, detached, condo, or unclassified.
Important distinctions: a duplex may also be semi-detached in ordinary language, but use duplex when the builder explicitly says Duplex. Use rear_lane for laned/rear-garage products. Use front_garage for front-attached/front-drive products.
The home_type field should preserve the builder's own visible label when present. Evidence must be a short literal phrase from the supplied candidate evidence that supports the classification or filled fields.
Do not use general knowledge about a model or builder that is not present in the supplied evidence."""

    for start in range(0, len(indexes), batch_size):
        batch_ids = indexes[start:start+batch_size]
        batch = [_ai_candidate_payload(items[i], i, context_chars) for i in batch_ids]
        user_input = json.dumps({
            "source": {"builder": source.get("builder", ""), "url": source.get("url", ""), "source_type": source.get("source_type", "both")},
            "candidates": batch,
        }, ensure_ascii=False)
        result = ai_response_json(model, instructions, user_input, "homewatch_candidate_enrichment", _ai_candidate_schema(), stats)
        if not result:
            continue
        by_id = {row.get("id"): row for row in result.get("candidates", []) if isinstance(row, dict)}
        for i in batch_ids:
            row = by_id.get(i)
            if not row:
                continue
            if _apply_ai_candidate(items[i], row, threshold):
                changed_ids.add(i)
    stats["enriched_candidates"] += len(changed_ids)
    if escalation:
        stats["escalations"] += 1
    return changed_ids


def _ai_page_material(page_url, html, max_chars):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.select("script,style,noscript,svg,iframe"):
        tag.decompose()
    text = clean_text(soup.get_text(" ", strip=True))[:max_chars]
    links = []
    seen = set()
    for a in soup.select("a[href]"):
        u = normalize_url(page_url, a.get("href"))
        if not u or not same_site(page_url, u) or u in seen:
            continue
        seen.add(u)
        label = clean_text(a.get_text(" ", strip=True))[:160]
        if label or any(k in (urlparse(u).path or "").lower() for k in ("home", "inventory", "quick", "model", "floorplan", "property")):
            links.append({"label": label, "url": u})
        if len(links) >= 220:
            break
    return {"page_url": page_url, "text": text, "links": links}


def ai_extract_from_pages(source, pages, stats, model=None):
    """Fallback when deterministic extraction returns zero candidates.

    Chromium still does the crawling. AI receives only the rendered text and
    same-site links and returns evidence-bound HomeWatch records.
    """
    if not stats.get("enabled") or not pages:
        return []
    model = model or stats["model"]
    max_pages = max(1, min(int(os.getenv("HOMEWATCH_AI_MAX_FALLBACK_PAGES", "3")), 8))
    max_chars = max(8000, min(int(os.getenv("HOMEWATCH_AI_PAGE_CONTEXT_CHARS", "32000")), 80000))
    threshold = max(0.0, min(float(os.getenv("HOMEWATCH_AI_CONFIDENCE_THRESHOLD", "0.80")), 1.0))
    materials = [_ai_page_material(u, h, max_chars) for u, h in pages[:max_pages]]

    instructions = """You are HomeWatch's evidence-bound page extraction engine.
Extract actual individual home listings or floorplans from the rendered public page material. Do not extract navigation labels, communities by themselves, promotions, contact cards, or generic product categories as homes.
Use only values literally supported by the supplied text or links. Never guess prices, square footage, addresses, model names, or product types.
URL must be a supplied same-site URL or the supplied page URL. Prefer the individual home/model detail URL when present.
For product_segment use only: front_garage, rear_lane, duplex, semi_detached, townhome, bungalow, detached, condo, unclassified.
Use quick_possession only when the evidence indicates a move-in-ready/quick-possession/spec/inventory home. Use presale for a floorplan or new-home offering marketed with starting/from pricing. Use model only for floorplans where pre-sale status is not supported; otherwise use unknown.
Evidence must be a short literal phrase from the supplied material. Return no candidate when the page does not actually contain enough evidence for a listing."""
    user_input = json.dumps({
        "source": {"builder": source.get("builder", ""), "url": source.get("url", ""), "source_type": source.get("source_type", "both")},
        "pages": materials,
    }, ensure_ascii=False)
    result = ai_response_json(model, instructions, user_input, "homewatch_page_extraction", _ai_page_schema(), stats)
    if not result:
        return []
    out = []
    seen = set()
    for row in result.get("candidates", []):
        if not isinstance(row, dict):
            continue
        try: conf = float(row.get("confidence") or 0)
        except Exception: conf = 0.0
        if conf < threshold:
            continue
        u = normalize_url(source.get("url", ""), row.get("url") or source.get("url", ""))
        if u and not same_site(source.get("url", ""), u):
            continue
        item = Item(
            name=clean_text(row.get("name"))[:300],
            price=int(row["price"]) if row.get("price") is not None else None,
            price_from=int(row["price_from"]) if row.get("price_from") is not None else None,
            price_to=int(row["price_to"]) if row.get("price_to") is not None else None,
            price_text=clean_text(row.get("price_text"))[:300],
            sqft=int(row["sqft"]) if row.get("sqft") is not None else None,
            bedrooms=float(row["bedrooms"]) if row.get("bedrooms") is not None else None,
            bathrooms=float(row["bathrooms"]) if row.get("bathrooms") is not None else None,
            community=clean_text(row.get("community"))[:200],
            address=clean_text(row.get("address"))[:300],
            home_type=clean_text(row.get("home_type"))[:120],
            garage=clean_text(row.get("garage"))[:120],
            possession_date=clean_text(row.get("possession_date"))[:160],
            url=(u or source.get("url", ""))[:1000],
            raw_text=clean_text(row.get("evidence"))[:1200],
            origin="ai-page",
            confidence=conf,
            evidence=("AI page extraction: " + clean_text(row.get("evidence")))[:1000],
        )
        seg = clean_text(row.get("product_segment"))
        if seg in AI_SEGMENTS and seg != "unclassified":
            item._ai_product_segment = seg
        lt = clean_text(row.get("listing_type"))
        if lt in ("quick_possession", "presale", "model"):
            item._ai_listing_type = lt
        strong = sum(bool(x) for x in (item.name, item.price or item.price_text, item.sqft, item.address, item.url))
        key = item.url or f"{item.name}|{item.address}|{item.sqft}|{item.price_text}"
        if strong >= 3 and key not in seen:
            seen.add(key); out.append(item)
    stats["extracted_candidates"] += len(out)
    return out

@dataclass
class Item:
    name: str = ""
    price: int | None = None
    price_from: int | None = None
    price_to: int | None = None
    price_text: str = ""
    sqft: int | None = None
    bedrooms: float | None = None
    bathrooms: float | None = None
    community: str = ""
    address: str = ""
    home_type: str = ""
    garage: str = ""
    possession_date: str = ""
    incentives: str = ""
    image_url: str = ""
    url: str = ""
    raw_text: str = ""
    detail_text: str = ""
    origin: str = "dom"
    confidence: float = 0.0
    evidence: str = ""


def clean_text(v):
    return re.sub(r"\s+", " ", str(v or "")).strip()


def money(text):
    """Return the current numeric asking price when one is explicit.
    Uses the last displayed dollar amount so sale/markdown cards such as
    "$338,900 $288,000" resolve to $288,000. Ambiguous shorthand such as
    "FROM THE MID $400s" stays text-only instead of becoming $400.
    """
    raw = text or ""
    if re.search(r"\$\s*\d{2,4}\s*s\b", raw, re.I):
        return None
    matches = list(PRICE_RE.finditer(raw))
    if not matches:
        return None
    try:
        value = int(float(matches[-1].group(1).replace(",", "")))
        return value if value >= 10000 else None
    except Exception:
        return None


def price_band(text):
    """Normalize builder shorthand such as 'from the high $500s'.

    The displayed wording remains the primary audit value in price_text. The
    numeric band makes pre-sale market comparisons possible without pretending
    that a starting price is a specific available-home price.
    """
    raw = clean_text(text)
    match = re.search(
        r"\b(?:from|starting\s+(?:at|from)?|priced\s+from)\s+(?:the\s+)?(?:(low|mid|high)\s+)?\$?\s*(\d{2,4})\s*s\b",
        raw,
        re.I,
    )
    if not match:
        return None, None

    band = int(match.group(2)) * 1000
    qualifier = (match.group(1) or "").lower()
    offset = {"low": 0, "mid": 40_000, "high": 60_000}.get(qualifier, 0)
    return band + offset, band + 100_000


def sq_ft(text):
    m = SQFT_RE.search(text or "")
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except Exception:
        return None


def regex_num(pattern, text):
    m = pattern.search(text or "")
    if not m:
        return None
    v = next((g for g in m.groups() if g), None)
    try:
        return float(v) if v else None
    except Exception:
        return None


def same_site(a, b):
    try:
        return (urlparse(a).hostname or "").lower().removeprefix("www.") == (urlparse(b).hostname or "").lower().removeprefix("www.")
    except Exception:
        return False


def normalize_url(base, href):
    if not href:
        return ""
    href = str(href).strip()
    if href.startswith(("mailto:", "tel:", "javascript:")):
        return ""
    return urldefrag(urljoin(base, href))[0]


def home_type(text):
    low = clean_text(text).lower()
    # Return the builder's product label, not a generic substring. Order is
    # intentionally specific-first so "Street Towns" never becomes Townhome.
    for label, terms in HOME_TYPE_LABELS:
        if any(term in low for term in terms):
            return label
    return ""


def exact_home_type_label(text):
    """Match a standalone builder product label without reading a whole card/page."""
    low = clean_text(text).lower().strip(" :-–—|•")
    if not low or len(low) > 90:
        return ""
    for label, terms in HOME_TYPE_LABELS:
        for term in terms:
            t = term.lower().strip()
            if low == t:
                return label
            # Allow harmless plural/punctuation suffixes, but not arbitrary prose.
            if low in (t + "s", t.rstrip("s")):
                return label
    return ""


def contextual_home_type(node):
    """Recover a product label that sits just outside an inner listing wrapper.

    Homes by Avi places the home type immediately before each listing's community.
    Our semantic View Home fallback can choose an inner wrapper beginning at the
    community/model, so the type is not inside node.get_text(). Scan backwards
    only as far as the previous listing's View Home CTA and take the nearest exact
    product label. This avoids borrowing a type from a neighbouring listing.
    """
    direct = home_type(clean_text(node.get_text(" ", strip=True)))
    if direct:
        return direct

    scanned = 0
    for el in node.previous_elements:
        if not isinstance(el, NavigableString):
            continue
        text = clean_text(el)
        if not text:
            continue
        scanned += 1
        label = exact_home_type_label(text)
        if label:
            return label
        if VIEW_HOME_RE.search(text):
            break
        if scanned >= 80:
            break
    return ""


def best_link(node, base):
    winner, best = "", -999
    for a in node.select("a[href]"):
        href = normalize_url(base, a.get("href"))
        if not href or not same_site(base, href):
            continue
        label = clean_text(a.get_text(" ", strip=True))
        low = f"{label} {href}".lower()
        score = 10 if VIEW_HOME_RE.search(label) else 0
        if any(x in low for x in ("quick-possession", "/home/", "/homes/", "inventory", "property")):
            score += 5
        if any(x in low for x in ("direction", "maps", "filter", "contact", "community")):
            score -= 10
        if score > best:
            winner, best = href, score
    return winner


def discover_nodes(soup):
    best_nodes, best_score = [], -1
    for selector in CARD_SELECTORS:
        try:
            nodes = soup.select(selector)
        except Exception:
            continue
        if not 2 <= len(nodes) <= 500:
            continue
        sample = nodes[:30]
        price_hits = sum(bool(PRICE_RE.search(n.get_text(" ", strip=True)) or CALL_FOR_PRICE_RE.search(n.get_text(" ", strip=True))) for n in sample)
        links = sum(bool(n.select_one("a[href]")) for n in sample)
        score = price_hits * 4 + links * 2 + min(len(nodes), 30) / 3
        if price_hits >= max(1, len(sample) // 5) and score > best_score:
            best_nodes, best_score = nodes, score
    return best_nodes




def discover_view_home_nodes(soup):
    """Fallback card discovery anchored on semantic 'View Home' links.

    Some builder sites (notably Homes by Avi) render perfectly structured
    listing content but do not expose predictable card class names. Starting
    from each View Home link, walk upward until the smallest ancestor contains
    the expected home fields. This is much safer than loosening generic card
    selectors globally.
    """
    out, seen = [], set()
    for a in soup.select("a[href]"):
        label = clean_text(a.get_text(" ", strip=True))
        if not VIEW_HOME_RE.search(label):
            continue
        node = a
        chosen = None
        for _ in range(9):
            node = getattr(node, "parent", None)
            if node is None or not hasattr(node, "get_text"):
                break
            raw = clean_text(node.get_text(" ", strip=True))
            if not raw or len(raw) > 2600:
                continue
            has_price = bool(PRICE_RE.search(raw) or CALL_FOR_PRICE_RE.search(raw) or re.search(r"\$\s*\d{2,4}\s*s\b", raw, re.I))
            has_sqft = bool(SQFT_RE.search(raw))
            has_bedbath = bool(BED_RE.search(raw) or BATH_RE.search(raw))
            has_address = bool(ADDRESS_RE.search(raw))
            view_count = sum(bool(VIEW_HOME_RE.search(clean_text(x.get_text(" ", strip=True)))) for x in node.select("a[href]"))
            if has_price and has_sqft and has_bedbath and has_address and view_count <= 2:
                chosen = node
                break
        if chosen is not None:
            ident = id(chosen)
            if ident not in seen:
                seen.add(ident)
                out.append(chosen)
    return out

def _address_from_node(node):
    # Prefer an individual rendered text fragment so fields before the address
    # (beds, baths, status) cannot bleed into a broad whole-card regex match.
    for part in node.stripped_strings:
        text = clean_text(part)
        if not re.search(r"\d", text):
            continue
        m = ADDRESS_RE.search(text)
        if m:
            return clean_text(m.group(0))
    return ""


def _name_and_community(node, base):
    links = list(node.select("a[href]"))
    view_href = ""
    for a in links:
        if VIEW_HOME_RE.search(clean_text(a.get_text(" ", strip=True))):
            view_href = normalize_url(base, a.get("href"))
            break
    if view_href:
        # Many builder cards link the model name and the 'View Home' CTA to
        # the same detail URL. The non-CTA label is a very strong model name.
        for a in links:
            label = clean_text(a.get_text(" ", strip=True))
            if label and not VIEW_HOME_RE.search(label) and normalize_url(base, a.get("href")) == view_href:
                if not re.search(r"direction|map|call|contact", label, re.I):
                    headings = [clean_text(h.get_text(" ", strip=True)) for h in node.select("h1,h2,h3,h4,h5")]
                    community = next((h for h in headings if h and h != label), "")
                    return label, community
    headings = [clean_text(h.get_text(" ", strip=True)) for h in node.select("h1,h2,h3,h4,h5") if clean_text(h.get_text(" ", strip=True))]
    if headings:
        return headings[-1], (headings[0] if len(headings) > 1 else "")
    strong = node.select_one("strong,[class*='title'],[class*='name']")
    return (clean_text(strong.get_text(" ", strip=True)) if strong else ""), ""


def parse_dom(node, base):
    raw = clean_text(node.get_text(" ", strip=True))[:10000]
    name, community = _name_and_community(node, base)
    price_matches = list(PRICE_RE.finditer(raw))
    if re.search(r"(?:FROM|STARTING|MID|LOW|HIGH)[^$]{0,20}\$\s*\d{2,4}\s*s\b", raw, re.I):
        mm = re.search(r"((?:FROM|STARTING|MID|LOW|HIGH)[^|•·\n]{0,40}\$\s*\d{2,4}\s*s)", raw, re.I)
        price_text = clean_text(mm.group(1)) if mm else clean_text(raw)
    elif price_matches:
        # Keep all displayed prices for auditability, while money() selects the last/current one.
        price_text = " ".join(m.group(0) for m in price_matches[-2:])
    else:
        price_text = "CALL FOR PRICE" if CALL_FOR_PRICE_RE.search(raw) else ""
    address = _address_from_node(node)
    garage = GARAGE_RE.search(raw)
    possession = POSSESSION_RE.search(raw)
    return Item(name=name[:300], price=money(price_text), price_text=price_text[:300], sqft=sq_ft(raw), bedrooms=regex_num(BED_RE, raw), bathrooms=regex_num(BATH_RE, raw), community=community[:200], address=address[:300], home_type=contextual_home_type(node)[:120], garage=(garage.group(1) if garage else "")[:120], possession_date=(clean_text(possession.group(1)) if possession else "")[:160], url=(best_link(node, base) or base)[:1000], raw_text=raw)


def evidence(node, item):
    raw = clean_text(node.get_text(" ", strip=True))
    links = node.select("a[href]")
    signals = {
        "price": bool(PRICE_RE.search(raw) or CALL_FOR_PRICE_RE.search(raw)),
        "sqft": bool(SQFT_RE.search(raw)),
        "beds": bool(BED_RE.search(raw)),
        "baths": bool(BATH_RE.search(raw)),
        "address": bool(ADDRESS_RE.search(raw)),
        "status": bool(STATUS_RE.search(raw)),
        "view_home": any(VIEW_HOME_RE.search(clean_text(a.get_text(" ", strip=True))) for a in links),
        "home_type": bool(contextual_home_type(node) or re.search(r"\b(?:front drive|laned home|street towns?)\b", raw, re.I)),
        "name": bool(item.name),
    }
    weights = {"price":2,"sqft":2,"beds":2,"baths":1,"address":2,"status":1,"view_home":3,"home_type":1,"name":1}
    score = sum(weights[k] for k,v in signals.items() if v)
    if len(raw) > 3500: score -= 8
    if len(PRICE_RE.findall(raw)) > 4: score -= 8
    if len(links) > 12: score -= 5
    structural = sum(signals[k] for k in ("sqft","beds","address","status","view_home","home_type"))
    return score >= 8 and structural >= 3 and signals["price"], score, signals


def _ci_get(d, keys):
    if not isinstance(d, dict): return None
    low = {str(k).lower(): v for k,v in d.items()}
    for k in keys:
        v = d.get(k, low.get(k.lower()))
        if v not in (None, "", []): return v
    return None


def _scalar(v):
    if isinstance(v, (str,int,float,bool)): return v
    if isinstance(v, dict):
        for k in ("value","name","title","url","src"):
            x = _ci_get(v,(k,))
            if isinstance(x,(str,int,float)): return x
    if isinstance(v,list) and v: return _scalar(v[0])
    return None



def host_name(url):
    try:
        return (urlparse(url).hostname or '').lower().removeprefix('www.')
    except Exception:
        return ''


def _flat_json(obj, prefix='', depth=0, out=None):
    """Flatten a JSON object for fuzzy builder-specific field discovery."""
    if out is None:
        out = {}
    if depth > 6:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = re.sub(r'[^a-z0-9]', '', str(k).lower())
            path = f'{prefix}.{key}' if prefix else key
            if isinstance(v, (dict, list)):
                _flat_json(v, path, depth + 1, out)
            elif v not in (None, ''):
                out.setdefault(key, v)
                out.setdefault(path, v)
    elif isinstance(obj, list):
        for v in obj[:50]:
            _flat_json(v, prefix, depth + 1, out)
    return out


def _fuzzy_json_value(obj, aliases):
    flat = _flat_json(obj)
    norms = [re.sub(r'[^a-z0-9]', '', a.lower()) for a in aliases]
    # exact normalized key first
    for a in norms:
        if a in flat:
            return flat[a]
    # then suffix / contains matches for wrappers such as field_webprice_value
    for a in norms:
        for k, v in flat.items():
            leaf = k.rsplit('.', 1)[-1]
            if leaf.endswith(a) or a.endswith(leaf) or (len(a) >= 5 and a in leaf):
                return v
    return None


def jayman_api_item(obj, response_url, base):
    """Parse Jayman inventory JSON across API/schema revisions.

    Jayman's listing HTML is intentionally thin; inventory arrives client-side.
    Their API field casing/naming has changed before, so use normalized aliases
    instead of requiring one fixed response schema.
    """
    def v(*aliases):
        return _fuzzy_json_value(obj, aliases)

    name = v('homeModelName','modelName','model','planName','homeName','name','title','plan')
    price_v = v('webPrice','price','salePrice','listPrice','startingPrice','displayPrice','priceFrom')
    sqft_v = v('webSqFt','squareFeet','squareFootage','sqft','sqFt','homeSize','size')
    beds_v = v('bedrooms','bedroomCount','beds')
    baths_v = v('bathrooms','bathroomCount','baths')
    community = v('communityName','community','neighbourhood','neighborhood','developmentName','development')
    address = v('fullAddress','streetAddress','address','civicAddress')
    home_style = v('homeTypeName','homeType','productType','productName','styleName','style','homeStyle')
    garage = v('garageType','garage','garageSpaces','garageCount')
    possession = v('possessionDate','moveInDate','availableDate','availability','possession','moveIn')
    image = v('primaryImage','imageUrl','image','thumbnailUrl','thumbnail','heroImage')
    url_v = v('detailUrl','homeUrl','pageUrl','url','permalink','slug')

    pt = clean_text(price_v)
    if price_v not in (None, '') and not PRICE_RE.search(pt):
        try:
            pt = f"${float(str(price_v).replace(',','').replace('$','')):,.0f}"
        except Exception:
            pass
    try:
        sqft = int(float(str(sqft_v).replace(',',''))) if sqft_v not in (None,'') else None
    except Exception:
        sqft = sq_ft(str(sqft_v or ''))
    def number(x):
        try:
            return float(str(x).replace(',','')) if x not in (None,'') else None
        except Exception:
            return None

    # Jayman sometimes returns a relative slug rather than a complete URL.
    u = clean_text(url_v)
    if u and not u.startswith(('http://','https://','/')):
        if '/' not in u and name:
            u = '/calgary/all-homes/' + u.strip('/') + '/'
        else:
            u = '/' + u.lstrip('/')
    u = normalize_url(base, u)

    item = Item(
        name=clean_text(name)[:300], price=money(pt), price_text=pt[:300], sqft=sqft,
        bedrooms=number(beds_v), bathrooms=number(baths_v), community=clean_text(community)[:200],
        address=clean_text(address)[:300], home_type=clean_text(home_style)[:120],
        garage=clean_text(garage)[:120], possession_date=clean_text(possession)[:160],
        image_url=normalize_url(base, str(image or ''))[:1000], url=u[:1000],
        raw_text=clean_text(json.dumps(obj, default=str, ensure_ascii=False))[:12000],
        origin='xhr-jayman', confidence=.94, evidence=f'Jayman XHR JSON: {response_url}'[:1000]
    )
    return item


def looks_like_jayman_record(obj):
    if not isinstance(obj, dict):
        return False
    # Inspect only this object and one common field-wrapper level. Do not let a
    # top-level API envelope look like one giant home merely because it contains
    # an array of valid records deeper down.
    local = {}
    for k, v in obj.items():
        nk = re.sub(r'[^a-z0-9]', '', str(k).lower())
        if not isinstance(v, (dict, list)):
            local[nk] = v
        elif isinstance(v, dict) and nk in ('fields','properties','attributes','data'):
            for kk, vv in v.items():
                if not isinstance(vv, (dict,list)):
                    local[re.sub(r'[^a-z0-9]', '', str(kk).lower())] = vv
    def lv(*aliases):
        for a in aliases:
            n = re.sub(r'[^a-z0-9]', '', a.lower())
            if n in local and local[n] not in (None,''):
                return local[n]
        return None
    probes = [
        lv('webPrice','price','salePrice','listPrice'),
        lv('homeModelName','modelName','planName','homeName','name','title'),
        lv('communityName','community','neighbourhood','neighborhood'),
        lv('squareFeet','webSqFt','sqft','squareFootage'),
        lv('address','fullAddress','streetAddress'),
        lv('detailUrl','homeUrl','url','permalink','slug'),
    ]
    return sum(x not in (None,'',[]) for x in probes) >= 3 and any(x not in (None,'',[]) for x in probes[:2])


def jayman_api_records(payload, response_url):
    out, seen, stack = [], set(), [payload]
    while stack and len(out) < 2500:
        obj = stack.pop()
        if isinstance(obj, list):
            stack.extend(obj[:5000])
            continue
        if not isinstance(obj, dict):
            continue
        stack.extend(v for v in obj.values() if isinstance(v,(dict,list)))
        if not looks_like_jayman_record(obj):
            continue
        digest = hashlib.sha1(json.dumps(obj, sort_keys=True, default=str)[:10000].encode()).hexdigest()
        if digest not in seen:
            seen.add(digest)
            out.append((obj,response_url))
    return out



def jayman_community_links(html, base='https://www.jayman.com/calgary/find-your-home/'):
    """Discover Calgary Jayman community pages from the server-rendered community index."""
    soup=BeautifulSoup(html,'html.parser')
    out=[]; seen=set()
    for a in soup.select('a[href]'):
        u=normalize_url(base,a.get('href'))
        if not u or not same_site(base,u):
            continue
        path=(urlparse(u).path or '').lower()
        if '/calgary/find-your-home/communities/' not in path:
            continue
        # Skip anchors into sections/fragments; normalize_url already strips fragments.
        if u.rstrip('/') in seen:
            continue
        seen.add(u.rstrip('/')); out.append(u)
    return out


def jayman_quick_cards(html, page_url):
    """Parse quick-possession cards from a Jayman community page.

    Community pages are server rendered even when the global quick-possession page
    is only a shell in headless Chromium. We identify QP inventory by its canonical
    /quick-possession-homes/ detail URL instead of depending on JS filters.
    """
    soup=BeautifulSoup(html,'html.parser')
    # Community name is normally the first useful h1/h2 on the page.
    community=''
    for h in soup.select('h1,h2'):
        t=clean_text(h.get_text(' ',strip=True))
        if t and not re.search(r'filters|find your|homes for sale|visit a sales centre',t,re.I):
            community=t; break

    out=[]; seen=set()
    for a in soup.select('a[href]'):
        u=normalize_url(page_url,a.get('href'))
        if not u or not same_site(page_url,u):
            continue
        path=(urlparse(u).path or '').lower()
        if '/quick-possession-homes/' not in path:
            continue
        if u in seen:
            continue

        node=a; chosen=None
        for _ in range(9):
            node=getattr(node,'parent',None)
            if node is None or not hasattr(node,'get_text'):
                break
            raw=clean_text(node.get_text(' ',strip=True))
            if not raw or len(raw)>2600:
                continue
            # Community cards consistently expose price + sqft + beds/baths.
            if (PRICE_RE.search(raw) or CALL_FOR_PRICE_RE.search(raw)) and SQFT_RE.search(raw) and (BED_RE.search(raw) or BATH_RE.search(raw)):
                chosen=node; break
        if chosen is None:
            continue

        raw=clean_text(chosen.get_text(' ',strip=True))[:10000]
        # Prefer a non-CTA anchor that points at the same detail URL as the model name.
        name=''
        for x in chosen.select('a[href]'):
            label=clean_text(x.get_text(' ',strip=True))
            if normalize_url(page_url,x.get('href'))==u and label and not VIEW_HOME_RE.search(label):
                if not re.search(r'contact|direction|map',label,re.I):
                    name=label; break
        if not name:
            hs=[clean_text(h.get_text(' ',strip=True)) for h in chosen.select('h2,h3,h4,h5,strong') if clean_text(h.get_text(' ',strip=True))]
            if hs: name=hs[-1]

        # Home style is frequently represented by icon/class names around the card.
        attrs=[]
        for tag in [chosen]+list(chosen.select('*'))[:100]:
            for v in getattr(tag,'attrs',{}).values():
                if isinstance(v,(list,tuple)): attrs.extend(clean_text(x).replace('-',' ') for x in v)
                elif isinstance(v,(str,int,float)): attrs.append(clean_text(v).replace('-',' '))
        ht=home_type(' '.join(attrs)+' '+raw) or jayman_style_from_name(name)

        prices=list(PRICE_RE.finditer(raw))
        price_text=prices[-1].group(0) if prices else ('CALL FOR PRICE' if CALL_FOR_PRICE_RE.search(raw) else '')
        item=Item(
            name=name[:300], price=money(price_text), price_text=price_text[:300], sqft=sq_ft(raw),
            bedrooms=regex_num(BED_RE,raw), bathrooms=regex_num(BATH_RE,raw), community=community[:200],
            address=_address_from_node(chosen)[:300], home_type=ht[:120], url=u[:1000], raw_text=raw,
            origin='jayman-community', confidence=.97, evidence='Jayman server-rendered community quick-possession card'
        )
        seen.add(u); out.append(item)
    return out


def jayman_enrich_detail(item, html):
    """Use detail page to fill address/community without overwriting strong card data."""
    soup=BeautifulSoup(html,'html.parser')
    raw=clean_text(soup.get_text(' ',strip=True))[:20000]
    if not item.address:
        for part in soup.stripped_strings:
            t=clean_text(part)
            m=ADDRESS_RE.search(t)
            if m:
                item.address=clean_text(m.group(0))[:300]; break
    if not item.community:
        m=re.search(r'\bIn\s+([^|•·]{2,100}?)(?=\s+Contact a sales associate|\s+Floorplans|$)',raw,re.I)
        if m: item.community=clean_text(m.group(1))[:200]
    if not item.home_type:
        item.home_type=jayman_style_from_model_html(html,item.name)[:120]
    item.detail_text=raw[:12000]
    return item



def jayman_style_from_name(name):
    """Conservative style fallback for Jayman model names that encode product."""
    low=clean_text(name).lower()
    if 'street town' in low or 'townhome' in low or 'town house' in low:
        return 'Townhome'
    if re.search(r'\bduplex\b', low) or 'semi-detached' in low or 'semi detached' in low or 'semi-attached' in low or 'semi attached' in low:
        return 'Semi-detached'
    if re.search(r'\b(?:villa|bungalow)s?\b', low):
        return 'Bungalow'
    if re.search(r'\bcondo\b', low):
        return 'Condo'
    return ''


def jayman_style_from_model_html(html, model_name=''):
    """Extract Jayman's explicit home style from the top of a model page."""
    soup=BeautifulSoup(html or '', 'html.parser')
    strings=[clean_text(x) for x in soup.stripped_strings if clean_text(x)]
    start=0
    target=clean_text(model_name).lower()
    if target:
        for i,t in enumerate(strings[:180]):
            if clean_text(t).lower()==target:
                start=i
                break
    window=' '.join(strings[start:start+35])[:1800].lower()
    if re.search(r'\bfront[ -]?attached garage\b|\bfront drive\b', window):
        return 'Front Attached garage'
    if re.search(r'\bdetached with optional rear garage\b|\blaned\b|\brear[ -]?garage\b', window):
        return 'Detached with optional rear garage'
    if re.search(r'\bsemi[ -]?(?:detached|attached)\b', window):
        return 'Semi-detached'
    if re.search(r'\bstreet towns?\b|\btownhomes?\b|\btown houses?\b', window):
        return 'Townhome'
    if re.search(r'\bcondos?\b', window):
        return 'Condo'
    if re.search(r'\b(?:bungalow|villa)s?\b', window):
        return 'Bungalow'
    return jayman_style_from_name(model_name)


def jayman_model_slug(name):
    text=clean_text(name)
    text=re.sub(r'^(?:move in(?: now| [a-z]+ \d{4})?|coming soon|quick possession)\s+', '', text, flags=re.I)
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')


def jayman_fetch_model_style(name, headers=None):
    """Resolve a Jayman model style from its server-rendered model page."""
    fallback=jayman_style_from_name(name)
    slug=jayman_model_slug(name)
    if not slug:
        return fallback, ''
    urls=[
        f'https://www.jayman.com/calgary/all-homes/home-models/{slug}-m/',
        f'https://www.jayman.com/find-your-home/find-a-home-model/{slug}-m/',
    ]
    hdr=headers or {'User-Agent':JAYMAN_BROWSER_UA,'Accept-Language':'en-CA,en;q=0.9'}
    for url in urls:
        try:
            r=requests.get(url,headers=hdr,timeout=12,allow_redirects=True)
            if r.status_code>=400:
                continue
            style=jayman_style_from_model_html(r.text,name)
            if style:
                return style,r.url
        except Exception:
            pass
    return fallback,''

def sterling_detail_links(soup, base):
    """Return unique Sterling quick-possession detail URLs from the listing page."""
    out=[]; seen=set()
    for a in soup.select('a[href]'):
        u=normalize_url(base,a.get('href'))
        if not u or not same_site(base,u):
            continue
        path=(urlparse(u).path or '').lower()
        if '/quick_possession/' not in path and '/quick-possession/' not in path:
            continue
        if u in seen:
            continue
        seen.add(u); out.append(u)
    return out


def sterling_identity_url(url):
    """Canonical identity for a Sterling quick-possession URL.

    Listing cards and detail requests can differ only by trailing slash/query or
    redirect normalization. Using the raw URL as a dict key prevented detail-page
    Style fields from being merged back into otherwise valid listing cards.
    """
    try:
        p=urlparse(url or "")
        host=(p.hostname or "").lower().removeprefix("www.")
        path=re.sub(r"/+", "/", p.path or "/").rstrip("/") or "/"
        return f"{host}{path}".lower()
    except Exception:
        return clean_text(url).lower().rstrip("/")


def sterling_parse_detail_html(html, url):
    """Parse Sterling's detail page, whose labels are explicit and stable."""
    soup=BeautifulSoup(html,'html.parser')
    # Keep navigation/contact dialogs out of the model and AI evidence. Sterling
    # places "Contact Us" headings before the actual home in the full document.
    soup=soup.select_one('main') or soup
    raw=clean_text(soup.get_text(' ',strip=True))[:30000]
    headings=[clean_text(h.get_text(' ',strip=True)) for h in soup.select('h1,h2,h3') if clean_text(h.get_text(' ',strip=True))]

    # Detail pages start with street address followed by model heading.
    address=''
    for h in headings[:8]:
        if ADDRESS_RE.search(h):
            address=clean_text(ADDRESS_RE.search(h).group(0)); break
    if not address:
        m=ADDRESS_RE.search(raw)
        if m: address=clean_text(m.group(0))

    model_node=soup.select_one('.home_model_single')
    model=clean_text(model_node.get_text(' ',strip=True)) if model_node else ''
    for h in headings[:12]:
        if model:
            break
        if h==address or ADDRESS_RE.search(h) or h.startswith('$'):
            continue
        if re.search(r'quick possession|features|download|showhome|your new home|take the next step',h,re.I):
            continue
        if 2 <= len(h) <= 100:
            model=h; break

    pm=PRICE_RE.search(raw)
    price_text=pm.group(0) if pm else ('CALL FOR PRICE' if CALL_FOR_PRICE_RE.search(raw) else '')
    sm=re.search(r'\bStyle\s*:\s*([^|•·]{2,80}?)(?=\s+Total Size\s*:|\s+Beds\s*:|\s+Bath\s*:|\s+Features\b|$)',raw,re.I)
    style=clean_text(sm.group(1)) if sm else ''
    qm=re.search(r'\bTotal Size\s*:\s*([0-9][0-9,]*)\s*(?:ft²|ft2|sq\.?\s*ft)',raw,re.I)
    sqft=int(qm.group(1).replace(',','')) if qm else sq_ft(raw)
    bb=re.search(r'\bBeds\s*:\s*([0-9]+(?:\.5)?)\s*/\s*Bath\s*:\s*([0-9]+(?:\.5)?)',raw,re.I)
    beds=float(bb.group(1)) if bb else None; baths=float(bb.group(2)) if bb else None
    pos=''
    xm=re.search(r'(?:Estimated|Guaranteed) Possession Date\s*:\s*([^|•·]{2,80}?)(?=\s+Style\s*:|\s+Total Size\s*:|$)',raw,re.I)
    if xm: pos=clean_text(xm.group(1))

    # Community appears directly beneath the address as "Community | City".
    community=''
    strings=[clean_text(x) for x in soup.stripped_strings if clean_text(x)]
    for i,t in enumerate(strings[:120]):
        if address and address.lower() in t.lower():
            for nxt in strings[i+1:i+6]:
                if '|' in nxt and not re.search(r'\$|beds|bath|style|size',nxt,re.I):
                    community=clean_text(nxt.split('|',1)[0]); break
            if community: break

    return Item(name=model[:300], price=money(price_text), price_text=price_text[:300], sqft=sqft,
                bedrooms=beds, bathrooms=baths, community=community[:200], address=address[:300],
                home_type=style[:120], possession_date=pos[:160], url=url[:1000], raw_text=raw[:12000],
                detail_text=raw[:12000], origin='detail-sterling', confidence=.99,
                evidence='Sterling explicit detail-page fields')


def jayman_prepare_page(page):
    """Select Calgary on Jayman's region gate so the inventory app can initialize."""
    for label in ('Calgary + area','Continue browsing'):
        try:
            loc=page.get_by_role('button',name=re.compile(re.escape(label),re.I))
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=5000)
                page.wait_for_timeout(1200)
        except Exception:
            pass

def sterling_card_nodes(soup):
    """Discover Sterling quick-possession cards by field density/link identity."""
    # The outer card holds data-style and data-address. Selecting the first
    # price/size/address ancestor stopped at an inner div and lost those fields.
    # Native cards also retain homes with street suffixes ADDRESS_RE cannot parse.
    native = [node for node in soup.select('.post_card_li')
              if node.select_one('a[href*="/quick_possession/"], a[href*="/quick-possession/"]')]
    if native:
        return native
    candidates, seen = [], set()
    # Their cards link both model/content and address to one quick-possession detail.
    for a in soup.select('a[href]'):
        href = clean_text(a.get('href'))
        if not href:
            continue
        node = a
        chosen = None
        for _ in range(8):
            node = getattr(node, 'parent', None)
            if node is None or not hasattr(node, 'get_text'):
                break
            raw = clean_text(node.get_text(' ', strip=True))
            if not raw or len(raw) > 2200:
                continue
            has_price = bool(PRICE_RE.search(raw) or re.search(r'\b(?:Sold|Conditionally Sold)\b', raw, re.I))
            has_sqft = bool(SQFT_RE.search(raw))
            # Sterling renders bedroom/bath/garage values as naked numerals, so address + price + sqft are stronger anchors.
            has_addr = bool(ADDRESS_RE.search(raw))
            if has_price and has_sqft and has_addr:
                chosen = node
                break
        if chosen is not None:
            ident=id(chosen)
            if ident not in seen:
                seen.add(ident); candidates.append(chosen)
    return candidates


def sterling_parse_dom(node, base):
    raw = clean_text(node.get_text(' ', strip=True))[:10000]
    links = [a for a in node.select('a[href]') if normalize_url(base,a.get('href'))]
    url = ''
    # Prefer the canonical quick-possession detail URL. A Sterling card can contain
    # several same-site links (community/model/etc.), and taking the first one can
    # assign the wrong identity to the listing.
    for a in links:
        h=normalize_url(base,a.get('href'))
        path=(urlparse(h).path or '').lower() if h else ''
        if h and same_site(base,h) and ('/quick_possession/' in path or '/quick-possession/' in path):
            url=h; break
    if not url:
        for a in links:
            h=normalize_url(base,a.get('href'))
            if same_site(base,h):
                url=h; break

    address = clean_text(node.get('data-address')) or _address_from_node(node)
    # Community is often a sibling immediately following the street address.
    community=''
    for idx, el in enumerate(list(node.stripped_strings)):
        t=clean_text(el)
        if address and address.lower() in t.lower() and len(t)>len(address):
            community=clean_text(re.sub(re.escape(address), '', t, flags=re.I)).strip(' -|')
            break
        if address and t.lower() == address.lower():
            rest=list(node.stripped_strings)
            if idx + 1 < len(rest):
                nxt=clean_text(rest[idx+1])
                if nxt and not re.fullmatch(r'(?:Immediate|[1-9]\s+to\s+[1-9]\s+months?)', nxt, re.I):
                    community=nxt
            break
    # Find visible title/model. Prefer headings/strong; otherwise strip known feature badges.
    texts=[clean_text(x) for x in node.stripped_strings if clean_text(x)]
    model_node=node.select_one('.post_card_home_model')
    name=clean_text(model_node.get_text(' ',strip=True)) if model_node else clean_text(node.get('data-home_model'))
    heads=[clean_text(h.get_text(' ',strip=True)) for h in node.select('h2,h3,h4,h5,strong') if clean_text(h.get_text(' ',strip=True))]
    ignore=re.compile(r'^(?:\$|\d+(?:\.5)?$|immediate$|\d+\s+to\s+\d+\s+months?$)',re.I)
    for t in reversed(heads or texts[:12]):
        if name:
            break
        if not ignore.search(t) and not ADDRESS_RE.search(t) and len(t)<120:
            name=t; break

    # Naked numeric sequence on Sterling is beds, baths, garage before sqft.
    beds=baths=None; garage=''
    numeric=[]
    for t in texts:
        if re.fullmatch(r'\d+(?:\.5)?', t):
            numeric.append(float(t))
    if len(numeric)>=2:
        beds,baths=numeric[0],numeric[1]
    if len(numeric)>=3:
        garage=f'{int(numeric[2]) if numeric[2].is_integer() else numeric[2]} car garage'

    price_match=PRICE_RE.search(raw)
    price_text=price_match.group(0) if price_match else ('Conditionally Sold' if re.search(r'Conditionally Sold',raw,re.I) else ('Sold' if re.search(r'\bSold\b',raw,re.I) else ''))
    possession=''
    pm=re.search(r'\b(Immediate|[1-9]\s+to\s+[1-9]\s+months?)\b',raw,re.I)
    if pm: possession=pm.group(1)

    # Home style often appears in class/data attributes even when not repeated in visible card copy.
    attr_parts=[]
    for tag in [node]+list(node.select('*'))[:80]:
        for v in getattr(tag,'attrs',{}).values():
            if isinstance(v,(list,tuple)):
                attr_parts.extend(clean_text(x).replace('-', ' ') for x in v)
            elif isinstance(v,(str,int,float)):
                attr_parts.append(clean_text(v).replace('-', ' '))
    attrs=' '.join(attr_parts)
    # Preserve the builder's exact per-home label, including Row Homes, rather
    # than inferring it from images, links, or feature badges elsewhere in a card.
    ht=clean_text(node.get('data-style')) or home_type(attrs+' '+raw)

    return Item(name=name[:300],price=money(price_text),price_text=price_text[:300],sqft=sq_ft(raw),
                bedrooms=beds,bathrooms=baths,community=community[:200],address=address[:300],home_type=ht[:120],
                garage=garage[:120],possession_date=possession[:160],url=(url or base)[:1000],raw_text=raw,
                origin='dom-sterling',confidence=.94,evidence='Sterling quick-possession card')

def api_records(payload, response_url):
    out, seen, stack = [], set(), [payload]
    while stack and len(out) < 1500:
        obj = stack.pop()
        if isinstance(obj, list):
            stack.extend(obj[:3000]); continue
        if not isinstance(obj, dict): continue
        stack.extend(v for v in obj.values() if isinstance(v,(dict,list)))
        vals = [_scalar(_ci_get(obj,k)) for k in (API_NAME_KEYS,API_PRICE_KEYS,API_SQFT_KEYS,API_BED_KEYS,API_ADDRESS_KEYS,API_URL_KEYS,API_STYLE_KEYS)]
        if sum(v not in (None,"",[]) for v in vals) < 3 or not any(vals[i] not in (None,"",[]) for i in (0,4,5)):
            continue
        digest = hashlib.sha1(json.dumps(obj,sort_keys=True,default=str)[:6000].encode()).hexdigest()
        if digest in seen: continue
        seen.add(digest); out.append((obj,response_url))
    return out


def api_item(obj, response_url, base):
    def val(keys): return _scalar(_ci_get(obj, keys))
    pv = val(API_PRICE_KEYS)
    pt = clean_text(pv)
    if pv not in (None,"") and not PRICE_RE.search(pt):
        try: pt = f"${float(str(pv).replace(',','').replace('$','')):,.0f}"
        except Exception: pass
    sv = val(API_SQFT_KEYS)
    try: sv = int(float(str(sv).replace(",",""))) if sv not in (None,"") else None
    except Exception: sv = sq_ft(str(sv or ""))
    def num(keys):
        try:
            x = val(keys); return float(x) if x not in (None,"") else None
        except Exception: return None
    u = normalize_url(base, str(val(API_URL_KEYS) or ""))
    return Item(name=clean_text(val(API_NAME_KEYS))[:300], price=money(pt), price_text=pt[:300], sqft=sv, bedrooms=num(API_BED_KEYS), bathrooms=num(API_BATH_KEYS), community=clean_text(val(API_COMMUNITY_KEYS))[:200], address=clean_text(val(API_ADDRESS_KEYS))[:300], home_type=clean_text(val(API_STYLE_KEYS))[:120], garage=clean_text(val(API_GARAGE_KEYS))[:120], possession_date=clean_text(val(API_POSSESSION_KEYS))[:160], image_url=normalize_url(base, str(val(API_IMAGE_KEYS) or ""))[:1000], url=u[:1000], raw_text=clean_text(json.dumps(obj,default=str,ensure_ascii=False))[:12000], origin="xhr", confidence=.9, evidence=f"XHR JSON response: {response_url}"[:1000])


def _node_number(node, selector):
    el = node.select_one(selector)
    if el is None:
        return None
    m = re.search(r"[0-9]+(?:\.5)?", clean_text(el.get_text(" ", strip=True)))
    return float(m.group(0)) if m else None


def _first_price_text(node):
    """Return the first displayed home price from a known listing card.

    Generic parsing intentionally uses the last amount for crossed-out sale prices.
    Daytona is the opposite: it prints the GST-included comparison price first and
    its lower pre-GST calculation second, so the explicit builder adapters select
    their price node directly.
    """
    m = PRICE_RE.search(clean_text(node.get_text(" ", strip=True))) if node else None
    return m.group(0) if m else ""


def trico_items_from_html(html, base):
    soup = BeautifulSoup(html or "", "html.parser")
    out = []
    for node in soup.select(".qp-card"):
        link = node.select_one('a[href*="/quick-possessions/"]')
        url = normalize_url(base, link.get("href")) if link else ""
        name_node = node.select_one(".model-and-price h3")
        price_node = node.select_one(".model-and-price .price")
        type_node = node.select_one(".home-type")
        address_node = node.select_one("address")
        image = node.select_one("img[src]")
        possession = node.select_one(".possession-date")
        type_text = clean_text(type_node.get_text(" ", strip=True)) if type_node else ""
        home_style, community = type_text, ""
        match = re.match(r"(.+?)\s+in\s+(.+)$", type_text, re.I)
        if match:
            home_style, community = clean_text(match.group(1)), clean_text(match.group(2))
        raw = clean_text(node.get_text(" ", strip=True))[:10000]
        price_text = clean_text(price_node.get_text(" ", strip=True)) if price_node else ""
        item = Item(
            name=(clean_text(name_node.get_text(" ", strip=True)) if name_node else "")[:300],
            price=money(price_text), price_text=price_text[:300], sqft=sq_ft(raw),
            bedrooms=_node_number(node, ".beds"), bathrooms=_node_number(node, ".baths"),
            community=community[:200],
            address=(clean_text(address_node.get_text(" ", strip=True)) if address_node else "")[:300],
            home_type=home_style[:120],
            possession_date=(clean_text(possession.get_text(" ", strip=True)) if possession else "")[:160],
            image_url=(normalize_url(base, image.get("src")) if image else "")[:1000],
            url=(url or base)[:1000], raw_text=raw, origin="dom-trico", confidence=.99,
            evidence="Trico explicit quick-possession card fields",
        )
        if item.name and item.url != base and (item.price or item.price_text) and item.sqft and item.address:
            out.append(item)
    return out


def daytona_items_from_html(html, base):
    soup = BeautifulSoup(html or "", "html.parser")
    out = []
    for node in soup.select(".card-home"):
        link = node.select_one("a.clickable-card-link[href], h3 a[href]")
        if link is None:
            continue
        url = normalize_url(base, link.get("href"))
        raw = clean_text(node.get_text(" ", strip=True))[:10000]
        price_node = node.select_one('[aria-label="Price"]')
        price_text = _first_price_text(price_node)
        if not price_text and price_node is not None:
            price_text = clean_text(price_node.get_text(" ", strip=True))[:300]
        community_node = node.select_one(".community-tag")
        community = clean_text(community_node.get_text(" ", strip=True)) if community_node else ""
        community = re.sub(r"^Community:\s*", "", community, flags=re.I)
        image = node.select_one("img[src]")
        item = Item(
            name=clean_text(link.get_text(" ", strip=True))[:300], price=money(price_text),
            price_text=price_text[:300], sqft=sq_ft(raw), bedrooms=regex_num(BED_RE, raw),
            bathrooms=regex_num(BATH_RE, raw), community=community[:200],
            image_url=(normalize_url(base, image.get("src")) if image else "")[:1000],
            url=url[:1000], raw_text=raw, origin="dom-daytona", confidence=.96,
            evidence="Daytona explicit move-in-ready card fields",
        )
        if item.name and item.url and item.sqft and (item.price or item.price_text):
            out.append(item)
    return out


def cedarglen_items_from_html(html, base):
    soup = BeautifulSoup(html or "", "html.parser")
    out = []
    for node in soup.select(".filter-item"):
        link = node.select_one('a[href*="/quick-possession/"]')
        if link is None:
            continue
        url = normalize_url(base, link.get("href"))
        headings = node.select("h1,h2,h3")
        name = clean_text(headings[0].get_text(" ", strip=True)) if headings else ""
        community_node = node.select_one("span.uppercase")
        price_node = node.select_one("span.font-semibold")
        image = node.select_one("img[src]")
        raw = clean_text(node.get_text(" ", strip=True))[:10000]
        price_text = clean_text(price_node.get_text(" ", strip=True)) if price_node else ""
        possession = ""
        m = re.search(r"\bAvailable\s+([A-Za-z]+\s+\d{4}|Now)\b", raw, re.I)
        if m:
            possession = clean_text(m.group(1))
        item = Item(
            name=name[:300], price=money(price_text), price_text=price_text[:300], sqft=sq_ft(raw),
            bedrooms=regex_num(BED_RE, raw), bathrooms=regex_num(BATH_RE, raw),
            community=(clean_text(community_node.get_text(" ", strip=True)) if community_node else "")[:200],
            address=_address_from_node(node)[:300], possession_date=possession[:160],
            image_url=(normalize_url(base, image.get("src")) if image else "")[:1000],
            url=url[:1000], raw_text=raw, origin="dom-cedarglen", confidence=.97,
            evidence="Cedarglen explicit quick-possession card fields",
        )
        if item.name and item.url and (item.price or item.price_text) and item.address:
            out.append(item)
    return out


def excel_calgary_community_links(html, base):
    """Return Excel quick-possession community pages outside its Edmonton group."""
    soup = BeautifulSoup(html or "", "html.parser")
    out, seen = [], set()
    for group in soup.select("li.community"):
        own_text = clean_text(" ".join(str(x) for x in group.find_all(string=True, recursive=False)))
        if "edmonton" in own_text.lower():
            continue
        for a in group.select('a[href*="/quick-possessions/"]'):
            url = normalize_url(base, a.get("href"))
            if not url or not same_site(base, url):
                continue
            url = urldefrag(url)[0].rstrip("/")
            if url not in seen:
                seen.add(url); out.append(url)
    return out


def excel_items_from_community_html(html, page_url):
    soup = BeautifulSoup(html or "", "html.parser")
    out = []
    community = (urlparse(page_url).path or "").rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title()
    canonical = soup.select_one('link[rel="canonical"]')
    if canonical and canonical.get("href"):
        community = (urlparse(canonical.get("href")).path or "").rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title()
    for node in soup.select(".home-tile"):
        link = node.find_parent("a", href=True)
        if link is None:
            continue
        name_node = node.select_one("h3")
        address_node = node.select_one("h4")
        price_node = node.select_one(".qp-price")
        image = node.select_one("img[src]")
        raw = clean_text(node.get_text(" ", strip=True))[:10000]
        price_text = clean_text(price_node.get_text(" ", strip=True)) if price_node else ""
        item = Item(
            name=(clean_text(name_node.get_text(" ", strip=True)) if name_node else "")[:300],
            price=money(price_text), price_text=price_text[:300], sqft=sq_ft(raw),
            bedrooms=regex_num(BED_RE, raw), bathrooms=regex_num(BATH_RE, raw), community=community[:200],
            address=(clean_text(address_node.get_text(" ", strip=True)) if address_node else "")[:300],
            image_url=(normalize_url(page_url, image.get("src")) if image else "")[:1000],
            url=normalize_url(page_url, link.get("href"))[:1000], raw_text=raw,
            origin="dom-excel", confidence=.97, evidence="Excel community quick-possession card fields",
        )
        if item.name and item.url and (item.price or item.price_text) and item.sqft and item.address:
            out.append(item)
    return out


def _detail_product_type(soup):
    """Extract an explicit product type from builder metadata or labeled fields."""
    for marker in soup.select(".sr-only"):
        if re.search(r"home\s*type", clean_text(marker.get_text(" ", strip=True)), re.I):
            parent = marker.parent
            value = clean_text(parent.get_text(" ", strip=True)) if parent else ""
            value = re.sub(r"^Home\s*type\s*:\s*", "", value, flags=re.I)
            if value and len(value) <= 100:
                return value
    meta = []
    if soup.title:
        meta.append(clean_text(soup.title.get_text(" ", strip=True)))
    for el in soup.select('meta[name="description"],meta[property="og:title"],meta[property="og:description"]'):
        if el.get("content"):
            meta.append(clean_text(el.get("content")))
    # Cedarglen's metadata is inconsistent: some homes identify their product
    # only in the body copy (for example, "Paired Quick Possession Home"). Keep
    # the evidence local to the detail page and include the rendered main text.
    body = soup.select_one("main") or soup
    text = (" | ".join(meta) + " | " + clean_text(body.get_text(" ", strip=True))[:12000])[:16000]
    patterns = (
        ("Front Garage", r"\bfront[ -]?(?:attached )?garage\b|\bfront[ -]?drive\b"),
        ("Rear Lane", r"\brear[ -]?lane\b|\blaned\b|\brear[ -]?garage\b"),
        ("Duplex", r"\bduplex\b"),
        ("Semi-detached", r"\bsemi[ -]?detached\b|\bpaired home\b|\bpaired quick possession\b"),
        ("Townhome", r"\btownhomes?\b|\btown houses?\b|\brow homes?\b"),
        ("Bungalow", r"\bbungalows?\b|\bvillas?\b"),
        ("Condo", r"\bcondos?\b|\bapartments?\b"),
        ("Detached", r"\bsingle[ -]?family\b"),
    )
    for label, pattern in patterns:
        if re.search(pattern, text, re.I):
            return label
    return ""


def enrich_builder_detail(item, html, site):
    soup = BeautifulSoup(html or "", "html.parser")
    main = soup.select_one("main") or soup
    detail = clean_text(main.get_text(" ", strip=True))[:12000]
    if not item.home_type:
        item.home_type = _detail_product_type(soup)[:120]
    if site == "daytona":
        location = soup.select_one('[aria-label="Location"]')
        if location is not None:
            item.address = (_address_from_node(location) or clean_text(location.get_text(" ", strip=True)))[:300]
    if site == "excel":
        promo = soup.select_one(".promo-bar.quick-possession")
        if promo is not None and not item.possession_date:
            item.possession_date = re.sub(r"^Quick Possession\s*-?\s*", "", clean_text(promo.get_text(" ", strip=True)), flags=re.I)[:160]
    item.detail_text = detail
    return item


def classify_listing_type(source_type, item):
    if source_type == "quick": return "quick_possession"
    if source_type == "presale": return "presale"
    if source_type == "model": return "model"
    text = f"{item.name} {item.raw_text} {item.detail_text} {item.possession_date} {item.address}".lower()
    if any(t in text for t in ("quick possession","quick-possession","move-in ready","move in ready","immediate possession","available now","spec home")) or (item.address and item.possession_date):
        return "quick_possession"
    return "model"


def classify_segment(item):
    # Classify from home-specific fields first. Page/card raw text can contain
    # neighbouring product types on JS-heavy builder sites, which caused whole
    # batches to collapse into one segment.
    primary = clean_text(f"{item.home_type} {item.garage} {item.name}").lower()

    def classify(text):
        if any(x in text for x in ("rear lane","rear-lane","rear garage","laned home","laned homes","lane home")) or re.search(r"\blaned\b", text): return "rear_lane"
        if any(x in text for x in ("front garage","front-garage","front attached","front-attached","front drive","front-drive","attached garage")): return "front_garage"
        if "duplex" in text: return "duplex"
        if any(x in text for x in ("semi-detached","semi detached","paired home")): return "semi_detached"
        if any(x in text for x in ("townhome","townhouse","rowhome","row home","street town","street towns")): return "townhome"
        if re.search(r"\b(?:bungalow|villa)s?\b", text): return "bungalow"
        if any(x in text for x in ("condo","apartment")): return "condo"
        if any(x in text for x in ("single family","single-family","detached")): return "detached"
        return None

    result = classify(primary)
    if result:
        return result

    # Fall back only to a compact candidate/detail record. Never classify from
    # a giant ancestor/page wrapper containing many different home styles.
    secondary = clean_text(f"{item.detail_text} {item.raw_text}")
    if len(secondary) <= 1800:
        return classify(secondary.lower()) or "unclassified"
    return "unclassified"


def crawl(source):
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    timeout = int(os.getenv("PLAYWRIGHT_TIMEOUT_MS", "45000"))
    settle = int(os.getenv("PLAYWRIGHT_SETTLE_MS", "2200"))
    max_pages = max(1, min(int(os.getenv("MAX_PAGES", "6")), 20))
    xhr = []
    network_debug = []
    pages = []
    items = []
    visited = set()

    with sync_playwright() as pw:
        is_jayman_source = host_name(source.get("url", "")).endswith("jayman.com")
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"] if is_jayman_source else []
        )
        context = browser.new_context(
            user_agent=JAYMAN_BROWSER_UA if is_jayman_source else HEADERS["User-Agent"],
            viewport={"width":1440,"height":1200},
            locale="en-CA",
            timezone_id="America/Edmonton" if is_jayman_source else None,
            extra_http_headers={"Accept-Language":"en-CA,en;q=0.9"} if is_jayman_source else None,
        )
        if is_jayman_source:
            context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'languages', {get: () => ['en-CA','en-US','en']});
                Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                window.chrome = window.chrome || {runtime: {}};
            """)
        page = context.new_page(); page.set_default_timeout(timeout)

        def capture(response):
            is_jayman = host_name(source.get("url", "")).endswith("jayman.com")
            try:
                ctype = (response.headers.get("content-type") or "").lower()
                if is_jayman and len(network_debug) < 120:
                    u=response.url
                    if same_site(source.get("url", ""), u) or any(k in u.lower() for k in ("api","graphql","search","inventory","home","property","azure","content")):
                        network_debug.append((response.status, ctype.split(';')[0], u))
                if not source.get("capture_xhr", True): return
                if "json" not in ctype and not any(x in response.url.lower() for x in ("api","graphql","search","inventory","homes","property")): return
                if response.status >= 400: return
                payload = response.json()
                if is_jayman:
                    xhr.extend(jayman_api_records(payload, response.url))
                else:
                    xhr.extend(api_records(payload, response.url))
            except Exception: pass
        page.on("response", capture)

        current = source["url"]
        source_host = host_name(source.get("url", ""))
        for _ in range(max_pages):
            current = urldefrag(current)[0]
            if current in visited: break
            visited.add(current)
            page.goto(current, wait_until="domcontentloaded", timeout=timeout)
            if host_name(source.get("url", "")).endswith("jayman.com"):
                jayman_prepare_page(page)
            try: page.wait_for_load_state("networkidle", timeout=min(timeout,10000))
            except PlaywrightTimeoutError: pass
            page.wait_for_timeout(settle)
            # Load-more and infinite scroll.
            for _ in range(6):
                clicked = False
                for role in ("button","link"):
                    try:
                        loc = page.get_by_role(role, name=LOAD_MORE_TEXT)
                        if loc.count() and loc.first.is_visible() and loc.first.is_enabled():
                            loc.first.click(timeout=4000); page.wait_for_timeout(900); clicked=True; break
                    except Exception: pass
                if not clicked: break
            for _ in range(3):
                try:
                    before = page.evaluate("document.body.scrollHeight")
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(700)
                    if page.evaluate("document.body.scrollHeight") <= before: break
                except Exception: break
            page.wait_for_timeout(900)

            # Daytona uses Livewire pagination with button[rel=next], so there is
            # no URL for the normal next-link discovery below to follow. Capture
            # every page from the component before leaving the initial URL.
            if source_host.endswith("daytonahomes.ca"):
                daytona_page = 1
                while True:
                    html = page.content()
                    pages.append((f"{current}#daytona-page={daytona_page}", html))
                    next_button = page.locator('button[rel="next"]')
                    try:
                        if not next_button.count() or not next_button.first.is_visible() or not next_button.first.is_enabled():
                            break
                        next_button.first.click(timeout=5000)
                        page.wait_for_timeout(max(settle, 1200))
                        daytona_page += 1
                        if daytona_page > 12:
                            break
                    except Exception:
                        break
                break

            html = page.content(); pages.append((current, html))
            soup = BeautifulSoup(html,"html.parser")
            nxt = ""
            for a in soup.select('a[rel="next"],a[href]'):
                label = clean_text(a.get_text(" ",strip=True)); aria=clean_text(a.get("aria-label") or ""); rel=" ".join(a.get("rel",[]))
                if "next" in rel.lower() or NEXT_TEXT.search(label) or "next" in aria.lower():
                    candidate = normalize_url(current,a.get("href"))
                    if candidate and candidate != current and same_site(source["url"],candidate): nxt=candidate; break
            if not nxt: break
            current=nxt

        dedupe=set()

        # These builders publish stable, complete listing markup. Parse their
        # server HTML directly so lazy loading and browser timing cannot truncate
        # the inventory. Daytona, Cedarglen, and Excel put the authoritative
        # product type on each detail page, which is fetched only for enrichment.
        source_host = host_name(source.get("url", ""))
        adapter_site = ""
        adapter_items = []
        detail_enrichment = False
        if source_host.endswith("tricohomes.com"):
            adapter_site = "trico"
        elif source_host.endswith("daytonahomes.ca"):
            adapter_site = "daytona"
            detail_enrichment = True
        elif source_host.endswith("cedarglenhomes.com"):
            adapter_site = "cedarglen"
            detail_enrichment = True
        elif source_host.endswith("excelhomes.ca"):
            adapter_site = "excel"
            detail_enrichment = True

        if adapter_site:
            adapter_timeout = max(5, min(int(os.getenv("BUILDER_DETAIL_TIMEOUT_SECONDS", "15")), 30))
            adapter_workers = max(2, min(int(os.getenv("BUILDER_DETAIL_WORKERS", "12")), 16))
            try:
                listing_response = requests.get(source["url"], headers=HEADERS, timeout=adapter_timeout, allow_redirects=True)
                if listing_response.status_code < 400:
                    listing_url = listing_response.url or source["url"]
                    if adapter_site == "trico":
                        adapter_items = trico_items_from_html(listing_response.text, listing_url)
                    elif adapter_site == "daytona":
                        adapter_items = daytona_items_from_html(listing_response.text, listing_url)
                    elif adapter_site == "cedarglen":
                        adapter_items = cedarglen_items_from_html(listing_response.text, listing_url)
                    elif adapter_site == "excel":
                        community_urls = excel_calgary_community_links(listing_response.text, listing_url)
                        print(f"EXCEL: discovered {len(community_urls)} Calgary-area community pages", flush=True)

                        def fetch_excel_community(url):
                            try:
                                response = requests.get(url, headers=HEADERS, timeout=adapter_timeout, allow_redirects=True)
                                if response.status_code < 400:
                                    return excel_items_from_community_html(response.text, response.url or url)
                            except Exception:
                                pass
                            return []

                        with ThreadPoolExecutor(max_workers=adapter_workers) as pool:
                            futures = [pool.submit(fetch_excel_community, url) for url in community_urls[:50]]
                            for future in as_completed(futures):
                                try:
                                    adapter_items.extend(future.result())
                                except Exception:
                                    pass
                    if adapter_site == "daytona":
                        # Merge all Livewire pages captured above with the server
                        # response. URL-based deduplication below removes page-1
                        # duplicates while preserving later pages.
                        for page_url, page_html in pages:
                            adapter_items.extend(daytona_items_from_html(page_html, page_url))
                else:
                    print(f"{adapter_site.upper()}: listing request returned HTTP {listing_response.status_code}; using Chromium discovery", flush=True)
            except Exception as exc:
                print(f"{adapter_site.upper()}: server listing discovery failed ({type(exc).__name__}); using Chromium discovery", flush=True)

            # Deduplicate before detail requests because community/navigation markup
            # can expose the same home more than once.
            unique_adapter_items = []
            seen_adapter = set()
            for item in adapter_items:
                key = item.url or f"{adapter_site}|{item.address}|{item.name}"
                if key in seen_adapter:
                    continue
                seen_adapter.add(key); unique_adapter_items.append(item)
            adapter_items = unique_adapter_items
            print(f"{adapter_site.upper()}: parsed {len(adapter_items)} complete listing cards", flush=True)

            if detail_enrichment and adapter_items:
                def fetch_builder_detail(item):
                    try:
                        response = requests.get(item.url, headers=HEADERS, timeout=adapter_timeout, allow_redirects=True)
                        if response.status_code < 400:
                            item.url = (response.url or item.url)[:1000]
                            return enrich_builder_detail(item, response.text, adapter_site)
                    except Exception:
                        pass
                    return item

                with ThreadPoolExecutor(max_workers=adapter_workers) as pool:
                    futures = [pool.submit(fetch_builder_detail, item) for item in adapter_items[:350]]
                    enriched = []
                    for future in as_completed(futures):
                        try:
                            enriched.append(future.result())
                        except Exception:
                            pass
                adapter_items = enriched
                classified = sum(classify_segment(item) != "unclassified" for item in adapter_items)
                print(f"{adapter_site.upper()}: detail enrichment classified {classified}/{len(adapter_items)} homes", flush=True)

            for item in adapter_items:
                strong = sum(bool(x) for x in (item.name, item.price or item.price_text, item.sqft, item.address, item.url))
                key = item.url or f"{adapter_site}|{item.address}|{item.name}"
                if strong < 4 or key in dedupe:
                    continue
                dedupe.add(key); items.append(item)

        # Jayman: community discovery is server-readable, but each community's
        # home grid is JavaScript-rendered. v2.6.3 incorrectly fetched those pages
        # with requests, which only returned the shell. Render each community inside
        # the established Calgary Playwright context and parse the resulting cards.
        if host_name(source.get("url", "")).endswith("jayman.com"):
            jay_headers={
                "User-Agent":JAYMAN_BROWSER_UA,
                "Accept-Language":"en-CA,en;q=0.9",
            }
            community_urls=[]
            try:
                idx=requests.get('https://www.jayman.com/calgary/find-your-home/',headers=jay_headers,timeout=15)
                community_urls=jayman_community_links(idx.text,idx.url) if idx.status_code<400 else []
            except Exception:
                pass

            # Fallback: the browser-rendered find-your-home page may expose links even
            # when the plain request does not.
            if not community_urls:
                try:
                    index_page=context.new_page(); index_page.set_default_timeout(timeout)
                    index_page.goto('https://www.jayman.com/calgary/find-your-home/',wait_until='domcontentloaded',timeout=timeout)
                    jayman_prepare_page(index_page)
                    index_page.wait_for_timeout(max(settle,1800))
                    community_urls=jayman_community_links(index_page.content(),index_page.url)
                    index_page.close()
                except Exception:
                    community_urls=[]

            # Normalize and force the Quick Possessions filter on each community page.
            filtered=[]; seen_c=set()
            for u in community_urls[:40]:
                base_u=u.split('?',1)[0]
                if base_u.rstrip('/') in seen_c: continue
                seen_c.add(base_u.rstrip('/'))
                sep='&' if '?' in u else '?'
                filtered.append(base_u.rstrip('/')+'/?homeTypes=Quick+Possessions&sortBy=WebPrice-ASC')
            community_urls=filtered
            print(f"JAYMAN: discovered {len(community_urls)} Calgary community pages; rendering with Chromium",flush=True)

            jayman_cards=[]
            comm_page=context.new_page(); comm_page.set_default_timeout(timeout)
            for idx_c,u in enumerate(community_urls,1):
                try:
                    comm_page.goto(u,wait_until='domcontentloaded',timeout=timeout)
                    jayman_prepare_page(comm_page)
                    try:
                        comm_page.wait_for_load_state('networkidle',timeout=min(timeout,7000))
                    except Exception:
                        pass
                    comm_page.wait_for_timeout(max(settle,1600))

                    # Trigger lazy home grids / Show More once or twice.
                    for _ in range(2):
                        try:
                            comm_page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                            comm_page.wait_for_timeout(650)
                        except Exception:
                            pass
                        clicked=False
                        for role in ('button','link'):
                            try:
                                loc=comm_page.get_by_role(role,name=LOAD_MORE_TEXT)
                                if loc.count() and loc.first.is_visible() and loc.first.is_enabled():
                                    loc.first.click(timeout=2500); comm_page.wait_for_timeout(800); clicked=True; break
                            except Exception:
                                pass
                        if not clicked:
                            break

                    html_c=comm_page.content()
                    found=jayman_quick_cards(html_c,comm_page.url)
                    jayman_cards.extend(found)
                    if not found and idx_c <= 2:
                        body=clean_text(comm_page.locator('body').inner_text(timeout=2500))[:900]
                        links=[normalize_url(comm_page.url,x.get('href')) for x in BeautifulSoup(html_c,'html.parser').select('a[href]')]
                        qp=sum('/quick-possession-homes/' in (urlparse(x).path or '').lower() for x in links if x)
                        print(f"JAYMAN COMMUNITY DEBUG {idx_c}: qp_links={qp}; body={body}",flush=True)
                except Exception as e:
                    if idx_c <= 3:
                        print(f"JAYMAN COMMUNITY ERROR {idx_c}: {type(e).__name__}: {e}",flush=True)
                if idx_c%5==0 or idx_c==len(community_urls):
                    print(f"JAYMAN: rendered {idx_c}/{len(community_urls)} communities; {len(jayman_cards)} raw quick-possession cards",flush=True)
            try: comm_page.close()
            except Exception: pass

            unique=[]; seen_j=set()
            for it in jayman_cards:
                if not it.url or it.url in seen_j: continue
                seen_j.add(it.url); unique.append(it)

            # QP cards often omit the style. Resolve each unique model once from
            # Jayman's model page, where the product type is explicit immediately
            # below the model name (e.g. Front attached garage).
            model_names=sorted({clean_text(it.name) for it in unique if clean_text(it.name) and not clean_text(it.home_type)})
            style_map={}
            if model_names:
                print(f"JAYMAN: resolving product types for {len(model_names)} unique models",flush=True)
                with ThreadPoolExecutor(max_workers=min(10,max(1,len(model_names)))) as style_pool:
                    style_futs={style_pool.submit(jayman_fetch_model_style,n,jay_headers):n for n in model_names}
                    done_styles=0
                    for sf in as_completed(style_futs):
                        n=style_futs[sf]
                        try:
                            style,style_url=sf.result()
                        except Exception:
                            style,style_url='',''
                        if style:
                            style_map[n.lower()]=style
                            print(f"JAYMAN STYLE: {n} -> {style}",flush=True)
                        done_styles+=1
                        if done_styles%10==0 or done_styles==len(style_futs):
                            print(f"JAYMAN: resolved {len(style_map)}/{done_styles} model product types",flush=True)
                for it in unique:
                    if not it.home_type:
                        it.home_type=style_map.get(clean_text(it.name).lower(),'')

            # Detail enrichment can remain HTTP because model/card data already came
            # from Chromium. If Jayman hides a detail field from requests, we retain
            # the strong card data rather than dropping the candidate.
            def enrich_jayman(it):
                try:
                    r=requests.get(it.url,headers=jay_headers,timeout=12,allow_redirects=True)
                    if r.status_code<400: return jayman_enrich_detail(it,r.text)
                except Exception: pass
                return it

            with ThreadPoolExecutor(max_workers=8) as pool:
                futs=[pool.submit(enrich_jayman,it) for it in unique[:350]]
                done=0
                for f in as_completed(futs):
                    done+=1
                    try: it=f.result()
                    except Exception: continue
                    strong=sum(bool(x) for x in (it.name,it.price or it.price_text,it.sqft,it.bedrooms,it.url))
                    if strong<4: continue
                    if it.url in dedupe: continue
                    dedupe.add(it.url); items.append(it)
                    if done%25==0 or done==len(futs):
                        print(f"JAYMAN: enriched {done}/{len(futs)} detail pages; {len(items)} candidates so far",flush=True)

        # Sterling: don't infer card wrappers. The listing page exposes canonical
        # quick-possession detail URLs; fetch those pages and parse their explicit labels.
        if host_name(source.get("url", "")).endswith("sterlingcalgary.com"):
            detail_urls=[]; seen_detail=set()

            # Sterling's server HTML contains the complete quick-possession inventory,
            # while Chromium can expose only the initially rendered/lazy-loaded chunk.
            # Discover from the raw listing response first, then merge any links the
            # browser found. This prevents partial crawls such as 23/100+ homes.
            try:
                raw_listing = requests.get(
                    source.get("url", "https://sterlingcalgary.com/find-your-home/"),
                    headers=HEADERS, timeout=20, allow_redirects=True
                )
                if raw_listing.status_code < 400:
                    raw_links = sterling_detail_links(
                        BeautifulSoup(raw_listing.text, "html.parser"),
                        raw_listing.url or source.get("url", "")
                    )
                    for u in raw_links:
                        if u not in seen_detail:
                            seen_detail.add(u); detail_urls.append(u)

                    # The listing page itself contains the complete inventory cards,
                    # including price, sqft, address, beds/baths and possession. Parse
                    # those cards first instead of requiring every detail page to pass
                    # a strict template. Detail pages are enrichment only.
                    raw_soup = BeautifulSoup(raw_listing.text, "html.parser")
                    server_cards = sterling_card_nodes(raw_soup)
                    server_card_items = 0
                    for node in server_cards:
                        try:
                            it = sterling_parse_dom(node, raw_listing.url or source.get("url", ""))
                        except Exception:
                            continue
                        strong = sum(bool(x) for x in (it.name, it.price or it.price_text, it.sqft, it.address, it.url))
                        if strong < 4:
                            continue
                        key = it.url or f"sterling|{it.address}|{it.name}"
                        if key in dedupe:
                            continue
                        dedupe.add(key); items.append(it); server_card_items += 1
                    print(f"STERLING: server HTML exposed {len(raw_links)} detail links and parsed {server_card_items} listing cards", flush=True)
                else:
                    print(f"STERLING: server listing request returned HTTP {raw_listing.status_code}; using Chromium discovery", flush=True)
            except Exception as e:
                print(f"STERLING: server listing discovery failed ({type(e).__name__}); using Chromium discovery", flush=True)

            browser_link_count = 0
            for page_url, html in pages:
                for u in sterling_detail_links(BeautifulSoup(html,"html.parser"), page_url):
                    browser_link_count += 1
                    if u not in seen_detail:
                        seen_detail.add(u); detail_urls.append(u)
            print(f"STERLING: Chromium exposed {browser_link_count} detail links; {len(detail_urls)} unique after merge", flush=True)

            # Fetch detail pages concurrently. The previous sequential loop could
            # take 10+ minutes for 90-100 homes. Sterling detail pages are public
            # HTML, so they do not need to share the Playwright page/context.
            sterling_workers = max(2, min(int(os.getenv("STERLING_DETAIL_WORKERS", "12")), 16))
            sterling_timeout = max(5, min(int(os.getenv("STERLING_DETAIL_TIMEOUT_SECONDS", "12")), 25))

            def fetch_sterling_detail(u):
                try:
                    resp = requests.get(u, headers=HEADERS, timeout=sterling_timeout, allow_redirects=True)
                    if resp.status_code >= 400:
                        return None
                    item = sterling_parse_detail_html(resp.text, resp.url or u)
                    # A detail page is useful even when only Style or one other field
                    # parsed successfully, because the canonical URL lets us merge it
                    # into the already-discovered listing card. Do not throw that
                    # evidence away merely because the standalone detail record is
                    # incomplete.
                    meaningful = sum(bool(x) for x in (item.name, item.price or item.price_text, item.sqft, item.address, item.home_type))
                    return item if meaningful >= 1 else None
                except Exception:
                    return None

            urls = detail_urls[:350]
            print(f"STERLING: discovered {len(urls)} detail pages; fetching with {sterling_workers} workers")
            # Build a canonical URL index once. Raw URL equality was too strict:
            # `/quick_possession/foo` and `/quick_possession/foo/` were treated as
            # different homes, so detail-page Style evidence never reached the card.
            sterling_index = {}
            for existing in items:
                if existing.url:
                    sterling_index[sterling_identity_url(existing.url)] = existing

            detail_styles = 0
            detail_style_merges = 0
            with ThreadPoolExecutor(max_workers=sterling_workers) as pool:
                futures = [pool.submit(fetch_sterling_detail, u) for u in urls]
                done = 0
                for future in as_completed(futures):
                    done += 1
                    item = future.result()
                    if item is not None:
                        if item.home_type:
                            detail_styles += 1
                        ident = sterling_identity_url(item.url) if item.url else ''
                        existing = sterling_index.get(ident) if ident else None
                        if existing is not None:
                            had_type = bool(existing.home_type)
                            for attr in ('name','price','price_text','sqft','bedrooms','bathrooms','community','address','home_type','garage','possession_date'):
                                if not getattr(existing,attr,None) and getattr(item,attr,None):
                                    setattr(existing,attr,getattr(item,attr))
                            if item.detail_text:
                                existing.detail_text=item.detail_text
                            if item.home_type and not had_type and existing.home_type:
                                detail_style_merges += 1
                        else:
                            # Only create a brand-new candidate from a detail page when
                            # it is independently strong. Partial detail parses are for
                            # enrichment, not new inventory creation.
                            strong = sum(bool(x) for x in (item.name, item.price or item.price_text, item.sqft, item.address, item.home_type))
                            if strong >= 4:
                                key=item.url or f"sterling|{item.address}|{item.name}"
                                if key not in dedupe:
                                    dedupe.add(key); items.append(item)
                                    if ident:
                                        sterling_index[ident]=item
                    if done % 20 == 0 or done == len(futures):
                        print(f"STERLING: fetched {done}/{len(futures)} detail pages; {len(items)} candidates so far")
            print(f"STERLING: detail pages exposed product style for {detail_styles}/{len(urls)} homes; merged style into {detail_style_merges} listing cards", flush=True)

        for page_url,html in pages:
            soup=BeautifulSoup(html,"html.parser")
            site = host_name(page_url)
            primary = discover_nodes(soup)[:500]
            fallback = discover_view_home_nodes(soup)[:500]
            sterling = [] if site.endswith("sterlingcalgary.com") else []
            # Site adapters are additive; generic discovery still catches future markup changes.
            for node in sterling + primary + fallback:
                item = sterling_parse_dom(node,page_url) if node in sterling else parse_dom(node,page_url)
                valid,score,signals=evidence(node,item)
                if not valid: continue
                key=item.url if item.url != page_url else f"{item.name}|{item.address}|{item.sqft}|{item.price_text}"
                if key in dedupe: continue
                dedupe.add(key)
                item.confidence=min(.98,max(item.confidence,score/14))
                mode = "Sterling adapter" if node in sterling else ("semantic View Home" if node in fallback else "DOM")
                item.evidence=mode+" signals: "+", ".join(k for k,v in signals.items() if v)
                items.append(item)
        for obj,resp in xhr:
            item = jayman_api_item(obj,resp,source["url"]) if host_name(source.get("url","")).endswith("jayman.com") else api_item(obj,resp,source["url"])
            key=item.url or f"xhr|{item.name}|{item.address}|{item.sqft}|{item.price_text}"
            strong=sum(bool(x) for x in (item.name,item.price or item.price_text,item.sqft,item.bedrooms,item.address,item.home_type,item.url))
            if strong < 3 or key in dedupe: continue
            dedupe.add(key); items.append(item)

        if host_name(source.get("url", "")).endswith("jayman.com") and not items:
            print("JAYMAN DEBUG: no candidates; captured network responses:", flush=True)
            for status, ctype, url in network_debug[:80]:
                print(f"JAYMAN NET {status} {ctype or '-'} {url}", flush=True)
            try:
                body_text=clean_text(page.locator('body').inner_text(timeout=3000))[:1200]
                print("JAYMAN BODY:", body_text, flush=True)
            except Exception:
                pass
        browser.close()

    # HomeWatch v3 AI pilot: deterministic Chromium parsing remains authoritative.
    # AI enriches evidence-backed gaps/classification, and becomes a fallback only
    # when deterministic extraction found no candidates.
    ai_stats = ai_new_stats()
    ai_mode = ai_stats.get("mode", "hybrid")
    if ai_stats.get("enabled") and ai_mode not in ("off", "disabled", "deterministic"):
        if items:
            print(f"AI: enriching {len(items)} deterministic candidates with {ai_stats['model']}", flush=True)
            ai_enrich_items(source, items, ai_stats)

            # Classification pass: every still-unclassified candidate gets an
            # explicit second look using its detail-page evidence first. This is
            # especially important for Sterling, where the inventory card may omit
            # the product style while the detail page contains an explicit Style:.
            unclassified = [
                i for i, item in enumerate(items)
                if classify_segment(item) == "unclassified"
                and not getattr(item, "_ai_product_segment", "")
            ]
            if unclassified:
                print(f"AI: targeted product classification for {len(unclassified)} unclassified candidates", flush=True)
                ai_enrich_items(source, items, ai_stats, only_indexes=unclassified)

            # Escalate every candidate that remains unclassified after the targeted
            # Luna pass. A high-confidence "unclassified" response should not
            # prevent the fallback model from reviewing richer detail evidence.
            if env_bool("HOMEWATCH_AI_ESCALATE", True) and ai_stats.get("fallback_model"):
                threshold = max(0.0, min(float(os.getenv("HOMEWATCH_AI_CONFIDENCE_THRESHOLD", "0.80")), 1.0))
                low = []
                for i, item in enumerate(items):
                    deterministic = classify_segment(item)
                    ai_seg = getattr(item, "_ai_product_segment", "")
                    if deterministic == "unclassified" and not ai_seg:
                        low.append(i)
                if low:
                    print(f"AI: escalating {len(low)} uncertain candidates to {ai_stats['fallback_model']}", flush=True)
                    ai_enrich_items(source, items, ai_stats, model=ai_stats["fallback_model"], only_indexes=low, escalation=True)
        else:
            print(f"AI: deterministic extraction returned 0 candidates; trying rendered-page fallback with {ai_stats['model']}", flush=True)
            items = ai_extract_from_pages(source, pages, ai_stats)
            if not items and env_bool("HOMEWATCH_AI_ESCALATE", True) and ai_stats.get("fallback_model"):
                print(f"AI: page fallback returned 0; escalating once to {ai_stats['fallback_model']}", flush=True)
                ai_stats["escalations"] += 1
                items = ai_extract_from_pages(source, pages, ai_stats, model=ai_stats["fallback_model"])

        print(
            f"AI: calls={ai_stats['calls']} input={ai_stats['input_tokens']} output={ai_stats['output_tokens']} "
            f"enriched={ai_stats['enriched_candidates']} extracted={ai_stats['extracted_candidates']} "
            f"est_cost=${ai_stats['estimated_cost_usd']:.4f} errors={ai_stats['errors']}",
            flush=True,
        )
    elif env_bool("HOMEWATCH_AI_ENABLED", True) and not os.getenv("OPENAI_API_KEY", "").strip():
        print("AI: enabled but OPENAI_API_KEY is not configured; using deterministic crawler only", flush=True)

    result=[]
    for item in items[:1000]:
        d=asdict(item)
        d["listing_type"] = getattr(item, "_ai_listing_type", "") or classify_listing_type(source.get("source_type","both"), item)
        if d["listing_type"] == "presale":
            price_from, price_to = price_band(item.price_text)
            d["price_from"] = price_from or item.price_from or item.price
            d["price_to"] = price_to or item.price_to
            d["price"] = None
        d["product_segment"] = getattr(item, "_ai_product_segment", "") or classify_segment(item)
        d["external_key"] = hashlib.sha1(f"{item.url}|{item.name}|{item.address}".lower().encode()).hexdigest()
        result.append(d)
    unclassified_count = sum(d["product_segment"] == "unclassified" for d in result)
    print(f"CLASSIFICATION: {len(result) - unclassified_count}/{len(result)} classified; {unclassified_count} unclassified", flush=True)
    site_adapters = {
        "jayman.com": "jayman-chromium-community-detail",
        "sterlingcalgary.com": "sterling-detail",
        "tricohomes.com": "trico-server-cards",
        "daytonahomes.ca": "daytona-server-cards-detail",
        "excelhomes.ca": "excel-community-cards-detail",
        "cedarglenhomes.com": "cedarglen-server-cards-detail",
    }
    adapter_name = next((value for domain, value in site_adapters.items() if host_name(source.get("url", "")).endswith(domain)), "generic")
    meta = {"pages_scanned":len(pages),"candidates":len(result),"xhr_records":len(xhr),"fetch_method":"playwright+xhr" if xhr else "playwright","site_adapter":adapter_name, "network_debug_count":len(network_debug)}
    meta.update({
        "ai_enabled": bool(ai_stats.get("enabled")),
        "ai_mode": ai_stats.get("mode"),
        "ai_model": ai_stats.get("model"),
        "ai_fallback_model": ai_stats.get("fallback_model"),
        "ai_calls": ai_stats.get("calls", 0),
        "ai_input_tokens": ai_stats.get("input_tokens", 0),
        "ai_output_tokens": ai_stats.get("output_tokens", 0),
        "ai_estimated_cost_usd": round(float(ai_stats.get("estimated_cost_usd", 0.0)), 6),
        "ai_enriched_candidates": ai_stats.get("enriched_candidates", 0),
        "ai_extracted_candidates": ai_stats.get("extracted_candidates", 0),
        "ai_escalations": ai_stats.get("escalations", 0),
        "ai_errors": ai_stats.get("errors", 0),
    })
    return result, meta
