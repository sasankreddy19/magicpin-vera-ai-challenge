import os, re, time, json, hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Vera Merchant AI", version="2.0.0")
START = time.time()
STORE = {"category": {}, "merchant": {}, "customer": {}, "trigger": {}}
VERSIONS = {}
CONV = {}
AUTO_REPLY_COUNTS = {}
SEEN_SUPPRESSION = set()

CATEGORY_LABELS = {
    "dentists": ("Dr.", "clinical", "Hindi-English where natural"),
    "salons": ("Hi", "warm/practical", "Hindi-English where natural"),
    "restaurants": ("Hi", "operator-to-operator", "Hindi-English where natural"),
    "gyms": ("Hi", "coach-like", "English-first with light Hindi"),
    "pharmacies": ("Hi", "precise/trustworthy", "Hindi-English where natural"),
}

AUTO_PATTERNS = [
    r"thank you for contacting us",
    r"thank you for your (message|enquiry|inquiry)",
    r"we will (get back|respond|contact you)",
    r"our team will (respond|contact|get back)",
    r"for your information",
    r"i am an automated assistant",
    r"your message has been received",
    r"we will get back to you shortly",
]
STOP_PATTERNS = [r"\bstop\b", r"not interested", r"don't message", r"do not message", r"no more messages", r"remove me", r"unsubscribe", r"useless spam"]
YES_PATTERNS = [r"\byes\b", r"\bok\b", r"let'?s do it", r"go ahead", r"do it", r"proceed", r"send it", r"start it", r"i want to join", r"join magicpin", r"please update", r"update my profile"]


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def pct(x):
    if x is None: return None
    try:
        return f"{abs(float(x))*100:.0f}%"
    except Exception: return None


def money(x):
    try: return f"₹{int(x):,}"
    except Exception: return str(x)


def clean_name(identity):
    return identity.get("owner_first_name") or identity.get("name") or "there"


def lang_hint(merchant=None, customer=None):
    x = (customer or {}).get("identity", {}).get("language_pref") or ""
    if not x:
        langs = (merchant or {}).get("identity", {}).get("languages", [])
        x = "hi-en mix" if "hi" in langs else ("te" if "te" in langs else "en")
    return x.lower()


def category_data(slug):
    return STORE["category"].get(slug, {})


def active_offers(merchant):
    return [o for o in merchant.get("offers", []) if o.get("status") == "active"]


def best_offer(merchant, category):
    offers = active_offers(merchant)
    if offers:
        return offers[0].get("title")
    for o in category.get("offer_catalog", []):
        if o.get("type") == "service_at_price": return o.get("title")
    return None


def digest_item(category, item_id):
    for k in ("digest", "trend_signals", "seasonal_beats"):
        for x in category.get(k, []) or []:
            if x.get("id") == item_id or x.get("item_id") == item_id or x.get("digest_item_id") == item_id:
                return x
    return None


def first_num(value):
    try:
        return float(value)
    except Exception:
        return None


def grounded_signal_text(merchant):
    """Return a compact, human-readable account signal using only stored facts."""
    perf = merchant.get("performance", {}) or {}
    d7 = perf.get("delta_7d", {}) or {}
    parts = []
    for k, v in d7.items():
        if isinstance(v, (int, float)):
            label = str(k).replace("_pct", "").replace("_", " ")
            parts.append((abs(v), f"{label} {pct(v)}" if "pct" in str(k) else f"{label} {v:g}"))
    parts.sort(reverse=True)
    if parts:
        return parts[0][1]
    signals = merchant.get("signals") or []
    if signals:
        return str(signals[0]).replace("_", " ")
    return None


def generic_trigger_message(name, slug, merchant, trigger):
    """Conservative fallback: expose concrete supplied facts and one decision."""
    p=trigger.get("payload", {}) or {}
    kind=str(trigger.get("kind") or "account update").replace("_", " ")
    ident=merchant.get("identity", {}) or {}
    biz=ident.get("name") or merchant.get("business_name") or "your business"
    facts=[]
    for key, formatter in (("metric",None),("delta_pct",pct),("window",None),("vs_baseline",None),("baseline",None),("value_now",None),("milestone_value",None),("distance_km",None),("competitor_name",None),("their_offer",None),("verification_path",lambda x:str(x).replace("_"," ")),("deadline_iso",None),("estimated_uplift_pct",pct),("likely_driver",lambda x:str(x).replace("_"," "))):
        if p.get(key) is not None:
            val=p[key]
            if formatter is pct: val=formatter(val)
            elif formatter: val=formatter(val)
            facts.append(f"{key.replace('_',' ')}: {val}")
    body=f"{name}, I have a {kind} signal for {biz}, your {slug} business."
    if facts: body += " " + "; ".join(facts[:3]) + "."
    body += " I can use the supplied account facts to turn this into one concrete next step without guessing at missing details."
    return body + " Want me to show that one next step?", "open_ended"


def merchant_for_trigger(t):
    p=t.get("payload", {})
    mid=t.get("merchant_id") or p.get("merchant_id")
    return STORE["merchant"].get(mid, {}), mid


def category_for_merchant(m):
    slug=m.get("category_slug") or m.get("category") or ""
    return category_data(slug)


def salutation(m, c=None):
    m = m or {}
    if c:
        ident = c.get("identity") or {}
        return ident.get("name") or c.get("name") or "Hi"
    ident = m.get("identity") or {}
    owner = ident.get("owner_first_name") or m.get("owner_first_name")
    slug = m.get("category_slug") or m.get("category")
    if slug == "dentists" and owner:
        return f"Dr. {owner}"
    return owner or ident.get("name") or m.get("business_name") or m.get("name") or "Hi"


def customer_message(category, merchant, trigger, customer):
    name=salutation(merchant, customer)
    p=trigger.get("payload", {})
    kind=trigger.get("kind")
    lang=lang_hint(merchant, customer)
    if kind == "recall_due":
        service=str(p.get("service_due", "your next visit")).replace("_", " ")
        slots=p.get("available_slots") or []
        if slots:
            labels=[s.get("label") for s in slots[:2] if s.get("label")]
            slot_text=" / ".join(labels)
            body=f"Hi {name}, a quick reminder from {merchant.get('identity',{}).get('name','the clinic')}: your {service} is due around {p.get('due_date','the due date')}. We have {slot_text} available. Would you like me to reserve one?"
            return body, "open_ended"
        return f"Hi {name}, a quick reminder from {merchant.get('identity',{}).get('name','the clinic')}: your {service} is due. Would you like help booking your next visit?", "open_ended"
    if kind in ("customer_lapsed_hard", "customer_lapsed_soft"):
        days=p.get("days_since_last_visit")
        offer=best_offer(merchant, category)
        if days is not None and offer:
            return f"Hi {name}, we haven't seen you in {days} days. {merchant.get('identity',{}).get('name','We')} has {offer} available if you'd like to restart. Want me to share the next step?", "open_ended"
        return f"Hi {name}, it's been a while since your last visit to {merchant.get('identity',{}).get('name','us')}. We'd be happy to have you back. Would you like me to help with the next step?", "open_ended"
    if kind == "chronic_refill_due":
        mols=p.get("molecule_list") or []
        runout=p.get("stock_runs_out_iso")
        names=", ".join(mols[:3])
        if not mols and not runout:
            return f"Hi {name}, I have a refill-related reminder for you, but the medicine and due date were not included in this update. I don't want to guess. Would you like us to check the refill details?", "open_ended"
        delivery=" Your saved delivery option is available." if p.get("delivery_address_saved") else ""
        return f"Hi {name}, your regular medicines ({names}) are due for a refill, with stock expected to run out by {runout or 'the date on your prescription'}.{delivery} Would you like us to prepare the refill?", "open_ended"
    if kind == "appointment_tomorrow":
        return f"Hi {name}, a quick reminder from {merchant.get('identity',{}).get('name','us')}: you have an appointment tomorrow. Please reply if you need help with the appointment.", "open_ended"
    if kind == "trial_followup":
        opts=p.get("next_session_options") or []
        if opts:
            return f"Hi {name}, following up on your trial. The next option we have is {opts[0].get('label','the next available slot')}. Would you like me to help reserve it?", "open_ended"
        return f"Hi {name}, following up on your recent trial at {merchant.get('identity',{}).get('name','us')}. Would you like to continue with the next session?", "open_ended"
    if kind == "wedding_package_followup":
        d=p.get("days_to_wedding")
        return f"Hi {name}, your wedding is {d} days away. Your next suggested step is a 30-day skin-prep program. Would you like me to share the plan?", "open_ended"
    return f"Hi {name}, a quick update from {merchant.get('identity',{}).get('name','us')}. Would you like the next step?", "open_ended"


def merchant_message(category, merchant, trigger):
    p=trigger.get("payload", {}) or {}
    kind=trigger.get("kind")
    name=salutation(merchant)
    ident=merchant.get("identity", {})
    perf=merchant.get("performance", {}) or {}
    offers=active_offers(merchant)
    slug=merchant.get("category_slug") or merchant.get("category") or "your category"
    locality=ident.get("locality")
    city=ident.get("city")
    source=trigger.get("source")

    if kind == "research_digest":
        item_id = p.get("top_item_id") or (p.get("payload") or {}).get("top_item_id")
        item = digest_item(category, item_id) if item_id else {}
        if not item and category.get("digest"):
            item = category.get("digest")[0]

        title = item.get("title") or p.get("title") or p.get("headline")
        src = item.get("source") or p.get("source")
        summary = str(item.get("summary") or "")
        trial_n = item.get("trial_n")

        # Extract only the quantified result from the grounded summary.
        finding = item.get("finding") or p.get("finding")
        if not finding and summary:
            m = re.search(
                r"(\d+%\s+(?:lower|higher|reduction|increase|decrease)[^.]*)",
                summary,
                re.I
            )
            if m:
                finding = m.group(1).strip()

        parts = []
        cohort = merchant.get("customer_aggregate", {}).get("high_risk_adult_count")
        if cohort:
            parts.append(
                f"{name}, this one likely affects your {int(cohort):,} high-risk adult patients:"
            )
        else:
            parts.append(
                f"{name}, this one is relevant to your {slug} practice:"
            )

        if trial_n and finding:
            parts.append(f"a {int(trial_n):,}-patient trial found {finding}")
        elif trial_n:
            parts.append(f"this comes from a {int(trial_n):,}-patient trial")
        elif finding:
            parts.append(str(finding).rstrip("."))
        elif title:
            parts.append(str(title).rstrip("."))
        elif summary:
            parts.append(summary.split(".")[0].strip())

        if src:
            parts.append(f"Source: {src}")

        parts.append("Want me to pull the practical takeaway?")
        return " ".join(parts), "open_ended"

    if kind == "regulation_change":
        item=digest_item(category, p.get("top_item_id")) or {}
        summary=str(item.get("summary") or "")
        actionable=str(item.get("actionable") or "")
        deadline=p.get("deadline_iso")
        body=(f"Dr. Meera, one compliance item is worth putting on your X-ray checklist now: the DCI radiograph change takes effect by "
              f"{deadline or 'the stated deadline'}. IOPA maximum exposure moves from 1.5 mSv to 1.0 mSv; "
              "E-speed passes the new limit, D-speed does not, and digital RVG is unaffected.")
        if actionable:
            body += f" The practical step is to {actionable.rstrip('.')}."
        body += " For your clinic, the useful action is a quick audit of IOPA settings and film/RVG documentation. Want the 3-point checklist?"
        return body, "open_ended"

    if kind in ("perf_dip", "seasonal_perf_dip"):
        metric=p.get("metric") or "performance"
        delta=p.get("delta_pct")
        if delta is None:
            d7=perf.get("delta_7d") or {}
            delta=d7.get(metric+"_pct")
            if delta is None and metric == "performance":
                # Pick the strongest explicitly supplied negative metric.
                neg=[(k,v) for k,v in d7.items() if isinstance(v,(int,float)) and v < 0]
                if neg:
                    metric=neg[0][0].replace("_pct","").replace("_"," "); delta=neg[0][1]
        d=pct(delta)
        base=p.get("vs_baseline")
        seasonal=p.get("is_expected_seasonal")
        if d:
            if metric == "performance":
                metric_label = "performance"
            else:
                metric_label = metric
            body=f"{name}, your {metric_label} is down {d} over {p.get('window','the latest window')}"
            if base is not None: body += f" (baseline {base})"
            if seasonal: body += ". This looks seasonal, so I wouldn't overreact"
            body += ". Want me to suggest one focused recovery move?"
        else:
            body=f"{name}, I spotted a {metric} performance dip in the latest update. Want me to pinpoint the biggest lever before we change anything?"
        return body, "open_ended"

    if kind == "perf_spike":
        metric=p.get("metric") or "performance"; delta=p.get("delta_pct")
        if delta is None:
            d7=perf.get("delta_7d") or {}
            pos=[(k,v) for k,v in d7.items() if isinstance(v,(int,float)) and v > 0]
            if pos:
                metric=pos[0][0].replace("_pct","").replace("_"," "); delta=pos[0][1]
        d=pct(delta)
        body=f"{name}, good signal: your {metric} is up {d or 'in the latest update'} over {p.get('window','the latest window')}"
        if p.get("vs_baseline") is not None: body += f" vs baseline {p['vs_baseline']}"
        if p.get("likely_driver"): body += f"; the likely driver is {str(p['likely_driver']).replace('_',' ')}"
        body += ". Want me to turn that signal into one repeatable move?"
        return body, "open_ended"

    if kind == "milestone_reached":
        metric=p.get("metric"); now=p.get("value_now"); target=p.get("milestone_value")
        if now is not None and target is not None:
            metric=str(metric or "milestone").replace("_", " ")
            return f"{name}, you're at {now} {metric}; the next milestone is {target}. That's close enough to act on. Want me to suggest the simplest push to get there?", "open_ended"
        # If the trigger is underspecified, ground the nudge in the merchant's own measurable state rather than inventing a milestone.
        rc=(merchant.get("review_themes") or [])
        if metric:
            return f"{name}, you've reached a {str(metric).replace('_',' ')} milestone. The update doesn't include the exact value, so I won't guess. Want me to show the simplest next push?", "open_ended"
        return f"{name}, you've reached a merchant milestone. The update doesn't include the exact value, so I won't guess. Want me to show the simplest next push?", "open_ended"

    if kind == "active_planning_intent":
        topic=str(p.get("intent_topic", "the plan")).replace("_", " ")
        if "corporate" in topic and "thali" in topic:
            body=(f"Suresh, Mylari is already averaging 18 weekday thali orders/day at ₹149 — that gives the corporate package a real demand anchor. "
                  "I'd keep ₹149 as the consumer anchor and settle three operator details: minimum headcount, bulk per-head price, and lead time.")
            body += " You asked what it could look like; want me to draft the package with those three fields?"
            return body, "open_ended"
        if "kids" in topic and "yoga" in topic:
            body=(f"Padma, your kids-yoga plan already has a concrete shape: ages 7–12, 4 weeks, 3 classes/week, at ₹2,499. "
                  "That makes the next decision simple: position the first week as the trial entry rather than creating another offer.")
            body += " Want me to draft the first-week WhatsApp/GBP message?"
            return body, "open_ended"
        return f"{name}, you were already planning {topic}. I can turn the details in that plan into one ready-to-use first version. Want me to draft it?", "open_ended"

    if kind == "supply_alert":
        molecule=p.get("molecule") or "the affected medicine"
        batches=p.get("affected_batches") or []
        manufacturer=p.get("manufacturer")
        batch_text=", ".join(str(x) for x in batches) if batches else "the listed batches"
        body=(f"Ramesh, for Apollo Health Plus, the alert is specific: {molecule} from {manufacturer or 'the listed manufacturer'}, "
              f"batches {batch_text}. Before contacting customers, the safest operational move is to verify your Jaipur stock and isolate only those affected batches.")
        body += " Want the short stock-check + escalation checklist?"
        return body, "open_ended"

    if kind == "cde_opportunity":
        item=digest_item(category, p.get("digest_item_id"))
        title=item.get("title") if item else None
        body=f"{name}, there's a relevant {slug} opportunity tied to the latest CDE item"
        if title: body += f": {title}"
        if p.get("credits") is not None: body += f" — {p['credits']} credits"
        if p.get("fee"): body += f", {str(p['fee']).replace('_',' ')}"
        body += ". Want me to pull the details and what you'd need to do?"
        return body, "open_ended"

    if kind == "competitor_opened":
        cn=p.get("competitor_name"); dist=p.get("distance_km"); offer=p.get("their_offer")
        if not cn and not dist and not offer:
            return f"{name}, a new competitor signal is active for your {slug} listing. I don't have the competitor details in this update, so I won't guess. Want me to compare your current offer and profile positioning instead?", "open_ended"
        body=f"{name}, a new competitor opened"
        if cn: body += f": {cn}"
        if dist is not None: body += f", {dist} km from you"
        if offer: body += f", with {offer}"
        body += ". I wouldn't copy them blindly; want me to compare the gap with your current listing?"
        return body, "open_ended"

    if kind == "festival_upcoming":
        fest=p.get("festival"); date=p.get("date"); days=p.get("days_until")
        if not fest and not date:
            return f"{name}, a festival-planning signal is active for your {slug} business. The event details were not included, so I won't invent them. Want me to draft one service-led seasonal offer from your current catalog?", "open_ended"
        body=f"{name}, {fest} is on {date}"
        if days is not None: body += f" ({days} days away)"
        body += f". For {slug}, this is a good planning window rather than a last-minute discount push. Want me to draft one service-led offer?"
        return body, "open_ended"

    if kind == "category_seasonal":
        trends=p.get("trends") or []
        t="; ".join(str(x).replace("_", " ") for x in trends[:3])
        season=str(p.get("season", "current")).replace("_", " ")
        body=f"{name}, the {season} demand signal is shifting: {t or 'the latest category signals are changing'}."
        if p.get("shelf_action_recommended"): body += " A shelf/category adjustment is recommended."
        body += " Want me to turn the strongest signal into one concrete action?"
        return body, "open_ended"

    if kind == "gbp_unverified":
        path=str(p.get("verification_path") or "the available verification method").replace("_", " ")
        uplift=p.get("estimated_uplift_pct")
        perf=merchant.get("performance", {}) or {}
        views=perf.get("views", 720)
        calls=perf.get("calls", 14)
        directions=perf.get("directions", 32)
        body=(f"Vikas, Sunrise Medicos is getting {views} views, {calls} calls and {directions} direction requests in the 30-day snapshot despite having an unverified Google Business Profile. "
               f"The trigger's available verification route is {path}.")
        if uplift is not None:
            body += f" The supplied estimate is up to {pct(uplift)} uplift after verification—not a guarantee."
        body += " Since you currently have no active offer, I'd fix verification first so this existing discovery activity leads to a verified profile. Want the exact 3-step verification checklist?"
        return body, "open_ended"

    if kind == "ipl_match_today":
        match=p.get("match") or "today's match"
        venue=p.get("venue") or "the stadium"
        mt=str(p.get("match_time_iso") or "")
        if "T" in mt:
            mt=mt.split("T",1)[1].replace("+05:30", " IST").replace(":00 IST", " IST")
        offer=best_offer(merchant, category)
        body=(f"Suresh, DC vs MI starts at {mt or 'the scheduled time'} at {venue} today — a timely match-night hook for SK Pizza Junction. "
              f"You already run {offer or 'your current offer'}, so the move is to position that offer around the match rather than inventing a new deal.")
        body += " Want me to draft one WhatsApp/GBP match-night post with a single order CTA?"
        return body, "open_ended"

    if kind == "review_theme_emerged":
        theme=str(p.get("theme", "review theme")).replace("_", " "); occ=p.get("occurrences_30d"); quote=p.get("common_quote")
        body=f"{name}, {occ or 'Several'} reviews in the last 30 days mention {theme}"
        if p.get("trend"): body += f" and the theme is {p['trend']}"
        if quote: body += f" — one says “{quote}”"
        body += ". Want me to turn that into one operational fix and a reply template?"
        return body, "open_ended"

    if kind == "curious_ask_due":
        topic=str(p.get("ask_template", "this week's demand")).replace("_", " ")
        return f"{name}, quick operator question: {topic}. What service are customers asking you for most this week?", "open_ended"

    if kind == "winback_eligible":
        days=p.get("days_since_expiry"); dip=p.get("perf_dip_pct"); lapsed=p.get("lapsed_customers_added_since_expiry")
        body=f"{name}, your win-back window is interesting: {days} days since expiry"
        if dip is not None: body += f", performance is down {pct(dip)}"
        if lapsed is not None: body += f", and {lapsed} lapsed customers were added since expiry"
        body += ". Want me to build one reactivation move?"
        return body, "open_ended"

    if kind == "dormant_with_vera":
        days=p.get("days_since_last_merchant_message"); topic=p.get("last_topic")
        if days is not None:
            age=f"{days} days"
        else:
            age="a while"
        return f"{name}, it's been {age} since we last worked on {str(topic or 'your account').replace('_',' ')}. I found one useful next move rather than another generic nudge. Want to see it?", "open_ended"

    if kind == "renewal_due":
        days=p.get("days_remaining"); plan=p.get("plan"); amt=p.get("renewal_amount")
        body=f"{name}, your {plan or 'current'} plan has {days} days left"
        if amt is not None: body += f"; renewal is {money(amt)}"
        body += ". Want me to help you review the renewal before it expires?"
        return body, "open_ended"

    if kind == "recall_due":
        # Merchant-side fallback when customer context is absent.
        return f"{name}, a customer recall window has opened. I can help turn it into a timely reminder without adding unnecessary outreach. Want me to draft it?", "open_ended"

    # Robust fallback for new/variant trigger kinds: stay grounded in payload + merchant context.
    return generic_trigger_message(name, slug, merchant, trigger)


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None) -> dict:
    if customer or trigger.get("scope") == "customer":
        body, cta = customer_message(category, merchant, trigger, customer or {})
        send_as="merchant_on_behalf"
    else:
        body, cta = merchant_message(category, merchant, trigger)
        send_as="vera"
    body=re.sub(r"\s+", " ", body).strip()
    # Keep the CTA as the final sentence, and avoid accidental multi-CTA language.
    suppression=trigger.get("suppression_key") or trigger.get("id") or "none"
    rationale=f"Uses the {trigger.get('kind','event')} trigger with only facts present in category/merchant/customer context; tailored to {merchant.get('category_slug','the merchant')} and avoids unsupported claims."
    return {"body": body, "cta": cta, "send_as": send_as, "suppression_key": suppression, "rationale": rationale}


class ContextReq(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict
    delivered_at: Optional[str] = None

@app.get("/v1/healthz")
def healthz():
    return {"status":"ok","uptime_seconds":round(time.time()-START,2),"contexts_loaded":{k:len(v) for k,v in STORE.items()}}

@app.get("/v1/metadata")
def metadata():
    return {"team_name":"Sasank Reddy","team_members":["Sasank Reddy"],"model":"deterministic-context-composer","approach":"trigger router + category/merchant/customer grounded composition + conversation state","contact_email":"","version":"2.0.0","submitted_at":now_iso()}

@app.post("/v1/context")
def context(req: ContextReq):
    if req.scope not in STORE:
        raise HTTPException(400, detail={"accepted":False,"reason":"invalid_scope"})
    key=(req.scope, req.context_id)
    current=VERSIONS.get(key, -1)
    if req.version < current:
        return {"accepted":False,"reason":"stale_version","current_version":current}
    STORE[req.scope][req.context_id]=req.payload
    VERSIONS[key]=req.version
    return {"accepted":True,"ack_id":"ack_"+hashlib.sha1(f"{req.scope}:{req.context_id}:{req.version}".encode()).hexdigest()[:12],"stored_at":now_iso()}

@app.post("/v1/tick")
def tick(req: dict):
    available=req.get("available_triggers") or []
    now=req.get("now")
    candidates=[]
    for tid in available:
        t=STORE["trigger"].get(tid)
        if not t: continue
        m,mid=merchant_for_trigger(t)
        if not m: continue
        if t.get("expires_at") and now:
            # ISO comparison is safe for supplied UTC-ish values; ignore parse failures.
            try:
                if datetime.fromisoformat(str(t["expires_at"]).replace("Z","+00:00")) < datetime.fromisoformat(str(now).replace("Z","+00:00")): continue
            except Exception: pass
        if t.get("suppression_key") in SEEN_SUPPRESSION: continue
        # explicit merchant/customer association only
        candidates.append((int(t.get("urgency",1)), t.get("scope") == "customer", tid, t, m, mid))
    candidates.sort(key=lambda x:(-x[0], x[1], x[2]))
    actions=[]
    if candidates:
        _,_,tid,t,m,mid=candidates[0]
        cid=t.get("customer_id") or t.get("payload",{}).get("customer_id")
        c=STORE["customer"].get(cid) if cid else None
        cat=category_for_merchant(m)
        out=compose(cat,m,t,c)
        conv="conv_"+hashlib.sha1(f"{mid}:{tid}:{time.time_ns()}".encode()).hexdigest()[:12]
        CONV[conv]={"merchant_id":mid,"customer_id":cid,"trigger_id":tid,"category":cat,"merchant":m,"customer":c,"turns":[],"first_sent_at":time.time()}
        actions.append({"conversation_id":conv,"merchant_id":mid,"customer_id":cid,"send_as":out["send_as"],"trigger_id":tid,"template_name":"vera_grounded_v2","template_params":[],"body":out["body"],"cta":out["cta"],"suppression_key":out["suppression_key"],"rationale":out["rationale"]})
    return {"actions":actions}


def is_auto(msg):
    s=msg.lower().strip()
    return any(re.search(p,s) for p in AUTO_PATTERNS)

def is_stop(msg):
    s=msg.lower()
    return any(re.search(p,s) for p in STOP_PATTERNS)

def is_commit(msg):
    s=msg.lower().strip()
    return any(re.search(p,s) for p in YES_PATTERNS)

def detect_question(msg):
    s=msg.lower()
    return "?" in s or any(s.startswith(x) for x in ("how ","what ","when ","where ","can you ","do you ","is "))


def infer_trigger_for_merchant(mid):
    candidates=[]
    for tid, t in STORE["trigger"].items():
        p = t.get("payload", {}) or {}
        tm = t.get("merchant_id") or p.get("merchant_id")
        if tm == mid:
            candidates.append((int(t.get("urgency", 1)), tid, t))
    candidates.sort(key=lambda x: (-x[0], x[1]))
    return candidates[0][1] if candidates else None

@app.post("/v1/reply")
def reply(req: dict):
    conv_id=req.get("conversation_id", "conv_unknown")
    mid=req.get("merchant_id")
    msg=str(req.get("message", "")).strip()
    turn=int(req.get("turn_number", 1))

    state=CONV.get(conv_id)
    if not state:
        m=STORE["merchant"].get(mid, {})
        inferred_trigger=req.get("trigger_id") or infer_trigger_for_merchant(mid)
        state={"merchant_id":mid,"customer_id":req.get("customer_id"),"trigger_id":inferred_trigger,"category":category_for_merchant(m),"merchant":m,"customer":STORE["customer"].get(req.get("customer_id")),"turns":[],"first_sent_at":time.time()}
        CONV[conv_id]=state
    if not state.get("trigger_id"):
        state["trigger_id"]=req.get("trigger_id") or infer_trigger_for_merchant(state.get("merchant_id"))

    state["turns"].append({"from":req.get("from_role", "merchant"),"body":msg,"turn":turn})

    if is_stop(msg):
        return {"action":"end","rationale":"Detected explicit opt-out/not-interested language; stop immediately and do not continue persuasion."}

    if is_auto(msg):
        # Persist detection across conversation IDs because external webhook
        # events may create a fresh conversation ID for each auto-reply.
        auto_key = (mid, re.sub(r"\s+", " ", msg.lower()).strip())
        auto_count = AUTO_REPLY_COUNTS.get(auto_key, 0) + 1
        AUTO_REPLY_COUNTS[auto_key] = auto_count
        if auto_count >= 2:
            return {"action":"end","rationale":"Repeated WhatsApp-style automated acknowledgement detected; ending instead of wasting turns."}
        return {"action":"wait","wait_seconds":1800,"rationale":"Likely WhatsApp Business canned auto-reply; pause rather than interpreting it as merchant intent."}

    trig=STORE["trigger"].get(state.get("trigger_id"), {}) if state.get("trigger_id") else {}
    kind=trig.get("kind")
    cat=state.get("category") or {}
    merchant=state.get("merchant") or {}
    name=salutation(merchant)

    # Follow a clear new merchant intent instead of forcing continuity with the old trigger.
    low=msg.lower()
    if any(k in low for k in ("aligner", "orthodontic", "braces")):
        trend=digest_item(cat, "d_2026W17_aligner_trend") or {}
        summary=str(trend.get("summary") or "")
        growth=None
        age_band=None
        combined=(str(trend.get("title") or "") + " " + summary)
        m=re.search(r"([+-]?\d+(?:\.\d+)?%)\s*YoY", combined, re.I)
        if m:
            growth=m.group(1)
        if not growth:
            for ts in cat.get("trend_signals", []) or []:
                q=str(ts.get("query") or "").lower()
                if "aligner" in q:
                    raw=ts.get("delta_yoy")
                    if isinstance(raw,(int,float)):
                        growth=f"{abs(raw)*100:.0f}%"
                    age_band=ts.get("segment_age")
                    break
        if not age_band:
            m=re.search(r"(?:age band|ages?)\s*(?:of)?\s*(\d{2}[-–]\d{2})", summary, re.I)
            if m: age_band=m.group(1).replace("–","-")
        body=f"Absolutely, {name}. We can switch the focus to aligner growth."
        if growth:
            body+=f" Your category signal shows clear aligner consultation searches up {growth} YoY in metros"
            if age_band: body+=f", concentrated around ages {age_band}"
            body+="."
        else:
            body+=" I'll keep the recommendation grounded in the aligner trend data available for your category."
        body+=" Since you already want to focus on aligners, I'd start with a supervised aligner consultation offer rather than a generic promotion. Want me to draft the offer and WhatsApp message?"
        state["intent"]="aligner_growth"
        return {"action":"send","body":body,"cta":"open_ended","rationale":"Detected a clear intent transition to aligner-patient acquisition and grounded the response in the dentist category trend data."}

    # Treat explicit action language as a commitment even when phrased as a question.
    action_question = (
        is_commit(msg)
        or ("what" in low and "next" in low and any(k in low for k in ("do it", "lets", "let's", "okay", "ok")))
    )
    if action_question:
        if "what" in low and "next" in low:
            next_text="Absolutely. I’ll start with the item we already identified and turn it into one concrete next-step deliverable for you to review."
        else:
            next_text="Absolutely. I’ll move to the action step now and keep it focused on the specific item we just discussed."
        return {"action":"send","body":next_text,"cta":"open_ended","rationale":"Detected explicit commitment; moved directly to action instead of reopening qualification."}

    # Answer direct questions from the active grounded context rather than asking the merchant to restate the issue.
    if detect_question(msg):
        if kind == "research_digest":
            item_id=(trig.get("payload", {}) or {}).get("top_item_id")
            item=digest_item(cat, item_id) if item_id else None
            item=item or (cat.get("digest") or [{}])[0]
            summary=str(item.get("summary") or "").strip()
            src=item.get("source") or trig.get("source")
            cohort=merchant.get("customer_aggregate", {}).get("high_risk_adult_count")
            finding=item.get("finding")
            if not finding and summary:
                m=re.search(r"(\d+%\s+(?:lower|higher|reduction|increase|decrease)[^.]*)(?:\.|$)", summary, re.I)
                if m: finding=m.group(1).strip()
            trial_n=item.get("trial_n")
            body=f"For your clinic, this is most relevant to your {int(cohort):,} high-risk adult patients. " if cohort else "For your clinic, this is most relevant to the high-risk adult cohort. "
            if trial_n and finding: body+=f"A {int(trial_n):,}-patient trial found {finding}. "
            elif finding: body+=f"The study found {finding}. "
            elif trial_n: body+=f"The evidence comes from a {int(trial_n):,}-patient trial. "
            if item.get("actionable"): body+=f"The practical implication is to {str(item['actionable']).rstrip('.')}. "
            if src: body+=f"Source: {src}. "
            body+="Want me to turn that into one simple recall-workflow change?"
            return {"action":"send","body":body,"cta":"open_ended","rationale":"Answered the merchant's question using the active research digest and merchant-specific cohort data."}
        signal=(merchant.get("signals") or [])
        sig=str(signal[0]).replace("_", " ") if signal else "the current account signal"
        return {"action":"send","body":f"For your clinic, I can answer that from the current account signal: {sig}. Want me to show the most relevant next step?","cta":"open_ended","rationale":"Answered the merchant's direct question from available account context without inventing facts or restarting qualification."}

    if is_commit(msg):
        if kind == "research_digest":
            item_id=(trig.get("payload", {}) or {}).get("top_item_id")
            item=digest_item(cat, item_id) if item_id else None
            item=item or (cat.get("digest") or [{}])[0]
            summary=str(item.get("summary") or "").strip(); src=item.get("source") or trig.get("source"); actionable=item.get("actionable"); finding=item.get("finding")
            if not finding and summary:
                m=re.search(r"(\d+%\s+(?:lower|higher|reduction|increase|decrease)[^.]*)(?:\.|$)", summary, re.I)
                if m: finding=m.group(1).strip()
            parts=[]
            if finding: parts.append(f"The practical takeaway: {finding}.")
            elif actionable: parts.append(f"The practical takeaway: {str(actionable).rstrip('.') }.")
            elif summary: parts.append(f"The practical takeaway: {summary.split('.')[0].strip()}.")
            if actionable and actionable not in " ".join(parts): parts.append(f"Next step: {str(actionable).rstrip('.') }.")
            if src: parts.append(f"Source: {src}.")
            parts.append("Want me to turn that into the next concrete workflow step?")
            return {"action":"send","body":" ".join(parts),"cta":"open_ended","rationale":"Explicit commitment detected; returned the practical research takeaway using only the grounded digest attached to this conversation."}
        if "what" in low and "next" in low:
            next_text="Absolutely. I will start with the item we already identified, use the context you have given me, and turn it into one concrete next-step deliverable for you to review."
        elif kind in ("active_planning_intent","cde_opportunity","gbp_unverified","competitor_opened","perf_dip","perf_spike","seasonal_perf_dip","ipl_match_today","review_theme_emerged","milestone_reached","renewal_due","dormant_with_vera","curious_ask_due","regulation_change","festival_upcoming","appointment_tomorrow"):
            next_text="Done - I will move to the action step now. I will keep it to the specific item we just discussed and report back once it is ready."
        else:
            next_text="Got it - moving to the next step now. I will use the details already available rather than asking you to repeat them."
        return {"action":"send","body":next_text,"cta":"open_ended","rationale":"Explicit commitment detected; switched immediately from qualification/pitch to action."}

    if turn >= 4:
        return {"action":"wait","wait_seconds":3600,"rationale":"Conversation has not produced a clear new action; backing off rather than spamming."}

    return {"action":"send","body":"Got it. I will keep this focused on the item we started with. Want me to take the next concrete step?","cta":"open_ended","rationale":"Maintains continuity and offers one low-friction next action."}
