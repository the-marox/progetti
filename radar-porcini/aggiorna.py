#!/usr/bin/env python3
"""Radar Porcini - aggiornamento giornaliero, tutto nel cloud.

La pioggia di un giorno segue la convenzione del Servizio Idrologico Regionale: dalle 9 alle 9, ora solare.
Qui il giorno D indica la pioggia dalle 9 di D alle 9 di D+1 (nell'archivio SIR porta la data D+1);
temperature, umidita e vento restano per giorno di calendario.

Parti (ognuna scrive i suoi file nella cartella corrente solo se riesce e stampa una riga JSON con l'esito):
  python3 aggiorna.py misure                       -> data.json, aria.json  (tabelle CFR in tempo reale)
  python3 aggiorna.py previsioni                   -> previsioni.json       (ICON-EU, poi ECMWF fino a 10 giorni)
  python3 aggiorna.py archivio [storico.json]      -> storico.json          (pioggia 90 giorni, minime e massime 40)
  python3 aggiorna.py radar [radar.json]           -> radar.json            (pioggia da radar e pluviometri, 90 giorni)
  python3 aggiorna.py [storico.json] [radar.json]  -> tutte le parti, una dopo l'altra
  python3 aggiorna.py valida <cartella>            -> copia nella cartella i soli file validi (e li elenca)
  python3 aggiorna.py avvisi <posti> <pagina.html> [avvisi.json di ieri] -> avvisi.json: punteggi dei posti salvati e
                                                      testo della mail se un posto diventa buono o ottimo in un giorno nuovo
  python3 aggiorna.py mail <avvisi.json> <cartella stato> [indirizzo dell'app] -> manda la mail dei posti (segreti
                                                      GMAIL_USER e GMAIL_APP_PASSWORD) e ricorda i giorni segnalati
  python3 aggiorna.py newsletter <pagina.html> <cartella stato> [indirizzo dell'app] -> mail agli account iscritti alla
                                                      newsletter, sui loro posti (segreto FIREBASE_SERVICE_ACCOUNT, firebase-admin)
Ogni lancio aggiunge il proprio esito, con l'ora e la durata, a esito.json nella stessa cartella.
Va lanciato poco dopo le 9 ora solare (8:16 UTC): con la lettura delle 9 la pioggia del giorno prima e' esatta.
Richiede: Playwright con Chromium, numpy, Pillow, pygrib (pip install pygrib), curl"""
import base64, bz2, glob, io, json, os, re, shutil, subprocess, sys, tempfile, time, datetime as dt, concurrent.futures as cf
import numpy as np

UTC = dt.timezone.utc
INIZIO = time.time()
GIORNO = dt.timedelta(days=1)
LAPSE = 0.0065  # gradi per metro
TMP = tempfile.mkdtemp(prefix="radarporcini_")


def scrivi(obj, percorso):
    """Scrive il JSON in modo atomico: un file interrotto a meta' non sostituisce mai quello buono. Un numero non valido
    (NaN, infinito) e' un errore: nel file non sarebbe JSON valido e la pagina non lo leggerebbe."""
    try:
        with open(percorso + ".tmp", "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except BaseException:
        try:
            os.remove(percorso + ".tmp")
        except OSError:
            pass
        raise
    os.replace(percorso + ".tmp", percorso)


def curl(url, out=None, head=False, intervallo=None, corpo=None):
    a = ["curl", "-sf", "--retry", "2", "-m", "90"] + (["-I"] if head else []) + (["-r", intervallo] if intervallo else [])
    if corpo is not None:
        a += ["-X", "POST", "-H", "Content-Type: application/json", "-d", corpo]
    a += (["-o", out] if out else []) + [url]
    r = subprocess.run(a, capture_output=True)
    return r.returncode == 0 and (True if (out or head) else r.stdout.decode("utf-8", "replace"))


def ora_di(s, predefinita=9.0):
    m = re.search(r"(\d+):(\d+)", s or "")
    return (int(m.group(1)) + int(m.group(2)) / 60) if m else predefinita


def rif_lettura(dati):
    """Ora della lettura CFR in UTC (le tabelle sono in ora solare, UTC+1); senza dati: adesso."""
    try:
        m = re.match(r"(\d+)/(\d+)/(\d+) (\d+):(\d+)", json.load(open(dati))["pioggia_al"])
        d, mo, y, h, mi = map(int, m.groups())
        return dt.datetime(y, mo, d, h, mi, tzinfo=UTC) - dt.timedelta(hours=1)
    except Exception:
        return dt.datetime.now(UTC).replace(second=0, microsecond=0)


def finestra_pioggia(D):
    """Dalle 9 di D alle 9 di D+1, ora solare (8 UTC)."""
    a = dt.datetime(D.year, D.month, D.day, 8, tzinfo=UTC)
    return a, a + GIORNO


def finestra_giorno(D):
    """Giorno di calendario in ora solare: dalle 23 UTC del giorno prima."""
    a = dt.datetime(D.year, D.month, D.day, tzinfo=UTC) - dt.timedelta(hours=1)
    return a, a + GIORNO


# ---------------------------------------------------------------- 1) misure CFR
JS_DATA = r"""async () => {
const num=s=>{s=String(s??'').replace(/<[^>]+>/g,'').trim();if(s===''||s==='-')return null;const v=parseFloat(s.replace(',','.'));return isNaN(v)?null:v;};
const tt=[...document.querySelectorAll('table')].find(x=>x.querySelectorAll('tbody tr').length>10);
const tref=(document.body.innerText.match(/riferite al ([0-9\/]+ [0-9\.]+)/)||[])[1]||'';
const T={};
jQuery(tt).DataTable().rows().nodes().toArray().forEach(r=>{const c=[...r.cells].map(c=>c.innerText.trim());const v=[num(c[7]),num(c[9]),num(c[11]),num(c[13])];if(v[0]===0&&v[1]===0){v[0]=null;v[1]=null;}if(v[2]===0&&v[3]===0){v[2]=null;v[3]=null;}if(v.every(x=>x===null))return;T[c[0]]=v;});
const hp=await fetch('/monitoraggio/stazioni.php?type=pluvio_men').then(r=>r.text());
const pref=(hp.replace(/&nbsp;/g,' ').match(/riferite al\s+([0-9\/]+\s+[0-9\.]+)/)||[])[1]||'';
const M=[];const re=/VALUES\[\d+\]\s*=\s*new Array\((.*?)\);/g;let m;
while((m=re.exec(hp))){const c=JSON.parse('['+m[1]+']');M.push([c[0],num(c[5]),num(c[7]),num(c[8]),num(c[9]),num(c[10]),num(c[11]),num(c[12]),num(c[13]),num(c[14])].concat(T[c[0]]||[null,null,null,null]));}
const dd=pref.split(' ')[0].split('/');
const hm=s=>s.replace(/(\d+)\.(\d+)$/,'$1:$2')+' (ora solare)';
return JSON.stringify({fonte:'Centro Funzionale Regione Toscana – rete SIR (pluviometri e termometri), dati in tempo reale non validati',giorno:dd[2]+'-'+dd[1]+'-'+dd[0],pioggia_al:hm(pref),temperature_al:hm(tref),letti_il:new Date().toISOString(),campi:['codice','oggi','g1','g2','g5','g7','g10','g15','g30','ggSecchi','tminOggi','tmaxOggi','tminIeri','tmaxIeri'],misure:M});
}"""

JS_ARIA = r"""async () => {
const num=s=>{s=String(s??'').trim();if(s===''||s==='-')return null;const v=parseFloat(s.replace(',','.'));return isNaN(v)?null:v;};
async function tabella(type){const f=document.createElement('iframe');f.style.cssText='position:absolute;width:1200px;height:800px;left:-5000px;top:0';f.src='/monitoraggio/stazioni.php?type='+type;document.body.appendChild(f);await new Promise(r=>f.onload=r);for(let i=0;i<160;i++){const w=f.contentWindow;const t=[...f.contentDocument.querySelectorAll('table')].find(x=>x.querySelectorAll('tbody tr').length>10);if(t&&w.jQuery&&w.jQuery.fn.dataTable&&w.jQuery.fn.dataTable.isDataTable(t)){const rows=w.jQuery(t).DataTable().rows().nodes().toArray().map(r=>[...r.cells].map(c=>c.innerText.trim()));const ref=(f.contentDocument.body.textContent.replace(/\u00a0/g,' ').match(/riferit\w*\s+al\s+([0-9\/]+\s+[0-9\.]+)/)||[])[1]||'';f.remove();return{rows,ref};}await new Promise(r=>setTimeout(r,250));}f.remove();throw new Error('tabella '+type+' non caricata');}
async function coord(type){const j=await fetch('/monitoraggio/actions.php?action=list&rt=0&type_gauge='+type+'&speed=km/h').then(r=>r.json());const C={};j.features.forEach(p=>{C[p.IDStazione]=[+(+p.Lat).toFixed(4),+(+p.Lon).toFixed(4)];});return C;}
const [U,W,CU,CW]=await Promise.all([tabella('igro'),tabella('anemo'),coord('igro'),coord('anemo')]);
const z2=(a,b)=>(a===0&&b===0)?[null,null]:[a,b];
const umid=U.rows.filter(c=>CU[c[0]]).map(c=>[c[0],...CU[c[0]],num(c[4]),...z2(num(c[7]),num(c[9])),...z2(num(c[11]),num(c[13]))]).filter(r=>r.slice(4).some(v=>v!==null));
const vento=W.rows.filter(c=>CW[c[0]]).map(c=>[c[0],...CW[c[0]],num(c[4]),num(c[10]),num(c[9]),num(c[14]),num(c[13]),num(c[11]),num(c[15])]).filter(r=>r.slice(4,8).some(v=>v!==null));
const hm=s=>s?s.replace(/(\d+)\.(\d+)$/,'$1:$2')+' (ora solare)':'';
return JSON.stringify({umidita_al:hm(U.ref),vento_al:hm(W.ref),letti_il:new Date().toISOString(),campi_umidita:['codice','lat','lon','quota','urMinOggi','urMaxOggi','urMinIeri','urMaxIeri'],campi_vento:['codice','lat','lon','quota','velMaxOggi','raffMaxOggi','velMaxIeri','raffMaxIeri','dirMaxOggi','dirMaxIeri'],umidita:umid,vento:vento});
}"""


def breve(e, n=400):
    """Messaggio d'errore su una riga, abbastanza corto da stare in esito.json."""
    return re.sub(r"\s+", " ", "%s: %s" % (type(e).__name__, e)).strip()[:n]


def avvia_chromium(p, opz):
    """Prova il Chromium di Playwright, poi quelli installati in /opt/pw-browsers."""
    errori = []
    for exe in [None, "/opt/pw-browsers/chromium"] + sorted(
            os.path.join(r, f) for r, _, fs in os.walk("/opt/pw-browsers") for f in fs if f in ("chrome", "headless_shell")):
        try:
            return p.chromium.launch(**opz) if exe is None else p.chromium.launch(executable_path=exe, **opz)
        except Exception as e:
            errori.append("%s -> %s" % (exe or "predefinito", breve(e, 200)))
    raise RuntimeError("Chromium non parte: " + " | ".join(errori))


def leggi_cfr(out="."):
    from playwright.sync_api import sync_playwright
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    esito = {}
    with sync_playwright() as p:
        opz = {"headless": True, "proxy": {"server": proxy} if proxy else None}
        b = avvia_chromium(p, opz)
        page = b.new_page(viewport={"width": 1280, "height": 900})
        for tentativo in range(3):
            if tentativo and time.time() - INIZIO > 150:
                break
            try:
                page.goto("https://www.sir.toscana.it/monitoraggio/stazioni.php?type=termo", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_function("() => window.jQuery && [...document.querySelectorAll('table')].some(t => t.querySelectorAll('tbody tr').length > 10)", timeout=60000)
                d = json.loads(page.evaluate(JS_DATA))
                if len(d["misure"]) < 300:
                    raise RuntimeError("solo %d stazioni" % len(d["misure"]))
                scrivi(d, os.path.join(out, "data.json"))
                esito["data"] = "pioggia al %s, %d stazioni" % (d["pioggia_al"], len(d["misure"]))
                break
            except Exception as e:
                esito["data"] = "errore: %s" % breve(e)
                time.sleep(10)
        for tentativo in range(3):
            if tentativo and time.time() - INIZIO > 240:
                break
            try:
                page.goto("https://www.cfr.toscana.it/index.php?IDS=42&IDSS=279", wait_until="domcontentloaded", timeout=60000)
                a = json.loads(page.evaluate(JS_ARIA))
                if len(a["umidita"]) < 100 or len(a["vento"]) < 50:
                    raise RuntimeError("umidita %d, vento %d" % (len(a["umidita"]), len(a["vento"])))
                scrivi(a, os.path.join(out, "aria.json"))
                esito["aria"] = "umidita e vento al %s" % a["umidita_al"]
                break
            except Exception as e:
                esito["aria"] = "errore: %s" % breve(e)
                time.sleep(10)
        b.close()
    return esito


# ---------------------------------------------------------------- 2) previsioni: ICON-EU, poi ECMWF
try:
    import pygrib  # noqa: E402
except ImportError:  # senza pygrib si aggiornano comunque misure, archivio e radar
    pygrib = None

BASE = "https://opendata.dwd.de/weather/nwp/icon-eu/grib"
EC = "https://data.ecmwf.int/forecasts"
# il portale ECMWF accetta al massimo 750 connessioni insieme, per tutti, e quando e' pieno rifiuta le nuove (succede
# nelle ore in cui escono le corse): gli stessi file sono copiati su Google Cloud e AWS, che il Centro europeo indica
# per quando il portale non risponde
EC_COPIE = ("https://storage.googleapis.com/ecmwf-open-data", "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com")
ec_conto = {}  # in questo giro: scaricamenti non riusciti per fonte, e file presi dalle copie
PASSO = 0.0625
J0, J1, I0, I1 = 203, 241, 530, 575  # griglia ICON-EU: lat 42,1875-44,5625, lon 9,625-12,4375
LAT0, LON0, NLAT, NLON = 29.5 + J0 * PASSO, -23.5 + I0 * PASSO, J1 - J0 + 1, I1 - I0 + 1
VARS = ("t_2m", "relhum_2m", "u_10m", "v_10m")
GIORNI_PREV = 11  # oggi e i dieci giorni dopo
EC_PASSI = list(range(0, 145, 3)) + list(range(150, 361, 6))
EGRIGLIA = {"lat0": 42.0, "lon0": 9.5, "passo": 0.25, "nlat": 12, "nlon": 13}  # celle ECMWF sopra la Toscana, da sud


def url(run, var, step):
    return f"{BASE}/{run:%H}/{var}/icon-eu_europe_regular-lat-lon_single-level_{run:%Y%m%d%H}_{step:03d}_{var.upper()}.grib2.bz2"


def scegli_corsa():
    cand = set()
    for h in ("00", "06", "12", "18"):
        for m in re.findall(r"single-level_(\d{10})_000_TOT_PREC", curl(f"{BASE}/{h}/tot_prec/") or ""):
            cand.add(dt.datetime.strptime(m, "%Y%m%d%H").replace(tzinfo=UTC))
    for run in sorted(cand, reverse=True):
        if all(curl(url(run, v, 120), head=True) for v in ("tot_prec",) + VARS):
            return run
    raise RuntimeError("nessuna corsa ICON-EU completa disponibile")


def campo(job):
    run, var, step = job
    if time.time() - INIZIO > 420:
        raise RuntimeError("tempo scaduto durante lo scaricamento delle previsioni")
    f = os.path.join(TMP, f"{var}_{step:03d}.bz2")
    u = (f"{BASE}/{run:%H}/hsurf/icon-eu_europe_regular-lat-lon_time-invariant_{run:%Y%m%d%H}_HSURF.grib2.bz2"
         if var == "hsurf" else url(run, var, step))
    if not curl(u, f):
        raise RuntimeError("download non riuscito: " + u)
    m = pygrib.fromstring(bz2.decompress(open(f, "rb").read()))
    os.remove(f)
    return (var, step), np.asarray(m.values, dtype=np.float64)[J0:J1 + 1, I0:I1 + 1]


def passi(s):  # passi ICON attorno a s ore (orari fino a 78, poi ogni 3) e peso
    s = min(max(s, 0.0), 120.0)
    k = 1 if s <= 78 else 3
    a, b = int(k * np.floor(s / k)), int(k * np.ceil(s / k))
    return a, b, (0.0 if a == b else (s - a) / (b - a))


def riassunto(tmin, tmax, U, uu, vv):
    sp = np.hypot(uu, vv) * 3.6
    im = sp.argmax(axis=0)
    pick = lambda X: np.take_along_axis(X, im[None], axis=0)[0]
    dirm = (np.degrees(np.arctan2(-pick(uu), -pick(vv))) + 360) % 360
    return {"tmin": tmin, "tmax": tmax, "urMin": np.clip(U.min(0), 0, 100), "urMax": np.clip(U.max(0), 0, 100), "vento": sp.max(0), "dir": dirm}


def icon_giorni(giorni, rif):
    run = scegli_corsa()
    fine = run + dt.timedelta(hours=120)
    ore = lambda x: (x - run).total_seconds() / 3600
    tp_passi = set()
    for D in giorni:
        a, b = finestra_pioggia(D)
        if a < fine:
            for x in (max(a, run), min(b, fine)):
                p = passi(ore(x)); tp_passi |= {p[0], p[1]}
    p = passi(ore(min(max(rif, run), fine))); tp_passi |= {p[0], p[1]}
    tp_passi.discard(0)
    jobs = [(run, "tot_prec", s) for s in sorted(tp_passi)] + [(run, v, s) for v in VARS for s in range(0, 121, 3)] + [(run, "hsurf", 0)]
    F = {}
    with cf.ThreadPoolExecutor(12) as ex:
        for k, v in ex.map(campo, jobs):
            F[k] = v
    zero = np.zeros_like(F[("hsurf", 0)])

    def tp(x):
        a, b, w = passi(ore(x))
        A = zero if a == 0 else F[("tot_prec", a)]
        B = zero if b == 0 else F[("tot_prec", b)]
        return A * (1 - w) + B * w

    out = {}
    for D in giorni:
        a, b = finestra_pioggia(D)
        if a >= fine:
            continue
        g = {"data": D.isoformat(), "modello": "ICON-EU", "copertura": round(max(0.0, (min(b, fine) - max(a, run)).total_seconds() / 86400), 2),
             "pioggia": np.maximum(0, tp(min(b, fine)) - tp(max(a, run)))}
        ga, gb = finestra_giorno(D)
        cs = [s for s in range(0, 121, 3) if ga <= run + dt.timedelta(hours=s) < gb]
        if len(cs) >= 5:
            T = np.stack([F[("t_2m", s)] for s in cs]) - 273.15
            g.update(riassunto(T.min(0), T.max(0), np.stack([F[("relhum_2m", s)] for s in cs]),
                               np.stack([F[("u_10m", s)] for s in cs]), np.stack([F[("v_10m", s)] for s in cs])))
        out[D] = g
    a, b = finestra_pioggia(giorni[0])
    x0 = max(rif, a, run)
    resto = (x0, np.maximum(0, tp(min(b, fine)) - tp(x0))) if x0 < min(b, fine) else None
    return {"run": run, "quota": F[("hsurf", 0)], "giorni": out, "resto": resto}


def ec_file(run, step, ext):
    return f"{run:%Y%m%d}/{run:%H}z/ifs/0p25/oper/{run:%Y%m%d%H}0000-{step}h-oper-fc.{ext}"


def ec_scarica(percorso, out=None, head=False, intervallo=None):
    """Un file dei dati aperti ECMWF dal portale o, se non lo da', dalle copie. Una fonte che in questo giro non ha dato
    3 file si prova per ultima (le richieste head non contano: per le corse non ancora uscite e' normale)."""
    for f in sorted((EC,) + EC_COPIE, key=lambda f: ec_conto.get(f, 0) >= 3):
        r = curl(f + "/" + percorso, out, head=head, intervallo=intervallo)
        if r:
            if f != EC and not head:
                ec_conto["copie"] = ec_conto.get("copie", 0) + 1
            return r
        if not head:
            ec_conto[f] = ec_conto.get(f, 0) + 1
    return False


def ec_attorno(h):  # passi ECMWF che racchiudono h ore dall'inizio della corsa, e peso
    if h <= 0:
        return 0, 0, 0.0
    for a, b in zip(EC_PASSI, EC_PASSI[1:]):
        if a <= h <= b:
            return a, b, (0.0 if a == b else (h - a) / (b - a))
    return EC_PASSI[-1], EC_PASSI[-1], 0.0


def ec_indice(job):
    run, step = job
    txt = ec_scarica(ec_file(run, step, "index"))
    if not txt:
        raise RuntimeError("indice ECMWF mancante al passo %d" % step)
    idx = {}
    for riga in txt.splitlines():
        if riga.strip():
            x = json.loads(riga)
            if x.get("levtype") == "sfc":
                idx[x["param"]] = (x["_offset"], x["_length"])
    return step, idx


def su_griglia(m):
    """Celle ECMWF (0,25 gradi) sopra la Toscana, righe da sud a nord come la griglia ICON."""
    v = np.asarray(m.values, dtype=np.float64)
    la1, lo1 = m["latitudeOfFirstGridPointInDegrees"], m["longitudeOfFirstGridPointInDegrees"]
    dla, dlo = m["jDirectionIncrementInDegrees"], m["iDirectionIncrementInDegrees"]
    g = EGRIGLIA
    lat, lon = g["lat0"] + g["passo"] * np.arange(g["nlat"]), g["lon0"] + g["passo"] * np.arange(g["nlon"])
    j = np.round(((lat - la1) / dla) if m["jScansPositively"] else ((la1 - lat) / dla)).astype(int)
    i = np.round(((lon - lo1) % 360) / dlo).astype(int)
    return v[np.ix_(j, i)]


def ec_campo(job):
    run, step, param, off, lun = job
    if time.time() - INIZIO > 420:
        raise RuntimeError("tempo scaduto durante lo scaricamento delle previsioni ECMWF")
    f = os.path.join(TMP, "ec_%s_%d.grib2" % (param, step))
    if not ec_scarica(ec_file(run, step, "grib2"), f, intervallo="%d-%d" % (off, off + lun - 1)):
        raise RuntimeError("download ECMWF non riuscito: %s al passo %d" % (param, step))
    m = pygrib.fromstring(open(f, "rb").read())
    os.remove(f)
    return (param, step), su_griglia(m)


def es(t):  # tensione di vapore saturo (hPa), t in gradi
    return 6.112 * np.exp(17.67 * t / (t + 243.5))


def ec_servono(run, giorni):
    """I passi della corsa che servono per questi giorni, e per ogni passo i campi."""
    ore = lambda x: (x - run).total_seconds() / 3600
    serve = {0: {"z"}}

    def add(s, p):
        serve.setdefault(s, set()).add(p)

    for D in giorni:
        for x in finestra_pioggia(D):
            a, b, w = ec_attorno(ore(x))
            for s in (a, b):
                if s > 0:
                    add(s, "tp")
        ga, gb = finestra_giorno(D)
        for s in EC_PASSI:
            if ga <= run + dt.timedelta(hours=s) < gb:
                for p in ("2t", "2d", "10u", "10v"):
                    add(s, p)
            P = 3 if s <= 144 else 6
            if s > 0 and ga <= run + dt.timedelta(hours=s - P / 2) < gb:
                add(s, "mx2t%d" % P); add(s, "mn2t%d" % P)
    return serve


def ecmwf_giorni(giorni):
    """Giorni dal modello IFS del Centro europeo (dati aperti a 0,25 gradi), sulle sue celle; quota della sua orografia.
    La corsa piu' recente si usa solo se arrivano tutti i file che servono, dal portale o dalle copie: se no si usa quella
    di 12 ore prima (le copie arrivano dopo il portale, e per la corsa appena uscita possono non averli ancora)."""
    adesso = dt.datetime.now(UTC)
    base = adesso.replace(hour=12 if adesso.hour >= 12 else 0, minute=0, second=0, microsecond=0)
    ultimo = finestra_pioggia(giorni[-1])[1]
    run, saltate = None, []
    for k in range(4):
        r = base - dt.timedelta(hours=12 * k)
        h = (ultimo - r).total_seconds() / 3600
        s = next((p for p in EC_PASSI if p >= h), None)
        if s is None or not ec_scarica(ec_file(r, s, "index"), head=True):
            continue
        serve = ec_servono(r, giorni)
        try:
            with cf.ThreadPoolExecutor(8) as ex:
                idx = dict(ex.map(ec_indice, [(r, p) for p in serve]))
            jobs = [(r, p, c) + idx[p][c] for p in serve for c in sorted(serve[p]) if c in idx[p]]
            F = {}
            with cf.ThreadPoolExecutor(12) as ex:
                for kk, v in ex.map(ec_campo, jobs):
                    F[kk] = v
        except RuntimeError as e:
            saltate.append("corsa %s UTC saltata: %s" % (r.strftime("%Y-%m-%d %H"), e))
            continue
        run = r
        break
    if run is None:
        raise RuntimeError("; ".join(saltate) or "nessuna corsa ECMWF disponibile")
    ore = lambda x: (x - run).total_seconds() / 3600
    zq = F[("z", 0)] / 9.80665

    def tp(x):
        a, b, w = ec_attorno(ore(x))
        A = 0.0 if a == 0 else F[("tp", a)]
        B = 0.0 if b == 0 else F[("tp", b)]
        return (A * (1 - w) + B * w) * 1000

    out = {}
    for D in giorni:
        a, b = finestra_pioggia(D)
        g = {"data": D.isoformat(), "modello": "ECMWF", "copertura": round(max(0.0, (b - max(a, run)).total_seconds() / 86400), 2),
             "pioggia": np.maximum(0, tp(b) - tp(max(a, run)))}
        ga, gb = finestra_giorno(D)
        cs = [s for s in EC_PASSI if ga <= run + dt.timedelta(hours=s) < gb and ("2t", s) in F]
        if len(cs) >= 3:
            T = np.stack([F[("2t", s)] for s in cs]) - 273.15
            Td = np.stack([F[("2d", s)] for s in cs]) - 273.15
            U = 100 * es(Td) / es(T)
            mx = [F[(p, s)] for (p, s) in F if p.startswith("mx2t") and ga <= run + dt.timedelta(hours=s - (3 if s <= 144 else 6) / 2) < gb]
            mn = [F[(p, s)] for (p, s) in F if p.startswith("mn2t") and ga <= run + dt.timedelta(hours=s - (3 if s <= 144 else 6) / 2) < gb]
            tmax = np.max(np.stack(mx), 0) - 273.15 if mx else T.max(0)
            tmin = np.min(np.stack(mn), 0) - 273.15 if mn else T.min(0)
            g.update(riassunto(tmin, tmax, U, np.stack([F[("10u", s)] for s in cs]), np.stack([F[("10v", s)] for s in cs])))
        out[D] = g
    return {"run": run, "quota": zq, "giorni": out, "saltate": saltate}


EP_SOGLIE = (5, 10, 20)  # mm in 24 ore


def ep_file(run, fine, ext):
    return f"{run:%Y%m%d}/{run:%H}z/ifs/0p25/enfo/{run:%Y%m%d%H}0000-{fine}h-enfo-ep.{ext}"


def ep_indice(run, fine):
    txt = ec_scarica(ep_file(run, fine, "index"))
    if not txt:
        return None
    idx = {}
    for riga in txt.splitlines():
        if riga.strip():
            x = json.loads(riga)
            if x.get("type") == "ep" and str(x.get("param", "")).startswith("tpg"):
                idx[(x["param"], x["step"])] = (x["_offset"], x["_length"])
    return idx


def ep_campo(job):
    run, fine, param, step, off, lun = job
    if time.time() - INIZIO > 450:
        raise RuntimeError("tempo scaduto durante lo scaricamento delle probabilita' ECMWF")
    f = os.path.join(TMP, "ep_%s_%s.grib2" % (param, step))
    if not ec_scarica(ep_file(run, fine, "grib2"), f, intervallo="%d-%d" % (off, off + lun - 1)):
        raise RuntimeError("download delle probabilita' ECMWF non riuscito: %s %s" % (param, step))
    m = pygrib.fromstring(open(f, "rb").read())
    os.remove(f)
    return (param, step), su_griglia(m)


def ecmwf_probabilita(giorni):
    """Probabilita' di superare 5, 10 e 20 mm in 24 ore dall'insieme di 51 previsioni ECMWF (dati aperti). Le finestre
    dell'insieme vanno dalle 12 alle 12 UTC: si usa quella che parte alle 12 del giorno, 4 ore dopo il giorno di pioggia 9-9."""
    adesso = dt.datetime.now(UTC)
    base = adesso.replace(hour=12 if adesso.hour >= 12 else 0, minute=0, second=0, microsecond=0)
    saltate = []
    for k in range(4):
        run = base - dt.timedelta(hours=12 * k)
        finestre = {}
        for D in giorni:
            h = int((dt.datetime(D.year, D.month, D.day, 12, tzinfo=UTC) - run).total_seconds() // 3600)
            if h >= 0 and h % 12 == 0 and h + 24 <= 360:
                finestre[D] = ("%d-%d" % (h, h + 24), 240 if h + 24 <= 240 else 360)
        if len(finestre) < len(giorni) - 1:
            continue
        indici = {}
        for fine in sorted({f for _, f in finestre.values()}):
            indici[fine] = ep_indice(run, fine)
        if not all(indici.values()):
            continue
        jobs = []
        for D, (step, fine) in finestre.items():
            for s in EP_SOGLIE:
                key = ("tpg%d" % s, step)
                if key in indici[fine]:
                    jobs.append((run, fine, key[0], step) + indici[fine][key])
        if not jobs:
            continue
        F = {}
        try:
            with cf.ThreadPoolExecutor(8) as ex:
                for kk, v in ex.map(ep_campo, jobs):
                    F[kk] = v
        except RuntimeError as e:
            saltate.append("corsa %s UTC saltata: %s" % (run.strftime("%Y-%m-%d %H"), e))
            continue
        out = []
        for D in giorni:
            if D not in finestre:
                continue
            step = finestre[D][0]
            g = {"data": D.isoformat()}
            for s in EP_SOGLIE:
                v = F.get(("tpg%d" % s, step))
                if v is not None:
                    g["p%d" % s] = [int(round(float(x))) for x in np.clip(np.asarray(v), 0, 100).ravel()]
            if len(g) > 1:
                out.append(g)
        if out:
            return {"corsa": run.strftime("%Y-%m-%dT%H:%MZ"), "soglie": list(EP_SOGLIE), "griglia": EGRIGLIA,
                    "finestra": "24 ore dalle 13 ora solare del giorno (il giorno di pioggia va dalle 9 alle 9)",
                    "unita": "percentuale delle 51 previsioni dell'insieme ECMWF", "giorni": out}
    raise RuntimeError("; ".join(saltate) or "probabilita' ECMWF non disponibili")


def previsioni(dati="data.json", uscita="previsioni.json"):
    """Pioggia per giorni dalle 9 alle 9 (come l'archivio), il resto per giorno di calendario. ICON-EU per i giorni che copre
    per intero (di solito fino al quarto), poi ECMWF fino al decimo; in piu' la probabilita' di pioggia dall'insieme ECMWF."""
    if pygrib is None:
        raise RuntimeError("manca pygrib (pip install pygrib)")
    rif = rif_lettura(dati)
    G = (rif + dt.timedelta(hours=1)).date()
    giorni = [G + k * GIORNO for k in range(GIORNI_PREV)]
    note, icon, ecm = [], None, None
    ec_conto.clear()
    try:
        icon = icon_giorni(giorni, rif)
        note.append("ICON-EU %s UTC" % icon["run"].strftime("%Y-%m-%d %H"))
    except Exception as e:
        note.append("ICON-EU non disponibile (%s)" % e)
    complete = {D for D, g in (icon["giorni"].items() if icon else []) if g["copertura"] >= 0.99}
    mancano = [D for D in giorni if D not in complete]
    if mancano:
        try:
            ecm = ecmwf_giorni(mancano)
            note.append("ECMWF %s UTC%s" % (ecm["run"].strftime("%Y-%m-%d %H"), " (%s)" % "; ".join(ecm["saltate"]) if ecm["saltate"] else ""))
        except Exception as e:
            note.append("ECMWF non disponibile (%s)" % e)
    scelti = []
    for D in giorni:
        if D in complete:
            scelti.append(icon["giorni"][D])
        elif ecm and D in ecm["giorni"]:
            scelti.append(ecm["giorni"][D])
        elif icon and D in icon["giorni"]:
            scelti.append(icon["giorni"][D])
    if not scelti:
        raise RuntimeError("nessuna previsione: " + "; ".join(note))
    prob = None
    try:
        prob = ecmwf_probabilita(giorni)
        note.append("probabilita' ECMWF %s UTC" % prob["corsa"][:13].replace("T", " "))
    except Exception as e:
        note.append("probabilita' non disponibili (%s)" % breve(e, 120))
    if ec_conto.get("copie"):
        note.append("%d file ECMWF dalle copie su Google Cloud e AWS (il portale non li dava)" % ec_conto["copie"])
    q = lambda arr, k=1: [int(round(float(v) * k)) for v in np.asarray(arr).ravel()]
    out_giorni = []
    for g in scelti:
        x = {"data": g["data"], "modello": g["modello"], "copertura": g["copertura"], "pioggia": q(g["pioggia"], 10)}
        if "tmin" in g:
            x.update(tmin=q(g["tmin"], 10), tmax=q(g["tmax"], 10), urMin=q(g["urMin"]), urMax=q(g["urMax"]), vento=q(g["vento"]), dir=q(g["dir"]))
        out_giorni.append(x)
    resto = None
    if icon and icon["resto"] and giorni[0] in complete:
        resto = {"giorno": G.isoformat(), "dalle": icon["resto"][0].strftime("%Y-%m-%dT%H:%MZ"), "pioggia": q(icon["resto"][1], 10)}
    corsa = icon["run"] if icon else ecm["run"]
    out = {"fonte": "DWD, modello ICON-EU, e ECMWF, modello IFS (dati aperti, licenza CC BY 4.0)",
           "corsa": corsa.strftime("%Y-%m-%dT%H:%MZ"), "corsa_ecmwf": ecm["run"].strftime("%Y-%m-%dT%H:%MZ") if ecm else None,
           "creato": dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "finestra_pioggia": "dalle 9 del giorno alle 9 del giorno dopo, ora solare",
           "griglia": {"lat0": LAT0, "lon0": LON0, "passo": PASSO, "nlat": NLAT, "nlon": NLON} if icon else EGRIGLIA,
           "griglia_ecmwf": EGRIGLIA if ecm else None, "quota_ecmwf": q(ecm["quota"]) if ecm else None,
           "unita": {"pioggia": "0,1 mm", "tmin": "0,1 °C", "tmax": "0,1 °C", "urMin": "%", "urMax": "%", "vento": "km/h (massimo dei valori ogni 3 o 6 ore)", "dir": "gradi, provenienza del vento più forte"},
           "quota": q(icon["quota"] if icon else ecm["quota"]), "resto_oggi": resto, "giorni": out_giorni, "probabilita": prob}
    scrivi(out, uscita)
    n_ec = sum(1 for g in out_giorni if g["modello"] == "ECMWF")
    return "%s; %d giorni (%d ICON-EU, %d ECMWF)" % ("; ".join(note), len(out_giorni), len(out_giorni) - n_ec, n_ec)


# ---------------------------------------------------------------- 3) archivio giornaliero delle stazioni
SIR = "https://www.sir.toscana.it/archivio/dati.php?A={anno}&IDS={cod}&IDST={tipo}"
NP, NT = 90, 40  # giorni di pioggia e di temperature conservati
LIMITE = 510  # secondi dall'avvio: lo script deve stare nei 10 minuti di un comando
CONVENZIONE = "pioggia dalle 9 alle 9 ora solare, con la data di inizio; temperature per giorno di calendario"


def analizza(h, tipo, anno):
    """Valori giornalieri di un anno: pioggia in mm ('-' = niente pioggia) oppure (minima, massima)."""
    out = {}
    for riga in re.findall(r"<tr[^>]*>(.*?)</tr>", h, re.S):
        celle = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", riga, re.S)
        if len(celle) < 13:
            continue
        g = re.sub(r"<[^>]+>", "", celle[0]).replace("&nbsp;", "").strip()
        if not g.isdigit():
            continue
        for mese, c in enumerate(celle[1:13], start=1):
            try:
                giorno = dt.date(anno, mese, int(g))
            except ValueError:
                continue
            if tipo == "pluvio":
                s = re.sub(r"<[^>]+>", "", c).replace("&nbsp;", "").strip()
                if s == "-":
                    out[giorno] = 0.0
                elif s:
                    try:
                        out[giorno] = float(s.replace(",", "."))
                    except ValueError:
                        pass
            else:
                vals = []
                for x in re.split(r"<br\s*/?>", c)[:2]:
                    s = re.sub(r"<[^>]+>", "", x).replace("&nbsp;", "").strip()
                    try:
                        vals.append(float(s.replace(",", ".")))
                    except ValueError:
                        vals.append(None)
                while len(vals) < 2:
                    vals.append(None)
                if vals[0] is not None or vals[1] is not None:
                    out[giorno] = (vals[1], vals[0])  # (minima, massima): la massima e' scritta per prima
    return out


def scarica_archivi(jobs, tempo_max=300):
    """Scarica in sequenza con una sola connessione: l'archivio regge circa una richiesta ogni 4 secondi."""
    cartella = tempfile.mkdtemp(prefix="sir_")
    cfg = os.path.join(cartella, "curl.cfg")
    with open(cfg, "w") as f:
        for i, (c, tipo, anno) in enumerate(jobs):
            f.write('url = "%s"\noutput = "%s"\n' % (SIR.format(anno=anno, cod=c, tipo=tipo), os.path.join(cartella, "%d.html" % i)))
    try:
        subprocess.run(["curl", "-sf", "--retry", "2", "-m", "90", "-K", cfg], capture_output=True, timeout=tempo_max)
    except subprocess.TimeoutExpired:
        pass  # i file non scaricati restano mancanti e si riprovano alla prossima esecuzione
    out = []
    for i, (c, tipo, anno) in enumerate(jobs):
        try:
            h = open(os.path.join(cartella, "%d.html" % i), encoding="utf-8", errors="replace").read()
            valido = ("Anno" in h and "</table>" in h) or "Nessun risultato" in h  # anno senza dati: risposta valida
            out.append(((c, tipo, anno), analizza(h, tipo, anno) if valido else None))
        except Exception:
            out.append(((c, tipo, anno), None))
    return out


def aggiorna_storico(dati="data.json", precedente=None, uscita="storico.json", max_richieste=100, tempo_max=None):
    """Pioggia giornaliera dalle 9 alle 9 (90 giorni) e minime e massime (40 giorni) fino a ieri, per stazione.
    Con la lettura delle 9 del giorno G la pioggia del giorno G-1 e' esatta: «oggi» (da mezzanotte alle 9) piu'
    «1 giorno» (dalle 9 di ieri a mezzanotte). Con una lettura a un'altra ora la finestra e' spostata: il valore resta tra
    le «stime» e si sostituisce col dato d'archivio appena c'e'. Minime e massime di ieri vengono dalle tabelle di oggi.
    I giorni che mancano, o che sono stimati, si prendono dall'archivio della stazione (un anno per richiesta) finche' c'e'
    tempo; per ogni stazione «verificato» dice fino a che giorno l'archivio e' gia' stato cercato."""
    d = json.load(open(dati))
    G = dt.date.fromisoformat(d["giorno"])
    al = G - GIORNO
    ora = ora_di(d.get("pioggia_al"))
    esatta = abs(ora - 9) <= 0.3
    giorniP = [al - (NP - 1 - i) * GIORNO for i in range(NP)]
    giorniT = giorniP[-NT:]
    live = {m[0]: m for m in d["misure"]}
    P = {c: {} for c in live}         # giorno -> mm (None: l'archivio non ha il dato)
    T = {c: {} for c in live}         # giorno -> (minima, massima)
    stime = {c: set() for c in live}  # giorni di pioggia da sostituire con l'archivio
    verif = {"pluvio": {}, "termo": {}}
    termo = {c for c, m in live.items() if any(v is not None for v in m[10:14])}
    if precedente and os.path.exists(precedente):
        try:
            s = json.load(open(precedente))
            al0 = dt.date.fromisoformat(s["al"])
            vecchia = "convenzione" not in s  # file della prima versione: pioggia con la data di fine
            sposta = GIORNO if vecchia else dt.timedelta(0)

            def giorni(arr, k=dt.timedelta(0)):
                for i, v in enumerate(arr):
                    yield al0 - (len(arr) - 1 - i) * GIORNO - k, (None if v is None else v / 10)

            for c, arr in s["pioggia"]["valori"].items():
                if c in live:
                    for g, x in giorni(arr, sposta):
                        if giorniP[0] <= g <= al and not (vecchia and g == al0 - GIORNO):
                            P[c][g] = x
            for k, i in (("tmin", 0), ("tmax", 1)):
                for c, arr in s["temperature"][k].items():
                    if c in live:
                        termo.add(c)
                        for g, x in giorni(arr):
                            if giorniT[0] <= g <= al:
                                t = list(T[c].get(g, (None, None)))
                                t[i] = x
                                T[c][g] = tuple(t)
            if not vecchia:
                for g, codici in (s.get("stime") or {}).items():
                    g = dt.date.fromisoformat(g)
                    for c in codici:
                        if c in live and giorniP[0] <= g <= al:
                            stime[c].add(g)
                for tipo in verif:
                    for c, g in ((s.get("verificato") or {}).get(tipo) or {}).items():
                        verif[tipo][c] = dt.date.fromisoformat(g)
        except Exception as e:
            print("storico precedente non leggibile:", e, file=sys.stderr)
    # pioggia di ieri (dalle 9 di ieri alle 9 di oggi) e minime e massime di ieri dalle tabelle in tempo reale
    for c, m in live.items():
        if m[12] is not None or m[13] is not None:
            t = T[c].get(al, (None, None))
            T[c][al] = (t[0] if m[12] is None else m[12], t[1] if m[13] is None else m[13])
            termo.add(c)
        if m[1] is None or m[2] is None or (P[c].get(al) is not None and al not in stime[c]):
            continue
        P[c][al] = round(m[1] + m[2], 1)
        asciutto = (m[3] or 0) + (m[1] or 0) < 1.0  # meno di 1 mm in due giorni: lo spostamento della finestra non conta
        if esatta or asciutto:
            stime[c].discard(al)
        else:
            stime[c].add(al)
    # richieste all'archivio: prima la pioggia che manca, poi le temperature, poi i giorni stimati.
    # Nell'archivio la pioggia del giorno g ha la data g+1; le temperature la data g.
    MIN = dt.date.min

    def manca(tipo, c, g):
        if tipo == "pluvio":
            if g == al:
                return False  # nell'archivio arriva domani
            if g not in P[c]:
                return True
            vuoto = P[c][g] is None
        else:
            t = T[c].get(g)
            if t is None:
                return True
            vuoto = t[0] is None or t[1] is None
        return vuoto and g > verif[tipo].get(c, MIN)

    servono = {}
    for c in live:
        for g in giorniP:
            if manca("pluvio", c, g):
                servono.setdefault((c, "pluvio"), set()).add((g + GIORNO).year)
    for c in sorted(termo):
        for g in giorniT:
            if manca("termo", c, g):
                servono.setdefault((c, "termo"), set()).add(g.year)
    jobs = [(0 if tipo == "pluvio" else 1, c, tipo, a) for (c, tipo), anni in servono.items() for a in sorted(anni)]
    for c in live:
        for a in sorted({(g + GIORNO).year for g in stime[c] if g < al} - servono.get((c, "pluvio"), set())):
            jobs.append((2, c, "pluvio", a))
    jobs.sort()
    rimandate = max(0, len(jobs) - max_richieste)
    jobs = [(c, tipo, a) for _, c, tipo, a in jobs[:max_richieste]]
    tempo = tempo_max if tempo_max is not None else max(20, min(300, LIMITE - (time.time() - INIZIO)))
    falliti, presi = 0, {}
    for (c, tipo, a), res in (scarica_archivi(jobs, tempo) if jobs else []):
        presi.setdefault((c, tipo), set())
        if res is None:
            falliti += 1
            continue
        presi[(c, tipo)].add(a)
        if tipo == "termo":
            for g in giorniT:
                if g.year == a:
                    t, r = T[c].get(g, (None, None)), res.get(g, (None, None))
                    primo, secondo = (r, t) if g < al else (t, r)  # l'archivio vale piu' delle tabelle, tranne ieri
                    T[c][g] = tuple(x if x is not None else y for x, y in zip(primo, secondo))
            continue
        for g in giorniP:
            if g >= al or (g + GIORNO).year != a:
                continue
            v = res.get(g + GIORNO)
            if v is not None:
                P[c][g] = v
            elif g not in P[c]:
                P[c][g] = None
            stime[c].discard(g)  # cercato nell'archivio: se non c'e' resta la stima
    for (c, tipo), anni in presi.items():
        if anni >= servono.get((c, tipo), set()):
            verif[tipo][c] = max(verif[tipo].get(c, MIN), al - GIORNO)
    q = lambda v: None if v is None else int(round(v * 10))
    stimati = sorted({g for c in live for g in stime[c] if giorniP[0] <= g <= al})
    out = {"fonte": "Servizio Idrologico Regionale Toscana: archivio delle stazioni (dati pre-validati) e tabelle in tempo reale",
           "convenzione": CONVENZIONE, "al": al.isoformat(), "aggiornato": dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "lettura": {"giorno": G.isoformat(), "ora": round(ora, 2)},
           "pioggia": {"giorni": NP, "unita": "0,1 mm", "valori": {c: [q(P[c].get(g)) for g in giorniP] for c in live}},
           "temperature": {"giorni": NT, "unita": "0,1 gradi C",
                           "tmin": {c: [q(T[c].get(g, (None, None))[0]) for g in giorniT] for c in sorted(termo)},
                           "tmax": {c: [q(T[c].get(g, (None, None))[1]) for g in giorniT] for c in sorted(termo)}},
           "stime": {g.isoformat(): sorted(c for c in live if g in stime[c]) for g in stimati},
           "verificato": {tipo: {c: g.isoformat() for c, g in sorted(verif[tipo].items()) if c in live} for tipo in verif}}
    scrivi(out, uscita)
    pieni = sum(1 for c in live for g in giorniP[-30:] if P[c].get(g) is not None)
    n_stime = sum(len(v) for v in out["stime"].values())
    return "al %s (lettura delle %s), %d richieste all'archivio (%d fallite, %d rimandate), %d valori stimati da rifare, %d%% dei giorni di pioggia recenti presenti" % (
        al, "%d:%02d" % (int(ora), round(ora % 1 * 60)), len(jobs), falliti, rimandate, n_stime, round(100 * pieni / max(1, 30 * len(live))))


# ---------------------------------------------------------------- 4) pioggia da radar e pluviometri (Protezione Civile)
RADAR_API = "https://radar-api.protezionecivile.it"
RLAT0, RLON0, RPASSO, RNLAT, RNLON = 44.50, 9.60, 0.02, 115, 140  # bordo nord, bordo ovest, celle di circa 1,6 x 2,2 km
RCOL = 10  # giorni per riga nell'immagine dell'archivio


def radar_giorno(g):
    """Pioggia dalle 9 di g alle 9 di g+1 (ora solare): prodotto «Merging» (radar corretto coi pluviometri) cumulato
    sulle 24 ore che finiscono alle 8 UTC di g+1, griglia di 0,01 gradi; ridotto a celle di 0,02 gradi. None se non c'e'."""
    from PIL import Image
    t = dt.datetime(g.year, g.month, g.day, 8, tzinfo=UTC) + GIORNO
    r = curl(RADAR_API + "/downloadProduct", corpo=json.dumps({"productType": "CUM24", "productDate": int(t.timestamp() * 1000)}))
    try:
        j = json.loads(r)
    except Exception:
        return None
    if ("Merging_%s_24h" % t.strftime("%Y%m%d%H%M")) not in str(j.get("key", "")):
        return None  # prodotto non ancora pronto
    f = os.path.join(TMP, "radar_%s.tif" % g.isoformat())
    if not curl(j["url"], f):
        return None
    try:
        a = np.array(Image.open(f), dtype=np.float32)
    finally:
        os.remove(f)
    r0, c0 = int(round((47.58 - RLAT0) / 0.01)), int(round((RLON0 - 5.60) / 0.01))  # la griglia parte da 47,58 N e 5,60 E
    sub = a[r0:r0 + 2 * RNLAT, c0:c0 + 2 * RNLON].astype(np.float64).reshape(RNLAT, 2, RNLON, 2)
    ok = sub >= 0
    n = ok.sum(axis=(1, 3))
    s = np.where(ok, sub, 0).sum(axis=(1, 3))
    return np.where(n > 0, s / np.maximum(n, 1), np.nan)


def codifica(mm):  # 0-254: radice quadrata per 16 (fine con poca pioggia, fino a 252 mm); 255 = nessun dato
    return np.where(np.isnan(mm), 255, np.clip(np.round(np.sqrt(np.maximum(np.nan_to_num(mm), 0)) * 16), 0, 254)).astype(np.uint8)


def decodifica(v):
    return np.where(v == 255, np.nan, (v.astype(np.float64) / 16) ** 2)


def aggiorna_radar(precedente=None, uscita="radar.json", attesa=540):
    """Archivio di 90 giorni della pioggia stimata da radar e pluviometri, in un'immagine (un riquadro per giorno)."""
    from PIL import Image
    adesso = dt.datetime.now(UTC)
    G = (adesso + dt.timedelta(hours=1)).date()  # oggi in ora solare
    al = G - GIORNO if adesso >= dt.datetime(G.year, G.month, G.day, 8, tzinfo=UTC) else G - 2 * GIORNO
    giorni = [al - (NP - 1 - i) * GIORNO for i in range(NP)]
    A = np.full((NP, RNLAT, RNLON), np.nan)
    if precedente and os.path.exists(precedente):
        try:
            s = json.load(open(precedente))
            al0, n0, col = dt.date.fromisoformat(s["al"]), s["giorni"], s["colonne"]
            im = np.array(Image.open(io.BytesIO(base64.b64decode(s["png"].split(",", 1)[1]))).convert("L"))
            for k in range(n0):
                g = al0 - (n0 - 1 - k) * GIORNO
                if giorni[0] <= g <= al:
                    r, c = divmod(k, col)
                    A[(g - giorni[0]).days] = decodifica(im[r * RNLAT:(r + 1) * RNLAT, c * RNLON:(c + 1) * RNLON])
        except Exception as e:
            print("radar precedente non leggibile:", e, file=sys.stderr)
    mancanti = [k for k in range(NP) if np.isnan(A[k]).all()]
    presi = 0
    with cf.ThreadPoolExecutor(6) as ex:
        for k, m in zip(mancanti, ex.map(lambda k: radar_giorno(giorni[k]) if time.time() - INIZIO < 300 else None, mancanti)):
            if m is not None:
                A[k] = m
                presi += 1
    # l'ultimo giorno si chiude alle 9 ora solare e il prodotto esce verso le 8:30 UTC: se serve si aspetta qualche minuto
    while np.isnan(A[-1]).all() and al == G - GIORNO and time.time() - INIZIO < attesa:
        time.sleep(30)
        m = radar_giorno(al)
        if m is not None:
            A[-1] = m
            presi += 1
    righe = (NP + RCOL - 1) // RCOL
    img = np.full((righe * RNLAT, RCOL * RNLON), 255, np.uint8)
    for k in range(NP):
        r, c = divmod(k, RCOL)
        img[r * RNLAT:(r + 1) * RNLAT, c * RNLON:(c + 1) * RNLON] = codifica(A[k])
    buf = io.BytesIO()
    Image.fromarray(img, "L").save(buf, format="PNG", optimize=True)
    vuoti = [k for k in range(NP) if np.isnan(A[k]).all()]
    out = {"fonte": "Dipartimento della Protezione Civile: pioggia stimata da radar e pluviometri (prodotto «Merging», cumulata di 24 ore), licenza CC BY-SA 4.0",
           "al": al.isoformat(), "aggiornato": dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "giorni": NP,
           "finestra": "dalle 9 del giorno alle 9 del giorno dopo, ora solare",
           "griglia": {"lat0": RLAT0, "lon0": RLON0, "passo": RPASSO, "nlat": RNLAT, "nlon": RNLON, "bordi": True},
           "colonne": RCOL, "codifica": "mm = (v/16)^2, 255 = nessun dato", "mancanti": vuoti,
           "png": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()}
    if len(vuoti) == NP:
        raise RuntimeError("nessun giorno di radar disponibile")
    scrivi(out, uscita)
    return "al %s, %d giorni scaricati, %d mancanti" % (al, presi, len(vuoti))


# ---------------------------------------------------------------- 5) file validi e avvisi per i posti salvati
def valida(dest=None):
    """I file della cartella corrente che si possono pubblicare; con dest li copia li'."""
    def carica(n):
        try:
            return json.load(open(n, encoding="utf-8"))
        except Exception:
            return None
    ok = []
    d = carica("data.json")
    if d and len(d.get("misure") or []) >= 300:
        ok.append("data.json")
    a = carica("aria.json")
    if a and len(a.get("umidita") or []) >= 100 and len(a.get("vento") or []) >= 50:
        ok.append("aria.json")
    p = carica("previsioni.json")
    if p and len(p.get("giorni") or []) >= 4:
        ok.append("previsioni.json")
    h = carica("storico.json")
    v = ((h or {}).get("pioggia") or {}).get("valori") if isinstance(h, dict) else None
    # l'archivio: quasi tutte le stazioni (come per data.json), ognuna coi suoi 90 giorni di pioggia
    if h and h.get("al") and isinstance(v, dict) and len(v) >= 300 and all(isinstance(x, list) and len(x) == NP for x in v.values()):
        ok.append("storico.json")
    r = carica("radar.json")
    # il radar: 90 giorni in un'immagine PNG, e almeno un giorno con i dati
    if r and r.get("al") and str(r.get("png") or "").startswith("data:image/png;base64,") and r.get("giorni") == NP and len(r.get("mancanti") or []) < NP:
        ok.append("radar.json")
    if carica("esito.json") is not None:
        ok.append("esito.json")
    if dest:
        os.makedirs(dest, exist_ok=True)
        for f in ok:
            if os.path.abspath(f) != os.path.abspath(os.path.join(dest, f)):
                shutil.copyfile(f, os.path.join(dest, f))
    return ok


def posto_da(x, nome_file):
    """Un posto salvato (documento dell'app, anche nella forma {"data": {...}}), oppure None se non ha coordinate in Toscana."""
    if not isinstance(x, dict):
        return None
    campi = x.get("data") if isinstance(x.get("data"), dict) and "lat" in x["data"] else x
    try:
        lat, lon = float(campi["lat"]), float(campi["lon"])
    except Exception:
        return None
    if not (41.3 <= lat <= 45.2 and 8.5 <= lon <= 13.5):
        return None
    # quota e tipo di bosco solo se validi: un valore scritto a mano non deve rompere il calcolo degli altri posti
    try:
        quota = float(campi["quota"]) if campi.get("quota") is not None and -10 <= float(campi["quota"]) <= 4000 else None
    except (TypeError, ValueError):
        quota = None
    try:
        bosco = int(campi["bosco"]) if campi.get("bosco") not in (None, "") and 0 <= int(campi["bosco"]) <= 20 else None
    except (TypeError, ValueError):
        bosco = None
    return {"id": str(x.get("id") or x.get("doc_id") or nome_file), "nome": str(campi.get("nome") or "Posto")[:60], "lat": lat, "lon": lon,
            "quota": quota, "bosco": bosco, "comune": str(campi.get("comune") or "")[:60],
            "specie": str(campi.get("specie") or "porcini")}


def leggi_posti(origine):
    """Un file JSON con l'elenco dei posti, oppure una cartella di file JSON (uno per posto, anche annidati)."""
    docs = []
    if os.path.isdir(origine):
        for f in sorted(glob.glob(os.path.join(origine, "**", "*.json"), recursive=True)):
            try:
                x = json.load(open(f, encoding="utf-8"))
            except Exception:
                continue
            docs.append((os.path.splitext(os.path.basename(f))[0], x))
    elif os.path.exists(origine):
        x = json.load(open(origine, encoding="utf-8"))
        for i, y in enumerate(x if isinstance(x, list) else x.get("documents") or x.get("docs") or x.get("posti") or []):
            docs.append((str(i), y))
    return [p for p in (posto_da(x, n) for n, x in docs) if p]


GIORNI_IT = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
MESI_IT = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]


def giorno_it(iso):
    d = dt.date.fromisoformat(iso)
    return "%s %d %s" % (GIORNI_IT[d.weekday()], d.day, MESI_IT[d.month - 1])


def punteggi_pagina(pagina, liste):
    """Punteggi dei posti per oggi e i 10 giorni dopo, calcolati nella pagina vera (Chromium senza schermo) con i dati della
    cartella corrente: lo stato dei dati e, per ogni lista di posti, la lista dei risultati (None se quella lista non si calcola)."""
    sito = tempfile.mkdtemp(prefix="radarsito_")
    shutil.copyfile(pagina, os.path.join(sito, "index.html"))
    for f in ("data.json", "aria.json", "previsioni.json", "storico.json", "radar.json", "sentieri.json"):
        if os.path.exists(f):
            shutil.copyfile(f, os.path.join(sito, f))
    tipi = {".html": "text/html; charset=utf-8", ".json": "application/json"}
    from playwright.sync_api import sync_playwright
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    with sync_playwright() as p:
        b = avvia_chromium(p, {"headless": True, "proxy": {"server": proxy} if proxy else None})
        pg = b.new_page(viewport={"width": 1200, "height": 900})

        def servi(route):
            nome = route.request.url.split("?")[0].split("radar.locale/", 1)[-1] or "index.html"
            f = os.path.join(sito, os.path.basename(nome))
            if os.path.isfile(f):
                route.fulfill(status=200, body=open(f, "rb").read(), headers={"content-type": tipi.get(os.path.splitext(f)[1], "application/octet-stream")})
            else:
                route.fulfill(status=404, body=b"")
        pg.route("http://radar.locale/**", servi)
        pg.goto("http://radar.locale/index.html", wait_until="domcontentloaded", timeout=90000)
        pg.wait_for_function("() => window.RadarPorcini && window.RadarPorcini.pronto()", timeout=150000)
        stato = pg.evaluate("RadarPorcini.stato()")
        risultati = []
        for l in liste:
            try:
                risultati.append(pg.evaluate("(l) => RadarPorcini.punteggi(l)", l))
            except Exception:
                risultati.append(None)
        b.close()
    shutil.rmtree(sito, ignore_errors=True)
    return stato, risultati


def valuta(risultati, prima, oggi):
    """Giorni buoni o ottimi di ogni posto da oggi in poi; nuovi quelli che l'ultima volta non lo erano (o lo erano meno)."""
    segnalati, nuovi = {}, []
    for r in risultati:
        for g in r["giorni"]:
            if g["data"] >= oggi and g["livello"] >= 3:
                k = "%s|%s" % (r["id"] or r["nome"], g["data"])
                segnalati[k] = g["livello"]
                if prima.get(k, 0) < g["livello"]:
                    nuovi.append((r, g))
    return segnalati, nuovi


def notizia(r, g):
    """Un posto che diventa buono o ottimo in un giorno, come lo confrontano la mail dei posti e la newsletter."""
    return "%s %s %s" % (r["nome"], g["data"], g["nome"])


def oggetto_mail(nuovi):
    r0, g0 = max(nuovi, key=lambda x: x[1]["score"])
    altri = len({x[0]["nome"] for x in nuovi}) - 1
    return "Radar Porcini: %s %s %s%s" % (r0["nome"], g0["nome"].lower(), giorno_it(g0["data"]),
                                         (" e un altro posto" if altri == 1 else " e altri %d posti" % altri) if altri > 0 else "")


def avvisi(origine, pagina, precedente=None, uscita="avvisi.json", app="https://claude.ai/artifact/QHRhL7qEY1tL79RvNu1wsQ"):
    """Calcola nella pagina vera il punteggio dei posti salvati per oggi e i 10 giorni dopo, con i dati della cartella
    corrente. Prepara la mail se un posto arriva a «Buono» o «Ottimo» in un giorno che ieri non lo era."""
    posti = leggi_posti(origine)
    oggi = dt.datetime.now(UTC).date().isoformat()
    prima = {}
    if precedente and os.path.exists(precedente):
        try:
            prima = json.load(open(precedente, encoding="utf-8")).get("segnalati") or {}
        except Exception:
            prima = {}
    out = {"quando": dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "posti": [], "segnalati": {}, "nuovi": [], "invia": False, "oggetto": "", "testo": ""}
    if not posti:
        scrivi(out, uscita)
        return "nessun posto salvato"
    stato, (risultati,) = punteggi_pagina(pagina, [posti])
    if risultati is None:
        raise RuntimeError("la pagina non ha calcolato i punteggi dei posti")
    out["dati"] = stato
    out["posti"] = risultati
    out["segnalati"], nuovi = valuta(risultati, prima, oggi)
    righe = []
    for r in risultati:
        buoni = [g for g in r["giorni"] if g["data"] >= oggi and g["livello"] >= 3]
        if buoni:
            righe.append("%s (%s%s m, %s): %s." % (r["nome"], (r["comune"].replace(" (", ", ").rstrip(")") + ", ") if r["comune"] else "",
                                                   int(round(r["quota"] or 0)), r["nomeSpecie"],
                                                   "; ".join("%s %s (%d)" % (g["nome"].lower(), giorno_it(g["data"]), g["score"]) for g in buoni)))
    out["nuovi"] = [notizia(r, g) for r, g in nuovi]
    if nuovi:
        out["invia"] = True
        out["oggetto"] = oggetto_mail(nuovi)
        out["testo"] = ("Buone notizie dai tuoi posti salvati in Radar Porcini.\n\n" + "\n".join(righe) +
                        "\n\nPunteggio da 0 a 100: buono da 45, ottimo da 65. Dati CFR %s; stima indicativa, controlla la scheda del punto prima di andare.\n\n"
                        "Apri l'app: %s\nVersione da installare sul telefono: https://the-marox.github.io/progetti/radar-porcini/\n\n"
                        "Ricevi questa mail perché hai salvato dei posti nella versione di Radar Porcini dentro Claude. Per non riceverla più, "
                        "elimina i posti o chiedi a Claude di togliere l'avviso." % ((stato or {}).get("pioggia_al") or "", app))
    scrivi(out, uscita)
    return "%d posti, %d giorni buoni o ottimi, %d nuovi%s" % (len(risultati), len(out["segnalati"]), len(nuovi), ": mail da inviare" if nuovi else "")


# ---------- la mail ----------
GG_IT = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]
LIV_IT = ["quasi nullo", "scarso", "discreto", "buono", "ottimo"]
COL_LIV = [("#D3D8CE", "#3E473A"), ("#C3D6B4", "#23331D"), ("#93BC7F", "#16260F"), ("#55904A", "#FFFFFF"), ("#2B6428", "#FFFFFF")]
PIEDE_POSTI = ("Ricevi questa mail dal flusso «Aggiorna Radar Porcini» del repository the-marox/progetti. "
               "Per non riceverla più, cancella il segreto POSTI del repository o disattiva il flusso.")
PIEDE_NEWSLETTER = ("Ricevi questa mail perché nel tuo account di Radar Porcini hai scelto la newsletter. "
                    "Per non riceverla più apri l'app, nella sezione «Il tuo account» togli la spunta a «Ricevi la newsletter».")


def giorno_corto(iso):
    d = dt.date.fromisoformat(iso)
    return "%s %d" % (GG_IT[d.weekday()], d.day)


def intervalli(giorni, livello):
    """Giorni consecutivi con quel livello, scritti come «dom 18 – gio 22»."""
    pezzi, inizio, prima = [], None, None
    for g in giorni + [None]:
        ok = g is not None and g["livello"] == livello
        if ok and inizio is not None and dt.date.fromisoformat(g["data"]) - dt.date.fromisoformat(prima["data"]) == GIORNO:
            prima = g
            continue
        if inizio is not None:
            pezzi.append(giorno_corto(inizio["data"]) + ("" if inizio is prima else " – " + giorno_corto(prima["data"])))
            inizio = prima = None
        if ok:
            inizio = prima = g
    return ", ".join(pezzi)


def elenco_it(a):
    return a[0] if len(a) == 1 else ", ".join(a[:-1]) + " e " + a[-1]


def frase_posto(r, g):
    """Il posto in una frase: dove, a che quota, in che bosco e quali specie hanno buone possibilità nei giorni buoni.
    «A «Poggio» (Reggello, FI), a 980 m in faggeta: ottime possibilità per porcino comune e porcino nero.»"""
    best = max(g, key=lambda x: x["score"])
    sp = {}
    for x in g:
        if x["livello"] >= 3:
            for s in x.get("specie") or []:
                if s["score"] >= 45:
                    sp[s["nome"]] = max(sp.get(s["nome"], 0), s["score"])
    nomi = sorted(sp, key=lambda n: -sp[n]) or [r.get("nomeSpecie") or "funghi"]
    luogo = "«%s»" % r["nome"]
    if r.get("comune"):
        luogo += " (%s)" % r["comune"].replace(" (", ", ").rstrip(")")
    dove = " ".join(x for x in ["a %d m" % round(r["quota"]) if r.get("quota") is not None else "", r.get("bosco") or ""] if x)
    return "A %s%s: %s possibilità per %s." % (luogo, ", " + dove if dove else "", "ottime" if best["livello"] >= 4 else "buone", elenco_it(nomi))


def componi(a, sito, oggi=None, piede=PIEDE_POSTI):
    """Testo semplice e HTML della mail, dai punteggi dei posti calcolati nella pagina."""
    import html
    oggi = oggi or dt.datetime.now(UTC).date().isoformat()
    posti = []
    for r in a.get("posti") or []:
        g = [x for x in r["giorni"] if x["data"] >= oggi]
        if not g:
            continue
        best = max(g, key=lambda x: x["score"])
        posti.append((r, g, best))
    posti.sort(key=lambda t: -t[2]["score"])
    buoni = [t for t in posti if t[2]["livello"] >= 3]
    altri = [t for t in posti if t[2]["livello"] < 3]
    pioggia = ((a.get("dati") or {}).get("pioggia_al") or "").replace(" (ora solare)", "")

    righe = ["I tuoi posti in Radar Porcini, giorno per giorno.", ""]
    for r, g, best in buoni:
        righe += ["■ " + r["nome"], "  " + frase_posto(r, g),
                  "  Giorno migliore: %s, %d (%s)" % (giorno_it(best["data"]), best["score"], LIV_IT[best["livello"]])]
        for liv in (4, 3):
            s = intervalli(g, liv)
            if s:
                righe.append("  %-8s%s" % (LIV_IT[liv].capitalize() + ":", s))
        righe.append("")
    if altri:
        righe += ["Sotto «buono» nei prossimi giorni: " + "; ".join("%s (al massimo %d %s)" % (r["nome"], b["score"], giorno_corto(b["data"])) for r, g, b in altri) + ".", ""]
    righe += ["Apri l'app: " + sito, "",
              "Punteggio da 0 a 100: buono da 45, ottimo da 65. È una stima: guarda la scheda del punto prima di andare." +
              (" Pioggia misurata fino al " + pioggia + "." if pioggia else ""), "", piede]
    testo = "\n".join(righe)

    e = html.escape
    F = "font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
    h = ['<div style="%sbackground:#EDF0E8;padding:16px 10px;color:#192017;">' % F,
         '<div style="max-width:560px;margin:0 auto;">',
         '<div style="font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:#566151;font-weight:700;margin:0 4px 4px;">Radar Porcini</div>',
         '<div style="font-size:20px;font-weight:800;margin:0 4px 14px;">I tuoi posti nei prossimi giorni</div>']
    for r, g, best in buoni:
        h.append('<div style="background:#F9FAF6;border:1px solid #CED6C7;border-radius:14px;padding:12px 12px 10px;margin-bottom:12px;">')
        h.append('<table role="presentation" width="100%%" cellpadding="0" cellspacing="0"><tr><td style="vertical-align:top;">'
                 '<div style="font-size:17px;font-weight:700;">%s</div></td>'
                 '<td style="vertical-align:top;text-align:right;white-space:nowrap;"><span style="display:inline-block;background:%s;color:%s;border-radius:99px;padding:3px 10px;font-size:13px;font-weight:700;">%d · %s</span>'
                 '<div style="font-size:12px;color:#566151;margin-top:3px;">%s</div></td></tr></table>'
                 % (e(r["nome"]), COL_LIV[best["livello"]][0], COL_LIV[best["livello"]][1], best["score"], LIV_IT[best["livello"]].capitalize(), e(giorno_it(best["data"]))))
        h.append('<div style="font-size:14px;line-height:1.4;margin-top:6px;">%s</div>' % e(frase_posto(r, g)))
        h.append('<div style="margin-top:10px;line-height:0;">')
        for x in g:
            bg, fg = COL_LIV[x["livello"]]
            bordo = "outline:2px solid #7A4A21;outline-offset:1px;" if x is best else ""
            d = dt.date.fromisoformat(x["data"])
            h.append('<span style="display:inline-block;width:42px;margin:0 3px 6px 0;padding:4px 0 5px;border-radius:7px;background:%s;color:%s;text-align:center;line-height:1.15;%s">'
                     '<span style="display:block;font-size:10px;text-transform:uppercase;opacity:.85;">%s</span>'
                     '<span style="display:block;font-size:14px;font-weight:700;">%d</span>'
                     '<span style="display:block;font-size:11px;">%d</span></span>' % (bg, fg, bordo, GG_IT[d.weekday()], d.day, x["score"]))
        h.append('</div>')
        for liv in (4, 3):
            s = intervalli(g, liv)
            if s:
                h.append('<div style="font-size:14px;margin-top:2px;"><b>%s:</b> %s</div>' % (LIV_IT[liv].capitalize(), e(s)))
        h.append('</div>')
    if altri:
        h.append('<div style="font-size:14px;color:#566151;margin:0 4px 14px;">Sotto «buono» nei prossimi giorni: %s.</div>'
                 % "; ".join("%s (al massimo %d %s)" % (e(r["nome"]), b["score"], giorno_corto(b["data"])) for r, g, b in altri))
    h.append('<div style="margin:4px 4px 16px;"><a href="%s" style="display:inline-block;background:#39663A;color:#FFFFFF;text-decoration:none;font-weight:700;border-radius:99px;padding:10px 18px;">Apri Radar Porcini</a></div>' % e(sito))
    h.append('<div style="font-size:12px;color:#566151;margin:0 4px;line-height:1.45;">Punteggio da 0 a 100: buono da 45, ottimo da 65. È una stima: guarda la scheda del punto prima di andare.%s<br><br>%s</div>'
             % (e(" Pioggia misurata fino al " + pioggia + ".") if pioggia else "", e(piede)))
    h.append('</div></div>')
    return testo, "".join(h)


class Posta:
    """Invio con Gmail (GMAIL_USER e GMAIL_APP_PASSWORD): una connessione per tutte le mail, riaperta se Gmail l'ha chiusa."""

    def __init__(self):
        self.utente = os.environ.get("GMAIL_USER", "").strip()
        self.chiave = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
        if not self.utente or not self.chiave:
            raise RuntimeError("mancano i segreti GMAIL_USER o GMAIL_APP_PASSWORD")
        self.s = None

    def invia(self, a, oggetto, testo, pagina):
        import smtplib
        from email.message import EmailMessage
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = "Radar Porcini <%s>" % self.utente, a, oggetto
        m.set_content(testo)
        m.add_alternative(pagina, subtype="html")
        # una connessione rimasta aperta tra una mail e l'altra Gmail puo' averla chiusa: si prova prima di usarla
        if self.s is not None:
            try:
                if self.s.noop()[0] != 250:
                    raise smtplib.SMTPServerDisconnected("connessione chiusa")
            except (smtplib.SMTPException, OSError):
                self.chiudi()
        # aprire la connessione si puo' riprovare, perche' nessuna mail e' ancora partita
        for prova in (1, 2):
            if self.s is not None:
                break
            try:
                self.s = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60)
                self.s.login(self.utente, self.chiave)
            except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, OSError):
                self.chiudi()
                if prova == 2:
                    raise
        # la mail invece una volta sola: se la connessione cade a meta' Gmail potrebbe averla gia' accettata, e
        # riprovare la manderebbe due volte. Una mail non partita non si segna, e si ripropone al giro completo dopo.
        try:
            self.s.send_message(m)
        except (smtplib.SMTPServerDisconnected, OSError):
            self.chiudi()
            raise

    def chiudi(self):
        if self.s is not None:
            try:
                self.s.quit()
            except Exception:
                try:
                    self.s.close()
                except Exception:
                    pass
            self.s = None


def mail(origine, cartella_stato, sito):
    """La mail dei posti del segreto POSTI, all'indirizzo che la invia. Poi ricorda i giorni segnalati (numeri dei posti e
    date, niente nomi): se la mail non parte non li ricorda, e domani l'avviso viene riproposto."""
    a = json.load(open(origine, encoding="utf-8"))
    print("posti controllati: %d, giorni buoni o ottimi: %d, nuovi: %d" % (len(a.get("posti") or []), len(a.get("segnalati") or {}), len(a.get("nuovi") or [])))
    if a.get("invia"):
        testo, pagina = componi(a, sito)
        posta = Posta()
        posta.invia(posta.utente, a["oggetto"], testo, pagina)
        posta.chiudi()
        # niente oggetto nel registro: in un repository pubblico lo legge chiunque, e contiene il nome di un posto
        print("mail inviata")
        # la newsletter, che gira dopo, legge qui che questo indirizzo ha gia' avuto queste notizie
        a["inviata"] = dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        scrivi(a, origine)
    else:
        print("nessun giorno nuovo da segnalare: nessuna mail")
    os.makedirs(cartella_stato, exist_ok=True)
    scrivi({"segnalati": a.get("segnalati") or {}}, os.path.join(cartella_stato, "avvisi.json"))


MAX_NEWSLETTER = 300  # mail al giorno al massimo (Gmail ne permette circa 500): le altre partono il giorno dopo


def indirizzo(a):
    """Un indirizzo come lo riconosce Gmail: senza maiuscole e, per gli indirizzi Gmail, senza punti e senza quello che segue un «+»."""
    loc, _, dom = (a or "").strip().lower().partition("@")
    if dom in ("gmail.com", "googlemail.com"):
        loc, dom = loc.split("+", 1)[0].replace(".", ""), "gmail.com"
    return loc + "@" + dom if loc and dom else ""


def notizie_mail_posti(cartella_stato, oggi):
    """Le notizie che la mail del segreto POSTI ha gia' portato oggi all'indirizzo GMAIL_USER. Il flusso lascia avvisi.json
    accanto alla cartella dello stato (non nello stato: ha i nomi dei posti, e lo stato finisce nella cache)."""
    f = os.path.join(os.path.dirname(os.path.abspath(cartella_stato)), "avvisi.json")
    try:
        a = json.load(open(f, encoding="utf-8"))
    except Exception:
        return set()
    if not str(a.get("inviata") or "").startswith(oggi):
        return set()
    return set(a.get("nuovi") or [])


def iscritti_newsletter():
    """Gli account con la newsletter attiva e l'email confermata, con i loro posti, letti con l'account di servizio di
    Firebase (segreto FIREBASE_SERVICE_ACCOUNT). L'indirizzo viene dall'account, non da quello che l'utente può scrivere."""
    import firebase_admin
    from firebase_admin import auth, credentials, firestore
    if not firebase_admin._apps:
        if os.environ.get("FIRESTORE_EMULATOR_HOST"):  # solo per le prove, con gli emulatori di Firebase
            from google.auth.credentials import AnonymousCredentials

            class Anonima(credentials.Base):
                def get_credential(self):
                    return AnonymousCredentials()
            firebase_admin.initialize_app(Anonima(), {"projectId": os.environ.get("GCLOUD_PROJECT") or "radar-porcini"})
        else:
            firebase_admin.initialize_app(credentials.Certificate(json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"])))
    db = firestore.client()
    iscritti, in_attesa = [], 0
    for u in auth.list_users().iterate_all():
        if u.disabled or not u.email:
            continue
        d = db.document("utenti", u.uid, "newsletter", "iscrizione").get()
        if not d.exists or not (d.to_dict() or {}).get("attiva"):
            continue
        if not u.email_verified:
            in_attesa += 1
            continue
        posti = [p for p in (posto_da(dict(x.to_dict() or {}, id=x.id), x.id) for x in db.collection("utenti", u.uid, "posti").stream()) if p]
        iscritti.append({"uid": u.uid, "email": u.email, "posti": posti})
    return iscritti, in_attesa


def newsletter(pagina, cartella_stato, sito):
    """Per ogni iscritto, i punteggi dei suoi posti e una mail se uno diventa buono o ottimo in un giorno nuovo.
    Nel registro (pubblico) solo numeri: mai indirizzi, nomi o coordinate."""
    import smtplib
    iscritti, in_attesa = iscritti_newsletter()
    file_stato = os.path.join(cartella_stato, "newsletter.json")
    try:
        prima = json.load(open(file_stato, encoding="utf-8")).get("utenti") or {}
    except Exception:
        prima = {}
    con_posti = [x for x in iscritti if x["posti"]]
    stato, risultati = punteggi_pagina(pagina, [x["posti"] for x in con_posti]) if con_posti else (None, [])
    oggi = dt.datetime.now(UTC).date().isoformat()
    dopo, inviate, errori, rimandate, coperte, posta = {}, 0, 0, 0, 0, None
    attivi = {x["uid"] for x in iscritti}
    # chi riceve anche la mail del segreto POSTI (l'indirizzo GMAIL_USER) non ha due mail uguali: se quella e' partita oggi
    # e la newsletter non ha notizie in piu', per lui la newsletter non parte
    gia, proprietario = notizie_mail_posti(cartella_stato, oggi), indirizzo(os.environ.get("GMAIL_USER"))
    os.makedirs(cartella_stato, exist_ok=True)

    def salva():
        # chi non e' piu' iscritto esce dallo stato; chi non e' ancora stato guardato (o e' iscritto senza posti) resta com'era
        tutti = {u: v for u, v in prima.items() if u in attivi}
        tutti.update(dopo)
        scrivi({"utenti": tutti}, file_stato)
    try:
        for x, ris in zip(con_posti, risultati):
            vecchi = (prima.get(x["uid"]) or {}).get("segnalati") or {}
            if ris is None:
                errori += 1
                dopo[x["uid"]] = {"segnalati": vecchi}
                print("i posti di un iscritto non si sono calcolati")
                continue
            segnalati, nuovi = valuta(ris, vecchi, oggi)
            if not nuovi:
                dopo[x["uid"]] = {"segnalati": segnalati}
                continue
            if gia and proprietario and indirizzo(x["email"]) == proprietario and {notizia(r, g) for r, g in nuovi} <= gia:
                coperte += 1
                dopo[x["uid"]] = {"segnalati": segnalati}
                continue
            if inviate >= MAX_NEWSLETTER:
                rimandate += 1
                dopo[x["uid"]] = {"segnalati": vecchi}
                continue
            try:
                posta = posta or Posta()
                testo, html_ = componi({"posti": ris, "dati": stato}, sito, oggi, PIEDE_NEWSLETTER)
                posta.invia(x["email"], oggetto_mail(nuovi), testo, html_)
                inviate += 1
                dopo[x["uid"]] = {"segnalati": segnalati}
                salva()
            except Exception as e:
                errori += 1
                dopo[x["uid"]] = {"segnalati": vecchi}
                print("una mail non è partita: %s" % type(e).__name__)
                # mancano i segreti di Gmail, o Gmail non accetta la password: inutile (e rischioso) provare le altre
                if isinstance(e, (RuntimeError, smtplib.SMTPAuthenticationError)):
                    break
    finally:
        if posta:
            posta.chiudi()
    salva()
    r = "%d iscritti con l'email confermata (%d con posti), %d in attesa di conferma, %d mail inviate%s%s%s" % (
        len(iscritti), len(con_posti), in_attesa, inviate, ", %d non partite" % errori if errori else "", ", %d rimandate a domani" % rimandate if rimandate else "",
        ", %d non mandate perché le stesse notizie erano già nella mail dei posti" % coperte if coperte else "")
    print(r)
    return r, errori


def main():
    args = sys.argv[1:]
    if args and args[0] == "valida":
        print(json.dumps({"validi": valida(args[1] if len(args) > 1 else None)}, ensure_ascii=False))
        return
    if args and args[0] == "mail":
        mail(args[1], args[2], args[3] if len(args) > 3 else os.environ.get("SITO_URL") or "https://the-marox.github.io/progetti/radar-porcini/")
        return
    if args and args[0] == "newsletter":
        r, errori = newsletter(args[1], args[2], args[3] if len(args) > 3 else os.environ.get("SITO_URL") or "https://the-marox.github.io/progetti/radar-porcini/")
        if errori:
            sys.exit(1)
        return
    if args and args[0] == "avvisi":
        t0 = time.time()
        try:
            r = avvisi(args[1], args[2], args[3] if len(args) > 3 else None)
        except Exception as e:
            r = "errore: %s" % breve(e)
        try:
            registro = json.load(open("esito.json", encoding="utf-8"))
        except Exception:
            registro = {}
        registro["avvisi"] = {"quando": dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "secondi": round(time.time() - t0), "esito": r}
        scrivi(registro, "esito.json")
        print(json.dumps({"avvisi": r}, ensure_ascii=False))
        return
    parti = ("misure", "previsioni", "archivio", "radar")
    scelte = [args.pop(0)] if args and args[0] in parti else list(parti)
    if len(scelte) == 1:
        prec_storico = args[0] if args and scelte[0] == "archivio" else None
        prec_radar = args[0] if args and scelte[0] == "radar" else None
    else:
        prec_storico = args[0] if len(args) > 0 else None
        prec_radar = args[1] if len(args) > 1 else None
    esito, tempi = {}, {}
    t = time.time()
    if "misure" in scelte:
        try:
            esito["misure"] = leggi_cfr(".")
        except Exception as e:
            esito["misure"] = {"data": "errore: %s" % breve(e)}
        tempi["misure"], t = round(time.time() - t), time.time()
    dati = "data.json" if os.path.exists("data.json") else ""
    if "previsioni" in scelte:
        try:
            esito["previsioni"] = previsioni(dati, "previsioni.json")
        except BaseException as e:
            esito["previsioni"] = "errore: %s" % breve(e)
        tempi["previsioni"], t = round(time.time() - t), time.time()
    if "archivio" in scelte:
        if dati:
            try:
                esito["archivio"] = aggiorna_storico(dati, prec_storico, "storico.json")
            except Exception as e:
                esito["archivio"] = "errore: %s" % breve(e)
        else:
            esito["archivio"] = "non aggiornato: mancano le misure di oggi (data.json)"
        tempi["archivio"], t = round(time.time() - t), time.time()
    if "radar" in scelte:
        try:
            esito["radar"] = aggiorna_radar(prec_radar, "radar.json")
        except Exception as e:
            esito["radar"] = "errore: %s" % breve(e)
        tempi["radar"] = round(time.time() - t)
    # esito.json raccoglie l'esito di ogni parte lanciata da questa cartella: si pubblica per capire cosa non va
    try:
        registro = json.load(open("esito.json", encoding="utf-8"))
    except Exception:
        registro = {}
    quando = dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    for k in esito:
        registro[k] = {"quando": quando, "secondi": tempi.get(k), "esito": esito[k]}
    try:
        scrivi(registro, "esito.json")
    except Exception:
        pass
    print(json.dumps(esito, ensure_ascii=False))


if __name__ == "__main__":
    main()
