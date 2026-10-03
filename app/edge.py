"""
Edge-case handler — catches non-analytical input (greetings, "who are you",
thanks, off-topic, abuse) BEFORE any model call. Zero cost, instant, on-brand.

Ported from NeuralAiGovernanceProject `services/edge_handler.py` and kept in
step with it: same rule-based shape — strong-intent early exit, ordered pattern
banks, meta-conversation + follow-up + non-English pass-throughs, then a
domain whitelist that BLOCKS anything with no MGNREGA / PMAY-G context. The
pattern banks are the reference's; the responses are rewritten for this
assistant (Meghalaya MGNREGA + PMAY-G, sourced from megh_db and the scheme
reference docs).

`detect_edge_case(question) -> {"type", "response", "suggestions"?} | None`
  - a dict  => stop here, return this canned reply (no model call)
  - None    => a real data / knowledge / follow-up question — let the pipeline route it
"""
import re

# ── Pattern banks ───────────────────────────────────────────────────────────
_GREETINGS = [
    r"^(hi|hello|hey|hii+|helo|namaste|namaskar|khublei|kumno|good\s*(morning|afternoon|evening|day|night))"
    r"(\s+(there|all|team|everyone|bot|assistant|folks|sir|madam))?[\s!.?]*$",
    r"^(howdy|greetings|sup|whats?\s*up|yo|hola)[\s!.?]*$",
]

_IDENTITY = [
    r"(who|what)\s+(are|r)\s+(you|u)\b",
    r"(your|ur)\s+(name|purpose|job|role)\b",
    r"are\s+you\s+(an?\s+)?(ai|bot|human|real|chatbot|robot|machine|gpt|chatgpt|claude|gemini|qwen|llm)",
    r"(tell|about)\s+(me\s+)?about\s+(yourself|you)\b",
    r"what\s+can\s+you\s+(do|help)\b",
    r"what\s+do\s+you\s+do\b",
    r"introduce\s+yourself\b",
    r"which\s+(ai|model|llm|technology|company)\s+(are|is|do|made|built)\s+you\b",
    r"(powered|built|made|developed|created|trained)\s+by\b",
    r"how\s+(do|does)\s+you\s+work\b",
    # conversational "how can you help me" / "can you help me" openers — these
    # carry no scheme keyword, so without a pattern here they fall all the way
    # through to the off-topic whitelist gate and get the blunt "I can only
    # answer..." reply instead of the friendly capabilities rundown. A message
    # that also names a scheme/domain term exits earlier at the _SCHEME_STRONG
    # check (step 1), so these stay safe to match loosely.
    r"^\W*(hi|hello|hey|hii+)?\W*,?\s*how\s+(can|could|do|would)\s+(you|u)\s+(help|assist)\b",
    r"^\W*(hi|hello|hey|hii+)?\W*,?\s*(can|could|would)\s+(you|u)\s+(help|assist)\s+me\b",
    r"what\s+can\s+(you|u)\s+help\s+(me\s+)?with\b",
    r"what\s+all\s+can\s+(you|u)\s+do\b",
]

_THANKS = [
    r"^(thanks?|thank\s*you|thank\s*u|ty|thx|thanku|dhanyavaad|dhanyawad|shukriya|khublei\s+shibun)\b.{0,20}$",
    r"^(that.?s?\s+(great|helpful|perfect|awesome|nice|good|excellent|useful|wonderful))[\s!.?]*$",
    r"^(great|perfect|awesome|excellent|brilliant|fantastic|helpful|nice)\s*(help|work)?[\s!.?]*$",
]

_GOODBYE = [
    r"^(bye|goodbye|good\s*bye|see\s*you|cya|take\s*care|later|ok\s*bye|done|that.?s\s+all)[\s!.?]*$",
    r"^(have\s+a\s+(good|great|nice)\s+(day|evening|night))[\s!.?]*$",
]

_PROFANITY_REDIRECT = [
    r"\b(fuck|fuk|shit|damn|bastard|crap|bloody|bullshit|wtf|stfu)\b",
]

_SILLY = [
    # jokes / entertainment / creative writing
    r"(tell|say)\s+(me\s+)?(a\s+)?(joke|funny|riddle)",
    r"make\s+me\s+laugh",
    r"sing\s+(a\s+)?song",
    r"write\s+(me\s+)?(a\s+)?(poem|story|essay|rap|song|lyrics|script)",
    r"(play|let.*play)\s+(a\s+)?game",
    # personal feelings / anthropomorphising
    r"do\s+you\s+(like|love|hate|feel|dream|sleep|eat|drink|breathe|think)",
    r"(favorite|favourite)\s+(color|colour|food|movie|song|book|sport|animal|team)",
    r"how\s+old\s+are\s+you",
    r"where\s+do\s+you\s+(live|stay|come\s+from)",
    r"are\s+you\s+(happy|sad|angry|tired|bored|excited|scared|lonely|married|single)",
    r"(marry|date|love|kiss|hug)\s+me",
    r"do\s+you\s+have\s+(feelings|emotions|heart|soul|family|friends|a\s+boyfriend|a\s+girlfriend)",
    # philosophical / general chit-chat
    r"what\s+is\s+the\s+(meaning|purpose)\s+of\s+(life|everything)",
    r"(is\s+god|does\s+god)\s+(real|exist)",
    r"what\s+(happens|comes)\s+after\s+death",
    # comparisons with other AI
    r"(better|worse)\s+than\s+(chatgpt|gpt|openai|claude|gemini|copilot|bard)",
    r"\bvs\s+(chatgpt|gpt|claude|gemini|copilot|bard)\b",
    # random unrelated tasks
    r"translate\s+(this|to|into)\s+",
    r"(write|draft|compose)\s+(a\s+)?(email|letter|message|whatsapp|cv|resume|application\s+for\s+leave)",
    r"solve\s+(this\s+)?(math|equation|sum|problem|puzzle)",
    r"what\s+is\s+\d+\s*[\+\-\*\/x]\s*\d+",
    r"(predict|forecast)\s+(the\s+)?(future|stock|crypto|price|weather|match)",
    r"give\s+me\s+(advice|tips)\s+on\s+(life|love|money|career|health|relationship)",
    # insults aimed at the assistant
    r"(stupid|dumb|useless|idiot|fool|trash|garbage|nonsense)\s*(bot|ai|system|app|assistant)?",
    r"you\s+(suck|are\s+bad|are\s+useless|are\s+dumb|are\s+wrong\s+always)",
    # general knowledge that isn't ours
    r"(capital|president|prime\s*minister|chief\s*minister|currency|population|area|gdp)\s+of\s+\w+",
    r"who\s+(is|was)\s+(the\s+)?(president|prime\s*minister|king|queen|ceo|founder|inventor|actor|actress|elon|musk|modi|trump)\b",
    r"(largest|smallest|tallest|longest|biggest|fastest|richest)\s+(country|city|river|mountain|building|company)",
    r"(what|how)\s+(is|does|do)\s+(gravity|photosynthesis|evolution|inflation|blockchain|electricity)\b",
    r"(recipe|ingredients|how\s+to\s+cook|how\s+to\s+make)\s+",
    r"(symptom|treatment|cure|medicine)\s+(for|of)\s+",
    r"\b(ipl|cricket|football|soccer|match\s+score|bollywood|hollywood|netflix)\b",
]

_OFF_TOPIC = [
    r"\b(weather|temperature|forecast|horoscope|zodiac|astrology|news\s+today)\b",
    r"\b(stock|share\s+market|sensex|nifty|bitcoin|crypto|nft|mutual\s+fund|investment)\b",
    r"\b(flight|train\s+ticket|irctc|hotel\s+booking|visa|passport|holiday\s+package|tourism)\b",
    r"\b(amazon|flipkart|online\s+shopping|price\s+of\s+(a|an|the)\b)",
    r"\b(exam\s+result|admission|jee|neet|upsc|board\s+result)\b",
]

# PERSONAL financial advice — "where should I invest my money", "best way to
# invest 1 lakh", "how do I grow my savings". Its own bank, checked AHEAD of
# the scheme-intent early exit: _SCHEME_STRONG counts a bare money unit
# ("lakh", "crore") as scheme intent, so these were waved through to the
# pipeline and collected the "which scheme?" picker instead of a refusal
# (reported 2026-09-18). Every pattern is anchored on the ADVICE phrasing,
# never on the money noun, so a real scheme question using the same units
# ("total expenditure in lakh") is untouched.
_MONEY_ADVICE = [
    r"\b(?:where|how|what)\b[^?.!]{0,30}\b(?:should|can|do|would|could)\s+(?:i|we)\b"
    r"[^?.!]{0,30}\b(?:invest|save|deposit|put)\b",
    r"\b(?:best|safest|good)\s+(?:way|place|option|scheme)s?\s+to\s+"
    r"(?:invest|save|deposit|park|grow)\b",
    r"\b(?:invest|investing)\s+(?:my|our|his|her|their|the)?\s*(?:money|savings|funds|cash)\b",
    r"\b(?:where|how)\s+to\s+(?:invest|save)\b",
    r"\b(?:grow|double|multiply)\s+(?:my|our)\s+(?:money|savings|wealth)\b",
]

# A personal request for money: "now give me five thousand loan for me i am in
# crisis" (reported 2026-09-29, right after a Focus Plus answer). It named no
# scheme, so the follow-up rewrite glued Focus Plus on and the DATA path asked
# "which year / area?" for a Focus Plus analytics query nobody asked. The
# assistant cannot give, lend or approve anything; it says so, then points at
# the schemes that do offer support. Anchored on a FIRST-PERSON request for
# money, never on the money noun alone: "how many loans were disbursed under CM
# Elevate Legacy?" and "give me the amount disbursed" are untouched.
_MONEY_NOUN = r"(?:loans?|money|cash|rupees?|funds?|financial\s+(?:help|aid|support|assistance))"
_AMOUNT_WORDS = (r"(?:(?:rs\.?|inr|₹)\s*)?(?:[\d,]+(?:\.\d+)?\s*(?:k|lakhs?|lacs?|thousand|crores?)?|"
                 r"(?:one|two|three|four|five|six|seven|eight|nine|ten|twenty|fifty|hundred|"
                 r"thousand|lakh|lakhs|crore)(?:\s+(?:hundred|thousand|lakhs?|crores?))*)")
_PERSONAL_REQUEST = [
    # "give me five thousand loan", "lend me 5000 rupees", "send us money"
    r"\b(?:give|lend|loan|send|grant|provide|pay|transfer|arrange|sanction|approve|release)\s+"
    r"(?:me|us)\s+(?:a\s+|an\s+|some\s+)?(?:" + _AMOUNT_WORDS + r"\s+)?(?:rs\.?\s+|rupees?\s+)?"
    + _MONEY_NOUN + r"\b",
    # "a loan for me", "money for my family"
    r"\b" + _MONEY_NOUN + r"\s+for\s+(?:me|myself|us|my\s+(?:family|son|daughter|wife|husband|"
    r"mother|father|children|kids))\b",
    # "I need a loan", "I want 5000 rupees", "can I get money"
    r"\b(?:i|we)\s+(?:need|want|require|urgently\s+need)\s+(?:a\s+|an\s+|some\s+)?"
    r"(?:" + _AMOUNT_WORDS + r"\s+)?(?:rs\.?\s+|rupees?\s+)?" + _MONEY_NOUN + r"\b",
    r"\bcan\s+(?:i|we)\s+(?:get|have|borrow)\s+(?:a\s+|an\s+|some\s+)?(?:" + _AMOUNT_WORDS
    + r"\s+)?(?:rs\.?\s+|rupees?\s+)?" + _MONEY_NOUN + r"\b",
    # "I am in crisis", "we are in debt"
    r"\b(?:i\s*(?:am|'m)|we\s*(?:are|'re))\s+in\s+(?:a\s+|great\s+|serious\s+|financial\s+)*"
    r"(?:crisis|debt|trouble|distress|emergency|need)\b",
]


# A request for help with something ILLEGAL or harmful — "i want to rob a bank,
# give me suggestions", "how to make fake job cards", "help me bribe the
# officer". Checked before everything else, including by the pipeline ahead of
# its own recommendation / pick steps: "give me some suggestions" in such a
# message was once read as a scheme recommendation and answered with the
# previous turn's scheme (reported 2026-09-24). Anchored on INTENT phrasing, so
# a legitimate question about wrongdoing ("how are fake job cards detected?",
# "fraud cases in MGNREGA social audits") is not refused.
_HARMFUL_INTENT = (
    r"(?:\bi\s+(?:want|wanna|plan|am\s+planning|need|intend|would\s+like)\s+to\b|"
    r"\bwe\s+(?:want|plan|need)\s+to\b|\bhow\s+(?:can|do|could|should|would)\s+(?:i|we|one|someone)\b|"
    r"\bhow\s+to\b|\bhelp\s+(?:me|us)\b|\bteach\s+me\b|\bshow\s+me\s+how\b|\bways?\s+to\b|"
    r"\btips?\s+(?:to|for|on)\b|\bideas?\s+(?:to|for)\b|\bsuggest\w*\s+(?:to|for|how)\b|"
    r"\bbest\s+way\s+to\b|\blet'?s\b|\bplan\s+to\b)")
_HARMFUL_ACT = (
    r"\b(?:rob\w*|loot\w*|steal\w*|burgl\w*|kidnap\w*|murder\w*|smuggl\w*|launder\w*|"
    r"forg(?:e|ed|ing|ery)\b|brib\w*|embezzl\w*|defraud\w*|swindl\w*|extort\w*|"
    r"counterfeit\w*|siphon\w*|hack\s+(?:into|the|a|an)\b|cheat\w*\s+(?:the\s+)?"
    r"(?:government|govt|scheme|bank|system|officer|people)\b|"
    r"(?:make|create|get|use|prepare)\s+(?:a\s+)?(?:fake|false|forged|duplicate)\s+\w+|"
    r"fake\s+(?:documents?|certificates?|job\s*cards?|aadhaa?r|ids?|beneficiar\w*|bills?)\b|"
    r"misuse\s+(?:the\s+)?(?:funds?|money|scheme)\b|evade\s+tax\w*|poison\w*|bomb\w*)")
_HARMFUL = [
    _HARMFUL_INTENT + r"[^.?!]{0,50}?" + _HARMFUL_ACT,
    r"\brob(?:bing|bed)?\s+(?:a\s+|the\s+)?bank\b|\bbank\s+robbery\b|\bmoney\s+laundering\b",
]


_CONFUSED = [
    r"^(i\s+don.?t\s+(know|understand)|huh|what\?|confused|i\s*m\s+confused|no\s+idea)[\s!.?]*$",
    r"^(help|help\s*me|i\s+need\s+help|guide\s*me|assist\s*me)[\s!.?]*$",
    r"^(hmm+|umm+|uh+|ok+|okay|k|kk|yeah|nah|sure|right|got\s*it|fine)[\s!.?]*$",
    r"^(start|begin|let.s\s*(start|begin|go)|go)[\s!.?]*$",
    r"^\?+$",
    # "what should I do?" / "what do I ask?" / "where do I start?" — a user
    # who wants to be pointed somewhere, not an off-topic question. These have
    # no domain vocabulary, so before this they fell through to the step-6
    # whitelist gate and got the blunt "I can only answer questions about those
    # schemes" bounce, which reads as a refusal to someone asking for guidance
    # (reported 2026-09-17). The _CONFUSED reply already says exactly what to
    # try, and carries the starter chips.
    r"^\W*what\s+(should|shall|can|do|would)\s+i\s+(do|ask|say|type|start\s+with|query)\b",
    r"^\W*(where|how)\s+(do|should|can)\s+i\s+(start|begin)\b",
    r"^\W*what\s*(next|now)\b[\s!.?]*$",
    r"^\W*(give|show|suggest)\s+(me\s+)?(some\s+)?(examples?|suggestions?|ideas?|options?)\b",
]

# Unmistakably-not-ours topics — blocked even if a place name (e.g. "Shillong")
# also appears, since the strong-intent early exit would otherwise wave them through.
_HARD_OFF_TOPIC = [
    r"\b(weather|temperature|forecast|rain(fall|y)?|humidity|horoscope|zodiac|astrology)\b",
    r"\b(bitcoin|crypto|sensex|nifty|share\s+price|stock\s+price)\b",
    r"\b(cricket|ipl|football|match\s+score|movie|film|song\s+lyrics)\b",
]
# ...unless the text also names a scheme outright, in which case route it.
_SCHEME_NAMED = re.compile(
    r"\b(mgnrega|mnrega|nrega|pmay[\s-]?g?|awaas|awas|focus[\s-]?plus|focusplus|"
    r"cm[\s-]?elevate|cmelevate|focus[\s-]?legacy|focuslegacy|producer[\s-]?groups?)\b|"
    r"focus\s*\+",
    re.IGNORECASE,
)

# ── Out-of-area — a place that is not in Meghalaya ─────────────────────────
# The assistant holds Meghalaya data only. A question anchored on another
# state, a neighbouring city, or "all-India" is out of scope even when it
# names a scheme and uses scheme vocabulary ("PMAY-G houses in Assam",
# "how many districts in Guwahati for PMAY-G") — so this is checked BEFORE
# the strong-intent early exit, and stands down only when a Meghalaya place
# is *also* named (e.g. "compare Meghalaya with Assam").
_OUT_OF_AREA = re.compile(
    r"\b("
    r"assam|arunachal(\s+pradesh)?|nagaland|manipur|mizoram|tripura|sikkim|"
    r"west\s+bengal|bengal|bihar|jharkhand|odisha|orissa|chhattisgarh|"
    r"madhya\s+pradesh|uttar\s+pradesh|uttarakhand|rajasthan|gujarat|"
    r"maharashtra|goa|karnataka|kerala|tamil\s*nadu|telangana|"
    r"andhra(\s+pradesh)?|punjab|haryana|himachal(\s+pradesh)?|"
    r"jammu|kashmir|ladakh|puducherry|pondicherry|chandigarh|"
    r"andaman|nicobar|lakshadweep|"
    r"guwahati|gauhati|dispur|silchar|dibrugarh|jorhat|tezpur|"
    r"kolkata|calcutta|new\s+delhi|delhi|mumbai|bombay|bengaluru|bangalore|"
    r"chennai|madras|hyderabad|pune|ahmedabad|jaipur|lucknow|kanpur|patna|"
    r"bhopal|indore|nagpur|visakhapatnam|"
    r"kohima|imphal|aizawl|agartala|itanagar|gangtok|dimapur|siliguri|"
    r"all[\s-]?india|pan[\s-]?india|nation[\s-]?wide|india|"
    r"across\s+the\s+country|whole\s+country|entire\s+country"
    r")\b",
    re.IGNORECASE,
)
# Any country/continent outside India — "how many beneficiaries in Madagascar"
# is exactly as out-of-scope as "... in Assam", but names no Indian place at
# all, so it needs its own list rather than living in _OUT_OF_AREA above.
# Not exhaustive (no regex list of world place names can be), but covers the
# countries/continents/generic phrasing QA and users actually ask about.
_FOREIGN_PLACE = re.compile(
    r"\b("
    r"madagasca\w*|nigeria|kenya|ethiopia|egypt|south\s+africa|morocco|ghana|"
    r"uganda|tanzania|zimbabwe|sudan|algeria|tunisia|libya|senegal|cameroon|"
    r"angola|mozambique|zambia|botswana|namibia|rwanda|"
    r"pakistan|bangladesh|nepal|bhutan|sri\s*lanka|myanmar|burma|"
    r"china|japan|north\s+korea|south\s+korea|vietnam|thailand|cambodia|laos|"
    r"malaysia|singapore|indonesia|philippines|mongolia|kazakhstan|uzbekistan|"
    r"afghanistan|iran|iraq|saudi\s+arabia|u\.?a\.?e\.?|dubai|abu\s+dhabi|qatar|"
    r"kuwait|oman|bahrain|israel|turkey|syria|yemen|lebanon|"
    r"united\s+kingdom|\buk\b|england|scotland|wales|ireland|france|germany|"
    r"italy|spain|portugal|netherlands|belgium|switzerland|austria|sweden|"
    r"norway|denmark|finland|poland|russia|ukraine|greece|hungary|romania|"
    r"czech(\s+republic)?|iceland|"
    # "u.s." must not be the start of a longer dotted word ("Umiong U.s.t", a
    # Focus Legacy producer group — all-PG run 2026-09-29)
    r"united\s+states(\s+of\s+america)?|\busa\b|\bu\.s(?:\.a)?(?=\.?(?:[\s,;:?!)]|$))|america|canada|"
    r"mexico|brazil|argentina|chile|peru|colombia|venezuela|cuba|"
    r"australia|new\s+zealand|\bfiji\b|"
    r"africa|europe|south\s+america|north\s+america|antarctica|"
    r"abroad|overseas|foreign\s+countr\w*|another\s+country|other\s+countr\w*|"
    r"outside\s+india"
    r")\b",
    re.IGNORECASE,
)
# A producer-group NAME the user labelled as one — "…named Rakkam China Banana
# Group", "members in Umden Manipur Banana Pg" — is not a place. Five stored
# Focus Legacy names hold a state or country word and were refused as
# out-of-area (all-PG run 2026-09-29). Masked only for the out-of-area test; a
# place written AFTER the name ("…producer groups in Assam") stays visible.
_GROUP_WORD = r"(?:producer\s+groups?|p\.?\s*g\.?s?|group)"
_NAMED_GROUP_SPAN = re.compile(
    rf"\b{_GROUP_WORD}\s+(?:named|called)\s+(?P<name>[^?]*?)\s*[?.!]*\s*$", re.IGNORECASE)
_MEMBERS_IN_GROUP_SPAN = re.compile(
    rf"\bmembers?\s+(?:are\s+|were\s+|is\s+)?(?:there\s+)?(?:in|of)\s+(?P<name>[^?]*?\b{_GROUP_WORD}"
    r"(?:[\s-]*\d+)?)\s*[?.!]*\s*$", re.IGNORECASE)
_PLACE_AFTER = re.compile(r"\s(?:in|from|of|at|within)\s", re.IGNORECASE)
# A Meghalaya VILLAGE can carry a state's name: "MANIPUR" is a village in Umling
# block, Ri Bhoi (Focus Legacy all-villages run 2026-09-29). "… for X village" is
# a village name, not the state.
_NAMED_VILLAGE_SPAN = re.compile(
    r"\b(?:in|for|of|at|to)\s+(?:the\s+)?(?P<name>[a-z0-9(&][\w().'&\- ]{0,60}?)\s+village\b", re.IGNORECASE)


# What a scheme / year chip appends after the name ("… Group for Focus Legacy
# across all financial years") — removed before the name spans are matched.
_CHIP_SCOPE_TAIL = re.compile(
    r"(?:\s*,?\s+(?:for|under|in|across)\s+(?:the\s+)?(?:focus\s+legacy|focus\s+plus|mgnrega|pmay-?g|"
    r"cm\s+elevate(?:\s+legacy)?|all\s+of\s+meghalaya|all\s+(?:the\s+)?(?:financial\s+)?years(?:\s+combined)?)"
    r"(?:\s+scheme)?)+\s*[?.!]*\s*$", re.IGNORECASE)


def _mask_group_name(ql: str) -> str:
    ql = _CHIP_SCOPE_TAIL.sub("", ql)
    ql = _NAMED_VILLAGE_SPAN.sub(lambda m: m.group(0).replace(m.group("name"), " "), ql)
    for rx in (_NAMED_GROUP_SPAN, _MEMBERS_IN_GROUP_SPAN):
        m = rx.search(ql)
        if m and not _PLACE_AFTER.search(" " + m.group("name") + " "):
            return ql[:m.start("name")] + " " + ql[m.end("name"):]
    return ql


_MEGHALAYA_PLACE = re.compile(
    r"\b("
    r"meghalaya|"
    r"(east|west|north|south|south\s+west|eastern\s+west)\s+(garo|khasi|jaintia)\s+hills?|"
    r"garo\s+hills?|khasi\s+hills?|jaintia\s+hills?|ri[\s-]?bhoi|ribhoi|"
    r"[ewns]\.?[gkj]\.?h|s\.?w\.?[gk]\.?h|e\.?w\.?k\.?h|"
    r"shillong|tura|jowai|nongpoh|baghmara|williamnagar|resubelpara|"
    r"nongstoin|mawkyrwat|ampati|khliehriat|mairang|nongthymmai|"
    r"mawsynram|sohra|cherrapunj\w*|dawki|nartiang|mawphlang"
    r")\b",
    re.IGNORECASE,
)

# ── Strong scheme intent (any of the four) — skip every edge check below ───
_SCHEME_STRONG = [
    r"\bmgnrega\b", r"\bmnrega\b", r"\bnrega\b", r"\bmgnregs\b",
    r"\bpmay\b", r"\bpmayg\b", r"\bpmay[\s-]?g\b", r"\bawaas\b", r"\bawas\b",
    r"\bfocus[\s-]?plus\b", r"\bfocusplus\b", r"focus\s*\+", r"\bproducer group",
    r"\bcm[\s-]?elevate\b", r"\bcmelevate\b", r"\bpiggery\b", r"\bpoultry\b",
    r"\bprime small enterprise\b", r"\bprime tourism vehicle\b", r"\bwarehouse scheme\b",
    r"\bsericulture\b", r"\bmotorcaravan\b", r"\bapplicant_?category\b", r"\bdata_?verified\b",
    r"\bmeghalayaone\b", r"\bmbda\b", r"\bdisbursement", r"\btranche\b", r"\bbatch_label\b",
    r"\bindira\s+awaas\b", r"\brural\s+hous", r"\bgramin\b",
    r"\bperson[\s-]?days?\b", r"\bmandays?\b", r"\bjob\s*card", r"\bmuster\b",
    r"\bwage", r"\bexpenditure\b", r"\bunskilled\b", r"\bsemi[\s-]?skilled\b",
    r"\bmaterial\s+(cost|exp)", r"\bhousehold", r"\bemployment\b",
    r"\b100\s*days?\b", r"\bhundred\s+days?\b", r"\blabour\s+budget\b",
    r"\bhouse(s)?\s+(sanction|complet|built|in\s+progress|pending)", r"\bsanction",
    r"\brelease", r"\bunspent\b", r"\butili[sz]ation\b", r"\binstal", r"\btranche\b",
    r"\bge[\s-]?tag", r"\bbeneficiar", r"\bpucca\b", r"\bkutcha\b",
    r"\bdistrict\b", r"\bblock\b", r"\bvillage\b", r"\bpanchayat\b", r"\bgram\s+panchayat\b",
    r"\bfinancial\s*year\b", r"\bfy\s*\d\d", r"\b(?:19|20|21)\d\d\s*[-/]\s*\d{2,4}\b",
    r"\b(?:19|20|21)\d\d\b", r"\bcrore\b", r"\blakh\b",
    r"\bmeghalaya\b", r"\bgaro\s+hills?\b", r"\bkhasi\s+hills?\b", r"\bjaintia\s+hills?\b",
    r"\bri[\s-]?bhoi\b", r"\bshillong\b", r"\btura\b", r"\bjowai\b", r"\bnongstoin\b",
    r"\bwilliamnagar\b", r"\bbaghmara\b", r"\bresubelpara\b", r"\bmairang\b",
    r"\bmawkyrwat\b", r"\bkhliehriat\b", r"\bnongpoh\b", r"\bampati\b",
    r"\beligib", r"\bscheme\b", r"\bcomponent", r"\bsubsidy\b", r"\bconvergence\b",
    r"\bhow\s+many\b", r"\btotal\b", r"\bcompare\b", r"\bcomparison\b", r"\btrend\b",
    r"\bby\s+(district|block|village|year)\b", r"\bcompletion\s+rate\b",
]

# ── Meta-conversation — about the chat itself, not the schemes ──────────────
# These carry no scheme keyword by nature; let them reach the pipeline (a full
# meta resolver is a pipeline TODO — for now they route like any other question).
# ── "Can you actually answer?" — a capability question, not a request ───────
# A user checking what the assistant is good for before committing to a real
# question: "are you able to give answers?", "can you answer my questions?",
# "if I ask about Meghalaya schemes, will you be able to answer?". These carry
# no scheme vocabulary of their own (or name the schemes only in passing, as
# the SUBJECT of the capability question rather than as something to query), so
# before this they took one of two wrong paths, both reported 2026-09-17:
#   * no domain word at all -> the step-6 whitelist gate -> the blunt "I can
#     only answer questions about those schemes" bounce, which reads as a
#     refusal to a question that was ABOUT that very capability; or
#   * the words "Meghalaya"/"schemes" present -> straight down the DATA path
#     -> the "Which scheme does your question concern?" pause, asking the user
#     to pick a scheme for a question that isn't asking for any scheme's data.
# The honest reply to all of them is a plain yes plus what's covered, so they
# get their own bank rather than being bent into _IDENTITY (which answers "who
# are you", a different question) or left to the LLM.
_CAPABILITY = [
    # "can/will/are you able to ... answer/help/tell/give"
    r"\b(can|could|will|would|are)\s+(you|u)\s+(be\s+)?(able\s+to\s+)?"
    r"(answer|reply|respond|help|assist|tell|give|provide|handle|do)\b",
    r"\b(are|r)\s+(you|u)\s+(able|capable)\b",
    r"\b(do|does)\s+(you|u)\s+(know|have|support|cover|handle)\b",
    r"\bis\s+it\s+possible\s+(for\s+(you|u)\s+)?to\s+(answer|help|get|find|know)\b",
    r"\b(you|u)\s+(can|could)\s+(answer|help|tell|give|provide)\b",
    r"\bwhat\s+(kind|type|sort)s?\s+of\s+(questions?|queries|things|data)\b",
    r"\bcan\s+i\s+(ask|get|know|find|query)\b",
    r"\bhow\s+(accurate|reliable|correct|trustworthy)\s+(are|is)\b",
]

# A capability phrase wrapped around an ACTUAL request — "can you tell me the
# total person-days in 2023-24?", "can you give me houses completed by
# district?" — is a real question, not a capability check. Any concrete metric,
# aggregate word, place or year makes it one, so it must reach the pipeline
# normally. Kept narrow on purpose: the bare "questions"/"answers"/"data" of a
# genuine capability check are NOT in here.
_ASKS_FOR_A_FIGURE = re.compile(
    # "tell me / explain / what is ... <scheme>" is a real KNOWLEDGE request,
    # even though it opens with a capability phrase ("can you tell me about
    # MGNREGA"). The scheme is the SUBJECT being asked about, not the topic of
    # a can-you check.
    r"\b(?:tell|explain|describe)\b[^?]{0,30}"
    r"\b(?:mgnrega|mnrega|nrega|pmay|awaas|awas|focus\s*\+?|focusplus|"
    r"cm\s*elevate|cmelevate|focus\s*legacy|focuslegacy|producer\s*groups?)\b|"
    r"\b(how\s+many|how\s+much|total|sum|count|number\s+of|average|avg|"
    r"person[\s-]?days?|expenditure|spend(?:ing)?|spent|wages?|job\s?cards?|"
    r"houses?|sanction\w*|complet\w*|disburs\w*|beneficiar\w*|applications?|"
    r"breakdown|compare|comparison|list\s+of|top\s+\d|highest|lowest|"
    r"by\s+(?:district|block|village|year)|district[\s-]?wise|block[\s-]?wise|"
    r"eligib\w*|documents?\s+required|who\s+can\s+apply|how\s+(?:do|to)\s+i\s+apply|"
    r"\bfy\s?20\d\d|\b20\d\d\b)\b",
    re.IGNORECASE,
)

_META_CONV = [
    r"\b(my|your)\s+(first|last|previous|prior|earlier)\s+(question|query|message|answer)",
    r"what\s+did\s+(i|you)\s+(ask|say|answer|tell|reply)",
    r"what\s+was\s+(my|your|the)\s+(question|answer|response|last|first)",
    r"(repeat|rephrase|restate|say\s+again)\s+(my|the|that|your|it)\b",
    r"(summari[sz]e|summary\s+of)\s+(our|this|the)\s+(conversation|chat|discussion)",
    r"what\s+have\s+(we|i)\s+(discussed|talked|covered|asked)",
]

# ── Follow-up fragments — reference a prior answer via pronouns / arithmetic ──
# No scheme keyword, but they only mean something in context — the pipeline's
# follow-up rewrite handles them, so never block these here.
_FOLLOWUP = [
    r"\b(sum|total|add|plus|combined?|altogether)\b.{0,30}\b(both|them|these|those|two|it)\b",
    r"\b(both|them|these|those)\b.{0,30}\b(sum|total|added?|plus|combined?|together|altogether)\b",
    r"(what|how\s+much).{0,20}(together|combined|altogether|in\s+total)\b",
    r"\b(difference|gap|subtract|minus)\b.{0,30}\b(both|them|these|those|two|the\s+other)\b",
    r"which\s+(is|one\s+is|are|has)\s+(the\s+)?(more|most|less|least|higher|highest|lower|lowest|bigger|biggest|smaller|smallest|greater|greatest|max|min)\b",
    r"(more|less|higher|lower|bigger|smaller|greater)\s+(of\s+)?(the\s+)?(two|both|them|these)\b",
    r"\b(in\s+this|from\s+(this|above|that|the\s+above)|of\s+these|among\s+these)\b",
    r"^(what\s+about|how\s+about|and|also|plus|what\s+of|what\s+if|now|then|ok(ay)?\s+and)\b.{0,45}$",
    r"^(now\s+)?(show|give|tell|calculate|compute|find|sort|order|rank|list)\s+(me\s+)?(both|them|the\s+total|the\s+sum|the\s+combined|by\s+\w+|for\s+\w+)\b",
    r"^(add|sum|combine|total)\s+(them|both|those|these)\s*(up)?[\s?]*$",
    r"^(and|but|so)\s+(the|what|for|in|by|about)\b.{0,45}$",
    r"^(how|why|when|where|what|who)\b.{0,45}\b(it|its|that|those|these|them|they|this(?:\s+one)?|the\s+same)\b.{0,15}[\s?]*$",
    r"^(explain|why|reason|elaborate|expand|clarify|correct|right|wrong|is\s+this|is\s+that|you\s+gave|you\s+said)\b",
    r"\b(is\s+this|is\s+that|is\s+it)\s+(correct|right|wrong|true|false|accurate|sure)\b",
    r"\b(you\s+gave|you\s+said|you\s+told|you\s+mentioned|you\s+showed)\b",
    r"^(yes|no|correct|wrong|exactly|not\s+right|that.?s\s+(right|wrong|correct|incorrect))\b",
]

# ── Domain whitelist — a question with NONE of these words is off-topic ─────
_DOMAIN_WORDS = [
    # scheme names + synonyms
    "mgnrega", "mnrega", "nrega", "pmay", "pmayg", "pmay-g", "awaas", "awas",
    "indira awaas", "employment guarantee", "rural housing", "rural employment",
    "focus plus", "focus+", "focusplus", "focus-plus", "producer group",
    "focus legacy", "focuslegacy", "producer groups", "pg id", "pg member",
    "pg-focus", "pg-lamp",
    # "PG" is Focus Legacy's everyday shorthand — the UI's own starter chips say
    # "Top 5 PGs" — but only the SPELLED-OUT forms were whitelisted, so
    # "is there any pg group with name sakania?" had no domain word and was
    # bounced as off-topic while the identical question with "producer group"
    # went through (reported 2026-09-23). Word-boundary, never a bare "pg":
    # these entries are used as unanchored regexes, and a loose "pg" matches
    # inside "upgrade", "upgrading" and "mpg".
    r"\bpgs?\b",
    "cm elevate", "cmelevate", "cm-elevate", "piggery", "poultry", "warehouse scheme",
    # CM Elevate Legacy's own vocabulary (lender and desanction fields).
    "lifcom", "desanction", "loan entity", "lender",
    "sericulture", "motorcaravan", "prime small enterprise", "prime tourism vehicle",
    "any business venture", "cinema theatre", "sports and wellness", "green taxi",
    "meghalayaone", "mbda", "meghalaya basin development", "farmer cash benefit",
    "benefit",
    # work / employment
    "beneficiar", "job card", "muster", "person day", "personday", "person-day",
    "manday", "man-day", "wage", "unskilled", "semi-skilled", "material cost",
    "100 day", "hundred day", "household", "worker", "labour", "labor",
    "employment", "works", "asset", "labour budget", "women employment",
    # housing
    "house", "housing", "dwelling", "pucca", "kutcha", "sanction", "geotag",
    "geo-tag", "geo tag", "installment", "instalment", "tranche", "completion certificate",
    "house status", "in progress", "in-progress",
    # geography
    "meghalaya", "district", "block", "village", "panchayat", "gram panchayat",
    "garo hills", "khasi hills", "jaintia hills", "ri bhoi", "ri-bhoi",
    "shillong", "tura", "jowai", "nongstoin", "williamnagar", "baghmara",
    "resubelpara", "mairang", "mawkyrwat", "khliehriat", "nongpoh", "ampati",
    "east khasi", "west khasi", "south west khasi", "east jaintia", "west jaintia",
    "east garo", "west garo", "north garo", "south garo", "south west garo",
    # finance
    "expenditure", "amount released", "amount sanctioned", "amount pending",
    "fund", "funds", "released", "pending", "unspent", "utilisation", "utilization",
    "crore", "lakh", "rupee", "budget", "disburs", "payment", "spend", "spent", "cost",
    # admin / rules
    "eligib", "eligible", "apply", "application", "documents", "aadhaar", "aadhar",
    "registration", "scheme", "component", "guidelines", "subsidy", "convergence",
    "financial year", "physical progress", "target", "achievement",
    "assistance", "unit cost", "per house", "wage rate", "notified wage",
    "secc", "socio economic", "priority list", "waitlist", "gram sabha",
    "social audit", "work demand", "demand for work", "payment delay",
    "bpl", "landless", "homeless", "scheduled caste", "scheduled tribe",
    "sc/st", "differently abled", "widow", "minority",
    # analytics
    "count", "total", "how many", "number of", "show", "list", "compare",
    "comparison", "trend", "breakdown", "distribution", "by district", "by block",
    "by year", "average", "percentage", "per cent", "percent", "rate",
    "completion rate", "top ", "highest", "lowest", "most", "least",
    "chart", "graph", "report", "data", "statistic", "summary",
    # time — any 4-digit year or FY-range form (in OR out of the data window, so
    # an out-of-bound year like "1999-20" reaches the pipeline's year guard
    # instead of being bounced here as off-topic)
    r"\b(?:19|20|21)\d\d\b", r"(?:19|20|21)\d\d\s*[-/]\s*\d{2,4}",
    "year", "month", "quarter", "period", "fiscal",
]


# ── Response templates ─────────────────────────────────────────────────────
STARTERS = [
    "Total MGNREGA person-days in Meghalaya in 2023-24",
    "PMAY-G houses completed by district",
    "Focus Plus payments by batch",
    "How many CM Elevate applications are on hold?",
    "Who is eligible for PMAY-G?",
]

# Which edge replies carry the starter chips (a plain "thanks" / "bye" should not).
_STARTER_KINDS = {"greeting", "identity", "capability", "money_advice", "profanity", "silly",
                  "off_topic", "confused", "personal_request"}

# One consistent line for every "that's not something I do" case — an unrelated
# topic, a general-knowledge question, or a place outside Meghalaya. Callers past
# the edge layer (the pipeline's OutOfScope handler) reuse it via out_of_scope().
_OUT_OF_SCOPE_REPLY = (
    "I'm Megh One AI, the assistant for Meghalaya's MGNREGA, PMAY-G, Focus Plus, "
    "CM Elevate, Focus Legacy (producer groups) and CM Elevate Legacy (sanctions and "
    "disbursements) schemes. I can only answer "
    "questions about those schemes and their data in Meghalaya — not other topics, "
    "other states, or places outside Meghalaya."
)

_RESPONSES = {
    "greeting": (
        "Hello! I answer questions about Meghalaya's MGNREGA, PMAY-G, Focus Plus, "
        "CM Elevate, Focus Legacy and CM Elevate Legacy data — person-days, expenditure, "
        "houses sanctioned and completed, Focus Plus disbursements, CM Elevate applications "
        "by scheme, Focus Legacy producer-group payments, CM Elevate Legacy subsidy and "
        "loans disbursed, district and block "
        "breakdowns — and general questions about how the schemes work."
    ),
    "identity": (
        "I can help you with Meghalaya's MGNREGA, PMAY-G, Focus Plus, CM Elevate, "
        "Focus Legacy and CM Elevate Legacy schemes — things like person-days and wage expenditure, houses "
        "sanctioned and completed, Focus Plus disbursements, CM Elevate applications, "
        "Focus Legacy payments to producer groups, CM Elevate Legacy sanctions and "
        "disbursements, district and block breakdowns, and "
        "eligibility or how-to-apply questions for any of these schemes. Just ask in "
        "plain language."
    ),
    # Answers the question that was actually asked — "can you?" — with a plain
    # yes first, then what that covers. Deliberately NOT the _OUT_OF_SCOPE_REPLY
    # ("I can only answer...", which reads as a refusal here) and not the
    # "which scheme?" pause (nothing is being queried yet).
    "capability": (
        "Yes — that's exactly what I'm here for. I can answer questions about "
        "Meghalaya's MGNREGA, PMAY-G, Focus Plus, CM Elevate, Focus Legacy and CM "
        "Elevate Legacy schemes, in two ways: the actual data (person-days, expenditure, houses "
        "sanctioned and completed, Focus Plus disbursements, CM Elevate applications, "
        "Focus Legacy producer-group payments, CM Elevate Legacy disbursements — by "
        "district, block, village or "
        "financial year), and how the schemes work (eligibility, benefits, documents, "
        "how to apply). Ask in plain language and I'll take it from there."
    ),
    # Personal financial advice. Says plainly that this is not what the
    # assistant does — a generic "I can only answer questions about those
    # schemes" never acknowledges that the question was about investing, and
    # reads as if the request was simply not understood.
    "money_advice": (
        "I can't advise on investing or saving your own money — I'm not a financial "
        "adviser, and that's outside what I do. I'm Megh One AI: I answer questions "
        "about Meghalaya's MGNREGA, PMAY-G, Focus Plus, CM Elevate and Focus Legacy "
        "schemes — who "
        "is eligible, what benefits they pay, how to apply, and the actual figures by "
        "district, block or year. If you'd like to know what any of those schemes "
        "offers, ask away."
    ),
    # A first-person request for money or a loan. Says plainly that the
    # assistant cannot give, lend or approve money, then where support exists.
    # Scheme facts only as the SME docs state them (CM Elevate Legacy records
    # subsidy and loans; MGNREGA pays wages for work; Focus Plus is a cash
    # benefit to farmers, not a loan).
    "personal_request": (
        "I'm sorry you're in a difficult spot. I can't give, lend or approve money — I'm "
        "Megh One AI, an assistant that reports Meghalaya's scheme data and explains how "
        "the schemes work; I can't process applications or payments. If you need support, "
        "these schemes may help: MGNREGA (paid work for rural households), CM Elevate "
        "(support for starting or growing an enterprise, with subsidy and bank or LIFCOM "
        "loans), PMAY-G (rural housing) and Focus Plus (a cash benefit for farmers — not a "
        "loan). Ask me \"who is eligible for CM Elevate?\" or \"how do I apply for "
        "MGNREGA?\" and I'll explain."
    ),
    "thanks": ("You're welcome. Ask me anything else about MGNREGA, PMAY-G, "
               "Focus Plus, CM Elevate, Focus Legacy or CM Elevate Legacy in Meghalaya."),
    "goodbye": "Thanks for using the Meghalaya scheme assistant. Come back any time.",
    "profanity": (
        "I'm here to help. I can answer MGNREGA, PMAY-G, Focus Plus, CM Elevate and "
        "Focus Legacy questions for Meghalaya — for example \"MGNREGA expenditure by "
        "district in 2024-25\" or \"documents needed to apply for PMAY-G\"."
    ),
    "silly": _OUT_OF_SCOPE_REPLY,
    "off_topic": _OUT_OF_SCOPE_REPLY,
    # Says plainly that it won't help — and why — then points at the lawful
    # help it CAN give, instead of an unrelated scheme answer.
    "harmful": (
        "I can't help with that — it's illegal and could cause real harm. I'm Megh One "
        "AI, and I only help with Meghalaya's government schemes. If money is the "
        "problem, there are lawful options I can explain: paid work under MGNREGA, "
        "housing help under PMAY-G, farm support under Focus Plus and Focus Legacy, or "
        "business support under CM Elevate."
    ),
    "confused": (
        "No problem. I can answer things like the count of PMAY-G houses completed "
        "in a district, total MGNREGA wage expenditure for a year, Focus Plus payments "
        "by batch, how many CM Elevate applications are on hold, how much Focus Legacy "
        "paid out to producer groups, or the eligibility criteria for a scheme."
    ),
}


# ── Scheme-specific capability reply ────────────────────────────────────────
# "can I get MGNREGA data?" names ONE scheme, so answering with the full
# four-scheme rundown — and then suggesting PMAY-G / Focus Plus / CM Elevate
# starters — reads as if the question wasn't listened to (reported 2026-09-17).
# When exactly one scheme is named, both the sentence and the chips narrow to
# it; a question naming none, or several, keeps the general reply unchanged.
_SCHEME_ALIASES = {
    "MGNREGA": re.compile(r"\b(mgnrega|mnrega|nrega)\b", re.IGNORECASE),
    "PMAY-G": re.compile(r"\b(pmay[\s-]?g?|awaas|awas)\b", re.IGNORECASE),
    "Focus Plus": re.compile(r"\b(focus[\s-]?plus|focusplus)\b|focus\s*\+", re.IGNORECASE),
    # Not when qualified as the Legacy dataset — that is its own entry below.
    "CM Elevate": re.compile(
        r"(?<!legacy )\b(cm[\s-]?elevate|cmelevate)\b(?![\s-]*(?:legacy|disbursements?)\b)",
        re.IGNORECASE),
    "CM Elevate Legacy": re.compile(
        r"\b(cm[\s-]?elevate|cmelevate)[\s-]*(legacy|disbursements?)\b|"
        r"\blegacy[\s-]+cm[\s-]?elevate\b|\belevate[\s-]?legacy\b",
        re.IGNORECASE),
    # Qualified spellings only — a bare "focus" names neither Focus scheme on its
    # own (see pipeline._is_ambiguous_focus), so narrowing on it would pick one
    # of the two at random.
    "Focus Legacy": re.compile(
        r"\b(focus[\s-]?legacy|focuslegacy|legacy[\s-]?focus|producer[\s-]?groups?)\b",
        re.IGNORECASE),
}

# What each scheme actually holds — the data side and the knowledge side — so
# the narrowed reply stays concrete instead of a generic "yes I cover it".
_SCHEME_CAPABILITY = {
    "MGNREGA": (
        "person-days, households and persons employed, job cards, 100-day "
        "completions, and wage / material / total expenditure — by district, "
        "block, village, assembly constituency or financial year"
    ),
    "PMAY-G": (
        "houses sanctioned, completed and in progress, construction stage, "
        "sanctioned and released amounts, and installments — by district, "
        "block, village or financial year"
    ),
    "Focus Plus": (
        "disbursements and amounts paid, beneficiaries, batches and tranches, "
        "bank-wise payments, and the 12.5K cohort's status / gender / "
        "occupation splits — by district, block, village or financial year"
    ),
    "CM Elevate": (
        "applications across its 15 sub-schemes (piggery, poultry, tourism "
        "vehicles, small enterprise and more), application mode, workflow "
        "level and verification status — by district, block or sub-scheme"
    ),
    "Focus Legacy": (
        "payments to producer groups, the groups themselves and their member "
        "counts, amount remitted (Rs 5,000 per member), producer group type, "
        "the product each group works on, and bank-wise payments — by district, "
        "block, village or financial year"
    ),
    "CM Elevate Legacy": (
        "CM-ELEVATE sanction and disbursement records across 13 schemes — amounts "
        "sanctioned, subsidy and loans disbursed, the share of the sanction paid out, "
        "lender (Bank / LIFCOM) and desanctioned records — by scheme, district, block, "
        "village or financial year (FY 2024-25 and 2025-26)"
    ),
}

_SCHEME_STARTERS = {
    "MGNREGA": [
        "Total MGNREGA person-days in Meghalaya in 2023-24",
        "MGNREGA expenditure by district",
        "How many MGNREGA job cards were issued in Ri Bhoi?",
        "Which block recorded the most MGNREGA person-days?",
        "Who is eligible for MGNREGA?",
    ],
    "PMAY-G": [
        "PMAY-G houses completed by district",
        "How many PMAY-G houses were sanctioned in 2023-24?",
        "PMAY-G amount released by district",
        "How many PMAY-G houses are still in progress?",
        "Who is eligible for PMAY-G?",
    ],
    "Focus Plus": [
        "Focus Plus payments by batch",
        "Total Focus Plus amount disbursed by district",
        "How many Focus Plus beneficiaries are there?",
        "Focus Plus disbursement by tranche",
        "Who is eligible for Focus Plus?",
    ],
    "CM Elevate": [
        "How many CM Elevate applications are on hold?",
        "CM Elevate applications by sub-scheme",
        "Which district has the most CM Elevate applications?",
        "CM Elevate applications by district",
        "Who is eligible for CM Elevate?",
    ],
    "Focus Legacy": [
        "How many producer groups were paid under Focus Legacy?",
        "Total Focus Legacy amount disbursed by district",
        "How many PG members were covered under Focus Legacy?",
        "Which products do Focus Legacy producer groups work on?",
        "What is the main objective of FOCUS?",
    ],
    "CM Elevate Legacy": [
        "What is the total amount disbursed under CM Elevate Legacy?",
        "CM Elevate Legacy disbursement by district",
        "CM Elevate Legacy records by scheme",
        "CM Elevate Legacy loans by lender",
        "CM Elevate Legacy records by financial year",
    ],
}


def _named_scheme(question: str) -> "str | None":
    """The single scheme named in `question`, or None when it names none or
    more than one (both of which want the general four-scheme reply)."""
    hits = [name for name, rx in _SCHEME_ALIASES.items() if rx.search(question or "")]
    return hits[0] if len(hits) == 1 else None


def _capability_reply(scheme: str) -> str:
    return (
        f"Yes — I can answer {scheme} questions for Meghalaya. That covers the "
        f"actual data ({_SCHEME_CAPABILITY[scheme]}), and how the scheme works "
        "(eligibility, benefits, documents, how to apply). Ask in plain language "
        "and I'll take it from there."
    )


# A yes/no question deserves a yes/no answer. "will you answer for West Bengal
# data?" / "can you give Assam figures?" / "do you cover Delhi?" are all asking
# whether the assistant CAN — so the reply has to open with "No", not with a
# statement of scope that leaves the question hanging.
_YES_NO_ASK = re.compile(
    r"^\W*(?:can|could|will|would|do|does|are|is|shall|may)\b",
    re.IGNORECASE,
)


# Places whose natural spelling isn't plain Title Case, plus the "not a place"
# scopes (_OUT_OF_AREA also matches "all india" / "nationwide", which name a
# COVERAGE rather than a state — "I don't hold data for All India" reads wrong).
_PLACE_SPELLING = {
    "usa": "the USA", "us": "the US", "uk": "the UK", "uae": "the UAE",
    "ncr": "the NCR", "j&k": "Jammu & Kashmir",
}
_NATIONAL_SCOPE = {
    "all india", "all-india", "pan india", "pan-india", "nationwide",
    "national", "whole country", "entire country", "across india", "india",
}


def _title_place(place: str) -> str:
    """'west bengal' -> 'West Bengal'; keeps acronyms and known spellings."""
    p = str(place).strip().lower()
    if p in _PLACE_SPELLING:
        return _PLACE_SPELLING[p]
    return " ".join(w if w.isupper() else w.capitalize() for w in str(place).split())


def _out_of_area_reply(question: str, place: str) -> str:
    """The off-topic reply, but naming the place the user actually asked about
    and answering a yes/no question as one."""
    national = str(place).strip().lower() in _NATIONAL_SCOPE
    name = _title_place(place)
    if national:
        # "all India" / "nationwide" is a coverage, not a place — phrase it as
        # a scope the assistant doesn't have rather than a missing state.
        lead = ("No — I can't give all-India figures. " if _YES_NO_ASK.match(question or "")
                else "I don't hold all-India figures. ")
    elif _YES_NO_ASK.match(question or ""):
        lead = f"No — I can't answer questions about {name}. "
    else:
        lead = f"I don't hold any data for {name}. "
    return (
        lead + "I'm Megh One AI, and I only cover Meghalaya's MGNREGA, PMAY-G, "
        "Focus Plus, CM Elevate, Focus Legacy and CM Elevate Legacy schemes — the data is "
        "Meghalaya's "
        "alone, so I have nothing for other states or countries. If there's something "
        "you'd like to know about these six schemes in Meghalaya, I can help with that."
    )


def _NAMES_UNSUPPORTED_SCHEME(question: str) -> bool:
    """True when the text names a real government scheme this assistant doesn't
    hold (PM-KISAN, Ujjwala, Jal Jeevan, …).

    The catalogue of those lives in app.pipeline (_UNSUPPORTED_SCHEME), which
    imports this module — so the import is done lazily, inside the call, to
    avoid a cycle at module load. Any failure degrades to False, i.e. exactly
    the behaviour before this check existed."""
    try:
        from app import pipeline as _pipeline

        return _pipeline._unsupported_scheme_named(question) is not None
    except Exception:  # noqa: BLE001 — never let this break edge detection
        return False


def _edge(kind: str, question: str = "", place: str = "") -> dict:
    out = {"type": kind, "response": _RESPONSES[kind]}
    # A capability question that names ONE scheme gets that scheme's answer and
    # that scheme's suggestions, rather than the whole catalogue.
    scheme = _named_scheme(question) if kind == "capability" else None
    if scheme:
        out["response"] = _capability_reply(scheme)
    # An out-of-area question gets the place it named, and a direct "No" when
    # it was phrased as a yes/no question.
    if place:
        out["response"] = _out_of_area_reply(question, place)
    # Any other refusal asked as a yes/no question ("will you answer questions
    # about cricket?", "do you know about bitcoin?") gets the "No" prefixed, so
    # the reply answers what was asked before stating what IS covered.
    elif kind in ("off_topic", "silly") and _YES_NO_ASK.match(question or ""):
        out["response"] = "No — that's outside what I cover. " + out["response"]
    if kind in _STARTER_KINDS:
        out["suggestions"] = list(_SCHEME_STARTERS[scheme]) if scheme else list(STARTERS)
    return out


def out_of_scope() -> dict:
    """The canonical off-topic / out-of-area reply ("I'm Megh One AI …"), for
    callers past the edge layer — e.g. the pipeline raising OutOfScope after it
    resolves a district/block that is not in Meghalaya."""
    return _edge("off_topic")


def is_personal_request(question: str) -> bool:
    """A first-person request for money or a loan ("give me five thousand
    loan for me"), see _PERSONAL_REQUEST."""
    ql = (question or "").lower()
    return any(re.search(p, ql) for p in _PERSONAL_REQUEST)


def has_domain_vocabulary(question: str) -> bool:
    """True when the text carries any scheme / place / measure word the
    whitelist gate (step 6 below) or the scheme-intent exit (step 1) knows.
    Used by pipeline's continuation check: a message with none of these
    cannot continue a scheme conversation on vocabulary alone."""
    ql = (question or "").lower()
    return (any(re.search(w, ql) for w in _DOMAIN_WORDS)
            or any(re.search(p, ql) for p in _SCHEME_STRONG))


def detect_harmful(question: str) -> dict | None:
    """The refusal for a request for help with something illegal or harmful,
    or None. Separate from detect_edge_case so the pipeline can run it FIRST —
    ahead of its own recommendation / pick steps, which otherwise claim
    "give me some suggestions" before the edge layer is reached."""
    ql = (question or "").lower()
    if any(re.search(p, ql) for p in _HARMFUL):
        return {"type": "harmful", "response": _RESPONSES["harmful"]}
    return None


def detect_edge_case(question: str, has_context: bool = False) -> dict | None:
    """`has_context`: True when there's a live prior scheme answer this turn
    could plausibly be a follow-up to (see pipeline._run_pipeline's
    `has_antecedent`). Only relaxes step 6, the blanket "no domain vocabulary
    anywhere -> off-topic" whitelist gate — every explicit block above it
    (hard off-topic, out-of-area, greeting/silly/profanity/etc.) still fires
    regardless of context. Without this, a short, legitimate follow-up that
    happens to use no scheme-specific word of its own ("What are the
    benefits?", right after discussing MGNREGA) gets blocked here, before the
    pipeline's own follow-up rewrite ever runs — which also breaks the
    antecedent chain for the NEXT turn, since this turn's route becomes
    "edge" instead of "knowledge"/"data"."""
    q = (question or "").strip()
    if len(q) < 2:
        return _edge("confused")

    ql = q.lower()

    # 00. A request for help with something illegal or harmful — refused before
    #     anything else, even when it also names a scheme.
    harmful = detect_harmful(q)
    if harmful:
        return harmful

    # 0. Hard off-topic (weather, markets, sport, film) wins even over a place
    #    name — unless a scheme is actually named.
    if not _SCHEME_NAMED.search(ql) and any(re.search(p, ql) for p in _HARD_OFF_TOPIC):
        return _edge("off_topic", ql)

    # 0b. Out-of-area — anchored on a place outside Meghalaya (another state, a
    #     neighbouring city, "all-India", or a foreign country/continent).
    #     Beats the scheme-intent early exit, unless a Meghalaya place is named
    #     too ("Meghalaya vs Assam").
    _ql_area = _mask_group_name(ql)
    _area = _OUT_OF_AREA.search(_ql_area) or _FOREIGN_PLACE.search(_ql_area)
    if _area and not _MEGHALAYA_PLACE.search(ql):
        # Name the place that was matched, and — when the user asked a yes/no
        # question ("will you answer for West Bengal data?") — open with the
        # "No" that actually answers it. The generic reply below states what
        # the assistant covers but never responds to the question that was
        # asked, which reads as evasive (reported 2026-09-17).
        return _edge("off_topic", ql, place=_area.group(0))

    # 0c. "Can you actually answer?" — a question ABOUT the assistant's
    #     capability, not a request for data. Checked BEFORE the scheme-intent
    #     exit below: "if I ask questions on Meghalaya schemes, will you be able
    #     to give answers?" names the schemes, so step 1 would wave it through
    #     to the DATA path, which then asks "which scheme?" for a question that
    #     is not asking for any scheme's data (reported 2026-09-17). A message
    #     that pairs a capability phrase with a REAL query ("can you tell me
    #     MGNREGA person-days in 2023-24?") is excluded by the metric/aggregate
    #     check, so those still route normally.
    #     A capability phrase wrapped around an OFF-TOPIC subject ("can you help
    #     me book a flight?", "will you answer questions about cricket?") is not
    #     a capability check either — answering "Yes, that's what I'm here for"
    #     to those is plainly wrong, so they fall through to the off-topic banks
    #     below, which now answer them with a direct "No".
    if (any(re.search(p, ql) for p in _CAPABILITY)
            and not _ASKS_FOR_A_FIGURE.search(ql)
            and not any(re.search(p, ql) for p in _SILLY)
            and not any(re.search(p, ql) for p in _OFF_TOPIC)):
        return _edge("capability", ql)

    # 0d. Personal financial advice ("where should I invest my money one
    #     lakh?"). Checked BEFORE the scheme-intent exit below, for the same
    #     reason the out-of-area check is: _SCHEME_STRONG counts a bare money
    #     unit — "lakh", "crore" — as proof of scheme intent, so this question
    #     exited straight to the pipeline and came back with the "which scheme
    #     does your question concern?" picker instead of being declined
    #     (reported 2026-09-18). Anchored on the ADVICE phrasing, never on the
    #     money noun, so "total expenditure in lakh" is unaffected. A question
    #     that also names a scheme outright is left alone — "can MGNREGA wages
    #     help me save" is a scheme question, however it is phrased.
    if not _SCHEME_NAMED.search(ql) and any(re.search(p, ql) for p in _MONEY_ADVICE):
        return _edge("money_advice", ql)

    # 0e. A personal request for money ("give me five thousand loan for me, i
    #     am in crisis"). Before the scheme-intent exit for the same reason as
    #     0d ("lakh" alone reads as scheme intent), and whatever scheme was
    #     discussed before: the assistant can give nobody a loan, in any scheme.
    if is_personal_request(ql):
        return _edge("personal_request", ql)

    # 1. Clear scheme intent → straight to the pipeline, skip every check.
    if any(re.search(p, ql) for p in _SCHEME_STRONG):
        return None

    # 2. Canned-reply banks, most specific first.
    # An off-topic SUBJECT beats the conversational openers above it. "can you
    # help me book a flight?" matches _IDENTITY's generic "can you help me"
    # pattern, which would answer with the friendly capabilities rundown and
    # never decline the thing actually asked for; the flight is what the
    # message is about, so the off-topic banks take it first.
    for bank, kind in ((_SILLY, "silly"), (_OFF_TOPIC, "off_topic")):
        if any(re.search(p, ql) for p in bank):
            return _edge(kind, ql)

    for bank, kind in (
        (_GREETINGS, "greeting"),
        (_IDENTITY, "identity"),
        (_THANKS, "thanks"),
        (_GOODBYE, "goodbye"),
        (_PROFANITY_REDIRECT, "profanity"),
        (_SILLY, "silly"),
        (_OFF_TOPIC, "off_topic"),
        (_CONFUSED, "confused"),
    ):
        if any(re.search(p, ql) for p in bank):
            return _edge(kind, ql)

    # 3. Meta-conversation ("what did I ask before?") → let the pipeline see it.
    if any(re.search(p, ql) for p in _META_CONV):
        return None

    # 4. Context-dependent follow-up fragments ("add them up", "which is higher",
    #    "explain that") → the pipeline's follow-up rewrite handles these.
    if any(re.search(p, ql) for p in _FOLLOWUP):
        return None

    # 5. Non-English (Khasi / Garo / Bengali / Hindi / Assamese script) → the
    #    model can read these; let it try.
    if sum(1 for c in q if ord(c) > 127) >= 3:
        return None

    # 6. Whitelist gate: no MGNREGA / PMAY-G vocabulary anywhere → off-topic.
    #    This is what stops "what is elon musk?" from ever reaching a model.
    #    Skipped when there's a live prior scheme answer to be a follow-up to —
    #    see the has_context note in the docstring above.
    # A question naming a real government scheme we simply don't hold ("what is
    # PM Kisan Yojana?") has no MGNREGA/PMAY-G vocabulary either, so this gate
    # bounced it with the blunt "I can only answer questions about those
    # schemes" — which never says the subject IS a scheme, just not one of the
    # four (reported 2026-09-18). Let it through: the pipeline's
    # _unsupported_scheme_named check names the scheme, explains the gap and
    # offers the four as one-tap chips. That is a far better answer than this
    # gate can give, and it is still a canned reply — no model call.
    if _NAMES_UNSUPPORTED_SCHEME(ql):
        return None
    if not has_context and not any(re.search(w, ql) for w in _DOMAIN_WORDS):
        return _edge("off_topic", ql)

    return None
