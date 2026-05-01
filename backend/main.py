import os
import json
import base64
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
ALLERGY_FILE = ROOT / "data" / "allergy.json"
RECIPE_FILE = ROOT / "data" / "recipe.json"
SYNONYMS_FILE = ROOT / "data" / "synonyms.json"
CONTAMINATION_FILE = ROOT / "data" / "contamination_rules.json"
FRONTEND_DIR = ROOT / "frontend"

load_dotenv(ROOT / ".env")

TYPHOON_API_KEY = os.getenv("TYPHOON_API_KEY", "")
TYPHOON_CHAT_URL = "https://api.opentyphoon.ai/v1/chat/completions"
TYPHOON_OCR_MODEL = os.getenv("TYPHOON_OCR_MODEL", "typhoon-ocr")
TYPHOON_CHAT_MODEL = os.getenv("TYPHOON_CHAT_MODEL", "typhoon-v2.1-12b-instruct")

with open(ALLERGY_FILE, "r", encoding="utf-8") as f:
    ALLERGENS = json.load(f)["allergens"]

with open(RECIPE_FILE, "r", encoding="utf-8") as f:
    DISHES = json.load(f)["dishes"]

try:
    with open(SYNONYMS_FILE, "r", encoding="utf-8") as f:
        SYNONYMS_RAW = json.load(f).get("synonyms", {})
except FileNotFoundError:
    SYNONYMS_RAW = {}

try:
    with open(CONTAMINATION_FILE, "r", encoding="utf-8") as f:
        CONTAMINATION_RULES = json.load(f).get("rules", [])
except FileNotFoundError:
    CONTAMINATION_RULES = []

FOODS = {"allergens": ALLERGENS, "dishes": DISHES}


def _build_synonym_index() -> dict[str, set[str]]:
    """Build term → set(group) lookup. Merges synonyms.json with allergy.json keywords.

    A user-typed allergen ("shrimp") will match any ingredient whose lowercased
    text contains any term from the same synonym group ("กุ้ง", "shrimp", "虾"...).
    """
    index: dict[str, set[str]] = {}

    def add_group(terms: list[str]):
        canonical = {str(t).strip().lower() for t in terms if str(t).strip()}
        for t in canonical:
            index.setdefault(t, set()).update(canonical)

    # 1. Synonyms file
    for _, terms in SYNONYMS_RAW.items():
        if isinstance(terms, list):
            add_group(terms)

    # 2. Built-in allergens — th/en names + keywords field
    for key, info in ALLERGENS.items():
        group = [info.get("th", ""), info.get("en", "")] + info.get("keywords", [])
        add_group([t for t in group if t])

    return index


SYNONYM_INDEX: dict[str, set[str]] = _build_synonym_index()
# In-process cache for LLM-expanded synonyms (key: lowercased term)
LLM_SYNONYM_CACHE: dict[str, list[str]] = {}


app = FastAPI(title="Thai Food Allergy Detector")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AnalyzeRequest(BaseModel):
    text: str
    allergies: list[str] = []


def normalize(s: str) -> str:
    return re.sub(r"\s+", "", s.lower().strip())


def find_dish_local(text: str) -> Optional[dict]:
    """Match a single dish name against the local food database (best score wins)."""
    if not text:
        return None
    norm_text = normalize(text)
    best = None
    best_score = 0

    for dish in FOODS["dishes"]:
        candidates = [dish["name_th"], dish["name_en"]] + dish.get("aliases", [])
        for cand in candidates:
            nc = normalize(cand)
            if not nc:
                continue
            if nc in norm_text or norm_text in nc:
                score = len(nc)
                if score > best_score:
                    best_score = score
                    best = dish
    return best


# Common Thai food stems (base dish words). When two dish names share a long
# stem like "กะเพรา" or "ส้มตำ", they're variants of the same dish — only the
# protein differs. Used by find_dish_fuzzy to recognize "กะเพราหมู" ≈ "กะเพราไก่".
_PROTEIN_WORDS = (
    "หมู", "ไก่", "เนื้อ", "กุ้ง", "ปลา", "ปู", "หอย", "ปลาหมึก",
    "เป็ด", "ทะเล", "กระดูกอ่อน", "ลูกชิ้น", "เห็ด", "เต้าหู้", "ไข่",
)


def _strip_protein_suffix(s: str) -> str:
    """Remove a trailing protein word so 'กะเพราหมู' → 'กะเพรา'."""
    for p in _PROTEIN_WORDS:
        if s.endswith(p) and len(s) > len(p) + 1:
            return s[: -len(p)]
    return s


def _matches_contamination_rule(name: str, rule: dict) -> bool:
    """Check if a dish name matches a contamination rule's pattern under its
    position constraints (start / after_prefix / after_protein / end / anywhere),
    while respecting the exclude_if_contains blacklist.

    Thai dish names have no spaces, so we use position-based matching instead of
    word boundaries (\\b doesn't work reliably). Patterns at the START of a name
    or right after known prefixes / proteins are reliable cooking-method markers.
    """
    norm = normalize(name)
    if not norm:
        return False

    # Blacklist check first — defends against false positives like
    # "ขนมปังปิ้ง" (toast) matching the grill rule via "ปิ้ง"
    for ex in rule.get("exclude_if_contains", []):
        if normalize(ex) in norm:
            return False

    patterns = rule.get("patterns", [])
    match_at = set(rule.get("match_at", ["start"]))
    prefixes = [normalize(p) for p in rule.get("prefixes", [])]
    proteins = [normalize(p) for p in _PROTEIN_WORDS]

    for pat in patterns:
        p = normalize(pat)
        if not p:
            continue

        if "start" in match_at and norm.startswith(p):
            return True
        if "after_prefix" in match_at:
            for prefix in prefixes:
                if prefix and norm.startswith(prefix + p):
                    return True
        if "after_protein" in match_at:
            for prot in proteins:
                if prot and (prot + p) in norm:
                    return True
        if "end" in match_at and norm.endswith(p):
            return True
        if "anywhere" in match_at and p in norm:
            return True
    return False


def infer_contamination_risk(
    dish_name: str,
    user_allergies: list[str] | None = None,
) -> list[dict]:
    """For a dish name, return list of contamination warnings the user cares about.

    Each entry: {rule_id, allergens: [keys], reason: {th/en/zh}}.

    When user_allergies is provided, filters each rule's may_contain to only the
    user's selected allergen keys — so if user isn't allergic to fish, the
    'wok shares oil with fried fish' warning is suppressed.
    """
    if not dish_name:
        return []
    user_set = set(user_allergies or [])
    warnings: list[dict] = []
    for rule in CONTAMINATION_RULES:
        if not _matches_contamination_rule(dish_name, rule):
            continue
        rule_allergens = rule.get("may_contain", [])
        if user_set:
            relevant = [a for a in rule_allergens if a in user_set]
            if not relevant:
                continue
        else:
            relevant = list(rule_allergens)
        warnings.append({
            "rule_id": rule.get("id", ""),
            "allergens": relevant,
            "reason": rule.get("reason", {}),
        })
    return warnings


def find_dish_fuzzy(name: str, threshold: float = 0.65) -> Optional[tuple[dict, float]]:
    """Fuzzy match a name against DB. Handles three OCR/menu cases:

    1. OCR truncation:    "ส้มตำปูปลา"  → "ส้มตำปูปลาร้า"
    2. Vowel confusion:   "ต้มจี๊ด"     → "ต้มจืด"
    3. Protein variant:   "ส้มตำหมู"    → "ส้มตำปู"      (same base, diff protein)
                          "กะเพราไก่"   → "ผัดกะเพราหมู"  (same base, diff protein)

    Score is max of:
      - SequenceMatcher.ratio()        (overall similarity)
      - Longest common substring frac  (catches shared stems)
      - Stripped-protein ratio          (catches variants)
      - Prefix bonus 0.88               (one is prefix of the other)
    """
    if not name:
        return None
    target = normalize(name)
    if len(target) < 3:
        return None
    target_stem = _strip_protein_suffix(target)

    best: Optional[tuple[dict, float]] = None
    for dish in FOODS["dishes"]:
        candidates = [dish["name_th"], dish["name_en"]] + dish.get("aliases", [])
        for cand in candidates:
            nc = normalize(cand)
            if not nc or len(nc) < 3:
                continue

            sm = SequenceMatcher(None, target, nc)
            ratio = sm.ratio()

            # Longest common substring as fraction of the shorter string —
            # catches "กะเพราหมู" vs "ผัดกะเพราไก่" sharing "กะเพรา".
            # Only boost when the LCS is at the START of at least one string,
            # since Thai dish names put the base ("กะเพรา", "ส้มตำ", "ลาบ") first
            # and the protein last. End-shared substrings like "ปิ้ง" in
            # "หมูปิ้ง" vs "ขนมปังปิ้ง" are cooking methods, not the same dish.
            match = sm.find_longest_match(0, len(target), 0, len(nc))
            shorter = min(len(target), len(nc))
            lcs_frac = match.size / shorter if shorter else 0.0
            lcs_at_start = match.a == 0 or match.b == 0
            if match.size >= 4 and lcs_frac >= 0.55 and lcs_at_start:
                ratio = max(ratio, 0.78)

            # Same stem after stripping protein suffix → variant of same dish
            nc_stem = _strip_protein_suffix(nc)
            if (
                target_stem
                and nc_stem
                and len(target_stem) >= 3
                and len(nc_stem) >= 3
                and (target_stem == nc_stem or target_stem in nc_stem or nc_stem in target_stem)
            ):
                ratio = max(ratio, 0.82)

            # Prefix bonus
            if target.startswith(nc) or nc.startswith(target):
                ratio = max(ratio, 0.88)

            if ratio >= threshold and (best is None or ratio > best[1]):
                best = (dish, ratio)
    return best


def find_all_local_matches(text: str) -> list[dict]:
    """Find ALL DB dishes that appear in the text. Uses longest-first matching
    with masking so longer dish names take priority over shorter aliases."""
    if not text:
        return []
    text = _strip_ocr_wrapper(text)
    norm_text = normalize(text)

    pairs: list[tuple[dict, str]] = []
    for dish in FOODS["dishes"]:
        names = [dish["name_th"], dish["name_en"]] + dish.get("aliases", [])
        for n in names:
            nn = normalize(n)
            if nn and len(nn) >= 3:
                pairs.append((dish, nn))
    pairs.sort(key=lambda p: -len(p[1]))

    found: dict[str, dict] = {}
    masked = list(norm_text)
    sentinel = "\0"

    for dish, alias_norm in pairs:
        if dish["name_th"] in found:
            continue
        haystack = "".join(masked)
        idx = haystack.find(alias_norm)
        if idx >= 0:
            found[dish["name_th"]] = dish
            for i in range(idx, idx + len(alias_norm)):
                masked[i] = sentinel

    return list(found.values())


async def typhoon_ocr(image_bytes: bytes, mime: str = "image/jpeg") -> str:
    """Call typhoon-ocr via chat completions vision API."""
    if not TYPHOON_API_KEY:
        raise HTTPException(
            status_code=400,
            detail="ยังไม่ได้ตั้งค่า TYPHOON_API_KEY ใน .env — ใส่ API key หรือใช้พิมพ์ชื่อเมนูแทนได้",
        )

    b64 = base64.b64encode(image_bytes).decode("utf-8")
    headers = {
        "Authorization": f"Bearer {TYPHOON_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": TYPHOON_OCR_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "อ่านข้อความเมนูอาหารทั้งหมดจากรูปนี้อย่างละเอียด "
                            "ลิสต์ชื่อเมนูทุกรายการที่เห็น คั่นด้วยขึ้นบรรทัดใหม่ "
                            "ถ้ามีหลายคอลัมน์ให้อ่านครบทุกคอลัมน์ "
                            "ถ้ามีลำดับที่ (เช่น 1, 2, 3...) ให้คงไว้ "
                            "ห้ามอธิบายเพิ่ม ห้ามสรุป"
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    },
                ],
            }
        ],
        "max_tokens": 8192,
        "temperature": 0.0,
    }

    async with httpx.AsyncClient(timeout=120) as client:
        try:
            r = await client.post(TYPHOON_CHAT_URL, headers=headers, json=payload)
            if r.status_code == 401:
                raise HTTPException(
                    status_code=401,
                    detail="Typhoon API key ไม่ถูกต้อง (401) — ตรวจสอบ TYPHOON_API_KEY ใน .env",
                )
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]["content"]
        except HTTPException:
            raise
        except httpx.HTTPStatusError as e:
            raise HTTPException(
                status_code=502,
                detail=f"Typhoon OCR error: {e.response.status_code} {e.response.text[:200]}",
            )


async def typhoon_lookup_ingredients(dish_name: str, use_web: bool = True) -> dict:
    """Ask Typhoon LLM to identify ingredients & allergens for an unknown dish.

    If `use_web` is True, fetch web snippets via DuckDuckGo and feed as context (RAG).
    """
    if not TYPHOON_API_KEY:
        return {"ingredients": [], "allergens": [], "confidence": "low"}

    web_context = ""
    web_used = False
    if use_web:
        web_context = await web_search_ingredients(dish_name)
        web_used = bool(web_context)

    allergen_list = ", ".join(
        [f"{k} ({v['th']})" for k, v in FOODS["allergens"].items()]
    )

    system = (
        "คุณเป็นผู้เชี่ยวชาญด้านอาหาร หน้าที่คือบอกวัตถุดิบหลักของเมนูอาหารอย่างย่อ "
        "และระบุสารก่อภูมิแพ้ที่อาจมี ตอบเป็น JSON เท่านั้น ห้ามมีข้อความอื่น"
    )
    web_block = f"\nข้อมูลจากเว็บ:\n{web_context}\n" if web_context else ""
    user = (
        f"เมนู: {dish_name}\n"
        f"รายการสารก่อภูมิแพ้ที่ต้องเลือกจาก (ใช้ key ภาษาอังกฤษ): {allergen_list}"
        f"{web_block}\n"
        "ตอบในรูปแบบ JSON นี้เท่านั้น:\n"
        '{"ingredients": ["วัตถุดิบ1", "วัตถุดิบ2", ...], '
        '"allergens": ["key1", "key2", ...], '
        '"confidence": "high|medium|low"}'
    )

    payload = {
        "model": TYPHOON_CHAT_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": 512,
        "temperature": 0.2,
    }
    headers = {
        "Authorization": f"Bearer {TYPHOON_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(TYPHOON_CHAT_URL, headers=headers, json=payload)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"Typhoon LLM lookup failed: {e}")
        return {
            "ingredients": [],
            "allergens": [],
            "confidence": "low",
            "web_search_used": web_used,
            "error": str(e)[:200],
        }

    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        return {
            "ingredients": [],
            "allergens": [],
            "confidence": "low",
            "web_search_used": web_used,
            "raw": content,
        }
    try:
        result = json.loads(m.group(0))
        known = set(FOODS["allergens"].keys())
        result["allergens"] = [a for a in result.get("allergens", []) if a in known]
        result["web_search_used"] = web_used
        return result
    except json.JSONDecodeError:
        return {
            "ingredients": [],
            "allergens": [],
            "confidence": "low",
            "web_search_used": web_used,
            "raw": content,
        }


def _strip_ocr_wrapper(text: str) -> str:
    """typhoon-ocr sometimes returns {"natural_text": "..."} — unwrap it."""
    s = text.strip()
    if s.startswith("{") and "natural_text" in s:
        try:
            obj = json.loads(s)
            if isinstance(obj, dict) and "natural_text" in obj:
                return str(obj["natural_text"])
        except json.JSONDecodeError:
            m = re.search(r'"natural_text"\s*:\s*"((?:[^"\\]|\\.)*)"', s, re.DOTALL)
            if m:
                return m.group(1).encode().decode("unicode_escape")
    return text


def heuristic_extract_dishes(ocr_text: str) -> list[str]:
    """Extract dish names from OCR text using regex — works without LLM.

    Handles:
      - Markdown headers (### name, ## name)
      - Markdown table rows (| name | price |)
      - Numbered lists (1 ชื่อเมนู, 1. ชื่อเมนู, ① ชื่อเมนู)
      - Price-tagged lines (ลาบหมู 50 บาท)
      - Bullet lists (- ชื่อเมนู, • ชื่อเมนู)
    """
    text = _strip_ocr_wrapper(ocr_text)
    candidates: list[str] = []

    PRICE_RE = re.compile(r"\d+(\.\d+)?\s*(บาท|baht|thb|\.\-|\.)\s*$", re.I)
    JUNK_RE = re.compile(
        r"^(หมายเหตุ|เปิดบริการ|ร้าน|menu|sale\s*here|แจกลิสต์|"
        r"\d+\s*เมนู|เก็บไว้|tel|โทร|line\b|fb\b|ig\b|http)",
        re.I,
    )
    SECTION_HEADER_RE = re.compile(
        r"^(ข้าวจานเดียว|เมนูเส้น|เมนูส้มตำ|เมนูลาบ|เมนูต้ม|เมนูแกง|เมนูยำ|"
        r"ของทอด|ของหวาน|เครื่องดื่ม|ต้ม\s*แกง|และอื่นๆ)\s*$"
    )

    def add(cand: str):
        cand = cand.strip(" \t.-•*#|:")
        cand = PRICE_RE.sub("", cand).strip()
        if not cand or len(cand) < 2:
            return
        if JUNK_RE.search(cand) or SECTION_HEADER_RE.search(cand):
            return
        # Reject if it's just digits or punctuation
        if not re.search(r"[ก-๛a-zA-Z]", cand):
            return
        candidates.append(cand)

    # Markdown headers
    for m in re.finditer(r"^\s*#{2,4}\s+(.+?)\s*$", text, re.M):
        add(m.group(1))

    # Markdown table rows: extract first cell only
    for m in re.finditer(r"^\s*\|\s*([^|]+?)\s*\|", text, re.M):
        cell = m.group(1).strip()
        if cell in ("---", "") or set(cell) <= {"-", " "}:
            continue
        add(cell)

    # Numbered list: "1 ชื่อ", "1. ชื่อ", "1) ชื่อ", "①ชื่อ"
    NUMBERED_RE = re.compile(
        r"^\s*(?:\d{1,3}[\.\)\s]|[①-⓿❶-➓])\s*(.+?)\s*$",
        re.M,
    )
    for m in NUMBERED_RE.finditer(text):
        add(m.group(1))

    # "name <space> price <space> บาท"
    for line in text.splitlines():
        line = line.strip(" \t-•*#|")
        if not line or len(line) < 3:
            continue
        m = re.match(r"^(.+?)\s+\d+(\.\d+)?\s*(บาท|baht|thb)\b", line, re.I)
        if m:
            add(m.group(1))

    # Bullet lists: "- ชื่อ", "• ชื่อ"
    for m in re.finditer(r"^\s*[-•*]\s+(.+?)\s*$", text, re.M):
        add(m.group(1))

    return candidates


async def extract_dish_names(ocr_text: str) -> list[str]:
    """Extract clean dish names from messy OCR text.

    Strategy: ALWAYS run regex heuristics + try LLM, then merge & dedupe.
    Heuristics catch markdown headers / table rows reliably; LLM catches free-form text.
    """
    if not ocr_text.strip():
        return []

    cleaned = _strip_ocr_wrapper(ocr_text)
    heuristic_names = heuristic_extract_dishes(ocr_text)

    llm_names: list[str] = []
    if TYPHOON_API_KEY:
        system = (
            "คุณเป็นผู้ช่วยแยกชื่อเมนูอาหารจากข้อความเมนูร้านอาหาร "
            "ตอบเป็น JSON array ของชื่อเมนูเท่านั้น ห้ามมีข้อความอื่น "
            "ห้ามใส่ราคา หมายเลขโทรศัพท์ ชื่อร้าน หรือเวลาทำการ"
        )
        user = (
            f"ข้อความจาก OCR เมนูร้านอาหาร:\n```\n{cleaned[:4000]}\n```\n\n"
            "ดึงชื่อเมนูอาหารทั้งหมด ตอบในรูปแบบ JSON array นี้เท่านั้น:\n"
            '["เมนู1", "เมนู2", ...]'
        )

        payload = {
            "model": TYPHOON_CHAT_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": 1024,
            "temperature": 0.0,
        }
        headers = {
            "Authorization": f"Bearer {TYPHOON_API_KEY}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(TYPHOON_CHAT_URL, headers=headers, json=payload)
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"]
            m = re.search(r"\[.*\]", content, re.DOTALL)
            if m:
                parsed = json.loads(m.group(0))
                llm_names = [
                    str(n).strip()
                    for n in parsed
                    if isinstance(n, (str, int, float)) and str(n).strip()
                ]
        except Exception as e:
            print(f"extract_dish_names LLM failed: {e}")

    # Merge heuristic + LLM, dedupe by normalized form
    seen: set[str] = set()
    merged: list[str] = []
    for name in llm_names + heuristic_names:
        key = normalize(name)
        if key and key not in seen:
            seen.add(key)
            merged.append(name)
    return merged


async def analyze_dish_name(name: str) -> dict:
    """Analyze a single dish name: exact DB → fuzzy DB → Typhoon LLM."""
    dish = find_dish_local(name)
    if dish:
        return {
            "source": "local_db",
            "dish_name_th": dish["name_th"],
            "dish_name_en": dish["name_en"],
            "query": name,
            "ingredients": dish["ingredients"],
            "allergens_detected": dish["allergens"],
            "confidence": "high",
        }
    fuzzy = find_dish_fuzzy(name)
    if fuzzy:
        d, ratio = fuzzy
        return {
            "source": "local_db_fuzzy",
            "dish_name_th": d["name_th"],
            "dish_name_en": d["name_en"],
            "query": name,
            "match_ratio": round(ratio, 2),
            "ingredients": d["ingredients"],
            "allergens_detected": d["allergens"],
            "confidence": "medium" if ratio < 0.85 else "high",
        }
    llm_result = await typhoon_lookup_ingredients(name)
    return {
        "source": "typhoon_llm",
        "dish_name_th": name,
        "dish_name_en": "",
        "query": name,
        "ingredients": llm_result.get("ingredients", []),
        "allergens_detected": llm_result.get("allergens", []),
        "confidence": llm_result.get("confidence", "low"),
        "web_search_used": llm_result.get("web_search_used", False),
    }


def detect_allergens_from_name(name: str) -> tuple[list[str], list[str]]:
    """Scan a dish name for allergen keywords. Returns (allergen_keys, matched_terms).

    SAFETY-CRITICAL: prevents false-safe results when a fuzzy/substring match
    drops information from the user-visible dish name.

    Example: query 'ข้าวผัดกุ้ง' matched DB 'ข้าวผัด' (generic). The DB entry
    has no shellfish, so without this scan the dish would be marked safe even
    though 'กุ้ง' (shrimp) appears in the name. This function catches that.
    """
    if not name:
        return [], []
    norm = normalize(name)

    # Generic Thai words too short or too broad to be reliable signals.
    # 'ถั่ว' alone is in the peanut keywords but also matches mung bean / soybean
    # ('ถั่วเขียว', 'ถั่วเหลือง'); we ignore it here to avoid false positives.
    BLACKLIST = {"ถั่ว"}

    found_keys: list[str] = []
    matched_terms: list[str] = []
    seen: set[str] = set()

    for key, info in ALLERGENS.items():
        if key in seen:
            continue
        candidates = (
            info.get("keywords", [])
            + [info.get("th", ""), info.get("en", "")]
        )
        for kw in candidates:
            kw_clean = (kw or "").strip()
            if not kw_clean or len(kw_clean) < 2 or kw_clean in BLACKLIST:
                continue
            if normalize(kw_clean) in norm:
                found_keys.append(key)
                matched_terms.append(kw_clean)
                seen.add(key)
                break
    return found_keys, matched_terms


async def _llm_expand_synonyms(term: str) -> list[str]:
    """Ask Typhoon LLM to translate an unknown allergen term into TH/EN/ZH variants.

    Result is cached in-process so repeated checks of the same term are free.
    Returns a list including the original term + translations, or just [term] on failure.
    """
    cache_key = term.lower().strip()
    if cache_key in LLM_SYNONYM_CACHE:
        return LLM_SYNONYM_CACHE[cache_key]
    if not TYPHOON_API_KEY:
        return [term]

    user = (
        f'คำว่า "{term}" คืออะไรในแง่ของอาหาร/วัตถุดิบ? '
        "ตอบเป็น JSON array ของชื่อวัตถุดิบนี้ในภาษาไทย, อังกฤษ, จีน "
        "และคำพ้องที่ใช้ในเมนูอาหาร (รวมชื่อทางวิทยาศาสตร์ ถ้ามี) "
        "ตัวอย่าง: [\"shrimp\", \"prawn\", \"กุ้ง\", \"虾\"]"
    )
    payload = {
        "model": TYPHOON_CHAT_MODEL,
        "messages": [
            {"role": "system", "content": "ตอบเป็น JSON array เท่านั้น ห้ามมีข้อความอื่น"},
            {"role": "user", "content": user},
        ],
        "max_tokens": 256,
        "temperature": 0.0,
    }
    headers = {"Authorization": f"Bearer {TYPHOON_API_KEY}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(TYPHOON_CHAT_URL, headers=headers, json=payload)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
        m = re.search(r"\[.*\]", content, re.DOTALL)
        if m:
            parsed = json.loads(m.group(0))
            terms = [str(x).strip() for x in parsed if isinstance(x, (str, int, float)) and str(x).strip()]
            if terms:
                # Always include the user's original term so display works
                if term not in terms:
                    terms.insert(0, term)
                LLM_SYNONYM_CACHE[cache_key] = terms
                return terms
    except Exception as e:
        print(f"[synonym_llm] failed for '{term}': {e}")

    LLM_SYNONYM_CACHE[cache_key] = [term]
    return [term]


async def expand_synonyms(term: str) -> list[str]:
    """Return all synonyms (TH/EN/ZH/etc.) for a custom allergen term.

    Lookup order:
      1. Static SYNONYM_INDEX (synonyms.json + allergy.json keywords)
      2. Typhoon LLM fallback (cached)
      3. Original term as-is
    """
    if not term:
        return []
    t = term.strip().lower()
    if t in SYNONYM_INDEX:
        return list(SYNONYM_INDEX[t])
    return await _llm_expand_synonyms(term)


async def check_allergens(
    allergens_in_dish: list[str],
    user_allergies: list[str],
    ingredients: list[str] | None = None,
    custom_allergies: list[str] | None = None,
    dish_name: str | None = None,
) -> list[dict]:
    """Return list of allergen alerts:
    - Built-in allergen matches (key vs key)
    - Custom allergen substring matches against ingredients + dish name,
      expanded via synonyms (SYNONYM_INDEX + LLM fallback)
    """
    matched: list[dict] = []
    for a in allergens_in_dish:
        if a in user_allergies and a in FOODS["allergens"]:
            info = FOODS["allergens"][a]
            matched.append({"key": a, "type": "builtin", **info})

    if custom_allergies:
        # Scan both ingredients AND the dish name. The dish name is critical
        # because fuzzy/substring matches strip information ('ข้าวผัดมะม่วง'
        # matched to 'ข้าวผัด' would otherwise lose 'มะม่วง').
        scan_blob = (" ".join(ingredients or []) + " " + (dish_name or "")).lower()
        for term in custom_allergies:
            base = term.strip()
            if not base:
                continue
            synonyms = await expand_synonyms(base)
            hit_synonym = None
            for syn in synonyms:
                s = syn.strip().lower()
                if s and s in scan_blob:
                    hit_synonym = syn
                    break
            if hit_synonym:
                matched.append({
                    "key": f"custom:{base}",
                    "type": "custom",
                    "th": base,
                    "en": base,
                    "icon": "⚠️",
                    "matched_synonym": hit_synonym,
                })
    return matched


async def web_search_ingredients(dish_name: str, max_results: int = 3) -> str:
    """Search the web for dish ingredients via DuckDuckGo. Returns concatenated snippets."""
    try:
        from ddgs import DDGS
    except ImportError:
        print(f"[web_search] ddgs not installed — skipping search for '{dish_name}'")
        return ""

    query = f"{dish_name} วัตถุดิบ ส่วนประกอบ"
    snippets: list[str] = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(query, region="th-th", max_results=max_results):
                title = r.get("title", "")
                body = r.get("body", "")
                if body:
                    snippets.append(f"- {title}: {body}")
    except Exception as e:
        print(f"[web_search] failed for '{dish_name}': {e}")
        return ""

    if snippets:
        print(f"[web_search] OK '{dish_name}' → {len(snippets)} snippets")
    else:
        print(f"[web_search] empty results for '{dish_name}'")
    return "\n".join(snippets)


@app.get("/api/debug/web_search")
async def debug_web_search(q: str = "ผัดไทย"):
    """Quick smoke test for DuckDuckGo: GET /api/debug/web_search?q=ผัดไทย"""
    try:
        from ddgs import DDGS as _DDGS  # noqa: F401
        ddgs_available = True
    except ImportError:
        ddgs_available = False

    snippets = await web_search_ingredients(q) if ddgs_available else ""
    return {
        "ddgs_installed": ddgs_available,
        "query": q,
        "snippet_count": len(snippets.split("\n")) if snippets else 0,
        "preview": snippets[:500] if snippets else None,
    }


@app.get("/api/allergens")
async def get_allergens():
    """List all available allergens for UI selection."""
    return [
        {"key": k, "th": v["th"], "en": v["en"], "icon": v["icon"]}
        for k, v in FOODS["allergens"].items()
    ]


async def enrich_dish_result(
    result: dict,
    user_allergies: list[str],
    custom_allergies: list[str] | None = None,
) -> dict:
    """Add alerts + allergen display info + contamination warnings.

    SAFETY: also scans the dish name (the user-visible OCR'd query) for
    allergen keywords. This is critical because fuzzy / substring matches
    drop information from the query — e.g. 'ข้าวผัดกุ้ง' fuzzy-matched to
    DB 'ข้าวผัด' would otherwise inherit the DB entry's clean ingredient
    list and miss the shrimp entirely.
    """
    query_name = result.get("query") or result.get("dish_name_th") or ""

    # Scan name for allergen keywords + matched ingredient terms
    extra_allergens, extra_terms = detect_allergens_from_name(query_name)
    if extra_allergens:
        existing = set(result.get("allergens_detected", []))
        result["allergens_detected"] = list(existing | set(extra_allergens))
    if extra_terms:
        existing_norm = {normalize(i) for i in result.get("ingredients", [])}
        result.setdefault("ingredients", [])
        for term in extra_terms:
            if normalize(term) not in existing_norm:
                result["ingredients"].append(term)
                existing_norm.add(normalize(term))

    alerts = await check_allergens(
        result["allergens_detected"],
        user_allergies,
        ingredients=result.get("ingredients", []),
        custom_allergies=custom_allergies,
        dish_name=query_name,
    )
    result["alerts"] = alerts
    result["has_alert"] = len(alerts) > 0
    result["allergens_info"] = [
        {"key": k, **FOODS["allergens"][k]}
        for k in result["allergens_detected"]
        if k in FOODS["allergens"]
    ]

    # Cross-contamination — only attach to dishes that are NOT already alerted
    # (alerted dishes already have a stronger warning; contamination would be redundant)
    if not result["has_alert"]:
        warnings = infer_contamination_risk(
            result.get("dish_name_th") or result.get("query") or "",
            user_allergies=user_allergies,
        )
        if warnings:
            # Enrich each warning's allergen keys with display info
            for w in warnings:
                w["allergens_info"] = [
                    {"key": k, **FOODS["allergens"][k]}
                    for k in w["allergens"]
                    if k in FOODS["allergens"]
                ]
            result["contamination_warnings"] = warnings

    return result


@app.post("/api/analyze")
async def analyze(
    image: Optional[UploadFile] = File(None),
    text: Optional[str] = Form(None),
    allergies: str = Form("[]"),
    custom_allergies: str = Form("[]"),
):
    """Receive image (menu photo) or text (single dish) and return dish analyses."""
    try:
        user_allergies = json.loads(allergies)
    except json.JSONDecodeError:
        user_allergies = []
    try:
        user_custom = json.loads(custom_allergies)
        if not isinstance(user_custom, list):
            user_custom = []
    except json.JSONDecodeError:
        user_custom = []

    ocr_text = ""
    is_menu = False  # True when an image was uploaded → may contain multiple dishes
    if image is not None:
        contents = await image.read()
        if not contents:
            raise HTTPException(400, "Empty image")
        mime = image.content_type or "image/jpeg"
        ocr_text = await typhoon_ocr(contents, mime)
        is_menu = True
    elif text:
        ocr_text = text
    else:
        raise HTTPException(400, "ต้องส่งรูปภาพหรือข้อความ")

    ocr_text = ocr_text.strip()

    extracted_names: list[str] = []
    db_matched_dishes: list[dict] = []

    if is_menu:
        extracted_names = await extract_dish_names(ocr_text)
        # find_all_local_matches scans the whole OCR blob for DB substrings, so
        # generic dishes ("ข้าวผัด") match inside specific menu items
        # ("ข้าวผัดกุ้ง", "ข้าวผัดปู"). When extracted_names already has the
        # specific names, step 1 just produces generic-vs-specific duplicate cards.
        # Only run it as a fallback when LLM + heuristic returned nothing.
        if extracted_names:
            db_matched_dishes = []
        else:
            db_matched_dishes = find_all_local_matches(ocr_text)
    else:
        # Single typed name: just look it up
        extracted_names = [ocr_text]

    dishes: list[dict] = []
    seen_dish_keys: set[str] = set()  # dedupe by canonical dish name_th

    # 1. Add all DB substring matches first (highest confidence)
    for d in db_matched_dishes:
        key = normalize(d["name_th"])
        if key in seen_dish_keys:
            continue
        seen_dish_keys.add(key)
        result = {
            "source": "local_db",
            "dish_name_th": d["name_th"],
            "dish_name_en": d["name_en"],
            "query": d["name_th"],
            "ingredients": d["ingredients"],
            "allergens_detected": d["allergens"],
            "confidence": "high",
        }
        dishes.append(await enrich_dish_result(result, user_allergies, user_custom))

    # 2. For each LLM-extracted name not already covered, try local DB then LLM lookup.
    #    Dedup is by *query name* — different OCR'd names that fuzzy-match the same DB
    #    dish should still each get their own card (they're different items on the menu).
    for name in extracted_names[:30]:
        norm = normalize(name)
        if not norm or norm in seen_dish_keys:
            continue
        # Quick local exact lookup for this specific name
        local = find_dish_local(name)
        if local:
            local_key = normalize(local["name_th"])
            is_exact_match = local_key == norm
            if is_exact_match:
                # User-visible name == DB name: real duplicate, dedup by DB key
                if local_key in seen_dish_keys:
                    continue
                seen_dish_keys.add(local_key)
                seen_dish_keys.add(norm)
                result = {
                    "source": "local_db",
                    "dish_name_th": local["name_th"],
                    "dish_name_en": local["name_en"],
                    "query": name,
                    "ingredients": local["ingredients"],
                    "allergens_detected": local["allergens"],
                    "confidence": "high",
                }
            else:
                # Substring / superstring match — DB entry is generic ("ข้าวผัด") and
                # the OCR'd name is more specific ("ข้าวผัดกุ้ง"). Treat as fuzzy so
                # each specific menu item keeps its own card with matched_to label.
                seen_dish_keys.add(norm)
                result = {
                    "source": "local_db_fuzzy",
                    "dish_name_th": name,
                    "dish_name_en": local.get("name_en", ""),
                    "matched_to": local["name_th"],
                    "query": name,
                    "match_ratio": 0.9,
                    "ingredients": local["ingredients"],
                    "allergens_detected": local["allergens"],
                    "confidence": "high",
                }
                dishes.append(await enrich_dish_result(result, user_allergies, user_custom))
                continue
        else:
            # Unknown dish — try fuzzy DB match before falling back to LLM
            fuzzy = find_dish_fuzzy(name)
            if fuzzy:
                d, ratio = fuzzy
                # Dedup by query (not DB key) — many OCR'd names may map to same DB dish.
                # Show user the original OCR'd name + what it was matched to.
                seen_dish_keys.add(norm)
                result = {
                    "source": "local_db_fuzzy",
                    "dish_name_th": name,
                    "dish_name_en": d.get("name_en", ""),
                    "matched_to": d["name_th"],
                    "query": name,
                    "match_ratio": round(ratio, 2),
                    "ingredients": d["ingredients"],
                    "allergens_detected": d["allergens"],
                    "confidence": "medium" if ratio < 0.85 else "high",
                }
                dishes.append(await enrich_dish_result(result, user_allergies, user_custom))
                continue
            llm_result = await typhoon_lookup_ingredients(name)
            seen_dish_keys.add(norm)
            result = {
                "source": "typhoon_llm",
                "dish_name_th": name,
                "dish_name_en": "",
                "query": name,
                "ingredients": llm_result.get("ingredients", []),
                "allergens_detected": llm_result.get("allergens", []),
                "confidence": llm_result.get("confidence", "low"),
                "web_search_used": llm_result.get("web_search_used", False),
            }
        dishes.append(await enrich_dish_result(result, user_allergies, user_custom))

    alerted = [d for d in dishes if d["has_alert"]]
    safe = [d for d in dishes if not d["has_alert"]]

    return {
        "ocr_text": ocr_text,
        "is_menu": is_menu,
        "dish_count": len(dishes),
        "alerted_count": len(alerted),
        "safe_count": len(safe),
        "dishes": dishes,
        "extracted_names": extracted_names,
        "db_matched_count": sum(
            1 for d in dishes if d["source"] in ("local_db", "local_db_fuzzy")
        ),
    }


# Serve frontend
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/")
    async def index():
        return FileResponse(str(FRONTEND_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
