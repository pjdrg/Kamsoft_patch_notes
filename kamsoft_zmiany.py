#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kamsoft_zmiany.py
-----------------
Pobiera changelogi KS-AOW z https://aktualizator-aow.kamsoft.pl/zmiany/<rok>/<wersja>.html
i buduje JEDEN samodzielny plik HTML z wyszukiwarka, filtrem modulow i oznaczeniem nowych wersji.

Uzycie:
  # pobierz nowe wersje i zbuduj viewer
  python kamsoft_zmiany.py --rok 2026 --prefix 2026.1.1 --od 0

  # kilka galezi wersji naraz
  python kamsoft_zmiany.py --rok 2026 --prefix 2026.1.1 2026.1.2 --od 0

  # tylko przebuduj HTML z tego co juz jest w cache (bez sieci)
  python kamsoft_zmiany.py --offline

Wynik: kamsoft_zmiany.html  (otwierasz w przegladarce, dziala offline)
Cache:  ./cache/<wersja>.html  +  ./cache/meta.json  (pierwsze-widziane => badge "NOWE")
"""

import argparse
import json
import re
import sys
import datetime as dt
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://aktualizator-aow.kamsoft.pl/zmiany/{rok}/{wersja}.html"
HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
META = CACHE / "meta.json"
OUT = HERE / "kamsoft_zmiany.html"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "kamsoft-zmiany-viewer/1.0"})


# --------------------------------------------------------------------------- #
#  Pobieranie / odkrywanie wersji
# --------------------------------------------------------------------------- #
def fetch_one(rok, wersja, timeout=15):
    """Zwraca HTML albo None (404 / blad)."""
    url = BASE.format(rok=rok, wersja=wersja)
    try:
        r = SESSION.get(url, timeout=timeout)
    except requests.RequestException as e:
        print(f"  ! {wersja}: blad sieci ({e})")
        return None
    # Kamsoft serwuje UTF-8, ale bez naglowka charset -> requests zgaduje ISO-8859-1.
    # Wymuszamy UTF-8, inaczej polskie znaki robia sie mojibake (oĂ³lne, RozwiÄzano...).
    r.encoding = "utf-8"
    if r.status_code == 200 and "miany" in r.text:
        return r.text
    return None


def probe(rok, wersja, meta, today, nowe, force):
    """True jesli wersja istnieje (z cache lub pobrana). Zapisuje do cache + meta."""
    cache_file = CACHE / f"{wersja}.html"
    if cache_file.exists() and not force:
        meta.setdefault(wersja, today)
        return True
    html = fetch_one(rok, wersja)
    if html:
        cache_file.write_text(html, "utf-8")
        if wersja not in meta:
            meta[wersja] = today
            nowe.append(wersja)
        print(f"  + {wersja}")
        return True
    return False


def walk_year(rok, meta, today, nowe, force, pg, ng, mg):
    """Przechodzi caly rok ROK.<major>.<minor>.<patch> z tolerancja luk.
    Radzi sobie z rolloverem (np. ...0.2.2 -> 1.0.0) bo kazdy wymiar
    konczy sie dopiero po pg/ng/mg kolejnych pustych wartosciach."""
    found = 0
    major_empty = 0
    m = 0
    while major_empty < mg:
        minor_empty = 0
        n = 0
        major_has = False
        while minor_empty < ng:
            patch_miss = 0
            p = 0
            minor_has = False
            while patch_miss < pg:
                wersja = f"{rok}.{m}.{n}.{p}"
                if probe(rok, wersja, meta, today, nowe, force):
                    minor_has = major_has = True
                    found += 1
                    patch_miss = 0
                else:
                    patch_miss += 1
                p += 1
            minor_empty = 0 if minor_has else minor_empty + 1
            n += 1
        major_empty = 0 if major_has else major_empty + 1
        m += 1
    return found


INDEX_URL = "https://aktualizator-aow.kamsoft.pl/zmiany/{rok}/"
VER_LINK_RE = re.compile(r"(\d{4}\.\d+\.\d+\.\d+)\.html")


def list_year_index(rok):
    """Probuje pobrac listing katalogu roku i wyciagnac wszystkie wersje.
    Zwraca posortowana liste wersji albo None, gdy listing niedostepny."""
    try:
        r = SESSION.get(INDEX_URL.format(rok=rok), timeout=15)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    vers = {v for v in VER_LINK_RE.findall(r.text) if v.startswith(f"{rok}.")}
    return sorted(vers, key=version_key) if vers else None


def discover(lata, force=False, pg=6, ng=6, mg=4):
    """Skanuje liste lat. Dla kazdego roku najpierw listing katalogu,
    a gdy niedostepny -> chodzenie po wersjach z tolerancja luk.
    Zwraca meta (wersja -> data pierwszego pobrania)."""
    CACHE.mkdir(exist_ok=True)
    meta = json.loads(META.read_text("utf-8")) if META.exists() else {}
    today = dt.date.today().isoformat()
    nowe = []

    for rok in lata:
        rok = str(rok)
        idx = list_year_index(rok)
        if idx:
            print(f"Rok {rok}: listing katalogu OK ({len(idx)} wersji)")
            for wersja in idx:
                probe(rok, wersja, meta, today, nowe, force)
        else:
            print(f"Rok {rok}: brak listingu, chodze po wersjach…")
            c = walk_year(rok, meta, today, nowe, force, pg, ng, mg)
            print(f"  rok {rok}: {c} wersji w cache")

    META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
    if nowe:
        print(f"\nNowo pobrane wersje: {', '.join(sorted(nowe, key=version_key))}")
    return meta


# --------------------------------------------------------------------------- #
#  Parsowanie changeloga
# --------------------------------------------------------------------------- #
HEADER_RE = re.compile(r"-{3,}.*?miany.*?-{0,}", re.IGNORECASE)
DASHES_RE = re.compile(r"[-—=]{2,}")
MODULE_RE = re.compile(r"\b(APW\d+|KS-[A-Z]+)\b")
ANCHOR_RE = re.compile(r"^\s*(\d{4}\.\d{2}\.\d{2}|\S+\.(dll|exe|ocx)\b|\[\d|[-*]\s)", re.I)


def reflow(lines):
    """Skleja zawijane linie Kamsoftu. Puste linie NIGDY nie rozbijaja wpisu —
    nowy wiersz zaczyna tylko kotwica: data, plik modulu, [wersja] albo bullet '-'."""
    out = []
    for raw in lines:
        s = raw.strip()
        if not s:
            continue                      # puste linie ignorujemy (to artefakt zawijania)
        if ANCHOR_RE.match(raw) or not out:
            out.append(s)
        else:
            out[-1] = (out[-1] + " " + s).strip()   # kontynuacja -> doklej do biezacego
    return "\n".join(out)


def parse_changelog(html):
    """Zwraca: (tytul, [ {name, module, body} ])  — tolerancyjnie, na bazie tekstu."""
    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.find("title")
    tytul = title_tag.get_text(strip=True) if title_tag else ""
    if not tytul:
        m = re.search(r"Zmiany w KS-AOW\s+[\d.]+", soup.get_text(" "))
        tytul = m.group(0) if m else "Zmiany KS-AOW"

    text = soup.get_text("\n")
    sekcje = []
    cur = {"name": "Ogolne", "module": "", "lines": []}

    for raw in text.splitlines():
        s = raw.strip()
        if s and re.search(r"-{3,}", s) and "miany" in s.lower():
            name = DASHES_RE.sub("", s).strip(" -—=")
            name = re.sub(r"\s+", " ", name)
            if name:
                if any(l.strip() for l in cur["lines"]):
                    sekcje.append(cur)
                mod = MODULE_RE.search(name)
                cur = {"name": name, "module": mod.group(1) if mod else "Ogolne", "lines": []}
                continue
        cur["lines"].append(raw)

    if any(l.strip() for l in cur["lines"]):
        sekcje.append(cur)

    out = []
    for sec in sekcje:
        body = reflow(sec["lines"])
        body = re.sub(r"\n{3,}", "\n\n", body).strip("\n")
        # zostaw tylko sekcje z realna trescia (bullet "-" albo linia z data)
        has_bullet = any(l.strip().startswith("-") for l in body.splitlines())
        has_date = re.search(r"\d{4}\.\d{2}\.\d{2}", body)
        if not (has_bullet or has_date):
            continue
        sec["body"] = body
        del sec["lines"]
        out.append(sec)
    return tytul, out


def version_key(v):
    return tuple(int(x) if x.isdigit() else 0 for x in v.split("."))


def build_dataset(meta):
    rows = []
    for f in CACHE.glob("*.html"):
        wersja = f.stem
        try:
            tytul, sekcje = parse_changelog(f.read_text("utf-8"))
        except Exception as e:
            print(f"  ! parse {wersja}: {e}")
            continue
        # Data publikacji = najnowsza data RRRR.MM.DD w tresci changeloga
        # (Kamsoft pisze ja pod naglowkiem). Fallback: data pobrania z meta.
        wszystkie_daty = []
        for s in sekcje:
            wszystkie_daty += re.findall(r"(?<!\d\.)\b(\d{4}\.\d{2}\.\d{2})(?!\.\d)", s["body"])
        data_pub = max(wszystkie_daty) if wszystkie_daty else meta.get(wersja, "")
        rows.append({
            "wersja": wersja,
            "tytul": tytul,
            "data": data_pub,
            "sekcje": sekcje,
        })
    rows.sort(key=lambda r: version_key(r["wersja"]), reverse=True)
    return rows


# --------------------------------------------------------------------------- #
#  Generowanie HTML
# --------------------------------------------------------------------------- #
HTML_TMPL = r"""<!doctype html>
<html lang="pl" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Zmiany KS-AOW — przegladarka</title>
<style>
  html[data-theme="dark"]{
    --bg:#0f1115; --panel:#161922; --panel2:#1d212b; --line:#2a2f3a;
    --txt:#e6e9ef; --mut:#98a2b3; --soft:#c4ccd9; --acc:#5aa9ff;
    --new:#2ec27e; --hit:#ffd54a; --bullet:#5aa9ff; --hdrbg:rgba(15,17,21,.92);
  }
  html[data-theme="light"]{
    --bg:#f4f6fa; --panel:#ffffff; --panel2:#eef1f6; --line:#dde2ea;
    --txt:#1b2430; --mut:#5b6675; --soft:#33404f; --acc:#1668d6;
    --new:#0e9f6e; --hit:#ffe27a; --bullet:#1668d6; --hdrbg:rgba(244,246,250,.92);
  }
  :root{--mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
    font:15px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  header{position:sticky;top:0;z-index:5;background:var(--hdrbg);
    backdrop-filter:blur(8px);border-bottom:1px solid var(--line);padding:7px 12px}
  .bar{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
  input,select,button{background:var(--panel2);color:var(--txt);border:1px solid var(--line);
    border-radius:7px;padding:5px 8px;font-size:13px;outline:none;line-height:1.2}
  input:focus,select:focus{border-color:var(--acc)}
  #q{flex:1 1 180px;min-width:140px}
  button{cursor:pointer}
  button:hover{border-color:var(--acc)}
  #theme{padding:5px 8px;font-size:14px;line-height:1}
  .chk{display:flex;align-items:center;gap:5px;color:var(--mut);font-size:12px;
    user-select:none;white-space:nowrap}
  .count{color:var(--mut);font-size:12px;white-space:nowrap;margin-left:auto}
  /* lata – kompaktowe, w tym samym rzedzie */
  .years{display:flex;gap:4px;flex-wrap:wrap;align-items:center}
  .ybtn{background:var(--panel2);border:1px solid var(--line);color:var(--mut);
    border-radius:20px;padding:4px 9px;font-size:12px;font-weight:600;cursor:pointer;line-height:1.1}
  .ybtn.on{background:color-mix(in srgb,var(--acc) 16%,transparent);
    border-color:var(--acc);color:var(--acc)}
  .ybtn.ghost{font-weight:400;color:var(--mut)}
  .ybtn:hover{border-color:var(--acc)}
  /* przycisk odznaczania przy wersji */
  .mark{font-size:11px;padding:3px 9px;border-radius:20px;border:1px solid var(--line);
    background:var(--panel2);color:var(--mut);cursor:pointer;font-weight:600}
  .mark:hover{border-color:var(--acc);color:var(--acc)}
  main{max-width:1000px;margin:0 auto;padding:16px 18px 80px}
  .ver{background:var(--panel);border:1px solid var(--line);border-radius:12px;margin:12px 0;overflow:hidden}
  .ver>summary{list-style:none;cursor:pointer;padding:12px 16px;display:flex;
    align-items:center;gap:10px;font-weight:600}
  .ver>summary::-webkit-details-marker{display:none}
  .ver>summary:hover{background:var(--panel2)}
  .vname{font-family:var(--mono);color:var(--acc)}
  .badge{font-size:11px;padding:2px 7px;border-radius:20px;font-weight:600}
  .badge.new{background:color-mix(in srgb,var(--new) 18%,transparent);color:var(--new);
    border:1px solid color-mix(in srgb,var(--new) 45%,transparent)}
  .vmeta{margin-left:auto;color:var(--mut);font-weight:400;font-size:12px}

  /* sekcja modulu */
  .sec{border-top:1px solid var(--line);padding:14px 16px 16px}
  .sec h3{margin:0 0 10px;font-size:13px;color:var(--soft);font-weight:700;
    letter-spacing:.3px;display:flex;gap:8px;align-items:center}
  .mtag{font-family:var(--mono);font-size:11px;color:var(--acc);
    background:var(--panel2);border:1px solid var(--line);padding:2px 7px;border-radius:6px}

  /* grupa: data + plik + lista zmian */
  .grp{padding:2px 0 4px;margin:0 0 6px}
  .grp + .grp{border-top:1px dashed var(--line);padding-top:12px;margin-top:12px}
  .dt{font-family:var(--mono);font-size:12px;color:var(--mut);
    letter-spacing:.3px;margin-bottom:6px}
  .dt .ver-in{color:var(--acc)}
  .file{font-family:var(--mono);font-size:12px;color:var(--soft);margin:2px 0 6px}
  ul.chg{list-style:none;margin:0;padding:0}
  ul.chg li{position:relative;padding-left:20px;margin:0 0 9px;
    line-height:1.62;color:var(--txt)}
  ul.chg li:last-child{margin-bottom:0}
  ul.chg li::before{content:"";position:absolute;left:4px;top:.62em;
    width:6px;height:6px;border-radius:50%;background:var(--bullet)}
  ul.sub{list-style:none;margin:7px 0 4px;padding:0}
  ul.sub li{position:relative;padding-left:18px;margin:0 0 6px;
    line-height:1.55;color:var(--soft)}
  ul.sub li:last-child{margin-bottom:2px}
  ul.sub li::before{content:"";position:absolute;left:3px;top:.62em;
    width:5px;height:5px;border-radius:1px;background:var(--mut)}
  .cont{padding-left:20px;color:var(--soft);line-height:1.6;margin:0 0 8px}
  mark{background:var(--hit);color:#111;border-radius:3px;padding:0 2px}
  .empty{color:var(--mut);text-align:center;padding:60px 0}
  footer{color:var(--mut);font-size:11px;text-align:center;padding:24px}
</style>
</head>
<body>
<header>
  <div class="bar">
    <input id="q" placeholder="Szukaj (ZSMOPL, KSeF, APW21…)" autocomplete="off">
    <select id="mod"><option value="">moduły</option></select>
    <div class="years" id="years"></div>
    <label class="chk"><input type="checkbox" id="onlynew"> nowe</label>
    <button id="markall" title="Oznacz wszystkie jako przeczytane">Odznacz</button>
    <button id="expand" title="Rozwiń wszystkie">Rozwiń</button>
    <button id="collapse" title="Zwiń wszystkie">Zwiń</button>
    <button id="theme" title="Przełącz motyw">&#9790;</button>
    <span class="count" id="count"></span>
  </div>
</header>
<main id="app"></main>
<footer>Wygenerowano __BUILD__ &middot; zrodlo: aktualizator-aow.kamsoft.pl</footer>

<script>
const DATA = __DATA__;

/* ---------- motyw ---------- */
const root = document.documentElement, tbtn = document.getElementById("theme");
function applyTheme(t){
  root.setAttribute("data-theme", t);
  tbtn.innerHTML = t === "dark" ? "&#9790;" : "&#9728;";
  tbtn.title = t === "dark" ? "Motyw ciemny — kliknij dla jasnego" : "Motyw jasny — kliknij dla ciemnego";
  try{ localStorage.setItem("ksaow-theme", t); }catch(e){}
}
let saved = null;
try{ saved = localStorage.getItem("ksaow-theme"); }catch(e){}
if(!saved){
  saved = matchMedia && matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}
applyTheme(saved);
tbtn.onclick = ()=>applyTheme(root.getAttribute("data-theme") === "dark" ? "light" : "dark");

/* ---------- NOWE = opublikowane w ostatnich N dni i nieodznaczone ---------- */
const NOWE_DNI = __NOWEDNI__;
let read = new Set();
try{ read = new Set(JSON.parse(localStorage.getItem("ksaow-read") || "[]")); }catch(e){}
function saveRead(){ try{ localStorage.setItem("ksaow-read", JSON.stringify([...read])); }catch(e){} }
function recent(v){
  if(!v.data) return false;
  const d = new Date(v.data.replace(/\./g, "-") + "T00:00:00");
  if(isNaN(d)) return false;
  return (Date.now() - d.getTime()) <= NOWE_DNI * 86400000;
}
function isNew(v){ return recent(v) && !read.has(v.wersja); }
function toggleRead(ver){ read.has(ver) ? read.delete(ver) : read.add(ver); saveRead(); render(); }
document.getElementById("markall").onclick = ()=>{
  DATA.forEach(v => { if(recent(v)) read.add(v.wersja); }); saveRead(); render();
};

/* ---------- panel lat ---------- */
const years = [...new Set(DATA.map(v => v.wersja.split(".")[0]))].sort().reverse();
let selYears = new Set(years);
const ybar = document.getElementById("years");
function renderYears(){
  ybar.innerHTML = "";
  years.forEach(y => {
    const b = document.createElement("button");
    b.className = "ybtn" + (selYears.has(y) ? " on" : "");
    b.textContent = y;
    b.onclick = ()=>{ selYears.has(y) ? selYears.delete(y) : selYears.add(y); renderYears(); render(); };
    ybar.appendChild(b);
  });
  if(years.length > 1){
    const all = document.createElement("button");
    all.className = "ybtn ghost"; all.textContent = "wszystkie";
    all.onclick = ()=>{ selYears = new Set(years); renderYears(); render(); };
    ybar.appendChild(all);
  }
}
renderYears();

/* ---------- filtry ---------- */
const mods = new Set();
DATA.forEach(v => v.sekcje.forEach(s => mods.add(s.module || "Ogolne")));
const sel = document.getElementById("mod");
[...mods].sort().forEach(m => {
  const o = document.createElement("option"); o.value = m; o.textContent = m; sel.appendChild(o);
});

const q = document.getElementById("q");
const onlynew = document.getElementById("onlynew");
const app = document.getElementById("app");
const count = document.getElementById("count");

function esc(s){return s.replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}
function hl(text, term){
  const e = esc(text);
  if(!term) return e;
  const rx = new RegExp("("+term.replace(/[.*+?^${}()|[\]\\]/g,"\\$&")+")","gi");
  return e.replace(rx,"<mark>$1</mark>");
}

const reDate = /^\d{4}\.\d{2}\.\d{2}/;
const reVer  = /\[([\d.]+)\]\s*$/;
const reFile = /\.(dll|exe|ocx)\b/i;

/* zamien tekst body (1 logiczna pozycja / linia) na strukture data->plik->bullety */
function renderBody(body, term){
  const lines = body.split("\n").map(l => l.trim()).filter(Boolean);
  let html = "", inGrp = false, inUl = false, inSub = false, liOpen = false;
  const closeSub = ()=>{ if(inSub){ html += "</ul>"; inSub = false; } };
  const closeLi  = ()=>{ closeSub(); if(liOpen){ html += "</li>"; liOpen = false; } };
  const closeUl  = ()=>{ closeLi(); if(inUl){ html += "</ul>"; inUl = false; } };
  const closeGrp = ()=>{ closeUl(); if(inGrp){ html += "</div>"; inGrp = false; } };
  const ensureGrp = ()=>{ if(!inGrp){ html += `<div class="grp">`; inGrp = true; } };

  for(const s of lines){
    if(reDate.test(s)){
      closeGrp();
      let head = s, ver = ""; const m = s.match(reVer);
      if(m){ ver = m[1]; head = s.replace(reVer,"").trim(); }
      html += `<div class="grp"><div class="dt">${hl(head,term)}` +
              (ver?` <span class="ver-in">[${esc(ver)}]</span>`:``) + `</div>`;
      inGrp = true;
    } else if(reFile.test(s) && !/^[-*]/.test(s)){
      closeUl(); ensureGrp();
      html += `<div class="file">${hl(s,term)}</div>`;
    } else if(/^-\s?/.test(s)){
      ensureGrp();
      if(!inUl){ html += `<ul class="chg">`; inUl = true; }
      closeLi();
      html += `<li>${hl(s.replace(/^-\s*/,""),term)}`;   // li zostaje otwarte na pod-punkty
      liOpen = true;
    } else if(/^\*\s?/.test(s)){
      ensureGrp();
      if(!inUl){ html += `<ul class="chg">`; inUl = true; }
      if(!liOpen){ html += `<li>`; liOpen = true; }       // pod-punkt bez rodzica
      if(!inSub){ html += `<ul class="sub">`; inSub = true; }
      html += `<li>${hl(s.replace(/^\*\s*/,""),term)}</li>`;
    } else {
      ensureGrp();
      if(liOpen){ closeSub(); html += ` ${hl(s,term)}`; }
      else { closeUl(); html += `<div class="cont">${hl(s,term)}</div>`; }
    }
  }
  closeGrp();
  return html;
}

function render(){
  const term = q.value.trim();
  const mod = sel.value;
  const onew = onlynew.checked;
  const rx = term ? new RegExp(term.replace(/[.*+?^${}()|[\]\\]/g,"\\$&"),"i") : null;
  app.innerHTML = "";
  let shownV = 0, shownS = 0;

  DATA.forEach(v => {
    if(!selYears.has(v.wersja.split(".")[0])) return;
    const vNew = isNew(v);
    const vRecent = recent(v);
    if(onew && !vNew) return;
    const secs = v.sekcje.filter(s => {
      if(mod && (s.module||"Ogolne") !== mod) return false;
      if(rx && !(rx.test(s.body) || rx.test(v.wersja) || rx.test(s.name))) return false;
      return true;
    });
    if(secs.length === 0) return;
    shownV++; shownS += secs.length;

    const d = document.createElement("details");
    d.className = "ver"; d.open = !!term || !!mod || onew;
    const sum = document.createElement("summary");
    sum.innerHTML =
      `<span class="vname">${esc(v.wersja)}</span>` +
      (vNew ? `<span class="badge new">NOWE</span>` : ``) +
      `<span class="vmeta">${secs.length} ${secs.length===1?"modul":"modulow"}${v.data?` &middot; ${v.data}`:``}</span>`;
    if(vRecent){
      const mb = document.createElement("button");
      mb.className = "mark";
      mb.textContent = read.has(v.wersja) ? "↺ przywróc" : "✓ przeczytane";
      mb.onclick = (e)=>{ e.preventDefault(); e.stopPropagation(); toggleRead(v.wersja); };
      sum.appendChild(mb);
    }
    d.appendChild(sum);

    secs.forEach(s => {
      const sec = document.createElement("div"); sec.className = "sec";
      sec.innerHTML =
        `<h3><span class="mtag">${esc(s.module||"Ogolne")}</span>${esc(s.name)}</h3>` +
        renderBody(s.body, term);
      d.appendChild(sec);
    });
    app.appendChild(d);
  });

  if(shownV === 0){
    app.innerHTML = `<div class="empty">Brak wynikow dla podanych kryteriow.</div>`;
    count.textContent = "";
  } else {
    count.textContent = `${shownV} wersji, ${shownS} modulow` + (term?` — fraza: "${term}"`:``);
  }
}

document.getElementById("expand").onclick = ()=>document.querySelectorAll("details.ver").forEach(d=>d.open=true);
document.getElementById("collapse").onclick = ()=>document.querySelectorAll("details.ver").forEach(d=>d.open=false);
let t; q.oninput = ()=>{clearTimeout(t);t=setTimeout(render,120);};
sel.onchange = render; onlynew.onchange = render;
render();
</script>
</body>
</html>
"""


def generate_html(rows, out_path=None, nowe_dni=7):
    payload = []
    for r in rows:
        payload.append({
            "wersja": r["wersja"],
            "data": r["data"],
            "sekcje": [{"name": s["name"], "module": s.get("module", "Ogolne"),
                        "body": s["body"]} for s in r["sekcje"]],
        })
    build = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    html = (HTML_TMPL
            .replace("__NVER__", str(len(rows)))
            .replace("__BUILD__", build)
            .replace("__NOWEDNI__", str(nowe_dni))
            .replace("__DATA__", json.dumps(payload, ensure_ascii=False)))
    target = Path(out_path) if out_path else OUT
    target.write_text(html, "utf-8")
    return target


# --------------------------------------------------------------------------- #
def main():
    rok_teraz = dt.date.today().year
    ap = argparse.ArgumentParser(description="Przegladarka changelogow KS-AOW")
    ap.add_argument("--lata", nargs="+", type=int, default=None,
                    help="konkretne lata do skanowania, np. --lata 2026 2025 2024")
    ap.add_argument("--od-roku", type=int, default=rok_teraz, dest="od_roku",
                    help="poczatek zakresu lat (domyslnie biezacy rok)")
    ap.add_argument("--do-roku", type=int, default=rok_teraz, dest="do_roku",
                    help="koniec zakresu lat (domyslnie biezacy rok)")
    ap.add_argument("--patch-gap", type=int, default=6, dest="pg",
                    help="fallback: ile pustych patchy konczy minor")
    ap.add_argument("--minor-gap", type=int, default=6, dest="ng",
                    help="fallback: ile pustych minorow konczy major")
    ap.add_argument("--major-gap", type=int, default=4, dest="mg",
                    help="fallback: ile pustych majorow konczy rok")
    ap.add_argument("--offline", action="store_true", help="nie pobieraj, zbuduj z cache")
    ap.add_argument("--odswiez", action="store_true",
                    help="pobierz ponownie nawet to co jest w cache")
    ap.add_argument("--out", default=None,
                    help="nazwa pliku wyjsciowego (np. index.html dla GitHub Pages)")
    ap.add_argument("--nowe-dni", type=int, default=7, dest="nowe_dni",
                    help="ile dni od publikacji wersja jest oznaczona jako NOWE (domyslnie 7)")
    args = ap.parse_args()

    lata = args.lata if args.lata else list(range(args.do_roku, args.od_roku - 1, -1))

    CACHE.mkdir(exist_ok=True)
    if args.offline:
        meta = json.loads(META.read_text("utf-8")) if META.exists() else {}
    else:
        print(f"Skanuje lata: {', '.join(map(str, lata))}")
        meta = discover(lata, force=args.odswiez, pg=args.pg, ng=args.ng, mg=args.mg)

    rows = build_dataset(meta)
    if not rows:
        print("Brak danych. Sprobuj np.: --od-roku 2019")
        sys.exit(1)

    out = generate_html(rows, args.out, args.nowe_dni)
    print(f"\nGotowe: {out}  ({len(rows)} wersji)")


if __name__ == "__main__":
    main()
