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

    # ── TAIWAN ─────────────────────────────────────────────────────────────────
    "MoE Taiwan": {
        "preferred_query": "Taiwan MoE Scholarship 2026 Indonesia deadline application timeline",
        "preferred_urls": [
            "https://taiwanscholarship.moe.gov.tw/web/pages.aspx?p=8",  # Application Process page (has dates)
            "https://taiwanscholarship.moe.gov.tw/web/pages.aspx?p=7",  # Scholarship Guidelines
            "https://taiwanscholarship.moe.gov.tw/Apply/",              # registration portal
            "https://taiwanscholarship.moe.gov.tw/",                    # root
            "https://www.roc-taiwan.org/id_en/",                        # TECC Indonesia root (embassy for ID applicants)
        ],
        "preferred_domains": [
            "taiwanscholarship.moe.gov.tw",
            "moe.edu.tw",
            "roc-taiwan.org",
        ],
        # Hard constraint: only accept dates from the official MoE portal.
        # Prevents the LLM from picking up US/Japan/other-country embassy announcement pages
        # which rank high in generic search results but are not relevant for Indonesian applicants.
        "date_source_domain": "taiwanscholarship.moe.gov.tw",
        "notes": (
            "MoE Taiwan Scholarship — official portal is taiwanscholarship.moe.gov.tw. "
            "TECC Indonesia (roc-taiwan.org/id_en/) is the correct embassy source for Indonesian applicants. "
            "Generic search returns US/Japan embassy news pages (depart.moe.edu.tw/dc/...) — these are "
            "overseas-facing announcements, NOT Indonesia-specific. date_source_domain forces LLM to "
            "reject dates from those non-portal pages. "
            "NOTE: roc-taiwan.org/id_en/post/3025.html was a known TECC scholarship page but returned 404 — "
            "removed. Use TECC root instead and rely on taiwanscholarship.moe.gov.tw for date authority."
        ),
    },

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
            "https://www2.daad.de/deutschland/stipendium/datenbank/en/21148-scholarship-database/?origin=5&status=3&subjectGrps=&daad=&q=&page=1&detail=57742130",
        ],
        "preferred_domains": ["daad.de"],
        # DAAD uses Bootstrap CSS tabs: Overview / Application requirements / Application Procedure / ...
        # All tab content is in the HTML simultaneously (CSS display:none hides inactive tabs).
        # The default 5,000-char budget is exhausted by the Overview section before reaching
        # the "Application Procedure" (bewerbungsprozess) tab where the actual dates are.
        # Increasing to 12,000 chars gets us into that section.
        "scrape_char_limit": 12000,
        "notes": (
            "Param-based DB URL (detail=57742130) is NEVER indexed by search engines. "
            "Direct injection required. Tab navigation is CSS-only (no URL change on tab switch). "
            "scrape_char_limit=12000 needed to reach bewerbungsprozess tab past the Overview section."
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
        # LOCKED MODE: the /work/graduates page has the date info, but it sits
        # past the 5,000-char scrape truncation limit (privacy modals consume the budget first).
        # The schedule is fixed ("Dec–Jan / Jun–Jul each year"), so we lock directly.
        "locked_urls": [
            "https://www.cmkfoundation-globalscholarship.org/work/graduates",
            "https://www.cmkfoundation-globalscholarship.org/",
        ],
        "preferred_domains":       ["cmkfoundation-globalscholarship.org"],
        "date_precision_expected": "monthly",
        # context_hint is prepended to the LLM user prompt when locked mode is active,
        # giving it the known fixed schedule so it can infer dates even if truncation hides them.
        "context_hint": (
            "KNOWN SCHEDULE (from official page cmkfoundation-globalscholarship.org/work/graduates):\n"
            "  Spring Semester application: Dec. – Jan. each year\n"
            "  Fall Semester application:   Jun. – Jul. each year\n"
            "Use the nearest upcoming cycle relative to TODAY's date. "
            "Apply monthly inference: start = first of start month, deadline = last of end month. "
            "Set date_precision = 'monthly'."
        ),
        "notes": (
            "LOCKED MODE: date content is buried past the 5,000-char scrape truncation limit "
            "due to large privacy modals appearing before the 'Selection Period' section. "
            "Fixed schedule: Spring=Dec–Jan, Fall=Jun–Jul. context_hint injects the known dates "
            "directly into the LLM prompt so truncation is irrelevant."
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
        # NOTE: IST (formerly Tokyo Tech) merged with TMD in 2024.
        # Scholarship info still lives on the OLD titech.ac.jp domain (not yet migrated to isct.ac.jp).
        # BOTH sites are JavaScript SPAs — scraping yields only JS bundles, no readable dates.
        # Solution: context_hint + admissions page as Info URL.
        "preferred_urls": [
            "https://www.titech.ac.jp/english/international-student-exchange/prospective-students/scholarships/adb-jsp",  # correct ADB-JSP page (old Tokyo Tech domain, not yet migrated)
            "https://admissions.isct.ac.jp/en/013/graduate",   # IST grad admissions index
            "https://www.isct.ac.jp/en/",                      # IST root fallback
        ],
        "preferred_domains": ["isct.ac.jp", "titech.ac.jp", "adb.org"],
        "context_hint": (
            "IMPORTANT CONTEXT for ADB-JSP Scholarship at Institute of Science Tokyo (IST):\n"
            "IST (formerly Tokyo Institute of Technology / Tokyo Tech) merged with TMD in 2024. "
            "The dedicated ADB-JSP scholarship page is on the OLD domain: "
            "https://www.titech.ac.jp/english/international-student-exchange/prospective-students/scholarships/adb-jsp\n"
            "However, titech.ac.jp uses a JavaScript SPA — scraped content has no dates.\n"
            "The ADB-JSP process at IST: (1) Applicant applies to a specific IST graduate program for October enrollment, "
            "(2) IST nominates eligible students to ADB after admission. "
            "Application deadline is set per graduate school/program and is NOT a single published date. "
            "If no specific date is found, set status='CLOSED' and note: deadlines vary by graduate program. "
            "The Info URL should be: https://www.titech.ac.jp/english/international-student-exchange/prospective-students/scholarships/adb-jsp\n"
        ),
        "notes": (
            "IST ADB-JSP: titech.ac.jp is the actual scholarship domain (legacy, pre-merger). "
            "Both isct.ac.jp and titech.ac.jp are JS SPAs — no scrapable dates. "
            "context_hint provides the LLM with correct Info URL and process description."
        ),
    },

    "(ADB-JSP Scholarship) Keio University": {
        "preferred_query": "Keio University ADB-JSP scholarship application deadline 2026 2027 admissions graduate",
        "preferred_urls": [
            "https://www.keio.ac.jp/en/admissions/",            # main admissions hub (English)
            "https://www.keio.ac.jp/en/",                       # root fallback
            "https://www.adb.org/work-with-us/careers/japan-scholarship-program",  # ADB JSP overview
        ],
        "preferred_domains": ["keio.ac.jp", "adb.org"],
        # context_hint: Keio has NO single ADB-JSP deadline page.
        # Admission is per-graduate-school; the ADB-JSP documents are sent AFTER admission.
        # Deadlines are embedded in each graduate school's application guide (PDF/HTML).
        "context_hint": (
            "IMPORTANT CONTEXT for Keio University ADB-JSP Scholarship:\n"
            "Keio University does NOT publish a single ADB-JSP scholarship deadline. "
            "The process is: (1) Applicant applies to a specific Keio graduate school for September enrollment, "
            "(2) Keio sends ADB-JSP documents after admission. "
            "Application deadlines are set per graduate school (e.g. Graduate School of System Design and "
            "Management requires application in Period II or III for September intake). "
            "If no specific date is found, set status='CLOSED' and note that deadlines vary by graduate school. "
            "The Info URL should be the Keio admissions page: https://www.keio.ac.jp/en/admissions/\n"
        ),
        "notes": (
            "ADB-JSP at Keio University — no single scholarship deadline page exists. "
            "Each graduate school sets its own September intake application period. "
            "context_hint explains this to the LLM so it doesn't hallucinate a single deadline. "
            "adb.org returns 403 for the scraper UA — kept as preferred_domain but not injected as URL."
        ),
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
