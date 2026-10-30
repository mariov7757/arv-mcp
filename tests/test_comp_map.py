"""Tests for comp_map.py — the comps map's marker/popup HTML (ported from the
WholesaleAutomater dashboard map) and the payload built from an Analysis."""
import pytest
from test_service import LAT, LON, _comp, _listing, _record, data  # noqa: F401 (shared fixture)

from arv_mcp import comp_map, service

# ── Markers ───────────────────────────────────────────────────────────────────


class TestMarkers:
    @pytest.mark.parametrize("price,label", [(432500, "$432K"), (999, "$999"), (None, "Sold"), (0, "Sold")])
    def test_price_label(self, price, label):
        assert comp_map._sold_price_label(price) == label

    def test_arv_comp_is_green_with_star(self):
        html = comp_map.sold_marker_html({"sold_price": 465000, "used_for_arv": True})
        assert "#1A7A4A" in html and "⭐$465K" in html and "border:2px solid #fff" in html

    def test_other_sold_comp_is_blue(self):
        html = comp_map.sold_marker_html({"sold_price": 465000, "used_for_arv": False})
        assert "#1A4A8A" in html and "⭐" not in html

    @pytest.mark.parametrize("status,eligible,color,icon", [
        ("ACTIVE", True, "#0D7377", "⭐🏠"), ("ACTIVE", False, "#5FA3A5", "🏠"),
        ("PENDING", True, "#5B4B8A", "⭐⏳"), ("PENDING", False, "#9B8FC4", "⏳"),
    ])
    def test_active_pending_colors(self, status, eligible, color, icon):
        html = comp_map.active_pending_marker_html(
            {"list_price": 310000, "listing_status": status, "used_for_arv": eligible})
        assert color in html and f"{icon}$310K" in html

    def test_pills_center_on_the_point(self):
        assert "transform:translate(-50%,-50%)" in comp_map.sold_marker_html({"sold_price": 1})


# ── Popups ────────────────────────────────────────────────────────────────────

def _breakdown(**raw):
    rows = {"Stories": "subject 1 vs comp 2", "Pool": "subject no pool vs comp has pool"}
    return [{"factor": f, "known": True, "raw": raw.get(f, 1.0), "detail": d} for f, d in rows.items()]


class TestPopups:
    def test_sold_popup_layout(self):
        html = comp_map.sold_popup_html({
            "address": "1260 Main St, Mesa, AZ", "sold_price": 465000, "beds": 4, "baths": 2.0,
            "sqft": 1710, "price_sqft": 272, "lot_sqft": 7303, "days_ago": 23, "distance_mi": 0.24,
            "year_built": 1979, "property_url": "https://example.com/c1", "used_for_arv": True,
            "score_breakdown": _breakdown(Pool=0.0),
        })
        for part in ("<b>1260 Main St, Mesa, AZ</b>", "Sold: $465,000", "4 bd / 2.0 ba",
                     "1,710 sqft · $272/sqft · 7,303 lot sqft", "23d ago · 0.24 mi · Built 1979",
                     "View on Realtor.com", "Δ Pool: subject no pool vs comp has pool"):
            assert part in html
        assert "Not used for ARV" not in html
        assert "Δ Stories" not in html  # only mismatches (raw 0.0) are noted

    def test_excluded_comp_shows_why(self):
        html = comp_map.sold_popup_html({"address": "x", "used_for_arv": False,
                                         "exclusion_reason": "Sqft: subject 2000 vs comp 3000"})
        assert "⚠ Not used for ARV: Sqft: subject 2000 vs comp 3000" in html

    def test_missing_facts_show_dashes(self):
        html = comp_map.sold_popup_html({"address": "x", "sold_price": 0, "beds": 0, "baths": 0, "sqft": 0})
        assert "Sold: —" in html and "— bd / — ba" in html and "sqft" not in html

    def test_values_are_html_escaped(self):
        html = comp_map.sold_popup_html({
            "address": '<img src=x onerror="alert(1)">', "used_for_arv": False,
            "exclusion_reason": "<script>bad()</script>", "property_url": 'https://x/"><script>',
        })
        assert "<img" not in html and "<script>" not in html
        assert "&lt;img" in html and "&lt;script&gt;" in html

    def test_links_open_through_the_host(self):
        html = comp_map.sold_popup_html({"address": "x", "property_url": "https://example.com/c1"})
        assert 'data-open-link' in html and 'href="https://example.com/c1"' in html

    def test_active_pending_popup(self):
        html = comp_map.active_pending_popup_html({
            "address": "500 Elm Ave", "listing_status": "PENDING", "list_price": 310000, "days_ago": 5,
            "year_built": 1985, "used_for_arv": True, "score_breakdown": [],
        })
        for part in ("· Pending", "Asking: $310,000", "Pending 5d ago · Built 1985",
                     "⭐ Would qualify as an ARV comp"):
            assert part in html

    def test_active_pending_that_would_not_qualify(self):
        html = comp_map.active_pending_popup_html({"address": "x", "used_for_arv": False,
                                                   "exclusion_reason": "different price tier"})
        assert "· Active" in html and "⚠ Would not qualify: different price tier" in html

    def test_subject_popup_on_and_off_market(self):
        est = {"arv": 515743, "eligible": [object()]}
        on = comp_map.subject_popup_html(_record(on_market=True, listed_price=399000), est,
                                         {"low": 541236, "high": 586339})
        assert "· Subject" in on and "Listed: $399,000" in on and "ARV: $515,743" in on
        assert "Renovated range: $541,236–$586,339" in on
        off = comp_map.subject_popup_html(_record(), {"arv": 1, "eligible": []}, None)
        assert "Last sold: $300,000 (2021-08-11)" in off and "fallback, not comp-backed" in off

    def test_reword_date_sold_for_listings(self):
        c = comp_map._reword_date_sold({
            "listing_status": "PENDING", "days_ago": 5, "exclusion_reason": "Date sold: sold 5 days ago",
            "score_breakdown": [{"factor": "Date sold", "detail": "sold 5 days ago"}],
        })
        assert c["score_breakdown"][0]["detail"] == "pending 5 days ago"
        assert c["exclusion_reason"] == "Date sold: pending 5 days ago"


# ── Payload (through service.get_comp_map) ───────────────────────────────────

class TestPayload:
    def test_payload_shape(self, data):
        data.sold = [_comp(i, 245 + i) for i in range(5)] + [_comp(9, 251, type="condos")]
        data.market = [_listing(0), _listing(1, "PENDING")]
        p = service.get_comp_map("1234 Main St, Mesa, AZ 85204")
        assert (p["subject"]["lat"], p["subject"]["lon"]) == (LAT, LON)
        assert "· Subject" in p["subject"]["popup"]
        assert len(p["sold"]) == 6 and all({"lat", "lon", "days_ago", "icon", "popup"} <= set(s) for s in p["sold"])
        assert [s["eligible"] for s in p["sold"]][-5:] == [True] * 5  # ARV comps drawn last
        assert "Not used for ARV" in p["sold"][0]["popup"]
        assert len(p["extra"]) == 2
        assert p["estimate"]["eligible_count"] == 5 and p["estimate"]["confidence_tier"] in (4, 5)
        assert p["default_time_window"] == "6M" and p["time_windows"]["3M"] == 90
        assert "Active" in p["legend"] or "active" in p["legend"]

    def test_listings_are_classified_against_the_sold_pool(self, data):
        data.sold = [_comp(i, 250) for i in range(5)]
        data.market = [_listing(0, price_sqft=255.0), _listing(1, price_sqft=520.0)]
        extra = service.get_comp_map("1234 Main St, Mesa, AZ")["extra"]
        assert "⭐ Would qualify" in extra[0]["popup"]
        assert "⚠ Would not qualify" in extra[1]["popup"]

    def test_skips_points_without_coordinates(self, data):
        data.sold = [_comp(i, 250) for i in range(5)] + [_comp(7, 250, latitude=None, longitude=None)]
        data.market = [dict(_listing(0), latitude=None)]
        p = service.get_comp_map("1234 Main St, Mesa, AZ")
        assert len(p["sold"]) == 5 and p["extra"] == []

    def test_reuses_get_arv_cache(self, data):
        service.get_arv("1234 Main St, Mesa, AZ")
        n = len(data.calls)
        service.get_comp_map("1234 Main St, Mesa, AZ", max_comps=3, include_market_context=False)
        assert len(data.calls) == n  # no new lookups or fetches

    def test_errors_match_get_arv(self, data):
        data.record = None
        data.suggestion = {"area_type": "city", "text": "Mesa, AZ"}
        with pytest.raises(service.ArvError) as exc:
            service.get_comp_map("99999 E Main St, Mesa, AZ 85204")
        assert exc.value.code == "ADDRESS_NOT_FOUND"
