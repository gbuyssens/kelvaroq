"""Create an offline HTML report: run_report.py RUN [OTHER_RUN] -o report.html."""
import argparse
import csv
import html
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import fmean

BUDGET = 1000 / 30
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
NUMBER = r"(\d+(?:\.\d+)?)"
STAGES = {
    "descriptors_us": ("Descripteurs", "descriptors"),
    "watch_us": ("Suivi et cohérence", "watch and coherence"),
    "deferred_us": ("Copies et barrières", "deferred copies and barriers"),
    "snapshots_us": ("Instantanés", "snapshots"),
    "card_setup_us": ("Préparation GPU", "card setup"),
    "execute_us": ("Exécution des listes", "ExecuteCommandLists"),
    "submit_other_us": ("Autre travail", "other"),
    "relay_wait_us": ("Attente du relais", "awaiting relay"),
    "submit_card_us": ("Attente GPU", "card fence"),
    "submit_lock_us": ("Verrous", "queue and submission locks"),
    "submit_fetch_us": ("Lecture GPU", "fetch fence"),
    "submit_dependency_us": ("Dépendances", "asleep with an unmet queue fence or value"),
    "submit_idle_us": ("Sans travail", "asleep awaiting work"),
}
MEMORY = {
    "to_card_bytes": "Envoi vers la carte", "from_card_bytes": "Lecture depuis la carte",
    "title_copy_bytes": "Copies depuis le titre", "refresh_copy_bytes": "Copies de rafraîchissement",
    "texture_upload_bytes": "Textures indexées envoyées", "inplace_bytes": "En place",
    "vram_usage_bytes": "VRAM utilisée", "vram_budget_bytes": "Budget VRAM", "ram_bytes": "RAM résidente",
}

@dataclass
class Run:
    name: str
    exact: list = field(default_factory=list)
    windows: list = field(default_factory=list)
    series: dict = field(default_factory=lambda: defaultdict(list))
    gap: int = 0
    unsuccessful: int = 0
    source: str = "CSV"

    def avg(self, key):
        values = self.series.get(key)
        return fmean(values) if values else None

def numeric(cell):
    try:
        n = float(cell)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) and 0 <= n < 2**64 - 1 else None

def read_csv(stream, run):
    reader = csv.DictReader(stream)
    if "frame_us" not in (reader.fieldnames or []):
        raise ValueError("Colonne frame_us absente du CSV")
    previous = None
    for row in reader:
        for k, cell in row.items():
            v = numeric(cell)
            if v is None:
                continue
            if k == "frame_us" and v > 0:
                run.exact.append(v / 1000)
            elif k in STAGES or k in MEMORY or k in {
                "title_us", "present_us", "gpu_busy_us", "gpu_idle_behind_us",
                "draws", "drawn", "dispatches", "dispatched", "pipelines_making", "pipeline_wait_us",
                "textures_waiting", "watch_calls", "watch_probes", "inplace_pages", "monitor_prev_ns",
            } or k.startswith("drop_"):
                run.series[k].append(v)
            elif k == "present_status" and v != 0:
                run.unsuccessful += 1
            elif k == "sequence":
                seq = int(v)
                if previous is not None:
                    run.gap += max(0, seq - previous - 1)
                previous = seq

def read_log(stream, run):
    run.source = "Journal"
    def capture(key, pattern, line, scale=1):
        match = re.search(pattern, line)
        if match:
            run.series[key].append(float(match[1]) * scale)
    for line in stream:
        line = ANSI.sub("", line)
        if "VELKIN_PRESENT " in line:
            match = re.search(r"\binterval_us=(\d+)", line)
            if match and int(match[1]) > 0:
                run.exact.append(int(match[1]) / 1000)
        elif "submit thread per frame in ms:" in line:
            overall, _, detail = line.partition("work by stage:")
            capture("work_total_us", "work and other time "+NUMBER, overall, 1000)
            for key, (_, label) in STAGES.items():
                source = overall if key.startswith("submit_") and key != "submit_other_us" or key == "relay_wait_us" else detail
                capture(key, re.escape(label)+" "+NUMBER, source, 1000)
        elif "ms/frame" in line:
            m = re.search(r"\b\d+(?:\.\d+)? fps,\s+"+NUMBER+r" ms/frame\b", line)
            if m:
                run.windows.append(float(m[1]))
            capture("watch_calls", "GetWriteWatch/frame "+NUMBER+" calls", line)
            capture("drop_pipeline", "dropped "+NUMBER+" without", line)
            capture("drop_indirect", "being made\\) and\\s+"+NUMBER+" indirect", line)
            capture("pipelines_making", r"\("+NUMBER+" being made", line)
        elif "the card's clock per frame" in line:
            capture("gpu_percent", "card about "+NUMBER+"% busy", line)
            capture("gpu_idle_behind_us", NUMBER+" ms of that while", line, 1000)
        elif "title calls per frame" in line:
            capture("present_us", "in ms: present "+NUMBER, line, 1000)
        elif "the copy queues per frame" in line:
            capture("queue_from_bytes", r"fetches of what shaders wrote: [\d.]+ jobs, "+NUMBER+" MB", line, 2**20)
            capture("queue_to_bytes", r"title bytes sent up: [\d.]+ jobs, "+NUMBER+" MB", line, 2**20)
        elif "host copies in the card's memory:" in line:
            capture("to_card_bytes", "per frame:\\s*"+NUMBER+" MB sent up", line, 1e6)
            capture("from_card_bytes", r"fetched \("+NUMBER+" MB", line, 1e6)
        elif "recording per frame" in line:
            capture("pipeline_wait_us", NUMBER+" ms waiting for pipelines", line, 1000)
        elif "the queue:" in line and "waiting for their memory" in line:
            capture("textures_waiting", NUMBER+" waiting for their memory", line)
        elif "copies per frame:" in line:
            capture("watch_probes", NUMBER+" watches ", line)

def load(path):
    run = Run(path.name)
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as stream:
        if path.suffix.lower() == ".csv":
            read_csv(stream, run)
        else:
            read_log(stream, run)
    if not run.exact and not run.windows:
        raise ValueError(f"Aucun intervalle de présentation dans {path.name}")
    return run

def fr(value, digits=1):
    return "—" if value is None else f"{value:.{digits}f}".replace(".", ",")

def quantile(sorted_values, q):
    return sorted_values[max(0, math.ceil(q*len(sorted_values))-1)]

def breakdown(run):
    stages = [(label, run.avg(key)) for key, (label, _) in STAGES.items()]
    if not any(run.series.get(k) for k in ("watch_us", "descriptors_us", "submit_other_us")):
        stages.insert(0, ("Travail d’envoi (global)", run.avg("work_total_us")))
    return sorted(((label, n/1000) for label,n in stages if n is not None), key=lambda s:-s[1])

def panel(run, common_scale):
    values = sorted(run.exact or run.windows)
    n = len(values)
    unit = "intervalles de présentation" if run.exact else "moyennes de fenêtres du journal"
    beyond = sum(v>BUDGET for v in values)
    def row(label, value):
        return f"<tr><th>{html.escape(label)}</th><td>{value}</td></tr>"
    def bar(label, value, extent, suffix):
        width = 100*value/extent if extent else 0
        return f'<div class="bar"><span>{html.escape(label)}</span><i><b style="width:{min(width,100):.2f}%"></b></i><em>{fr(value)} {suffix}</em></div>'
    parts = [f"<article><h2>{html.escape(run.name)}</h2><p>{run.source} · {n:,} {unit}</p>",
             f'<strong>{fr(quantile(values,.50))} <small>ms · médiane</small></strong><table>',
             row("p95 / p99",f"{fr(quantile(values,.95))} / {fr(quantile(values,.99))} ms"),
             row("Moyenne / maximum",f"{fr(fmean(values))} / {fr(max(values))} ms"),
             row("Au-delà de 33,33 ms",f"{beyond:,} ({fr(beyond*100/n)} %)"),"</table><h3>Distribution des durées</h3>"]
    import bisect
    start=0
    for upper,label in [(BUDGET,"≤ 33,33 ms"),(50,"33,33–50 ms"),(75,"50–75 ms"),(100,"75–100 ms"),(150,"100–150 ms"),(math.inf,"> 150 ms")]:
        stop=bisect.bisect_right(values,upper)
        parts.append(bar(label,(stop-start)*100/n,100,"%"))
        start=stop
    parts.append("<h3>Fil d’envoi · ms par image</h3>")
    stages=breakdown(run)
    if stages:
        parts.extend(bar(label,v,common_scale,"ms") for label,v in stages)
        active=[s for s in stages if s[0]!="Sans travail" and s[1]>0]
        if active:
            parts.append("<p>Principaux coûts · "+" · ".join(f"{k} {fr(v)} ms" for k,v in active[:3])+"</p>")
    else:
        parts.append("<p>Étapes absentes de cette capture.</p>")
    parts.append("<h3>GPU, mémoire et commandes</h3><table>")
    metrics = [(k,v,2**20,"Mio") for k,v in MEMORY.items()]
    metrics += [("queue_to_bytes","Envoi par la file de copie",2**20,"Mio"),("queue_from_bytes","Lecture par la file de copie",2**20,"Mio"),
                ("gpu_busy_us","GPU occupé (estimation)",1000,"ms"),("gpu_percent","Occupation GPU estimée",1,"%"),
                ("gpu_idle_behind_us","GPU inactif pendant le travail CPU",1000,"ms"),("title_us","Titre entre présentations",1000,"ms"),
                ("present_us","Appels de présentation du titre",1000,"ms"),("inplace_pages","Pages en place",1,""),
                ("pipeline_wait_us","Attente des pipelines",1000,"ms"),("pipelines_making","Pipelines en construction",1,""),
                ("textures_waiting","Textures en attente mémoire",1,""),("drop_pipeline","Pertes de pipeline",1,""),("drop_indirect","Pertes indirectes",1,""),
                ("watch_calls","Appels système write-watch",1,""),("watch_probes","Requêtes de suivi des écritures",1,""),("monitor_prev_ns","Publication précédente",1000,"µs")]
    for k,label,scale,suffix in metrics:
        value=run.avg(k)
        if value is not None:
            parts.append(row(label,fr(value/scale)+" "+suffix))
    parts.append("</table>")
    if run.unsuccessful:
        parts.append(f"<p>{run.unsuccessful} appels DXGI sans image affichée.</p>")
    if run.gap:
        parts.append(f"<p>{run.gap} enregistrements manquants entre les séquences.</p>")
    parts.append("</article>")
    return "".join(parts)

def render(runs):
    scale=max((v for r in runs for _,v in breakdown(r)),default=1)
    css="""*{box-sizing:border-box}body{background:#07151e;color:#edf4f7;font:15px 'Segoe UI',sans-serif;margin:32px auto;padding:0 24px;max-width:1450px}
h1{font-size:32px}h2{overflow-wrap:anywhere;color:#a1f4d4;margin-top:0}h3{margin:22px 0 10px;color:#b4c8d3;font-size:16px}
p,small{color:#b1c3ce}main{display:grid;grid-auto-columns:minmax(0,1fr);grid-auto-flow:column;gap:20px}article{background:#102430;padding:22px;border-radius:8px}
strong{font-size:32px}small{font-size:15px}table{border-collapse:collapse;width:100%}td,th{padding:4px 0;border-bottom:1px solid #29404d}th{text-align:left;font-weight:400}td,em{text-align:right;font-variant-numeric:tabular-nums}
.bar{display:grid;grid-template-columns:minmax(100px,1fr) 1fr 77px;align-items:center;gap:8px;font-size:13px;margin:5px 0}
i{height:7px;background:#263e4d}b{display:block;height:100%;background:#9fe3c8}em{font-style:normal}
@media(max-width:780px){main{grid-auto-flow:row}}@media print{@page{size:A3 landscape;margin:8mm}body{margin:0;font-size:11px}h3{margin:8px 0}article{padding:10px}.bar{margin:2px 0}*{print-color-adjust:exact}}"""
    return ('<!doctype html><html lang="fr"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Velkin · Rapport de performances</title><style>'+css+'</style><h1>Rapport de performances</h1>'
            '<p>Objectif 30 images/s · 33,33 ms. Le titre, le fil d’envoi et le GPU peuvent travailler simultanément. '
            'Les compteurs du CSV sont moyennés par image disponible. Les compteurs du journal sont moyennés par fenêtre publiée.</p><main>'
            +''.join(panel(r,scale) for r in runs)+'</main></html>')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run",type=Path)
    parser.add_argument("other",type=Path,nargs="?")
    parser.add_argument("-o","--output",type=Path,default=Path("rapport.html"))
    args=parser.parse_args()
    inputs=[p for p in [args.run,args.other] if p]
    if any(args.output.resolve()==p.resolve() for p in inputs):
        parser.error("Choisir une sortie distincte des sources")
    try:
        args.output.write_text(render([load(p) for p in inputs]),encoding="utf-8")
    except (OSError,ValueError) as error:
        parser.exit(1,f"{error}\n")

if __name__=="__main__":
    main()
