# progetti
## Radar Porcini

Dove e quando cercare funghi in Toscana: https://the-marox.github.io/progetti/radar-porcini/

Il sito sta nel ramo `gh-pages`, cartella `radar-porcini/` (la pagina, `aggiorna.py` e i file di dati).
Il flusso `.github/workflows/aggiorna.yml` (ramo `master`) legge le stazioni del Centro Funzionale della Regione
Toscana, le previsioni ICON-EU ed ECMWF e il radar della Protezione Civile, e pubblica i file di dati aggiornati
accanto alla pagina:

- alle 8:20 UTC il giro completo (misure, previsioni, archivio, radar e avvisi via mail per i posti salvati);
- alle 11:20, 14:20 e 17:20 UTC solo misure e previsioni; se quel giorno il giro del mattino è saltato, il primo
  di questi lo fa completo.

L'esito dell'ultimo aggiornamento è in https://the-marox.github.io/progetti/radar-porcini/esito.json.
Il vecchio flusso `radar-porcini.yml` è disattivato.
