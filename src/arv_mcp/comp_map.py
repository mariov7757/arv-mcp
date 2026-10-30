"""
comp_map.py — the comps map's data: subject, sold comps and active/pending
listings, each with coordinates and ready-made marker and popup HTML.

The marker pills, popup layouts, colors and legend are ported from the
listing-detail parcel map in the private WholesaleAutomater dashboard
(Folium/Leaflet), so the map reads the same: green ⭐ price tags for comps
that fed the estimate, blue for other sold comps (click for why they weren't
used), teal/purple for active/pending listings, and an orange home pin for
the subject. Folium's DivIcon wrapper is gone (ui/comp_map.html places the
HTML with Leaflet directly), parcel lot lines are not drawn, and every value
from the data source is HTML-escaped because it renders inside a host app.
"""

from html import escape

from . import arv

# "Sold within" filter choices on the map (days); "All" shows the whole pool.
MAP_TIME_WINDOWS = {"3M": 90, "6M": 180, "1Y": 365, "All": 10**9}
DEFAULT_TIME_WINDOW = "6M"

# Sold-comp marker colors.
_ARV_COMP   = "#1A7A4A"  # green — fed the estimate
_OTHER_COMP = "#1A4A8A"  # blue — sold, not used

# Active/pending marker colors — deliberately distinct from the sold-comp
# green/blue and the orange subject marker.
_ACTIVE_ELIGIBLE  = "#0D7377"  # teal  — active, would qualify as an ARV comp
_ACTIVE_OTHER     = "#5FA3A5"  # muted teal — active, would not qualify
_PENDING_ELIGIBLE = "#5B4B8A"  # purple — pending, would qualify
_PENDING_OTHER    = "#9B8FC4"  # muted purple — pending, would not qualify


def _e(value) -> str:
    return escape(str(value), quote=True)


def _sold_price_label(price) -> str:
    """Short price tag for a marker ("$700K"). Falls back to a plain 'Sold'
    when no price is on file."""
    if not price:
        return "Sold"
    if price >= 1000:
        return f"${round(price / 1000)}K"
    return f"${price:,.0f}"


def _pill(bg: str, prefix: str, label: str, border: str) -> str:
    return (
        f'<div style="background:{bg};color:#fff;font-size:15px;font-weight:700;'
        f'padding:4px 9px;border-radius:6px;{border}white-space:nowrap;'
        'box-shadow:0 1px 4px rgba(0,0,0,.5);transform:translate(-50%,-50%);'
        f'display:inline-block;">{prefix}{_e(label)}</div>'
    )


def sold_marker_html(s: dict) -> str:
    """Price-tag marker for a sold comp. Comps that fed the estimate get a
    green tag with a ⭐ so they stand out from the plain blue sold comps."""
    label = _sold_price_label(s.get("sold_price"))
    if s.get("used_for_arv"):
        return _pill(_ARV_COMP, "⭐", label, "border:2px solid #fff;")
    return _pill(_OTHER_COMP, "", label, "border:1px solid rgba(255,255,255,.6);")


def active_pending_marker_html(c: dict) -> str:
    """Price-tag marker for an active/pending listing — same pill style,
    colored by status (teal=active, purple=pending) and shaded by whether it
    would qualify as an ARV comp. These never feed the estimate."""
    label = _sold_price_label(c.get("list_price"))
    status = c.get("listing_status") or "ACTIVE"
    icon = "⏳" if status == "PENDING" else "🏠"
    eligible = bool(c.get("used_for_arv"))
    if status == "PENDING":
        bg = _PENDING_ELIGIBLE if eligible else _PENDING_OTHER
    else:
        bg = _ACTIVE_ELIGIBLE if eligible else _ACTIVE_OTHER
    star = "⭐" if eligible else ""
    border = "border:2px solid #fff;" if eligible else "border:1px solid rgba(255,255,255,.6);"
    return _pill(bg, f"{star}{icon}", label, border)


def comp_mismatch_notes(c: dict) -> list[str]:
    """Subject-vs-comp differences worth a glance — Stories and Pool are
    soft-scored, not gated, so a mismatch here doesn't mean the comp was
    excluded."""
    notes = []
    for row in c.get("score_breakdown") or []:
        if row.get("factor") in ("Stories", "Pool") and row.get("known") and row.get("raw") == 0.0:
            notes.append(f"{row['factor']}: {row['detail']}")
    return notes


def _link(url) -> str:
    return f'<a href="{_e(url)}" target="_blank" rel="noopener" data-open-link>View on Realtor.com</a>'


def _popup(rows: list[str]) -> str:
    return '<div style="font-size:12px;line-height:1.5">' + "<br>".join(rows) + "</div>"


def sold_popup_html(s: dict) -> str:
    addr = s.get("address") or "(no address)"
    price = s.get("sold_price")
    price_str = f"${price:,.0f}" if price else "—"
    sqft = s.get("sqft")
    pps = s.get("price_sqft")
    lot_sqft = s.get("lot_sqft")
    beds, baths = s.get("beds"), s.get("baths")
    days = s.get("days_ago")
    rows = [f"<b>{_e(addr)}</b>", f"Sold: {price_str}"]
    if beds is not None or baths is not None:
        rows.append(f"{beds or '—'} bd / {baths or '—'} ba")
    if sqft:
        row = f"{sqft:,} sqft" + (f" · ${pps:,.0f}/sqft" if pps else "")
        if lot_sqft:
            row += f" · {lot_sqft:,} lot sqft"
        rows.append(row)
    if days is not None:
        row = f"{days}d ago · {s.get('distance_mi', '—')} mi"
        year = s.get("year_built")
        if year:
            row += f" · Built {year}"
        rows.append(row)
    if s.get("property_url"):
        rows.append(_link(s["property_url"]))
    if not s.get("used_for_arv") and s.get("exclusion_reason"):
        rows.append(f'<span style="color:#8A4A00">⚠ Not used for ARV: {_e(s["exclusion_reason"])}</span>')
    for note in comp_mismatch_notes(s):
        rows.append(f'<span style="color:#5B4B8A">Δ {_e(note)}</span>')
    return _popup(rows)


def active_pending_popup_html(c: dict) -> str:
    addr = c.get("address") or "(no address)"
    status = c.get("listing_status") or "ACTIVE"
    status_label = "Pending" if status == "PENDING" else "Active"
    price = c.get("list_price")
    price_str = f"${price:,.0f}" if price else "—"
    sqft = c.get("sqft")
    pps = c.get("price_sqft")
    lot_sqft = c.get("lot_sqft")
    beds, baths = c.get("beds"), c.get("baths")
    days = c.get("days_ago")
    rows = [f'<b>{_e(addr)}</b> <span style="color:#5B4B8A;font-weight:700">· {status_label}</span>',
            f"Asking: {price_str}"]
    if beds is not None or baths is not None:
        rows.append(f"{beds or '—'} bd / {baths or '—'} ba")
    if sqft:
        row = f"{sqft:,} sqft" + (f" · ${pps:,.0f}/sqft" if pps else "")
        if lot_sqft:
            row += f" · {lot_sqft:,} lot sqft"
        rows.append(row)
    if days is not None:
        verb = "Pending" if status == "PENDING" else "Listed"
        row = f"{verb} {days}d ago"
        year = c.get("year_built")
        if year:
            row += f" · Built {year}"
        rows.append(row)
    if c.get("property_url"):
        rows.append(_link(c["property_url"]))
    if c.get("used_for_arv"):
        rows.append('<span style="color:#0e6655">⭐ Would qualify as an ARV comp</span>')
    elif c.get("exclusion_reason"):
        rows.append(f'<span style="color:#8A4A00">⚠ Would not qualify: {_e(c["exclusion_reason"])}</span>')
    for note in comp_mismatch_notes(c):
        rows.append(f'<span style="color:#5B4B8A">Δ {_e(note)}</span>')
    return _popup(rows)


def subject_popup_html(subject: dict, est: dict, renovated: dict | None) -> str:
    """Click-popup for the subject pin, laid out like a comp's: its own
    details (asking price while it's on the market, otherwise its last sale)
    plus this estimate's ARV."""
    street = " ".join(p for p in (subject.get("address"), subject.get("unit")) if p)
    addr = street or subject.get("formatted_address") or "(no address)"
    on_market = bool(subject.get("on_market"))
    price = subject.get("listed_price") if on_market else None
    sqft = subject.get("sqft")
    lot_sqft = subject.get("lot_sqft")
    beds, baths = subject.get("beds"), subject.get("baths")
    year = subject.get("year_built")
    rows = [f'<b>{_e(addr)}</b> <span style="color:#C77800;font-weight:700">· Subject</span>']
    if on_market:
        rows.append(f"Listed: {f'${price:,.0f}' if price else '—'}")
    elif subject.get("last_sold_price"):
        when = f" ({subject['last_sold_date']})" if subject.get("last_sold_date") else ""
        rows.append(f"Last sold: ${subject['last_sold_price']:,.0f}{_e(when)}")
    if beds is not None or baths is not None:
        rows.append(f"{beds or '—'} bd / {baths or '—'} ba")
    if sqft:
        pps = (price / sqft) if price else None
        row = f"{sqft:,} sqft" + (f" · ${pps:,.0f}/sqft" if pps else "")
        if lot_sqft:
            row += f" · {lot_sqft:,} lot sqft"
        rows.append(row)
    rows.append(f"ARV: ${est['arv']:,.0f}" + (" (fallback, not comp-backed)" if not est["eligible"] else ""))
    if renovated:
        rows.append(f"Renovated range: ${renovated['low']:,.0f}–${renovated['high']:,.0f}")
    if year:
        rows.append(f"Built {year}")
    if subject.get("property_url"):
        rows.append(_link(subject["property_url"]))
    return _popup(rows)


def _reword_date_sold(comp: dict) -> dict:
    """The scorer's recency detail reads "sold N days ago" — right for a sold
    comp, misleading for an active/pending listing. Rewrites that detail (and
    the same text inside exclusion_reason) to "listed"/"pending". Mutates the
    given dict, which belongs only to this map payload."""
    verb = "pending" if comp.get("listing_status") == "PENDING" else "listed"
    old = f"sold {comp.get('days_ago')} days ago"
    new = f"{verb} {comp.get('days_ago')} days ago"
    for row in comp.get("score_breakdown") or []:
        if row.get("factor") == "Date sold" and row.get("detail") == old:
            row["detail"] = new
    if comp.get("exclusion_reason") and old in comp["exclusion_reason"]:
        comp["exclusion_reason"] = comp["exclusion_reason"].replace(old, new)
    return comp


def _as_map_comp(v: arv.CompVerdict) -> dict:
    """A CompVerdict flattened into the comp dict the marker/popup builders read."""
    return {
        **v.comp,
        "used_for_arv": v.eligible,
        "exclusion_reason": v.exclusion_reason,
        "score_breakdown": [dict(row) for row in v.breakdown],
        "distance_mi": round(v.distance_mi, 2) if v.distance_mi is not None else "—",
    }


def _classify_market(a) -> list[dict]:
    """Active/pending listings classified against the SAME pool as the real
    sold comps, so "would qualify" means "would qualify against this
    estimate's micro-market" rather than one built only from asking prices.
    Display only; never fed into the estimate."""
    if not a.market:
        return []
    pool = list(a.comps) + list(a.market)
    classified = arv.classify_comps(a.subject, pool, max_distance_mi=a.window.radius_mi,
                                    recency_half_life_days=a.recency_half_life)
    new_ids = {id(c) for c in a.market}
    return [_reword_date_sold(_as_map_comp(v)) for v in classified if id(v.comp) in new_ids]


def legend(has_extra: bool) -> str:
    text = ("🟧 Subject   ·   🟩 ⭐ Comp used for ARV   ·   "
            "🟦 Other sold comp (click for why it wasn't used)   (label = sold price)")
    if has_extra:
        text += "   ·   🔎 teal 🏠 = active, purple ⏳ = pending (⭐ = would qualify as an ARV comp)"
    return text


def build_map_payload(a) -> dict:
    """The map's whole payload for a service.Analysis."""
    subject = a.subject
    eligible = a.eligible
    renovated = None if a.fallback else arv.renovated_range(eligible, subject.get("sqft"))

    sold = []
    # Draw the ARV comps last so their markers sit on top of the plain ones.
    for v in sorted(a.classified, key=lambda v: v.eligible):
        c = v.comp
        if c.get("latitude") is None or c.get("longitude") is None:
            continue
        mc = _as_map_comp(v)
        sold.append({"lat": c["latitude"], "lon": c["longitude"], "days_ago": c.get("days_ago") or 0,
                     "eligible": v.eligible, "icon": sold_marker_html(mc), "popup": sold_popup_html(mc)})

    extra = [{"lat": c["latitude"], "lon": c["longitude"],
              "icon": active_pending_marker_html(c), "popup": active_pending_popup_html(c)}
             for c in _classify_market(a)
             if c.get("latitude") is not None and c.get("longitude") is not None]

    street = " ".join(p for p in (subject.get("address"), subject.get("unit")) if p)
    return {
        "subject": {
            "lat": subject["latitude"], "lon": subject["longitude"],
            "label": street or subject.get("formatted_address") or "Subject",
            "popup": subject_popup_html(subject, a.est, renovated),
        },
        "estimate": {
            "arv": a.est["arv"],
            "renovated_low": renovated["low"] if renovated else None,
            "renovated_high": renovated["high"] if renovated else None,
            "fallback": a.fallback,
            "eligible_count": len(eligible),
            "confidence_tier": arv.comp_confidence_tier(eligible),
        },
        "radius_mi": a.window.radius_mi,
        "days_back": a.window.days_back,
        "time_windows": MAP_TIME_WINDOWS,
        "default_time_window": DEFAULT_TIME_WINDOW,
        "sold": sold,
        "extra": extra,
        "legend": legend(bool(extra)),
    }
