"""
Per-scholarship configuration overrides for SchReminder Scout.

Key   = scholarship name exactly as written in spreadsheet (case-insensitive match at runtime).
Value = dict of overrides (all optional):
  preferred_query          : str   — replaces the auto-generated search query
  preferred_urls           : list  — injected at the FRONT of the scrape queue (before search results)
  preferred_domains        : list  — temporarily added to OFFICIAL_DOMAINS for this run only
  date_source_domain       : str   — if set, LLM is told dates MUST only come from this domain;
                                     all other domain dates are hard-rejected in the prompt
  needs_translation        : bool  — translate scraped non-English text before LLM
  translation_lang         : str   — source language code hint (e.g. "kk", "ru")
  date_precision_expected  : str   — hint to email renderer ("monthly", "quarterly")
  notes                    : str   — human-readable note (ignored by engine at runtime)

Add a new entry for any scholarship that:
  - consistently returns the wrong authoritative page via generic search
  - has a param-based or low-ranked URL that search engines never index
  - has language barriers on the primary site
  - has month-range dates instead of exact dates
"""

SCHOLARSHIP_CONFIG = {

    # ── JAPAN ──────────────────────────────────────────────────────────────────
    "MEXT (Monbukagakusho) - Research Student": {
        "preferred_query":   "MEXT Research Student Scholarship 2026 Indonesia embassy deadline jadwal",
        "preferred_urls":    [
            # Indonesian embassy — primary authoritative source for Indonesian applicants.
            # Try multiple paths since the direct page sometimes returns 403.
            "https://www.id.emb-japan.go.jp/itpr_id/sch_rs.html",      # direct MEXT RS page
            "https://www.id.emb-japan.go.jp/itpr_id/beasiswa.html",    # general scholarship listing
            "https://www.id.emb-japan.go.jp/",                          # embassy root
        ],
        "preferred_domains": ["id.emb-japan.go.jp"],
        # Hard constraint: LLM must only accept dates from this domain.
        # This prevents the India embassy (in.emb-japan.go.jp) or generic portals
        # from being used as the date source.
        "date_source_domain": "id.emb-japan.go.jp",
        "notes": (
            "INDONESIAN embassy is the ONLY authoritative source. "
            "id.emb-japan.go.jp = Indonesia. in.emb-japan.go.jp = INDIA (wrong). "
            "studyinjapan.go.jp is a global portal — NOT Indonesia-specific. "
            "date_source_domain forces LLM to reject dates from any other domain."
        ),
    },

    # ── KOREA ──────────────────────────────────────────────────────────────────
    "Global Korea Scholarship (GKS) - Graduate": {
        # LOCKED MODE: skip search engine entirely — only scrape these URLs.
        # {year} is substituted at runtime with the current calendar year
        # (e.g. 2026 → article for 2026 cycle, 2027 → article for 2027 cycle).
        "locked_urls": [
            "https://gksscholarship.com/gks-scholarship-{year}-indonesia-global-korea-scholarship-indonesia/",
            "https://gksscholarship.com/",          # homepage fallback if year-article 404s
        ],
        "preferred_domains": ["gksscholarship.com", "niied.go.kr"],
        # Hard constraint: LLM must only accept dates from gksscholarship.com.
        # Prevents the LLM from dismissing it as a "blog" and falling back to
        # MOFA PDFs or studyinkorea.go.kr Korean-language archive pages.
        "date_source_domain": "gksscholarship.com",
        "notes": (
            "LOCKED MODE: gksscholarship.com is the OFFICIAL GKS foundation portal "
            "for Indonesian applicants — NOT a third-party blog. "
            "{year} is substituted at runtime so the year-specific Indonesia article "
            "is always fetched first. No search engine is used for this scholarship."
        ),
    },

    # ── IRELAND (GOI-IES) ─────────────────────────────────────────────────────
    "Government of Ireland International Education Scholarship (GOI-IES)": {
        "preferred_query":   "Government of Ireland International Education Scholarship HEA 2026 deadline",
        "preferred_urls":    ["https://hea.ie/policy/internationalisation/goi-ies/"],
        "preferred_domains": ["hea.ie"],
        "notes": (
            "HEA Ireland (Higher Education Authority) is the issuer. "
            "Ranks ~#5 in generic searches. Must inject directly."
        ),
    },

    # ── IRELAND (GO-PSP) ──────────────────────────────────────────────────────
    "Government of Ireland Postgraduate Scholarship Programme (GO-PSP)": {
        "preferred_query":   "Government of Ireland Postgraduate Scholarship Programme 2026 IRC deadline",
        "preferred_urls":    ["https://research.ie/funding/goipg/"],
        "preferred_domains": ["research.ie"],
        "notes": "Irish Research Council (IRC) is the issuer. Different from GOI-IES.",
    },

    # ── KAZAKHSTAN ────────────────────────────────────────────────────────────
    "Kazakhstan Government Scholarship (Bolashak)": {
        "preferred_query":    "Bolashak Kazakhstan Government Scholarship 2026 deadline English",
        "preferred_urls":     [
            "https://www.bolashak.gov.kz/en/scholarship-program",
            "https://konkurs.bolashak.gov.kz/",
        ],
        "preferred_domains":  ["bolashak.gov.kz", "konkurs.bolashak.gov.kz"],
        "needs_translation":  True,
        "translation_lang":   "ru",
        "notes": (
            "Site defaults to Kazakh/Russian. Try English subdomain first. "
            "konkurs.bolashak.gov.kz is the registration portal. "
            "Translation sub-step fires if ASCII ratio < 5%."
        ),
    },

    # ── MALAYSIA (MTCP) ───────────────────────────────────────────────────────
    "MTCP Scholarship": {
        "preferred_query":   "MTCP Malaysia Technical Cooperation Programme scholarship 2026 deadline application",
        "preferred_urls":    [
            "https://mtcp.kln.gov.my/scholarship",
            "https://mtcp.kln.gov.my/news",
            "https://mtcp.kln.gov.my/announcement",
        ],
        "preferred_domains": ["mtcp.kln.gov.my"],
        "notes": (
            "Main page may embed dates in images on some cycles. "
            "Branching into /news and /announcement sub-pages may find text-based deadline notices. "
            "If still no dates found, remark will explicitly say [NO_RESULTS]."
        ),
    },

    # ── GERMANY / DAAD ────────────────────────────────────────────────────────
    "DAAD STEM Discipline": {
        "preferred_query":   "DAAD STEM scholarship 2026 deadline Germany engineering sciences application",
        "preferred_urls":    [
            "https://www2.daad.de/deutschland/stipendium/datenbank/en/21148-scholarship-database/?origin=5&status=3&subjectGrps=&daad=&q=&page=1&detail=57742130#voraussetzungen",
        ],
        "preferred_domains": ["daad.de"],
        "notes": (
            "Param-based DB URL (detail=57742130) is NEVER indexed by search engines. "
            "Direct injection required. daad.org/en/2025/... is a news page, not the official DB entry."
        ),
    },

    "DAAD EPOS": {
        "preferred_query":   "DAAD EPOS scholarship 2026 deadline postgraduate application Germany",
        "preferred_urls":    [
            "https://www2.daad.de/deutschland/stipendium/datenbank/en/21148-scholarship-database/?origin=5&status=3&subjectGrps=&daad=",
            "https://www.daad.de/en/",
        ],
        "preferred_domains": ["daad.de"],
        "notes": "DAAD EPOS — use scholarship database, not news articles.",
    },

    # ── HYUNDAI CMK ───────────────────────────────────────────────────────────
    "Hyundai Motor Chung Mong-Koo Global Scholarship": {
        "preferred_query":         "Hyundai CMK Foundation Global Scholarship 2026 graduate deadline",
        "preferred_urls":          ["https://www.cmkfoundation-globalscholarship.org/work/graduates"],
        "preferred_domains":       ["cmkfoundation-globalscholarship.org"],
        "date_precision_expected": "monthly",
        "notes": (
            "Dates published as month ranges only (e.g. Dec-Jan, Jun-Jul). "
            "LLM date_precision_expected hint tells it to infer first/last of month."
        ),
    },

    # ── LPDP ─────────────────────────────────────────────────────────────────
    # Any scholarship with separate registration windows per Tahap/Phase
    # should have one config entry per row in the spreadsheet.
    "LPDP STEM Industri Strategis (Tahap 1)": {
        "preferred_query":   "LPDP STEM Industri Strategis Tahap 1 2026 jadwal pendaftaran timeline",
        "preferred_urls":    ["https://beasiswalpdp.kemenkeu.go.id/"],
        "preferred_domains": ["beasiswalpdp.kemenkeu.go.id", "lpdp.kemenkeu.go.id"],
    },
    "LPDP STEM Industri Strategis (Tahap 2)": {
        "preferred_query":   "LPDP STEM Industri Strategis Tahap 2 2026 jadwal pendaftaran timeline",
        "preferred_urls":    ["https://beasiswalpdp.kemenkeu.go.id/"],
        "preferred_domains": ["beasiswalpdp.kemenkeu.go.id", "lpdp.kemenkeu.go.id"],
    },

    # ── ANSO (uni-to-uni — separate rows, handled by B3 parser) ──────────────
    "(ANSO Scholarship) UCAS": {
        "preferred_urls":    ["https://english.ucas.ac.cn/index.php/admission/international-students/deadline"],
        "preferred_domains": ["ucas.ac.cn", "anso.org.cn"],
        "notes": "CAS-ANSO via UCAS. University page is the authoritative source for deadline.",
    },
    "(ANSO Scholarship) USTC": {
        "preferred_urls":    [
            "https://en.ustc.edu.cn/",
            "https://en.ustc.edu.cn/info/1043/3098.htm",
        ],
        "preferred_domains": ["ustc.edu.cn", "anso.org.cn"],
        "notes": "ANSO via University of Science and Technology of China. Second URL is the known ANSO admissions page.",
    },

    # ── ADB-JSP (uni-to-uni — handled by B3 parser) ───────────────────────────
    "(ADB-JSP Scholarship) Institute of Science Tokyo": {
        "preferred_urls":    ["https://www.isct.ac.jp/en/"],
        "preferred_domains": ["isct.ac.jp", "adb.org"],
        "notes": "ADB-JSP at Institute of Science Tokyo (formerly Tokyo Tech). Check English admissions page.",
    },
    "(ADB-JSP Scholarship) Keio University": {
        "preferred_urls":    ["https://www.keio.ac.jp/en/"],
        "preferred_domains": ["keio.ac.jp", "adb.org"],
        "notes": "ADB-JSP at Keio University. Check English graduate admissions page.",
    },
}


def get_scholarship_config(name: str) -> dict:
    """
    Case-insensitive exact-name lookup against SCHOLARSHIP_CONFIG.
    Returns the config dict if found, or {} if no entry exists (normal pipeline).
    """
    name_lower = name.strip().lower()
    for key, cfg in SCHOLARSHIP_CONFIG.items():
        if key.strip().lower() == name_lower:
            return cfg
    return {}
