#!/usr/bin/env python3
"""
Rule regression tests. Run: python3 test_rules.py

Every case here is a decision somebody made deliberately. The MUST_PASS block is the
important half: a rule that wrongly rejects is invisible in production, because a
rejected posting is never scored and nobody ever learns it was good.
"""

import json
import os
import sys

import pipeline
from pipeline import apply_rules

HERE = os.path.dirname(os.path.abspath(__file__))
RULES = json.load(open(os.path.join(HERE, "rules.json"), encoding="utf-8"))["rules"]


def check(jd="", title="Project Coordinator", location="Berlin", **fields):
    post = dict(title=title, jd=jd, location=location, company="Test Org",
                employment_type=None, seniority=None)
    post.update(fields)
    return apply_rules(post, RULES, [])


# (label, kwargs, expected rule id or None)
CASES = [
    # ---- MUST REJECT -------------------------------------------------------
    ("Werkstudent role", dict(jd="Wir suchen eine:n Werkstudent:in für 20h/Woche."), "HARD_WERKSTUDENT"),
    ("Pflichtpraktikum", dict(jd="Es handelt sich um ein Pflichtpraktikum."), "HARD_ENROLLED"),
    ("enrolled required", dict(jd="You must be enrolled at a university."), "HARD_ENROLLED"),
    ("German C1", dict(jd="Deutschkenntnisse auf C1 Niveau."), "HARD_LANG_DE"),
    ("verhandlungssicher", dict(jd="Verhandlungssicheres Deutsch in Wort und Schrift."), "HARD_LANG_DE"),
    ("fluent German EN", dict(jd="Fluent German and English required."), "HARD_LANG_DE"),
    ("native German", dict(jd="Native German speaker."), "HARD_LANG_DE"),
    ("5+ years", dict(jd="5+ years of experience in project management."), "HARD_EXP_5Y"),
    ("7 years", dict(jd="7 years of professional experience required."), "HARD_EXP_5Y"),
    ("direct reports", dict(jd="You will have three direct reports."), "HARD_MGMT"),
    ("team of 8", dict(jd="You will lead a team of 8 coordinators."), "HARD_MGMT"),
    ("P&L", dict(jd="Full P&L responsibility for the region."), "HARD_PL"),
    ("US only", dict(jd="Must be authorized to work in the United States."), "HARD_REMOTE_NONEU"),
    ("intern field", dict(employment_type="intern"), "HARD_EMPLOYMENT_TYPE"),
    ("working_student field", dict(employment_type="working_student"), "HARD_EMPLOYMENT_TYPE"),
    ("executive seniority", dict(seniority="executive"), "HARD_SENIORITY"),
    ("US location", dict(location="United States, Remote"), "HARD_LOCATION_NONEU"),
    ("Australia remote", dict(location="LiveEO Australia (Remote)"), "HARD_LOCATION_NONEU"),
    ("London", dict(location="London"), "HARD_LOCATION_NONEU"),
    ("Munich", dict(location="Munich"), "HARD_LOCATION"),

    # ---- MUST PASS (false rejects are the expensive kind) ------------------
    ("3-5 years range", dict(jd="3-5 years of experience in a similar role."), None),
    ("3+ years soft", dict(jd="3+ years of experience preferred."), None),
    ("good German B2", dict(jd="Gute Deutschkenntnisse sind von Vorteil."), None),
    ("German a plus", dict(jd="German language skills are a plus."), None),
    ("English working lang", dict(jd="English is our working language; German is helpful."), None),
    ("lead a project", dict(jd="You will lead a project from concept to delivery."), None),
    ("work with a team", dict(jd="You will work with a team of researchers."), None),
    ("trainee/Volontariat", dict(employment_type="trainee", jd="A two-year Volontariat."), None),
    ("entry seniority", dict(seniority="entry"), None),
    ("experienced seniority", dict(seniority="experienced", jd="Entry level welcome."), None),
    ("company 5 years old", dict(jd="We have been growing for 5 years and now serve 200 clients."), None),
    ("no location given", dict(location=None), None),
    ("Berlin hybrid", dict(location="Berlin Office (Hybrid)"), None),
    ("EU remote", dict(location="Europe (Remote)"), None),
    ("mentions US harmlessly", dict(jd="We have offices in Berlin and the United States.", location="Berlin"), None),
    ("revenue mentioned once", dict(jd="You will support the team that reports on revenue."), None),
]


# --- German level, separated from the word ------------------------------------
# All four cleared HARD_LANG_DE on the night of 31 Aug 2026, in a single run. Every one
# of them puts something between the language and the level, which is exactly what the
# original pattern could not see. Found by reading the JDs during scoring, not by a test.
LANG_CASES = [
    ("verhandlungssicher separated",
     dict(jd="Verhandlungssichere Kenntnisse in deutscher und englischer Sprache."), "HARD_LANG_DE"),
    ("verhandlungssicher + der",
     dict(jd="Verhandlungssichere Kenntnisse in der deutschen Sprache."), "HARD_LANG_DE"),
    ("Deutschkenntnisse sind einwandfrei",
     dict(jd="Deine Deutschkenntnisse sind einwandfrei."), "HARD_LANG_DE"),
    ("einwandfreies Deutsch",
     dict(jd="Einwandfreies Deutsch in Wort und Schrift."), "HARD_LANG_DE"),
    ("German at least C1", dict(jd="Fluent in English, German at least C1."), "HARD_LANG_DE"),
    ("German bracketed C2", dict(jd="German (C2 or Native) and English at a negotiation level."), "HARD_LANG_DE"),
    ("Deutsch bracketed C1", dict(jd="Sprachen: Deutsch (C1), Englisch (B2)."), "HARD_LANG_DE"),
]

# The half that matters. Every one of these is a posting the candidate can actually do, and each
# would be killed by the obvious lazy widening (a wildcard gap between the language and
# the level, or between 'verhandlungssicher' and 'Deutsch').
LANG_MUST_PASS = [
    ("negotiation English, good German",
     dict(jd="Verhandlungssicheres Englisch sowie gute Deutschkenntnisse.")),
    ("B2 German, C1 English", dict(jd="German (B2) required, C1 English preferred.")),
    ("sehr gute Deutschkenntnisse", dict(jd="Sehr gute Deutschkenntnisse in Wort und Schrift.")),
    ("Deutsch B2 explicitly", dict(jd="Deutsch min. B2. Abgeschlossenes Studium.")),
    ("German helpful", dict(jd="English is our working language; German is helpful.")),
    ("native English wanted", dict(jd="Native English speaker, German a plus.")),
    ("C1 English only", dict(jd="English at C1 level; no German required.")),
]

# --- 5+ years, German phrasing --------------------------------------------------
# The German half of HARD_EXP_5Y matched only "Berufserfahrung" until 30 Aug 2026, so a
# PMO role asking for "Mindestens 5 Jahre Erfahrung" cleared the filters and reached the
# digest. Found by the nightly run, not by a test - hence these.
EXP_CASES = [
    ("bare Erfahrung", dict(jd="Mindesten 5 Jahre Erfahrung im PMO-Umfeld."), "HARD_EXP_5Y"),
    ("einschlägige Erfahrung", dict(jd="Mindestens 5 Jahre einschlägige Erfahrung."), "HARD_EXP_5Y"),
    ("Berufserfahrung", dict(jd="mindestens 7 Jahre Berufserfahrung."), "HARD_EXP_5Y"),
    ("Projekterfahrung", dict(jd="5+ Jahre Projekterfahrung erwünscht."), "HARD_EXP_5Y"),
    ("fundierte Führungserfahrung", dict(jd="mindestens 6 Jahre fundierte Führungserfahrung."), "HARD_EXP_5Y"),
    ("English still works", dict(jd="At least 5 years of relevant experience."), "HARD_EXP_5Y"),
    # 31 Aug: the English half had the German half's bug. Formo named the field, not
    # an experience noun, and cleared the filter.
    ("N+ years in <field>", dict(jd="5+ years in B2B marketing, ideally in food ingredients."), "HARD_EXP_5Y"),
    ("N+ years as <role>", dict(jd="8+ years as a programme manager."), "HARD_EXP_5Y"),
    ("mindestens N Jahre im", dict(jd="Mindestens 5 Jahre im Projektmanagement."), "HARD_EXP_5Y"),
    ("at least N years in", dict(jd="At least 6 years in a similar role."), "HARD_EXP_5Y"),
]

# Ranges whose lower bound is under 5 mean they would take the candidate. None may be rejected.
EXP_MUST_PASS = [
    ("dashed range", dict(jd="3-5 Jahre Erfahrung in der Projektarbeit.")),
    ("German written range", dict(jd="3 bis 5 Jahre Erfahrung.")),
    ("English written range", dict(jd="3 to 5 years of experience.")),
    ("two years", dict(jd="2 Jahre Erfahrung sind ausreichend.")),
    ("company age", dict(jd="Wir bestehen seit 5 Jahren am Markt.")),
    ("project duration", dict(jd="Das Projekt läuft über 5 Jahre.")),
    ("junior range", dict(jd="1-3 years of experience.")),
    ("range with a plus", dict(jd="3-5+ years in a comparable role.")),
    ("company history names a field", dict(jd="We have been growing for 5 years in Berlin.")),
    ("bare N years in, no minimum", dict(jd="Our team has 5 years in climate finance behind it.")),
]

# --- function and seniority title rules --------------------------------------
# Built from the 88 survivors of the 29 Aug run. The MUST-NOT half is taken verbatim
# from postings we want to keep, including three that earlier drafts of these rules
# killed: Co-Leitung, Chief of Staff, and Executive Assistant.
FN_CASES = [
    # engineering
    ("Full Stack Engineer", dict(title="Full Stack Engineer (f/m/x)"), "HARD_FN_ENGINEERING"),
    ("Engineering, All", dict(title="Engineering, All"), "HARD_FN_ENGINEERING"),
    ("Softwareentwicklung", dict(title="Agile Projektmanager Softwareentwicklung (m/w/d)"), "HARD_FN_ENGINEERING"),
    ("Webentwickler", dict(title="Webentwickler (m/w/d)"), "HARD_FN_ENGINEERING"),
    ("System Administrator", dict(title="System Administrator - Workplace IT (f/m/x)"), "HARD_FN_ENGINEERING"),
    ("Site Reliability", dict(title="Senior Site Reliability Engineer"), "HARD_FN_ENGINEERING"),
    ("Forward Deployed Engineer", dict(title="Forward Deployed Engineer"), "HARD_FN_ENGINEERING"),
    ("Success Engineer", dict(title="Success Engineer"), "HARD_FN_ENGINEERING"),
    ("Geospatial Data Analyst", dict(title="Geospatial Data Analyst (f/m/x)"), "HARD_FN_ENGINEERING"),
    # sales
    ("Account Executive", dict(title="Account Executive Central and Eastern Europe"), "HARD_FN_SALES"),
    ("Sales Development Rep", dict(title="Sales Development Representative"), "HARD_FN_SALES"),
    ("Telesales", dict(title="Inside Sales Specialist - Telesales (w/m/d)"), "HARD_FN_SALES"),
    ("Key Accounts", dict(title="Sales Specialist Key Accounts (w/m/d)"), "HARD_FN_SALES"),
    ("Vertrieb", dict(title="Mitarbeiter Vertrieb (m/w/d)"), "HARD_FN_SALES"),
    ("Store Manager", dict(title="Assistant Store Manager (w/m/x)"), "HARD_FN_SALES"),
    # marketing specialisms
    ("Paid Social", dict(title="Paid Social Team Lead"), "HARD_FN_MARKETING_SPECIALIST"),
    ("SEO", dict(title="SEO Marketing Manager"), "HARD_FN_MARKETING_SPECIALIST"),
    ("Product Marketing", dict(title="Product Marketing"), "HARD_FN_MARKETING_SPECIALIST"),
    ("Revenue Operations", dict(title="Revenue Operations Associate"), "HARD_FN_SALES"),
    # working student, by title (LinkedIn has no employment_type field)
    ("Working Student title", dict(title="Revenue Operations Working Student"), "HARD_FN_SALES"),
    ("Werkstudent title", dict(title="Werkstudent:in Projektmanagement (m/w/d)"), "HARD_WORKING_STUDENT_TITLE"),
    ("Studentische Hilfskraft", dict(title="Studentische Hilfskraft Kommunikation (gn)"), "HARD_WORKING_STUDENT_TITLE"),
    ("Praktikant", dict(title="Praktikant:in Eventmanagement"), "HARD_WORKING_STUDENT_TITLE"),
    # accounting
    ("Finance & Accounting", dict(title="Projektmanager Finance & Accounting - Rechnungsmanagement (m/w/d)"), "HARD_FN_ACCOUNTING"),
    ("Buchhaltung", dict(title="Sachbearbeiter Buchhaltung (m/w/d)"), "HARD_FN_ACCOUNTING"),
    ("Payroll", dict(title="Payroll Specialist"), "HARD_FN_ACCOUNTING"),
    # natural science and laboratory
    ("Junior Chemiker*in", dict(title="Junior Chemiker*in für Chemikalienbewertung & Umweltschutz"), "HARD_FN_SCIENCE_LAB"),
    ("Laboratory Technician", dict(title="Laboratory Technician / Operator BTA/CTA"), "HARD_FN_SCIENCE_LAB"),
    ("Fermentation Operator", dict(title="Fermentation Operator (f/m/x)"), "HARD_FN_SCIENCE_LAB"),
    ("Protein research", dict(title="Research Associate - Protein Characterization & Functionality"), "HARD_FN_SCIENCE_LAB"),
    ("Physiker", dict(title="Physiker (m/w/d) Messtechnik"), "HARD_FN_SCIENCE_LAB"),
    # construction and electrical trades
    ("Tiefbau", dict(title="Baukoordinator*in für den Kabelleitungstiefbau"), "HARD_FN_ENGINEERING"),
    ("Bauleitung", dict(title="Bauleiter (m/w/d) Hochbau"), "HARD_FN_ENGINEERING"),
    # 31 Aug: same employer as the Tiefbau case above, different noun, cleared the rule.
    ("Bau von Trafostationen", dict(title="Baukoordinator*in für den Bau von Trafostationen"), "HARD_FN_ENGINEERING"),
    ("Baukoordinator alone", dict(title="Baukoordinator (m/w/d)"), "HARD_FN_ENGINEERING"),
    # legal / logistics
    ("Legal Fellow", dict(title="Legal Fellow (Spring 2027)"), "HARD_FN_LEGAL"),
    ("Logistics Manager", dict(title="Logistics Manager - 3PL & Last Mile"), "HARD_FN_LOGISTICS"),
    ("Fulfilment", dict(title="Global Director of Logistics & Fulfilment"), "HARD_FN_LOGISTICS"),
    # seniority
    ("Senior Project Manager", dict(title="Senior Project Manager"), "HARD_SENIORITY_TITLE"),
    ("(Senior) in parens", dict(title="(Senior) Earth Observation Data Supplier Manager"), "HARD_SENIORITY_TITLE"),
    ("Head of Marketing", dict(title="Head of Marketing"), "HARD_SENIORITY_TITLE"),
    ("Lead Learning Designer", dict(title="Lead Learning Experience Designer"), "HARD_SENIORITY_TITLE"),
    ("Principal", dict(title="Principal, Global Event Security"), "HARD_SENIORITY_TITLE"),
    ("Teamlead", dict(title="Teamlead Office Management (all genders)"), "HARD_SENIORITY_TITLE"),
]

# The half that matters: postings these rules must NOT touch.
FN_MUST_PASS = [
    ("Projektmanager Klimaschutz", dict(title="Projektmanager Klimaschutzprojekte (d/m/w)")),
    ("Koordinator is not Baukoordinator", dict(title="Koordinator Bildungsprogramme (m/w/d) | Vollzeit | Berlin")),
    ("Projektkoordination survives", dict(title="Projektkoordinator:in Nachhaltigkeit")),
    ("Trainee Projektentwicklung", dict(title="Trainee on the Job im Bereich Klimaschutz-Projektentwicklung (d/m/w)")),
    ("Co-Leitung is not 'Lead'", dict(title="Co-Leitung von Young Entrepreneurs in Science")),
    ("Kaufmännische Leitung", dict(title="Kaufmännische Leitung (m/w/d) - Vollzeit oder Teilzeit")),
    ("Chief of Staff is a target", dict(title="Founder's Associate / Chief of Staff")),
    ("Executive Assistant is a target", dict(title="Executive Assistant to the CEO")),
    ("Teamassistenz", dict(title="Teamassistenz (d/w/m) KIS-Projekt")),
    ("Assistenz der Geschäftsführung", dict(title="Office Management & Assistenz der Geschäftsführung (m/w/d)")),
    ("Junior Projektmanager", dict(title="Junior Projektmanager (m/w/d) Normung Bauwesen")),
    ("Referent Klimapolitik", dict(title="Referent Klimapolitik (d/m/w)")),
    ("Veranstaltung und Kommunikation", dict(title="Mitarbeiterin (d/w/m) Veranstaltungsmanagement und Kommunikation")),
    ("Trainee Event Management", dict(title="Trainee Event Management (all genders)")),
    ("Research Associate at a think tank", dict(title="Research Associate, European Foreign Policy")),
    ("Wissenschaftliche Mitarbeit", dict(title="Wissenschaftliche*r Mitarbeiter*in Klimapolitik")),
    ("Normung Bauwesen is a PM role", dict(title="Junior Projektmanager (m/w/d) Normung Bauwesen")),
    ("Assistant at a logistics firm", dict(title="Assistenz der Geschäftsleitung Logistik (m/w/d)")),
    ("Office Manager at a warehouse firm", dict(title="Office Manager (m/w/d) Lager & Verwaltung")),
    ("jd mentions the lab", dict(title="Project Coordinator", jd="You will coordinate with our laboratory team on protein research.")),
    ("Trainee is not a working student", dict(title="Trainee on the Job Klimaschutz (d/m/w)")),
    ("Student Service is not a student role", dict(title="Student Service und Event Specialist (m/w/d)")),
    ("Koordinator Bildungsprogramme", dict(title="Koordinator Bildungsprogramme (m/w/d)")),
    ("Marketing AND communications", dict(title="Marketing & Communications Coordinator")),
    ("Social media in a comms role", dict(title="Communications Officer, Social Media")),
    ("Leadership is not lead", dict(title="Leadership Programme Coordinator")),
    ("Founder's Associate", dict(title="Founder's Associate - Special Projects")),
    ("Chief of Staff again", dict(title="Chief of Staff")),
    ("Projektentwicklung is not Entwickler", dict(title="Referent Projektentwicklung (m/w/d)")),
    ("Initiativbewerbung", dict(title="Initiativbewerbung")),
    # JD text must never trigger a title rule.
    ("jd mentions engineers", dict(title="Project Coordinator",
                                   jd="You will work closely with our engineering team and the sales team.")),
    ("jd mentions invoices", dict(title="Team Assistant",
                                  jd="You will process invoices and liaise with our accounting team.")),
    ("Kaufmännische Leitung still passes", dict(title="Kaufmännische Leitung (m/w/d)")),
    ("jd mentions logistics", dict(title="Event Coordinator",
                                   jd="You will handle event logistics and supplier contracts.")),
    ("jd mentions a senior colleague", dict(title="Programme Assistant",
                                            jd="You report to the Senior Programme Manager.")),
]

# --- UX research exclusion (profile 9b) ---------------------------------------
UX_CASES = [
    ("UX Researcher", dict(title="UX Researcher (f/m/d)"), "HARD_UX_RESEARCH"),
    ("User Researcher", dict(title="User Researcher (f/m/d)"), "HARD_UX_RESEARCH"),
    ("Design Researcher", dict(title="Design Researcher"), "HARD_UX_RESEARCH"),
    # A senior UX role is rejected either way; seniority simply fires first.
    ("Lead UX Researcher", dict(title="Lead UX Researcher"), "HARD_SENIORITY_TITLE"),
    # Must NOT reject: the phrase in a JD body, or a coordination role next to it.
    ("jd mentions user research", dict(jd="You will work closely with our user research team."), None),
    ("Research Assistant", dict(title="Research Assistant, Climate Policy"), None),
    ("Project Coordinator", dict(title="Project Coordinator, Research Programmes"), None),
]

# --- source sanity: board identity -------------------------------------------
# The real defect is not "Personio serves demo data", it is "we asked for a board that
# is not theirs and something answered". So the check is whether the board says whose it
# is. Cases below are the 17 boards probed on 2026-08-29: every real one declares an
# owner in one of the two places, every unclaimed one declares none in either.
IDENTITY_CASES = [
    # (label, subcompany values on the postings, board <title>, expected owner or None)
    ("atmosfair: subcompany + title", ["atmosfair gGmbH"], "Jobs bei atmosfair gGmbH", "atmosfair gGmbH"),
    ("BUND: subcompany only", ["Bund f\u00fcr Umwelt und Naturschutz Deutschland"], "Jobs bei", "Bund f\u00fcr Umwelt und Naturschutz Deutschland"),
    ("CORRECTIV: subcompany only", ["Correctiv - Recherchen f\u00fcr die Gesellschaft"], "Jobs bei", "Correctiv - Recherchen f\u00fcr die Gesellschaft"),
    ("ECFR: title only", [""], "Jobs at ECFR", "ECFR"),
    ("RSF: title only", [""], "Jobs bei Reporter ohne Grenzen e. V.", "Reporter ohne Grenzen e. V."),
    ("LiveEO: title only", [""], "Jobs bei LiveEO GmbH", "LiveEO GmbH"),
    # The four unclaimed boards - neither place names an owner.
    ("WWF slug: unclaimed", [""], "Jobs bei", None),
    ("adelphi slug: unclaimed", [""], "Jobs bei", None),
    ("Ecosia slug: unclaimed", [""], "Jobs at", None),
    ("dgap slug: unclaimed", [""], "Jobs bei", None),
]

# Name matching only ever warns, so the must-NOT-warn half is the important one.
MISMATCH_CASES = [
    ("ProVeg vs ProVeg International", "ProVeg", "ProVeg International", False),
    ("BUND vs its legal name", "BUND", "Bund f\u00fcr Umwelt und Naturschutz Deutschland", False),
    ("atmosfair vs atmosfair gGmbH", "atmosfair", "atmosfair gGmbH", False),
    ("DRI vs DRI HQ", "Democracy Reporting International", "DRI HQ", False),
    ("Falling Walls vs Foundation gGmbH", "Falling Walls", "Falling Walls Foundation gGmbH", False),
    # A slug occupied by a different real company must be flagged.
    ("WWF vs some other firm", "WWF Deutschland", "Kanzlei Meier & Partner", True),
    ("Ecosia vs a telecoms firm", "Ecosia", "Safaricom Networks", True),
]

def main() -> int:
    fails = []
    for label, kw, expect in EXP_CASES:
        got = check(**kw)
        got_id = got.split(":")[0] if got else None
        if got_id != expect:
            fails.append((f"[exp] {label}", expect, got_id, got))

    for label, kw in EXP_MUST_PASS:
        got = check(**kw)
        if got:
            fails.append((f"[exp must-pass] {label}", None, got.split(":")[0], got))

    for label, kw, expect in LANG_CASES:
        got = check(**kw)
        got_id = got.split(":")[0] if got else None
        if got_id != expect:
            fails.append((f"[lang] {label}", expect, got_id, got))

    for label, kw in LANG_MUST_PASS:
        got = check(**kw)
        if got:
            fails.append((f"[lang must-pass] {label}", None, got.split(":")[0], got))

    for label, kw, expect in FN_CASES:
        got = check(**kw)
        got_id = got.split(":")[0] if got else None
        if got_id != expect:
            fails.append((f"[fn] {label}", expect, got_id, got))

    for label, kw in FN_MUST_PASS:
        got = check(**kw)
        if got:
            fails.append((f"[fn must-pass] {label}", None, got.split(":")[0], got))

    for label, kw, expect in UX_CASES:
        got = check(**kw)
        got_id = got.split(":")[0] if got else None
        if got_id != expect:
            fails.append((f"[ux] {label}", expect, got_id, got))

    for label, subs, title, expect in IDENTITY_CASES:
        posts = [{"subcompany": x} for x in subs]
        pipeline.get = lambda url, _t=title: (
            f"<html><head><title>{_t}</title></head></html>".encode("utf-8"))
        got2 = pipeline.personio_board_owner("slug", posts)
        if got2 != expect:
            fails.append((f"[identity] {label}", expect, got2, ""))

    for label, expected, declared, expect in MISMATCH_CASES:
        got3 = pipeline.owner_mismatch(expected, declared)
        if got3 != expect:
            fails.append((f"[owner] {label}", expect, got3, ""))

    for label, kw, expect in CASES:
        got = check(**kw)
        got_id = got.split(":")[0] if got else None
        if got_id != expect:
            fails.append((label, expect, got_id, got))

    for label, expect, got_id, got in fails:
        print(f"FAIL  {label}\n      expected: {expect}\n      got:      {got_id}"
              f"\n      evidence: {(got or '')[:120]}")
    total = (len(CASES) + len(IDENTITY_CASES) + len(UX_CASES) + len(MISMATCH_CASES)
             + len(FN_CASES) + len(FN_MUST_PASS)
             + len(EXP_CASES) + len(EXP_MUST_PASS)
             + len(LANG_CASES) + len(LANG_MUST_PASS))
    print(f"\n{total - len(fails)}/{total} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
